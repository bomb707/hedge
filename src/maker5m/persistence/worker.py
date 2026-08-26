"""The consumer: drains the hot buffer continuously, forever, on its own thread.

P8's buffer was a measurement harness — it held a whole market and was analysed at the end. That
is why it is sized at 320,000 observations and costs about 195 MiB: it was never draining. P11
takes the trade P8's own note recorded as P11's to take, and drains continuously, so the steady
state holds a batch rather than a market.

The producer/consumer contract, and why it is not a `queue.Queue`
-----------------------------------------------------------------
Invariant I19 forbids Plane 1 waiting on anything Plane 3 controls, and `queue.Queue.put` takes
a lock this thread holds. So the hot side keeps using `collections.deque`, and this side takes
from it with `popleft`.

That relies on a real property of CPython rather than a hopeful one: `deque.append` and
`deque.popleft` are individually atomic with respect to other threads, because each completes
within a single bytecode's C implementation without releasing the GIL. It is documented, it is
relied on by `queue.Queue` itself, and it means a producer appending while this thread pops
neither blocks nor corrupts. **This is a CPython assumption** (verified on CPython 3.12), not a
language guarantee, and it is tested under sustained concurrent load rather than asserted.

What this thread must never do is reach back. It holds the database connection, it takes the
exceptions, and it counts its own failures. A stalled or broken sink shows up as drops and sink
errors in the manifest, and as nothing at all in the trading loop.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from maker5m.domain import Outcome
from maker5m.persistence.records import MarketIdentity, build_decision_record
from maker5m.persistence.schema import Manifest, MarketMetrics
from maker5m.persistence.store import TelemetryStore
from maker5m.telemetry.analyzer import TelemetryAnalyzer
from maker5m.telemetry.observation import (
    OBS_FILL,
    OBS_SEQ,
    Observation,
    ObservationBuffer,
)

__all__ = ["DEFAULT_POLL_SECONDS", "PersistenceWorker", "WorkerStats"]

DEFAULT_POLL_SECONDS: Final[float] = 0.02
"""How long to sleep when the buffer is empty.

Short enough that a burst is drained promptly, long enough that an idle market does not spin a
core. The producer never waits on this thread, so the value trades latency-to-disk against idle
CPU and affects nothing on the trading path."""

DEFAULT_DRAIN_LIMIT: Final[int] = 4_096
"""Observations taken per pass before yielding, so one burst cannot monopolise the thread."""


@dataclass(slots=True)
class WorkerStats:
    """What the consumer did, and what it lost. OPERATIONAL measurements."""

    observations_consumed: int = 0
    decisions_written: int = 0
    fills_seen: int = 0
    passes: int = 0
    buffer_high_water: int = 0
    sequence_gaps: int = 0
    lost_observations: int = 0
    first_gap_at: int | None = None
    last_gap_at: int | None = None
    stalled_ns: int = 0

    def summary(self) -> dict[str, object]:
        return {
            "observations_consumed": self.observations_consumed,
            "decisions_written": self.decisions_written,
            "fills_seen": self.fills_seen,
            "passes": self.passes,
            "buffer_high_water": self.buffer_high_water,
            "sequence_gaps": self.sequence_gaps,
            "lost_observations": self.lost_observations,
            "first_gap_at": self.first_gap_at,
            "last_gap_at": self.last_gap_at,
        }


@dataclass(slots=True)
class PersistenceWorker:
    """Drains one observation buffer into one store, on one thread.

    The analyzer is fed here too, in the same order, so there is exactly one consumer of the
    capture stream and P8's measurement and P11's persistence cannot disagree about what
    happened. Feeding it incrementally is identical to feeding it a whole market at the end:
    `TelemetryAnalyzer.process` is an in-order fold, and order is preserved.
    """

    buffer: ObservationBuffer
    store: TelemetryStore
    identity: MarketIdentity
    analyzer: TelemetryAnalyzer | None = None
    poll_seconds: float = DEFAULT_POLL_SECONDS
    drain_limit: int = DEFAULT_DRAIN_LIMIT
    stats: WorkerStats = field(default_factory=WorkerStats)

    stall: Callable[[], bool] | None = None
    """Controlled fault injection: while this returns ``True`` the worker consumes nothing.

    Present so a real market can be run with a deliberately stalled sink and the bounded buffer
    can actually be exercised. It stalls the *consumer*, never the producer — which is the whole
    point of the experiment."""

    _thread: threading.Thread | None = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _persistence_sequence: int = 0
    _expected_seq: int = 0
    _started: bool = False

    # -- lifecycle -------------------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            raise RuntimeError("worker already started")
        self._started = True
        self.store.open()
        self.store.register_market(
            market_id=self.identity.market_id,
            slug=self.identity.slug,
            condition_id=self.identity.condition_id,
            provenance=self.identity.provenance,
        )
        self._thread = threading.Thread(target=self._run, name="maker5m-persistence", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        """Ask the thread to finish, drain what is left, and close the store.

        Called from the control plane at end of market, never from a decision cycle. The join
        is here and only here: this is Plane 3 waiting for Plane 3.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self.drain_once()
        self.store.flush()

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.stall is not None and self.stall():
                self._stop.wait(self.poll_seconds)
                continue
            drained = self.drain_once()
            if drained == 0:
                self._stop.wait(self.poll_seconds)

    # -- draining --------------------------------------------------------------------------

    def drain_once(self) -> int:
        """Take up to `drain_limit` observations and persist them. Never raises.

        `popleft` rather than `drain()`: taking the whole deque would race with a producer still
        appending to it, and would also mean holding a market-sized list in this thread — which
        is precisely the retention P11 exists to stop.
        """
        records = self.buffer.records
        occupancy = len(records)
        if occupancy > self.stats.buffer_high_water:
            self.stats.buffer_high_water = occupancy

        taken = 0
        while taken < self.drain_limit:
            try:
                observation = records.popleft()
            except IndexError:
                break
            taken += 1
            try:
                self._consume(observation)
            except Exception:
                self.store.sink_errors += 1
        if taken:
            self.stats.passes += 1
            self.stats.observations_consumed += taken
            self.buffer.drained += taken
        return taken

    def _consume(self, observation: Observation) -> None:
        seq = observation[OBS_SEQ]
        assert isinstance(seq, int)
        if seq > self._expected_seq:
            # The bounded buffer dropped the oldest. The loss is exact and is recorded as such;
            # nothing is interpolated across it.
            lost = seq - self._expected_seq
            self.stats.sequence_gaps += 1
            self.stats.lost_observations += lost
            if self.stats.first_gap_at is None:
                self.stats.first_gap_at = self._expected_seq
            self.stats.last_gap_at = seq
        self._expected_seq = seq + 1

        if self.analyzer is not None:
            self.analyzer.process(observation)

        if observation[OBS_FILL] is not None:
            self.stats.fills_seen += 1
            return

        self._persistence_sequence += 1
        record = build_decision_record(
            observation,
            self.identity,
            persistence_sequence=self._persistence_sequence,
            event_id=f"{self.identity.slug}-{seq:08d}",
            up_estimate=self._estimate("UP"),
            down_estimate=self._estimate("DOWN"),
        )
        self.store.write_decision(record)
        self.stats.decisions_written += 1

    def _estimate(self, side: str) -> Any:
        """P8's queue estimate for one side, or ``None``. Never a second queue model."""
        analyzer = self.analyzer
        if analyzer is None:
            return None
        outcome = Outcome.UP if side == "UP" else Outcome.DOWN
        return analyzer.shadow.estimate(outcome)

    # -- closing ---------------------------------------------------------------------------

    def write_metrics(self, metrics: MarketMetrics) -> None:
        self.store.write_metrics(metrics)

    def close_market(self, manifest: Manifest) -> None:
        self.store.write_manifest(manifest)
