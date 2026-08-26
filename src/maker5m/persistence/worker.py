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
from maker5m.persistence.analytics import MetricsAccumulator, risk_row
from maker5m.persistence.capture import BoundedChannel
from maker5m.persistence.records import (
    MarketIdentity,
    build_decision_record,
    build_fill_record,
)
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
    consume_errors: int = 0
    risk_written: int = 0
    fills_written: int = 0
    error_samples: list[str] = field(default_factory=list)
    """A few distinct failure descriptions, kept so a silent count is never the only clue.

    Bounded: a broken record type would otherwise produce one string per observation. The point
    is to name the fault, not to log the market."""

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
            "consume_errors": self.consume_errors,
            "risk_written": self.risk_written,
            "fills_written": self.fills_written,
            "error_samples": list(self.error_samples),
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
    fills: BoundedChannel | None = None
    """Canonical fills, published from Plane 1 through a bounded non-blocking channel."""

    risk: BoundedChannel | None = None
    """P9 records, published as they are produced rather than dumped after DONE.

    Continuous because P11 is the durability phase: a mid-market crash should leave a useful
    partial risk audit beside the useful partial decision audit. P9's own `RiskTrace` is
    untouched and remains the in-memory authority; this is a copy travelling to disk."""

    analyzer: TelemetryAnalyzer | None = None
    metrics: MetricsAccumulator | None = None
    """Folded here rather than by a second pass over stored rows, so a market that crashes has
    whatever was true when it stopped rather than nothing at all."""

    poll_seconds: float = DEFAULT_POLL_SECONDS
    drain_limit: int = DEFAULT_DRAIN_LIMIT
    stats: WorkerStats = field(default_factory=WorkerStats)

    stall: Callable[[], bool] | None = None
    """Controlled fault injection: while this returns ``True`` the worker consumes nothing.

    Present so a real market can be run with a deliberately stalled sink and the bounded buffer
    can actually be exercised. It stalls the *consumer*, never the producer — which is the whole
    point of the experiment."""

    _draining: threading.Lock = field(default_factory=threading.Lock, repr=False)
    """Consumer-side only. The producer never sees this lock and never waits on it.

    It exists because sequence accounting is only meaningful with a single consumer: two threads
    popping the same deque interleave, so each sees the other's observations as forward jumps and
    reports gaps that never happened. Phantom gaps are worse than no accounting at all — they
    would mark a whole market's telemetry incomplete for nothing, and would hide a real gap in
    the noise. Found by running two drainers by accident in a load test."""

    _thread: threading.Thread | None = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _ready: threading.Event = field(default_factory=threading.Event, repr=False)
    _persistence_sequence: int = 0
    _expected_seq: int = 0
    _started: bool = False

    # -- lifecycle -------------------------------------------------------------------------

    def start(self) -> None:
        """Launch the consumer. The connection is opened *by the thread that will use it*.

        sqlite3 refuses a connection used from a thread other than the one that created it, and
        it is right to: the ownership claim this module makes has to be true of the actual
        object, not merely of the design. Opening it here on the caller's thread made every
        write fail with `ProgrammingError` — 4,000 sink errors and one row — while the unit
        tests passed, because they drained on the main thread. The real-market benchmark found
        it, which is the reason that benchmark exists.
        """
        if self._started:
            raise RuntimeError("worker already started")
        self._started = True
        self._thread = threading.Thread(target=self._run, name="maker5m-persistence", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10.0):
            raise RuntimeError("persistence worker did not open its store")

    def stop(self, timeout: float = 30.0) -> None:
        """Ask the thread to finish, drain what is left, and close the store.

        Called from the control plane at end of market, never from a decision cycle. The join
        is here and only here: this is Plane 3 waiting for Plane 3.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():  # pragma: no cover - only on a wedged worker
                self.store.sink_errors += 1
                return
        # The thread has finished and released the connection, so the final drain and close
        # happen here only when there is no thread left to do them.
        if self._thread is None:
            self.drain_once()
            self.store.close()

    def _run(self) -> None:
        try:
            self.store.open()
            self.store.register_market(
                market_id=self.identity.market_id,
                slug=self.identity.slug,
                condition_id=self.identity.condition_id,
                provenance=self.identity.provenance,
            )
        except Exception:
            self.store.sink_errors += 1
        finally:
            self._ready.set()

        while not self._stop.is_set():
            if self.stall is not None and self.stall():
                self._stop.wait(self.poll_seconds)
                continue
            drained = self.drain_once() + self.drain_side_channels()
            if drained == 0:
                self._stop.wait(self.poll_seconds)

        # Final drain and close, on the thread that owns the connection.
        while self.drain_once() or self.drain_side_channels():
            pass
        self.store.close()

    # -- draining --------------------------------------------------------------------------

    def drain_once(self) -> int:
        """Take up to `drain_limit` observations and persist them. Never raises.

        `popleft` rather than `drain()`: taking the whole deque would race with a producer still
        appending to it, and would also mean holding a market-sized list in this thread — which
        is precisely the retention P11 exists to stop.

        **Single consumer.** Concurrent callers are turned away rather than interleaved; see
        `_draining`.
        """
        if not self._draining.acquire(blocking=False):
            # Another thread is already draining. Returning rather than waiting keeps this a
            # non-blocking consumer, and the observations are not lost — the other drainer has
            # them.
            return 0
        try:
            return self._drain_locked()
        finally:
            self._draining.release()

    def _drain_locked(self) -> int:
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
            except Exception as error:
                self._record_consume_error(error)
        if taken:
            self.stats.passes += 1
            self.stats.observations_consumed += taken
            self.buffer.drained += taken
        return taken

    def _record_consume_error(self, error: Exception) -> None:
        """Count the failure and keep a description of it.

        A bare counter was the original shape and it hid a real defect for as long as it
        existed: 1,789 records per run were failing to build and the only symptom was a number
        that could equally have meant a full disk. Whatever swallows an exception owes the
        reader its name.
        """
        self.store.sink_errors += 1
        self.stats.consume_errors += 1
        description = f"{type(error).__name__}: {error}"
        samples = self.stats.error_samples
        if description not in samples and len(samples) < 8:
            samples.append(description)

    def drain_side_channels(self) -> int:
        """Persist whatever fills and risk records are waiting. Never raises.

        Risk first: a decision record names the `risk_sequence` that governed it, and the
        verifier checks that the row it names exists. Writing the verdict before the decisions
        it governed keeps that reference satisfiable at every point in the file, including after
        a crash.
        """
        if not self._draining.acquire(blocking=False):
            return 0
        try:
            return self._drain_risk() + self._drain_fills()
        finally:
            self._draining.release()

    def _drain_risk(self) -> int:
        channel = self.risk
        if channel is None:
            return 0
        taken = 0
        while taken < self.drain_limit:
            try:
                record = channel.records.popleft()
            except IndexError:
                break
            taken += 1
            try:
                self._persistence_sequence += 1
                self.store.write_risk(
                    risk_row(
                        record,
                        market_id=self.identity.market_id,
                        persistence_sequence=self._persistence_sequence,
                    )
                )
                self.stats.risk_written += 1
            except Exception as error:
                self._record_consume_error(error)
        channel.drained += taken
        return taken

    def _drain_fills(self) -> int:
        channel = self.fills
        if channel is None:
            return 0
        taken = 0
        while taken < self.drain_limit:
            try:
                capture = channel.records.popleft()
            except IndexError:
                break
            taken += 1
            try:
                self._persistence_sequence += 1
                record = build_fill_record(
                    capture, self.identity, persistence_sequence=self._persistence_sequence
                )
                self.store.write_fill(record)
                if self.metrics is not None:
                    self.metrics.observe_fill(record)
                self.stats.fills_written += 1
            except Exception as error:
                self._record_consume_error(error)
        channel.drained += taken
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
            up_estimate=self._estimate("UP"),
            down_estimate=self._estimate("DOWN"),
        )
        self.store.write_decision(record)
        if self.metrics is not None:
            self.metrics.observe_decision(record)
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
