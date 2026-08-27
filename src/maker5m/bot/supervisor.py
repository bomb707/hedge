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
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from multiprocessing import get_context
from pathlib import Path
from typing import Any

from maker5m.bot.cold import ColdRequest, cold_finalize
from maker5m.bot.config import PaperConfig, config_identity
from maker5m.bot.corpus import CorpusIndex
from maker5m.bot.session import MarketSession, PrearmRecord
from maker5m.bot.settle import settle_market
from maker5m.feeds.discovery import discover_market, slug_for, t0_of_slug
from maker5m.market.timebase import NANOS_PER_SECOND
from maker5m.safety import LIVE_TRADING_ENABLED
from maker5m.ui import (
    CommandBridge,
    CommandInbox,
    HotCommandChannel,
    SnapshotChannel,
    drain_operator_commands,
)

__all__ = ["Supervisor", "UiPlane"]

MARKET_SECONDS = 300
PREARM_LEAD_SECONDS = 75
"""How early discovery starts. The capture opens its own feeds at T0-30; this is the metadata."""

MAX_COLD_BACKLOG = 3
"""Closed markets whose cold work may be in flight at once. Bounded so a slow chain cannot
accumulate raw 650 MB stores without limit."""


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
    ui: UiPlane = field(init=False)
    corpus: CorpusIndex = field(init=False)
    identity: dict[str, Any] = field(init=False)
    pool: ProcessPoolExecutor | None = None
    cold: set[asyncio.Task[None]] = field(default_factory=set)
    sessions: set[asyncio.Task[None]] = field(default_factory=set)
    activations: set[asyncio.Task[None]] = field(default_factory=set)
    attempted: int = 0
    completed: int = 0
    skipped: list[str] = field(default_factory=list)
    log: Any = print
    restarted: bool = False

    def __post_init__(self) -> None:
        if LIVE_TRADING_ENABLED:  # pragma: no cover - the flag is a constant False
            raise RuntimeError("refusing to start a paper run while live trading is enabled")
        self.ui = UiPlane(directory=self.config.ui_dir)
        self.corpus = CorpusIndex(path=self.config.corpus_path)
        self.identity = config_identity(self.config)
        self.config.evidence_dir.mkdir(parents=True, exist_ok=True)

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
        context = get_context("spawn")
        self.pool = ProcessPoolExecutor(max_workers=2, mp_context=context)
        already = self.corpus.completed_slugs()
        try:
            await self._loop(already)
        finally:
            await self._drain_cold()
            self.ui.stop()
            if self.pool is not None:
                self.pool.shutdown(wait=True)
            self.pool = None

    async def _loop(self, already: set[str]) -> None:
        """Launch each market in time to prearm it, and never wait on a closed one.

        The cadence is the market's, not ours. A capture opens its own feeds at T0-30 and runs
        to T0+305, which is five seconds *past* the next market's T0 — so sessions must be
        launched while the previous one is still trading. Waiting for market N to return before
        looking at N+1 would skip N+1 entirely, every time. The first version of this loop did
        exactly that.
        """
        launched: dict[str, MarketSession] = {}
        t0 = self._first_t0()
        while self.target_markets is None or self.completed < self.target_markets:
            slug = slug_for(t0)
            if slug in already or slug in launched:
                t0 += MARKET_SECONDS
                continue

            await self._sleep_until(t0 - PREARM_LEAD_SECONDS)
            market, prearm = await self.prearm(slug)
            if market is None:
                self._record_prearm_failure(prearm)
                t0 += MARKET_SECONDS
                continue

            session = MarketSession(market=market, config=self.config, prearm=prearm, ui=self.ui)
            session.source_revision = str(self.identity.get("source_revision", ""))
            self.attempted += 1
            launched[slug] = session
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
            if len(self.cold) > MAX_COLD_BACKLOG:
                # Recorded, never waited on. A slow chain must not cost a market.
                self.log(f"    cold backlog is {len(self.cold)} markets")
            t0 += MARKET_SECONDS

        await self._drain_sessions()

    def _closer(self, session: MarketSession) -> Callable[[asyncio.Task[None]], None]:
        def done(_task: asyncio.Task[None]) -> None:
            self._closed(session)

        return done

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

    async def _bound_cold(self) -> None:
        """Wait, between markets and never during one, if the backlog has grown."""
        while len(self.cold) > MAX_COLD_BACKLOG:
            done, _ = await asyncio.wait(self.cold, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                self.cold.discard(task)

    async def _drain_cold(self) -> None:
        if self.cold:
            await asyncio.gather(*list(self.cold), return_exceptions=True)

    async def _finalize(self, session: MarketSession) -> None:
        """Everything a closed market still owes, none of it on a trading path."""
        try:
            await session.write_journal()
            await session.settle(settle_market)
            await asyncio.to_thread(session.close_store)
            cold = await self._cold_result(session)
            session.publish_close(cold)
            entry = self._entry(session, cold)
            if not self.corpus.append(entry):
                self.log(f"    corpus append failed for {session.slug}")
            if entry.get("verification_status") == "COMPLETE" and entry.get("evidence_eligible"):
                self.completed += 1
            self.log(
                f"    {session.slug}: {entry.get('verification_status')} "
                f"replay={entry.get('replay', {}).get('status')} "
                f"eligible={entry.get('evidence_eligible')} "
                f"({self.completed} complete)"
            )
        except Exception as error:  # pragma: no cover - the cold path never kills the run
            self.log(f"    {session.slug}: cold path failed: {type(error).__name__}: {error}")
        finally:
            session.release()

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

    def _record_prearm_failure(self, prearm: PrearmRecord) -> None:
        self.corpus.append(
            {
                "slug": prearm.slug,
                "epoch": self.config.epoch,
                "config_sha256": self.identity.get("config_sha256"),
                "source_revision": self.identity.get("source_revision"),
                "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "verification_status": "NOT_STARTED",
                "evidence_eligible": False,
                "prearm": prearm.summary(),
                "incidents": [f"prearm failed: {prearm.error}"],
            }
        )

    def _entry(self, session: MarketSession, cold: dict[str, Any]) -> dict[str, Any]:
        manifest = session.manifest
        stats = session.worker.stats
        capture = session.capture
        counters = (
            capture.counters.summary()
            if capture is not None and hasattr(capture.counters, "summary")
            else {}
        )
        replay = dict(cold.get("replay") or {})
        settlement = session.settlement
        decision = None if settlement is None else settlement.decision
        status = str(cold.get("verification_status", "CORRUPT"))
        quality = session.quality.summary()
        thresholds = self.config.thresholds
        stale_fraction = quality["fractions"].get("STALE")

        operational_faults: list[str] = []
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
        if not session.prearm.ok or session.prearm.ready_ns >= session.prearm.t0_ns:
            operational_faults.append("OPERATIONAL: prearm was not ready before T0")

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
            "provenance": session.identity.provenance,
            "live_trading_enabled": LIVE_TRADING_ENABLED,
            "orders_sent": 0,
            "redemptions_sent": 0,
            "journal": {
                "path": str(session.journal_path),
                "bytes": replay.get("journal_bytes", session.journal_bytes),
                "sha256": replay.get("journal_sha256"),
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
            "feed_counters": counters,
            "risk_states": dict(sorted(session.risk_states.items())),
            "places_by_risk_state": dict(sorted(session.places_by_state.items())),
            "quality_l3": quality,
            "phases": session.phase_first_seen,
            "prearm": session.prearm.summary(),
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
            "resources": {
                "start": None
                if session.started_resources is None
                else session.started_resources.summary(),
                "end": None
                if session.finished_resources is None
                else session.finished_resources.summary(),
                "cold_backlog": len(self.cold),
            },
            "incidents": list(session.incidents),
            "operational_faults": operational_faults,
            "evidence_eligible": eligible,
        }
