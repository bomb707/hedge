"""The buffer between the trading path and telemetry analysis.

Two properties matter and they pull in opposite directions. Capture must never make trading
wait, so the buffer is bounded and drops rather than blocking. But a dropped observation means
a depth change at our own price went unseen, so the queue estimate cannot be continued across
it — and that must be *visible*, not quietly bridged.
"""

from __future__ import annotations

import sys
import time

import pytest

from maker5m.domain import Outcome
from maker5m.numeric import parse_share
from maker5m.telemetry import TelemetryAnalyzer, TelemetryOrderError
from maker5m.telemetry.observation import (
    DEFAULT_OBSERVATION_CAPACITY,
    NOT_CAPTURED,
    OBS_SEQ,
    ObservationBuffer,
)
from maker5m.telemetry.queue_estimate import QueueConfidence
from tests.telemetry.harness import analyzed, build, step, wants


def blank(seq: int) -> tuple[object, ...]:
    """A fill-shaped observation: enough to carry a sequence, no plan to interpret."""
    return (
        seq,
        seq,
        "ShadowFill",
        True,
        *([NOT_CAPTURED] * 6),
        None,
        0,
        0,
        None,
        None,
        None,
        (Outcome.UP, "shadow-00000001", False),
    )


# -- non-blocking capture -----------------------------------------------------------------


def test_capture_never_blocks_even_when_the_buffer_is_full() -> None:
    buffer = ObservationBuffer(capacity=64)
    for seq in range(64):
        buffer.capture(blank(seq))
    assert len(buffer) == 64

    start = time.perf_counter_ns()
    for seq in range(64, 5_000):
        buffer.capture(blank(seq))
    elapsed = time.perf_counter_ns() - start

    assert len(buffer) == 64, "the bound must hold"
    assert buffer.accepted == 5_000
    # 4,936 captures into a full buffer. A blocking implementation would not finish in this.
    assert elapsed < 100_000_000, f"capture into a full buffer took {elapsed} ns"


def test_a_full_buffer_keeps_the_most_recent_observations() -> None:
    """Drop-oldest: recent observations describe the market we are still in."""
    buffer = ObservationBuffer(capacity=8)
    for seq in range(20):
        buffer.capture(blank(seq))
    assert [o[OBS_SEQ] for o in buffer] == list(range(12, 20))


def test_the_drop_count_is_visible() -> None:
    buffer = ObservationBuffer(capacity=10)
    for seq in range(10):
        buffer.capture(blank(seq))
    assert buffer.dropped == 0
    for seq in range(10, 25):
        buffer.capture(blank(seq))
    assert buffer.dropped == 15
    assert buffer.accepted == 25


def test_draining_does_not_confuse_the_drop_count() -> None:
    buffer = ObservationBuffer(capacity=10)
    for seq in range(8):
        buffer.capture(blank(seq))
    assert buffer.drain() == [blank(seq) for seq in range(8)]
    assert buffer.dropped == 0
    for seq in range(8, 30):
        buffer.capture(blank(seq))
    assert buffer.dropped == 12
    assert len(buffer) == 10


def test_a_capacity_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="capacity"):
        ObservationBuffer(capacity=0)


# -- ordering and gaps ---------------------------------------------------------------------


def test_the_analyzer_requires_capture_order() -> None:
    analyzer = TelemetryAnalyzer()
    analyzer.process(blank(0))
    analyzer.process(blank(1))
    with pytest.raises(TelemetryOrderError, match="out of order"):
        analyzer.process(blank(0))


def test_out_of_order_input_fails_closed_rather_than_being_sorted() -> None:
    """A stream that arrived out of order has unknown provenance. Do not repair it."""
    analyzer = TelemetryAnalyzer()
    for seq in (0, 1, 2, 3):
        analyzer.process(blank(seq))
    with pytest.raises(TelemetryOrderError):
        analyzer.process(blank(2))
    assert analyzer.processed == 4, "nothing was folded from the rejected observation"


def test_a_gap_is_counted_and_not_bridged() -> None:
    analyzer = TelemetryAnalyzer()
    analyzer.process(blank(0))
    analyzer.process(blank(7))
    assert analyzer.gaps == 1
    assert analyzer.lost_observations == 6
    analyzer.process(blank(8))
    assert analyzer.gaps == 1, "a contiguous observation is not a second gap"


def test_a_dropped_observation_invalidates_queue_continuity() -> None:
    """The estimate depends on having seen every depth change. A gap ends that."""
    harness = build(sample_every=1)
    step(harness, up=wants("0.63"), up_bid="0.63", up_bid_size="40", up_ask="0.64")
    for size in ("35", "30"):
        step(harness, up=wants("0.63"), up_bid="0.63", up_bid_size=size, up_ask="0.64")

    healthy = analyzed(harness)
    before = healthy.shadow.estimate(Outcome.UP)
    assert before is not None
    assert before.confidence is QueueConfidence.ESTIMATED
    assert before.ahead == parse_share("30")

    # Replay the same stream with one observation missing, as a drop would leave it.
    observations = list(harness.buffer)
    lossy = TelemetryAnalyzer(sampling=harness.sampling)
    lossy.run(observations[:1] + observations[2:])

    after = lossy.shadow.estimate(Outcome.UP)
    assert after is not None
    assert after.confidence is QueueConfidence.STALE, "a gap must not be silently bridged"
    assert lossy.gaps == 1
    assert lossy.lost_observations == 1


def test_a_stale_estimate_stops_reading_as_at_front() -> None:
    """The consequence that matters: a gap must not leave a flattering classification."""
    harness = build(sample_every=1)
    step(harness, up=wants("0.63"), up_bid="0.63", up_bid_size="0", up_ask="0.64")
    for size in ("0", "0"):
        step(harness, up=wants("0.63"), up_bid="0.63", up_bid_size=size, up_ask="0.64")

    observations = list(harness.buffer)
    assert analyzed(harness).counters.quality.get("AT_FRONT", 0) >= 2

    lossy = TelemetryAnalyzer(sampling=harness.sampling)
    lossy.run(observations[:1] + observations[2:])
    assert lossy.counters.quality.get("AT_FRONT", 0) <= 1
    assert lossy.counters.quality.get("STALE", 0) >= 1


# -- sizing ------------------------------------------------------------------------------


def test_the_default_capacity_covers_a_measured_market_with_room() -> None:
    """The busiest market actually measured produced 117,772 cycles."""
    assert DEFAULT_OBSERVATION_CAPACITY >= 117_772
    # And it is bounded, not effectively infinite: ~638 bytes retained per observation.
    assert DEFAULT_OBSERVATION_CAPACITY * 638 < 128 * 1024 * 1024


def test_one_observation_is_small() -> None:
    """A tuple of references, not a copy of the world."""
    observation = blank(0)
    assert sys.getsizeof(observation) < 256
