"""Market after market, in one process, with the next one already warm.

The shape
---------
Five-minute markets are contiguous: market N+1's T0 is market N's T0+300, and a capture opens its
feeds thirty seconds before its own T0. So the sessions **overlap by design** — while N is
finishing its last thirty-five seconds, N+1 is already subscribed and warming its book — and the
supervisor's job is to keep those two facts from becoming one confused market.

Three rules follow, and they are the whole design:

1. **Nothing is "the current market".** Every session owns its own pipeline, risk controller,
   persistence worker, store, publisher and aggregates, keyed by slug. The only shared objects
   are the UI transport and the process's cold-work pool.
2. **Cold work never blocks a live market.** Settlement can take minutes and arrives after the
   next market has started; verification, replay and lzma are tens of seconds of CPU each. All of
   it runs after the session's own trading window, on threads for the network-bound parts and in
   a *separate interpreter* for the CPU-bound ones.
3. **Discovery happens early and off the loop.** `discover_market` is blocking urllib. Called
   from the event loop it would stall the ingress consumer of whatever market is trading, which
   is the exact failure P12B spent a round removing. It runs in a thread, well before T0.

What the supervisor does not do is decide anything about a market. It starts sessions, hands
operator commands to whichever one is active, keeps the cold backlog bounded, and writes one
corpus line per attempt.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from multiprocessing import get_context
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Final

from maker5m.bot.attempts import FAILED, FINISHED, AttemptLedger, LedgerWriteError
from maker5m.bot.audit import AuditIO
from maker5m.bot.cold import ColdRequest, cold_finalize
from maker5m.bot.config import PaperConfig, config_identity
from maker5m.bot.corpus import CorpusIndex
from maker5m.bot.maintenance import AllocatorMaintenance, MaintenanceWindow, maintenance_window
from maker5m.bot.qualify import qualify_all
from maker5m.bot.resources import LIVE_SESSIONS, GcObserver, pace_full_collections
from maker5m.bot.session import MarketSession, PrearmRecord
from maker5m.bot.settle import settle_market
from maker5m.feeds.discovery import discover_market, slug_for, t0_of_slug
from maker5m.market.timebase import NANOS_PER_SECOND
from maker5m.safety import LIVE_TRADING_ENABLED
from maker5m.telemetry.metrics import quantile
from maker5m.ui import (
    CommandBridge,
    CommandInbox,
    HotCommandChannel,
    SnapshotChannel,
    drain_operator_commands,
)

RELEASE_SETTLE_S: Final[float] = 2.0
"""How long after a market is released before the settled reading is taken.

Not a delay that matters — the slot this market held is given back a moment later either way,
and five other markets may be running — but the reading it separates does matter. Worker threads
are still finishing at the instant `release` returns, and a number taken then describes a market
that is still shutting down rather than one that is gone.
"""


MAINTENANCE_POLL_S: Final[float] = 1.0
"""How often the maintenance owner asks whether the rollover window is open. OPERATIONAL.

The window is thirteen seconds wide, so one second is plenty and the check itself is arithmetic
over the live sessions' `t0_ns` — no `/proc`, no allocator call, nothing that costs anything to
ask.
"""

MAINTENANCE_LEAD_S: Final[float] = 2.0
"""The interval either side of a trim over which ingress is compared against itself."""

MAINTENANCE_TAIL_S: Final[float] = 10.0
"""How long after a trim ingress is still watched. Observation only; it may run past the margin."""

__all__ = ["Supervisor", "UiPlane"]


def _tiers(samples: list[int]) -> dict[str, int | None]:
    ordered = sorted(samples)
    if not ordered:
        return {"n": 0, "p50": None, "p95": None, "p99": None, "max": None}
    return {
        "n": len(ordered),
        "p50": quantile(ordered, 0.50),
        "p95": quantile(ordered, 0.95),
        "p99": quantile(ordered, 0.99),
        "max": ordered[-1],
    }


def _flushed_print(message: str) -> None:
    """Progress that survives a kill. Plane 3, on the supervisor's own coroutine.

    Flushed because a redirected stdout is block-buffered, and a run log that only appears if
    the process exits cleanly is not much use during a run that is meant to last hours. The
    durable record is the corpus index either way; this is for the operator watching.
    """
    print(message, flush=True)


MARKET_SECONDS = 300
PREARM_LEAD_SECONDS = 75
"""How early discovery starts. The capture opens its own feeds at T0-30; this is the metadata."""

MAX_MARKET_LIFECYCLES = 6
"""Markets that may owe finalisation at once. **A reservation, not a check.**

The previous version tested `len(cold) < cap` at launch and let an already-running session add to
the count when it closed, so with a cap of three the count could reach four: two live sessions
plus a full cold queue, each of which still owed its own finalisation. A launch check that a
running market can walk past is not a bound.

A market now takes a slot *before* it is launched and holds it until its terminal attempt record
and corpus row are written. Six covers the architecture's steady state — two live or warming
sessions and up to four finalisations, since a settlement watch can run four hundred seconds
before verification, replay and compression even begin — and nothing can exceed it, because
nothing runs without a slot."""

MAX_COLD_BACKLOG = MAX_MARKET_LIFECYCLES
"""Closed markets whose cold work may be in flight at once. **OPERATIONAL, and enforced.**

A settlement watch can run for four hundred seconds before verification, replay and compression
even begin, so a slow chain or a provider fault accumulates tasks and 650 MB raw stores. P13's
first version defined this constant, wrote a `_bound_cold` helper, and then never called it from
the launch loop — it logged "cold backlog is N markets" and launched anyway, which is not a
bound. It is now waited on before a market is launched and, if capacity does not appear in time,
the slot is skipped and recorded. Integrity beats continuity: a missing five-minute window that
says why is better than an unbounded queue nobody is watching."""

LAUNCH_DEADLINE_SECONDS = 45
"""How late a market may still be launched. The capture opens its own feeds at T0-30."""


@dataclass(slots=True)
class UiPlane:
    """The one operator surface, shared across markets, owned by nobody in particular.

    The bridge, the inbox and the snapshot file are process-level: an operator has one dashboard,
    not one per five minutes. What is *not* shared is who a command applies to. Two sessions are
    past their T0 for about five seconds at every handoff, and a command arriving in that window
    must land on exactly one of them — the one the supervisor has designated active — rather than
    on whichever happened to tick first.
    """

    directory: Path
    channel: HotCommandChannel = field(default_factory=HotCommandChannel)
    bridge: CommandBridge | None = None
    active_slug: str | None = None
    delivered: dict[str, int] = field(default_factory=dict)

    def start(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "inbox").mkdir(parents=True, exist_ok=True)
        self.bridge = CommandBridge(
            inbox=CommandInbox(self.directory / "inbox"),
            channel=self.channel,
            snapshot=SnapshotChannel(self.directory / "snapshot.json"),
        )
        self.bridge.start()

    def stop(self) -> None:
        if self.bridge is not None:
            self.bridge.stop(timeout=5)

    def drain_commands(self, slug: str, ingress: Any, **kwargs: Any) -> int:
        """Hand waiting commands to the active session only. Called from the ingress owner.

        A session that is not active takes nothing and leaves the commands where they are, so
        the operator's halt reaches the market that is actually trading rather than the one that
        is thirty seconds from its first event.
        """
        if slug != self.active_slug:
            return 0
        moved = drain_operator_commands(self.channel, ingress, **kwargs)
        if moved:
            self.delivered[slug] = self.delivered.get(slug, 0) + moved
        return moved


@dataclass(slots=True)
class Supervisor:
    """Runs markets back to back and records each one exactly once."""

    config: PaperConfig
    target_markets: int | None = None
    launch_limit: int | None = None
    """Stop launching after this many markets, whatever the cold path has finished.

    Separate from `target_markets` because completion lags: settlement can take minutes, so a
    run that stopped only on completed markets would launch two or three more while waiting.
    A pilot wants exactly the markets it asked for."""

    ui: UiPlane = field(init=False)
    audit: AuditIO = field(init=False)
    identity: dict[str, Any] = field(init=False)
    pool: ProcessPoolExecutor | None = None
    cold: set[asyncio.Task[None]] = field(default_factory=set)
    sessions: set[asyncio.Task[None]] = field(default_factory=set)
    activations: set[asyncio.Task[None]] = field(default_factory=set)
    attempted: int = 0
    completed_existing: int = 0
    """Durable qualifying rows this epoch already held when the process started."""

    skipped: list[str] = field(default_factory=list)
    skipped_slots: list[dict[str, Any]] = field(default_factory=list)
    cold_high_water: int = 0
    lifecycles: int = 0
    """Markets that owe finalisation: launched-and-trading plus finalising.

    Never exceeds the cap, because nothing runs without a slot."""

    lifecycle_high_water: int = 0
    append_failures: int = 0
    ledger_failures: int = 0
    recovered_attempts: list[dict[str, Any]] = field(default_factory=list)
    integrity_faults: list[str] = field(default_factory=list)
    halted_for_integrity: bool = False
    qualified_attempts: set[str] = field(default_factory=set)
    """Attempts whose result qualifies. Membership, not arithmetic — an id is in it or it is not.

    Seeded by the full startup audit and extended one market at a time by the same shared
    qualifier. The runtime total is this set's size, so counting a market twice would require
    adding the same attempt id twice, which a set declines to do."""

    qualified_slugs: set[str] = field(default_factory=set)
    """Markets already counted. Two attempts producing a result for one slug is not two markets."""

    last_durable_count: int = 0
    """The joined qualifying count as of the last full audit."""
    gc_observer: GcObserver = field(default_factory=GcObserver)
    maintenance: AllocatorMaintenance = field(init=False)
    """Allocator maintenance, and the rollover window it is confined to. OPERATIONAL, Plane 3."""
    quiescent: dict[str, Any] | None = None
    """What a full collection and a heap trim released once every market had drained."""
    allow_dirty_requested: bool = False
    run_mode: str = field(init=False, default="ACCEPTANCE_CLEAN")
    log: Any = _flushed_print
    restarted: bool = False

    def __post_init__(self) -> None:
        if LIVE_TRADING_ENABLED:  # pragma: no cover - the flag is a constant False
            raise RuntimeError("refusing to start a paper run while live trading is enabled")
        self.ui = UiPlane(directory=self.config.ui_dir)
        # One owner for the corpus index and the attempt ledger, on its own thread. The event
        # loop this supervisor runs on is also consuming a live market's frames, and an fsync on
        # it delays them. `corpus` and `ledger` below are views onto this owner rather than
        # separate handles, so there is no second reference to get out of step with it.
        self.audit = AuditIO(
            corpus=CorpusIndex(path=self.config.corpus_path),
            ledger=AttemptLedger(path=self.config.corpus_path.with_name("attempts.jsonl")),
        )
        self.identity = config_identity(self.config)
        # The policy the identity above has already committed to, made real. One object, so a
        # run cannot claim one maintenance policy in its corpus rows and perform another.
        self.maintenance = AllocatorMaintenance(
            enabled=self.config.allocator_maintenance,
            margin_s=self.config.maintenance_margin_s,
        )
        # What kind of run this is, decided once and recorded on everything it produces. A run
        # against modified tracked source can collect, persist, replay and verify — it simply
        # cannot be final empirical evidence, and the row has to say so itself rather than
        # depending on somebody remembering which run wrote it.
        self.run_mode = (
            "ACCEPTANCE_CLEAN"
            if self.identity.get("working_tree_clean") is True
            else "EXPLORATORY_DIRTY"
        )
        self.config.evidence_dir.mkdir(parents=True, exist_ok=True)

    def _orphans(self, attempt: dict[str, Any]) -> dict[str, Any]:
        """What an abandoned attempt left behind, so nothing sits on disk unreferenced."""
        found: dict[str, Any] = {}
        for key in ("expected_journal", "expected_store", "expected_latency"):
            raw = attempt.get(key)
            if not raw:
                continue
            for candidate in (Path(str(raw)), Path(str(raw) + ".xz")):
                if candidate.exists():
                    found[candidate.name] = {
                        "path": str(candidate),
                        "bytes": candidate.stat().st_size,
                    }
        return found

    @property
    def corpus(self) -> CorpusIndex:
        """The audit owner's index. Read-only here: there is one of it, and it lives there."""
        return self.audit.corpus

    @property
    def ledger(self) -> AttemptLedger:
        return self.audit.ledger

    def _expectation(self, *, verify_latency: bool = True) -> dict[str, Any]:
        """What a qualifying row for this run must agree with. One place, one answer."""
        return {
            "epoch": self.config.epoch,
            "config_sha256": str(self.identity.get("config_sha256")),
            "source_revision": str(self.identity.get("source_revision")),
            "source_tree_sha": str(self.identity.get("source_tree_sha")),
            "run_mode": self.run_mode,
            "verify_latency": verify_latency,
        }

    def qualifying_now(self, *, verify_latency: bool = True) -> int:
        """The durable joined count, computed here and now. Synchronous; startup and tests only.

        Production reads `completed`, which is the size of a set the same qualifier maintains.
        This exists so a caller outside the event loop — the runner's startup line, a test — can
        ask the question directly without standing up a worker.
        """
        return qualify_all(
            self.corpus.entries(),
            self.ledger.events(),
            **self._expectation(verify_latency=verify_latency),
        ).count

    # -- prearm --------------------------------------------------------------------------

    async def prearm(self, slug: str) -> tuple[Any, PrearmRecord]:
        """Discover one market's identity, ids and rules, off the event loop."""
        t0_ns = t0_of_slug(slug) * NANOS_PER_SECOND
        started = time.time_ns()
        try:
            market = await asyncio.to_thread(discover_market, slug)
        except Exception as error:
            return None, PrearmRecord(
                slug=slug,
                t0_ns=t0_ns,
                started_ns=started,
                ready_ns=time.time_ns(),
                ok=False,
                error=f"{type(error).__name__}: {error}",
            )
        return market, PrearmRecord(
            slug=slug, t0_ns=t0_ns, started_ns=started, ready_ns=time.time_ns(), ok=True
        )

    # -- the loop ------------------------------------------------------------------------

    async def run(self) -> None:
        """Collect markets until the target is met or the process is stopped."""
        self.ui.start()
        self.audit.start()
        self.gc_observer.install()
        pace_full_collections()
        context = get_context("spawn")
        self.pool = ProcessPoolExecutor(max_workers=2, mp_context=context)
        # Anything a previous process died in the middle of, closed off and inventoried before
        # this one starts. Nothing is presumed to have completed and nothing is deleted.
        self.recovered_attempts = await self.audit.recover(inventory=self._orphans)
        for attempt in self.recovered_attempts:
            self.log(
                f"    recovered abandoned attempt {attempt.get('slug')} "
                f"({attempt.get('attempt_id')}): not collected"
            )
        if self.ledger.recovery_failures:
            # An acceptance run whose audit ledger cannot be written is not healthy enough to
            # collect. Reported, and not started.
            self._integrity_fault(
                f"{len(self.ledger.recovery_failures)} abandoned attempt(s) could not be closed "
                "off durably; they remain open"
            )
        already = self.corpus.completed_slugs()
        # One complete joined audit, off the loop, before any market is launched. This is where
        # historical revalidation belongs: it costs one pass at startup instead of a pass per
        # market, which over two hundred markets is 20,100 artifact decompressions.
        baseline = await self.audit.full_audit(self._expectation())
        self.qualified_attempts = baseline.attempt_ids
        self.qualified_slugs = baseline.slugs
        self.completed_existing = baseline.count
        self.last_durable_count = baseline.count
        if not baseline.consistent:
            self._integrity_fault(
                f"the corpus holds {len(baseline.duplicate_result_attempts)} attempt(s) with "
                f"more than one result and {len(baseline.duplicate_market_slugs)} market(s) with "
                "more than one qualifying result"
            )
        maintenance = asyncio.create_task(self._maintain(), name="allocator-maintenance")
        try:
            await self._loop(already)
        finally:
            self.maintenance.shutting_down = True
            maintenance.cancel()
            with suppress(asyncio.CancelledError):
                await maintenance
            await self._drain_cold()
            self._write_maintenance_artifact()
            await self._quiescent_probe()
            self.ui.stop()
            self.gc_observer.remove()
            self.audit.stop()
            if self.pool is not None:
                self.pool.shutdown(wait=True)
            self.pool = None

    # -- allocator maintenance ---------------------------------------------------------------

    def _feed_state(self, label: str) -> dict[str, Any]:
        """What every live market's ingress has done, at one instant. Integers only.

        Cheap on purpose: attribute reads off objects that already exist, no `/proc`, no
        allocation of consequence. It runs on the event loop between maintenance steps, so it has
        to cost nothing — the memory readings are taken in the worker thread beside the trim.
        """
        markets: dict[str, Any] = {}
        for session in list(LIVE_SESSIONS):
            runs = getattr(session, "runs", None)
            if not runs:
                continue
            pipeline = runs[0].pipeline
            markets[str(session.slug)] = {
                "ingress_ordinal": pipeline.merger.ordinal,
                "clob_messages": pipeline.counters.clob_messages,
                "spot_messages": pipeline.counters.spot_messages,
                "reconnects": pipeline.counters.reconnects,
                "malformed": pipeline.counters.malformed,
                "clob_health": pipeline.clob_health.status.value,
                "clob_awaiting_snapshot": pipeline.clob_health.awaiting_snapshot,
                "spot_health": pipeline.spot_health.status.value,
                "buffer_depth": len(session.buffer),
                "buffer_accepted": session.buffer.accepted,
                "buffer_dropped": session.buffer.dropped,
                "risk_dropped": session.risk_channel.dropped,
                "observations": len(session.hot_path_ns),
            }
        return {"label": label, "at_ns": time.time_ns(), "markets": markets}

    @staticmethod
    def _hot_path_span(session: Any, start: int, end: int) -> int | None:
        """The largest `observe` in a slice of a live market's samples. Absent if empty."""
        samples = getattr(session, "hot_path_ns", None)
        if not samples:
            return None
        window = samples[start:end]
        return max(window) if window else None

    def _hot_path_between(self, before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
        """Per market, the worst observation recorded between two feed states."""
        worst: dict[str, int] = {}
        by_slug = {str(session.slug): session for session in LIVE_SESSIONS}
        for slug, start in before["markets"].items():
            end = after["markets"].get(slug)
            if end is None or slug not in by_slug:
                continue
            largest = self._hot_path_span(
                by_slug[slug], int(start["observations"]), int(end["observations"])
            )
            if largest is not None:
                worst[slug] = largest
        return worst

    async def _maintain(self) -> None:
        """Own the rollover maintenance window. One `malloc_trim` per rollover, measured.

        The trim runs in a thread, and that is **not** claimed to isolate it: `malloc_trim` takes
        the allocator's locks process-wide, so a feed thread trying to allocate a book update
        waits on it wherever the call is made from. What confines the damage is the window, not
        the thread — no market is in `QUOTE` or `ENDGAME` while this runs, and the contract is
        re-checked immediately before the call so a window that has closed is skipped rather than
        entered late.

        The readings either side answer the question the pilot exists to ask: did any market's
        ingress notice.
        """
        if not self.maintenance.enabled:
            return
        while True:
            await asyncio.sleep(MAINTENANCE_POLL_S)
            window = self.maintenance.consider(time.time_ns(), list(LIVE_SESSIONS))
            if not window.allowed or not self.maintenance.claim(window):
                continue
            try:
                await self._maintain_once(window)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # pragma: no cover - a diagnostic never ends a run badly
                self.log(f"    allocator maintenance failed: {type(error).__name__}: {error}")

    async def _maintain_once(self, window: MaintenanceWindow) -> None:
        opened = self._feed_state("window_open")
        await asyncio.sleep(MAINTENANCE_LEAD_S)
        before = self._feed_state("before_trim")

        # The contract again, at the instant of the call rather than at the instant it was
        # noticed. Two seconds passed while the "before" readings were taken, and a window that
        # has closed in the meantime is skipped: never run late.
        recheck = maintenance_window(
            time.time_ns(),
            list(LIVE_SESSIONS),
            margin_s=self.maintenance.margin_s,
            shutting_down=self.maintenance.shutting_down,
        )
        if not recheck.allowed:
            self.log(f"    allocator maintenance skipped at the boundary: {recheck.reason}")
            self.maintenance.refusals["skipped at the boundary"] = (
                self.maintenance.refusals.get("skipped at the boundary", 0) + 1
            )
            return

        record = await asyncio.to_thread(self.maintenance.trim, recheck)
        during = self._feed_state("after_trim")
        await asyncio.sleep(MAINTENANCE_LEAD_S)
        after_2s = self._feed_state("2s_after")
        await asyncio.sleep(MAINTENANCE_TAIL_S - MAINTENANCE_LEAD_S)
        after_10s = self._feed_state("10s_after")

        record["feed"] = {
            "window_open": opened,
            "before_trim": before,
            "after_trim": during,
            "2s_after": after_2s,
            "10s_after": after_10s,
            "hot_path_max_ns": {
                "2s_before": self._hot_path_between(opened, before),
                "during_trim": self._hot_path_between(before, during),
                "2s_after": self._hot_path_between(during, after_2s),
                "10s_after": self._hot_path_between(after_2s, after_10s),
            },
        }
        released = record["released_rss_bytes"]
        self.log(
            f"    allocator maintenance at rollover {record['rollover']}: "
            f"malloc_trim {record['duration_ns'] / 1e6:.1f} ms, "
            f"released {0.0 if released is None else released / 1e6:.1f} MB, "
            f"fordblks {int(record['before']['fordblks'] or 0) / 1e6:.1f} -> "
            f"{int(record['after']['fordblks'] or 0) / 1e6:.1f} MB"
        )

    def _write_maintenance_artifact(self) -> None:
        """Put every trim's readings on disk before the process that holds them exits.

        The corpus row carries the aggregate — how many trims, how long, how much came back. The
        per-trim record is what answers whether ingress noticed, and it is far too large to repeat
        in every market's row, so it is written once beside the corpus. Nothing else reads it; it
        is evidence.
        """
        if not self.maintenance.events and not self.maintenance.refusals:
            return
        path = self.config.corpus_path.with_name("maintenance.json")
        payload = {
            "epoch": self.config.epoch,
            "run_mode": self.run_mode,
            "label": "CONTROLLED_LOCAL_ALLOCATOR_MAINTENANCE_ON_REAL_MARKET_DATA",
            "source_revision": self.identity.get("source_revision"),
            "source_tree_sha": self.identity.get("source_tree_sha"),
            "config_sha256": self.identity.get("config_sha256"),
            "summary": self.maintenance.summary(),
            "events": self.maintenance.events,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2), "utf-8")
        except OSError as error:  # pragma: no cover - a diagnostic never ends a run badly
            self.log(f"    maintenance artifact not written: {type(error).__name__}: {error}")
            return
        self.log(f"    maintenance artifact: {path} ({len(self.maintenance.events)} trims)")

    async def _quiescent_probe(self) -> None:
        """The one moment in a collection run when nothing is trading. Measure it there.

        Every market has closed and every cold task has drained, so a full collection and a heap
        trim can be timed without pausing anything that matters — which is the whole reason the
        probe lives here and nowhere else. What it separates is the question P13's corpus could
        not answer: whether resident memory is being held by live Python objects, by a C heap
        that nobody is using but glibc has not returned, or by neither.

        Nothing is *fixed* by this. `malloc_trim` runs after the last market, so no market's
        numbers are altered by it, and the result is recorded as a measurement.
        """
        from maker5m.bot.diagnostics import quiescent_probe
        from maker5m.bot.resources import LIVE_SESSIONS

        live = len(LIVE_SESSIONS)
        try:
            probe = await asyncio.to_thread(
                quiescent_probe,
                "end_of_run",
                live_sessions=live,
                pending_tasks=sum(1 for task in asyncio.all_tasks() if not task.done()),
            )
        except Exception as error:  # pragma: no cover - a diagnostic never ends a run badly
            self.log(f"    quiescent probe failed: {type(error).__name__}: {error}")
            return
        self.quiescent = probe.summary()
        if probe.refused:
            self.log(f"    quiescent probe refused: {probe.refused}")
            return
        released_gc = probe.gc_release_bytes or 0
        released_trim = probe.trim_release_bytes or 0
        self.log(
            f"    quiescent probe: {probe.verdict()} "
            f"(full collection {-released_gc / 1e6:.1f} MB in {probe.gc_seconds:.2f}s, "
            f"malloc_trim {-released_trim / 1e6:.1f} MB)"
        )

    async def _loop(self, already: set[str]) -> None:
        """Launch each market in time to prearm it, and never wait on a closed one.

        The cadence is the market's, not ours. A capture opens its own feeds at T0-30 and runs
        to T0+305, which is five seconds *past* the next market's T0 — so sessions must be
        launched while the previous one is still trading. Waiting for market N to return before
        looking at N+1 would skip N+1 entirely, every time. The first version of this loop did
        exactly that.
        """
        # Slugs, not sessions. Holding the session objects here kept every market of the run
        # alive: the corrected pilot ended with four live sessions after four markets, so
        # "released" was false however thoroughly `release()` cleared their insides. The only
        # thing this needs to know is which slugs have been launched.
        launched: set[str] = set()
        t0 = self._first_t0()
        while await self._still_collecting(len(launched)):
            slug = slug_for(t0)
            if slug in already or slug in launched:
                t0 += MARKET_SECONDS
                continue

            await self._sleep_until(t0 - PREARM_LEAD_SECONDS)
            # The reservation is taken here, before the market exists, and released only when
            # its terminal record is written. Waiting happens to a market that has not started;
            # a running one is never made to wait on a closed one's settlement.
            if not await self._reserve(deadline=t0 - LAUNCH_DEADLINE_SECONDS):
                await self._record_skip(
                    slug,
                    t0,
                    "COLD_CAPACITY_UNAVAILABLE",
                    f"{self.lifecycles} markets still owed finalisation at a cap of "
                    f"{MAX_MARKET_LIFECYCLES}; this slot was not collected",
                )
                self.log(f"[{time.strftime('%H:%M:%S')}] {slug} skipped: no lifecycle capacity")
                t0 += MARKET_SECONDS
                continue

            market, prearm = await self.prearm(slug)
            if market is None:
                self._release()
                await self._record_prearm_failure(prearm)
                t0 += MARKET_SECONDS
                continue

            try:
                attempt_id = await self.audit.start_attempt(
                    slug=slug,
                    t0_ns=t0 * NANOS_PER_SECOND,
                    identity={
                        "epoch": self.config.epoch,
                        "config_sha256": self.identity.get("config_sha256"),
                        "source_revision": self.identity.get("source_revision"),
                        "source_tree_sha": self.identity.get("source_tree_sha"),
                        "working_tree_clean": self.identity.get("working_tree_clean"),
                        "run_mode": self.run_mode,
                        "allow_dirty_requested": self.allow_dirty_requested,
                    },
                    prearm=prearm.summary(),
                    expected_journal=str(self.config.evidence_dir / f"{slug}.journal.ndjson"),
                    expected_store=str(self.config.evidence_dir / f"{slug}.p11.sqlite3"),
                    expected_latency=str(self.config.evidence_dir / f"{slug}.latency.json.xz"),
                )
            except LedgerWriteError as error:
                # Fail closed. A market nobody recorded is worse than a market nobody collected.
                self._release()
                self.log(f"[{time.strftime('%H:%M:%S')}] {slug} NOT launched: {error}")
                self.ledger_failures += 1
                t0 += MARKET_SECONDS
                continue

            session = MarketSession(market=market, config=self.config, prearm=prearm, ui=self.ui)
            session.source_revision = str(self.identity.get("source_revision", ""))
            session.attempt_id = attempt_id
            self.attempted += 1
            launched.add(slug)
            self.log(
                f"[{time.strftime('%H:%M:%S')}] {slug} launched "
                f"(prearm lead {prearm.lead_seconds:.1f}s, attempt {self.attempted})"
            )

            task = asyncio.create_task(session.run(), name=f"session-{slug}")
            self.sessions.add(task)
            task.add_done_callback(self.sessions.discard)
            task.add_done_callback(self._closer(session))
            # The handoff instant, decided by the market's clock rather than by whichever
            # session's tick happens to run first. Operator commands follow this.
            self.activations.add(asyncio.create_task(self._activate_at(slug, t0)))
            t0 += MARKET_SECONDS

        await self._drain_sessions()

    def _closer(self, session: MarketSession) -> Callable[[asyncio.Task[None]], None]:
        def done(_task: asyncio.Task[None]) -> None:
            self._closed(session)

        return done

    @property
    def completed(self) -> int:
        """Markets that count: the size of the qualified-attempt set.

        Not a running total, and not "what was there plus what we added" — an attempt is a member
        of this set or it is not, so the same market cannot be counted twice however many times
        its finalisation is observed. The target is a property of the corpus rather than of this
        process's uptime: a collector restarted after 150 markets collects fifty more.
        """
        return len(self.qualified_attempts)

    @property
    def completed_this_process(self) -> int:
        return max(0, len(self.qualified_attempts) - self.completed_existing)

    async def _still_collecting(self, launched: int) -> bool:
        """Whether to launch another market, confirming the target against durable truth first.

        The incremental set says when the target *might* be met; the full audit says whether it
        is. If the audit comes back short — a row that did not survive, an artifact that no
        longer validates — the durable answer replaces the running set and collection continues.
        """
        if not self._may_launch(launched):
            return False
        if self.target_markets is None or self.completed < self.target_markets:
            return True
        if await self._confirm_target():
            self.log(f"    target met: {self.last_durable_count} markets confirmed by full audit")
            return False
        self.log(
            f"    the running set said {self.target_markets}; the full audit says "
            f"{self.last_durable_count}. Continuing."
        )
        return self._may_launch(launched)

    def _may_launch(self, launched: int) -> bool:
        """The conditions that stop a run regardless of the target: a halt, or a launch limit.

        Deliberately not the target. `_still_collecting` owns that, because the target is only
        met once a full durable audit says so, and a guard that answered it here would return
        False before the confirmation ever ran — which it did.
        """
        if self.halted_for_integrity:
            return False
        return self.launch_limit is None or launched < self.launch_limit

    def _closed(self, session: MarketSession) -> None:
        """A session's trading window ended. Everything it still owes is cold from here."""
        self.log(
            f"[{time.strftime('%H:%M:%S')}] {session.slug} closed: "
            f"{session.worker.stats.decisions_written} decisions, "
            f"{session.buffer.dropped} dropped, {session.worker.store.sink_errors} sink errors"
        )
        self._start_cold(session)

    async def _activate_at(self, slug: str, t0_seconds: int) -> None:
        """Make this market the one operator commands apply to, at its own T0."""
        await self._sleep_until(t0_seconds)
        self.ui.active_slug = slug

    async def _sleep_until(self, when_seconds: float) -> None:
        remaining = when_seconds - time.time()
        while remaining > 0:
            await asyncio.sleep(min(remaining, 5.0))
            remaining = when_seconds - time.time()

    def _first_t0(self) -> int:
        """The first market with enough lead to discover and prearm it properly."""
        now = int(time.time())
        t0 = ((now // MARKET_SECONDS) + 1) * MARKET_SECONDS
        while t0 - now < PREARM_LEAD_SECONDS:
            t0 += MARKET_SECONDS
        return t0

    async def _drain_sessions(self) -> None:
        if self.sessions:
            await asyncio.gather(*list(self.sessions), return_exceptions=True)
        if self.activations:
            for task in list(self.activations):
                task.cancel()
            await asyncio.gather(*list(self.activations), return_exceptions=True)

    # -- cold path -----------------------------------------------------------------------

    def _start_cold(self, session: MarketSession) -> None:
        task = asyncio.create_task(self._finalize(session), name=f"cold-{session.slug}")
        self.cold.add(task)
        task.add_done_callback(self.cold.discard)
        if len(self.cold) > self.cold_high_water:
            self.cold_high_water = len(self.cold)

    async def _reserve(self, *, deadline: float) -> bool:
        """Take a lifecycle slot for a market that has not started yet.

        Returns whether one was available before the last moment the market could still be
        launched. A market that cannot reserve is not launched, and its slot is recorded as
        skipped — an unbounded queue nobody is watching is worse than a five-minute gap that
        says why.
        """
        while self.lifecycles >= MAX_MARKET_LIFECYCLES:
            if time.time() >= deadline:
                return False
            if self.cold:
                await asyncio.wait(self.cold, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
                self.cold = {task for task in self.cold if not task.done()}
            else:  # pragma: no cover - only if a live session is wedged
                await asyncio.sleep(1.0)
        self.lifecycles += 1
        if self.lifecycles > self.lifecycle_high_water:
            self.lifecycle_high_water = self.lifecycles
        return True

    async def _admit(self, entry: dict[str, Any], slug: str) -> None:
        """Judge one newly written market and, if it counts, admit it to the qualified set.

        The same shared qualifier the startup audit uses, applied to one row — so exactly one
        latency artifact is read, not every artifact ever written. Re-validating the whole
        corpus after every market is 20,100 decompressions across two hundred markets, all of
        them on the thread that is also consuming the next market's frames, and all of them
        answering a question the startup audit already answered.

        Membership, not arithmetic. An attempt is in the set or it is not, so a market cannot be
        counted twice; a slug already counted is a duplicate result and an integrity fault,
        because two attempts producing a result for one market is not two markets.
        """
        judgement = await self.audit.judge_row(entry, self._expectation())
        if not judgement.qualifies:
            self.log(f"    {slug}: does not count — {'; '.join(judgement.reasons)}")
            return
        attempt = judgement.attempt_id
        if attempt is None:
            return
        if attempt in self.qualified_attempts:
            self._integrity_fault(f"attempt {attempt} already has a qualifying result")
            return
        if judgement.slug in self.qualified_slugs:
            self._integrity_fault(
                f"DUPLICATE_MARKET_RESULT: {judgement.slug} already has a qualifying result "
                f"from another attempt"
            )
            return
        self.qualified_attempts.add(attempt)
        self.qualified_slugs.add(judgement.slug)

    async def _confirm_target(self) -> bool:
        """The full joined audit, off the loop, before any completion is claimed.

        The incremental set is judged one market at a time and never re-reads history; this is
        where history is re-read. If the durable truth is short of the incremental count, the
        durable truth wins and collection continues.
        """
        report = await self.audit.full_audit(self._expectation())
        self.last_durable_count = report.count
        if not report.consistent:
            self._integrity_fault(
                f"the corpus holds {len(report.duplicate_result_attempts)} attempt(s) with more "
                f"than one result and {len(report.duplicate_market_slugs)} duplicated market(s)"
            )
        if report.attempt_ids != self.qualified_attempts:
            missing = len(self.qualified_attempts - report.attempt_ids)
            extra = len(report.attempt_ids - self.qualified_attempts)
            self.log(
                f"    full audit disagrees with the running set: {missing} counted but not "
                f"durable, {extra} durable but not counted — taking the durable answer"
            )
            self.qualified_attempts = report.attempt_ids
            self.qualified_slugs = report.slugs
        return self.target_markets is not None and report.count >= self.target_markets

    def _integrity_fault(self, detail: str) -> None:
        """The audit trail failed. Record it, and stop collecting *acceptance* evidence.

        The policy, stated so it is not inferred: the in-memory slot is released so markets
        already running finish normally and nothing deadlocks, but an ACCEPTANCE_CLEAN run stops
        launching new markets. A corpus whose ledger cannot be written is not a corpus — an
        exploratory run says so and carries on, because nothing there is being counted.
        """
        self.ledger_failures += 1
        self.integrity_faults.append(detail)
        self.log(f"    COLLECTOR INTEGRITY FAULT: {detail}")
        if self.run_mode == "ACCEPTANCE_CLEAN":
            self.halted_for_integrity = True

    def _release(self) -> None:
        """Give the slot back. Called once per reservation, after the terminal record."""
        self.lifecycles = max(0, self.lifecycles - 1)

    async def _drain_cold(self) -> None:
        if self.cold:
            await asyncio.gather(*list(self.cold), return_exceptions=True)

    async def _finalize(self, session: MarketSession) -> None:
        """Everything a closed market still owes, none of it on a trading path."""
        entry: dict[str, Any] = {}
        appended = False
        try:
            await session.write_journal()
            build = {
                "source_revision": str(self.identity.get("source_revision")),
                "source_tree_sha": str(self.identity.get("source_tree_sha")),
                "config_sha256": str(self.identity.get("config_sha256")),
                "epoch": self.config.epoch,
                "run_mode": self.run_mode,
            }
            await session.write_latency_artifact(build)
            await session.checkpoint("after_latency_write")
            await session.settle(settle_market)
            await session.checkpoint("after_settlement")
            await asyncio.to_thread(session.close_store)
            await session.checkpoint("after_store_close")
            cold = await self._cold_result(session)
            await session.checkpoint("after_cold_result")
            session.publish_close(cold)
            # The persisted artifact, read back and checked against the identity this market's
            # row is about to claim. Not the object that was written a moment ago.
            await session.verify_latency_artifact(
                {
                    **build,
                    "slug": session.slug,
                    "market_id": session.identity.market_id,
                    "condition_id": session.identity.condition_id,
                    "t0_ns": session.t0_ns,
                    "sample_every": self.config.sample_every,
                }
            )
            session.finish()
            await session.checkpoint("after_release")
            # A released market's memory is not a released market's memory one instant later:
            # worker threads are still finishing and the allocator has not settled. The two
            # readings are the difference between "still shutting down" and "still resident",
            # and both are taken here — before the row is built — because a reading that arrives
            # after `_entry` is not in the row and cannot be analysed later.
            await asyncio.sleep(RELEASE_SETTLE_S)
            await session.checkpoint("post_release_settled", tracked_objects=True)
            # Exactly which collections ran while this market was live, from the process event
            # log. Not a running maximum, which is what the accepted corpus had to report.
            session.gc_window = self.gc_observer.window(
                session.live_from_ns, session.live_to_ns or perf_counter_ns()
            ).summary()
            entry = self._entry(session, cold)
            # Durability first. A market counts when its row is on the disk, not when the
            # verifier liked it: the previous version incremented the total whether or not the
            # append succeeded, so a full disk would have produced a "200-market corpus" with
            # fewer than two hundred rows in it.
            appended = await self.audit.append_row(entry)
            if not appended:
                self.append_failures += 1
                self.log(f"    corpus append FAILED for {session.slug}; it does not count")
        except Exception as error:  # pragma: no cover - the cold path never kills the run
            self.log(f"    {session.slug}: cold path failed: {type(error).__name__}: {error}")
            session.incidents.append(f"cold path failed: {type(error).__name__}: {error}")
        finally:
            session.release()
            # Corpus row first, then the terminal record, then the count. A market that verified
            # perfectly and whose terminal record did not reach the disk is not an accounting
            # rounding error: it is a market this collector cannot prove it finished, and the
            # ledger's answer is checked rather than assumed.
            terminal = await self.audit.finish_attempt(
                session.attempt_id or "unknown",
                event=FINISHED if appended else FAILED,
                slug=session.slug,
                epoch=self.config.epoch,
                config_sha256=self.identity.get("config_sha256"),
                source_revision=self.identity.get("source_revision"),
                source_tree_sha=self.identity.get("source_tree_sha"),
                run_mode=self.run_mode,
                verification_status=entry.get("verification_status"),
                evidence_eligible=entry.get("evidence_eligible", False),
                corpus_appended=appended,
                journal_sha256=(entry.get("journal") or {}).get("sha256"),
                store_sha256=(entry.get("store") or {}).get("sha256"),
                latency_sha256=(entry.get("latency_artifact") or {}).get("sha256"),
                incidents=list(session.incidents),
            )
            if not terminal:
                self._integrity_fault(
                    f"the terminal attempt record for {session.slug} could not be written; "
                    "the market is retained and cannot count"
                )
            if appended and terminal:
                await self._admit(entry, session.slug)
            self.log(
                f"    {session.slug}: {entry.get('verification_status')} "
                f"replay={entry.get('replay', {}).get('status')} "
                f"eligible={entry.get('evidence_eligible')} appended={appended} "
                f"terminal={terminal} "
                f"(markets {self.completed}, last full audit {self.last_durable_count})"
            )
            self._release()

    async def _cold_result(self, session: MarketSession) -> dict[str, Any]:
        if self.pool is None:  # pragma: no cover - only outside `run`
            return cold_finalize(self._cold_request(session))
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self.pool, cold_finalize, self._cold_request(session))
        except Exception as error:
            return {
                "slug": session.slug,
                "verification_status": "CORRUPT",
                "error": f"cold worker failed: {type(error).__name__}: {error}",
                "replay": {"status": "NOT_RUN"},
            }

    def _cold_request(self, session: MarketSession) -> ColdRequest:
        return ColdRequest(
            slug=session.slug,
            journal_path=str(session.journal_path),
            database_path=str(session.database),
            expected_database_sha256=session.database_sha256,
            keep_raw_store=self.config.keep_raw_store,
        )

    # -- the corpus entry ----------------------------------------------------------------

    async def _record_skip(self, slug: str, t0_seconds: int, reason: str, detail: str) -> None:
        """A market this collector chose not to start. Recorded, so the gap is auditable."""
        entry = {
            "slug": slug,
            "epoch": self.config.epoch,
            "config_sha256": self.identity.get("config_sha256"),
            "source_revision": self.identity.get("source_revision"),
            "t0_ns": t0_seconds * NANOS_PER_SECOND,
            "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "verification_status": "NOT_STARTED",
            "evidence_eligible": False,
            "run_mode": self.run_mode,
            "working_tree_clean": self.identity.get("working_tree_clean"),
            "skip_reason": reason,
            "incidents": [detail],
            "cold_backlog": len(self.cold),
            "cold_backlog_high_water": self.cold_high_water,
        }
        self.skipped_slots.append({"slug": slug, "reason": reason})
        await self.audit.append_row(entry)

    async def _record_prearm_failure(self, prearm: PrearmRecord) -> None:
        await self.audit.append_row(
            {
                "slug": prearm.slug,
                "epoch": self.config.epoch,
                "config_sha256": self.identity.get("config_sha256"),
                "source_revision": self.identity.get("source_revision"),
                "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "verification_status": "NOT_STARTED",
                "evidence_eligible": False,
                "run_mode": self.run_mode,
                "working_tree_clean": self.identity.get("working_tree_clean"),
                "prearm": prearm.summary(),
                "incidents": [f"prearm failed: {prearm.error}"],
            }
        )

    def _entry(self, session: MarketSession, cold: dict[str, Any]) -> dict[str, Any]:
        manifest = session.manifest
        stats = session.worker.stats
        counters = session.feed_counters
        replay = dict(cold.get("replay") or {})
        settlement = session.settlement
        decision = None if settlement is None else settlement.decision
        status = str(cold.get("verification_status", "CORRUPT"))
        quality = session.quality.summary()
        thresholds = self.config.thresholds
        stale_fraction = quality["fractions"].get("STALE")

        # Every decision observation carries exactly two side opportunities, UP and DOWN, and
        # P13 classifies both. With no own fills — and there are none, because no order is ever
        # sent — the denominator is therefore exactly twice the committed decision rows. A market
        # where it does not come out is one whose distribution has no interpretable base, so it
        # is retained and not counted.
        expected = 2 * stats.decisions_written
        classified = int(quality["classified"])
        actions = dict(sorted(session.analyzer.counters.actions.items()))
        action_total = sum(actions.values())

        operational_faults: list[str] = []
        for fault in session.latency_faults:
            operational_faults.append(f"OPERATIONAL: latency artifact rejected — {fault}")
        if session.latency is None:
            operational_faults.append(
                "OPERATIONAL: no live latency artifact; the market's own latency did not survive "
                "the session and a replay cannot stand in for it"
            )
        elif not any(
            session.latency.series.get(name) for name in ("clob_receive_to_decide",)
        ) or not any(session.latency.series.get(name) for name in ("spot_receive_to_decide",)):
            operational_faults.append(
                "OPERATIONAL: the latency artifact has no samples for one of the two triggers "
                f"(CLOB {session.latency.series.get('clob_receive_to_decide')}, "
                f"spot {session.latency.series.get('spot_receive_to_decide')})"
            )
        if classified != expected:
            operational_faults.append(
                f"OPERATIONAL: {classified} side classifications for {stats.decisions_written} "
                f"decisions; the exhaustive rule requires {expected}"
            )
        if action_total != expected:
            operational_faults.append(
                f"OPERATIONAL: {action_total} side actions for {stats.decisions_written} "
                f"decisions; the exhaustive rule requires {expected}"
            )
        clob_messages = int(counters.get("clob_messages") or 0)
        spot_messages = int(counters.get("spot_messages") or 0)
        if clob_messages < thresholds.min_clob_messages:
            operational_faults.append(
                f"OPERATIONAL: {clob_messages} CLOB messages is below the broken-collector "
                f"floor of {thresholds.min_clob_messages}"
            )
        if spot_messages < thresholds.min_spot_messages:
            operational_faults.append(
                f"OPERATIONAL: {spot_messages} BTC messages is below the broken-collector "
                f"floor of {thresholds.min_spot_messages}"
            )
        if stats.decisions_written < thresholds.min_decisions:
            operational_faults.append(
                f"OPERATIONAL: {stats.decisions_written} decisions is below the "
                f"broken-collector floor of {thresholds.min_decisions}"
            )
        if stale_fraction is not None and stale_fraction > thresholds.max_stale_fraction:
            operational_faults.append(
                f"OPERATIONAL: classified STALE for {stale_fraction:.3f} of cycles, above the "
                f"broken-collector ceiling of {thresholds.max_stale_fraction}"
            )
        prearm = session.prearm_summary()
        if not prearm["feed_ready_before_t0"]:
            operational_faults.append(
                "OPERATIONAL: the market data was not warm before T0 — "
                f"clob_ready={prearm['clob_book_ready_ns']} "
                f"spot_ready={prearm['spot_first_valid_ns']}"
            )

        # A dirty run's market is retained, verified and replayed like any other. It is simply
        # not acceptance evidence, and says so in its own row.
        if self.run_mode != "ACCEPTANCE_CLEAN":
            operational_faults.append(
                "OPERATIONAL: collected from modified tracked source (EXPLORATORY_DIRTY); "
                "retained, and not eligible as final empirical evidence"
            )

        eligible = (
            status == "COMPLETE"
            and replay.get("status") == "EXACT"
            and bool(replay.get("byte_roundtrip_identical"))
            and not operational_faults
            and not session.incidents
        )
        return {
            "schema_version": self.identity.get("corpus_schema_version"),
            "slug": session.slug,
            "epoch": self.config.epoch,
            "market_id": session.identity.market_id,
            "condition_id": session.identity.condition_id,
            "t0_ns": session.t0_ns,
            "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "config_sha256": self.identity.get("config_sha256"),
            "source_revision": self.identity.get("source_revision"),
            "source_tree_sha": self.identity.get("source_tree_sha"),
            "working_tree_clean": self.identity.get("working_tree_clean"),
            "run_mode": self.run_mode,
            "allow_dirty_requested": self.allow_dirty_requested,
            "sample_every": self.config.sample_every,
            "provenance": session.identity.provenance,
            "live_trading_enabled": LIVE_TRADING_ENABLED,
            "orders_sent": 0,
            "redemptions_sent": 0,
            "journal": {
                "path": str(session.journal_path),
                "bytes": replay.get("journal_bytes", session.journal_bytes),
                "sha256": replay.get("journal_sha256"),
                # The writer hashed the bytes as they left this process; the cold child read the
                # finished file back and hashed it again. Two independent computations over the
                # same journal, recorded separately so a disagreement is a visible fact rather
                # than one of them quietly winning.
                "writer_bytes": session.journal_bytes,
                "writer_sha256": session.journal_sha256,
                "writer_agrees": bool(
                    session.journal_sha256
                    and session.journal_sha256 == replay.get("journal_sha256")
                    and session.journal_bytes == replay.get("journal_bytes")
                ),
            },
            "store": {
                "path": str(session.database),
                "sha256": cold.get("database_sha256"),
                "bytes": cold.get("database_bytes"),
                "archive": cold.get("archive"),
            },
            "verification_status": status,
            "verification": cold.get("verification"),
            "telemetry_complete": None if manifest is None else manifest.telemetry_complete,
            "replay": replay,
            "decisions": stats.decisions_written,
            "risk_records": stats.risk_written,
            "fills": stats.fills_written,
            "dropped_records": session.buffer.dropped,
            "sequence_gaps": stats.sequence_gaps,
            "sink_errors": session.worker.store.sink_errors + session.closing_sink_errors,
            "worker": stats.summary(),
            # The ingress owner's own cost, measured on the real market rather than replayed.
            # This is the whole of what the session adds to a cycle: P7's shadow reconcile plus
            # the P8 capture, timed around `InstrumentedRun.observe`.
            "hot_path_observe_ns": session.hot_path_tiers or _tiers(session.hot_path_ns),
            "latency_artifact": None if session.latency is None else session.latency.summary(),
            "feed_counters": counters,
            "risk_states": dict(sorted(session.risk_states.items())),
            "places_by_risk_state": dict(sorted(session.places_by_state.items())),
            "quality_l3": quality,
            "classification": {
                "mode": "EVERY_DECISION",
                "rule": "two side opportunities per decision observation, UP and DOWN",
                "expected_classifications": expected,
                "actual_classifications": classified,
                "classification_complete": classified == expected,
                "own_fill_observations": stats.fills_seen,
                "note": (
                    "The denominator is 2 x committed decision rows because P13 sends no order "
                    "and therefore records no own fill. If own fills ever enter this stream the "
                    "formula changes and must be restated, not assumed."
                ),
            },
            "action_counts": actions,
            "action_total": action_total,
            "phases": session.phase_first_seen,
            "prearm": prearm,
            "settlement": None
            if decision is None
            else {
                "state": decision.state.value,
                "winning_outcome": (
                    None if decision.winning_outcome is None else decision.winning_outcome.value
                ),
                "authoritative_block": decision.authoritative_block,
                "payout_numerators": list(
                    () if decision.payout is None else decision.payout.numerators
                ),
                "redemption_enabled": False,
            },
            "commands": session.commands,
            "attempt_id": session.attempt_id,
            "resources": {
                "start": None
                if session.started_resources is None
                else session.started_resources.summary(),
                "trading_end": None
                if session.finished_resources is None
                else session.finished_resources.summary(),
                "post_release": None
                if session.released_resources is None
                else session.released_resources.summary(),
                "note": (
                    "`trading_end` is taken while the market's own graph is still held; "
                    "`post_release` after it has been let go. The first pilot reported only the "
                    "former and read 1.25 GB as though it described a released market."
                ),
                "cold_backlog": len(self.cold),
                "cold_backlog_high_water": self.cold_high_water,
                "cold_backlog_cap": MAX_COLD_BACKLOG,
                # Eleven readings across this market's cold path, each one a place a step in
                # resident memory could appear. Three samples per market said the process grew;
                # they could not say where, which is why P13's resource gate could not close.
                "checkpoints": session.checkpoints,
                "gc_window": session.gc_window,
                "market_lifecycles": self.lifecycles,
                "market_lifecycle_high_water": self.lifecycle_high_water,
                "market_lifecycle_cap": MAX_MARKET_LIFECYCLES,
                "gc": self.gc_observer.summary(),
                "allocator_maintenance": self.maintenance.summary(),
                "quiescent": self.quiescent,
                "audit_io": self.audit.summary(),
            },
            "incidents": list(session.incidents),
            "operational_faults": operational_faults,
            "evidence_eligible": eligible,
        }
