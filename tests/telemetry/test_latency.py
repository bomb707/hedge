"""The latency clock, stage traces, sink backpressure, and sampling."""

from __future__ import annotations

import time

import pytest

from maker5m.domain import ParameterStatus
from maker5m.telemetry import (
    ALWAYS_TRACED_KINDS,
    SAMPLING_STATUS,
    Distribution,
    SamplingPolicy,
    Stage,
    TelemetrySink,
    TraceBuilder,
    perf_now_ns,
    quantile,
)


class FakeClock:
    """A deterministic latency clock, so stage arithmetic is testable exactly."""

    def __init__(self, start: int = 1_000_000) -> None:
        self.value = start

    def __call__(self) -> int:
        return self.value

    def advance(self, ns: int) -> None:
        self.value += ns


# -- the clock ------------------------------------------------------------


def test_the_latency_clock_is_monotonic() -> None:
    readings = [perf_now_ns() for _ in range(20_000)]
    assert readings == sorted(readings)


def test_the_latency_clock_is_high_resolution() -> None:
    """Distinct readings across a short busy loop, so sub-microsecond stages are measurable."""
    readings = {perf_now_ns() for _ in range(5_000)}
    assert len(readings) > 100


def test_the_latency_clock_is_not_the_ingress_clock() -> None:
    """Different domains. Subtracting one from the other would be meaningless."""
    assert abs(perf_now_ns() - time.time_ns()) > 1_000_000_000


def test_a_deterministic_clock_can_be_injected() -> None:
    clock = FakeClock()
    trace = TraceBuilder()
    trace.mark(Stage.RAW_RECEIVE, clock)
    clock.advance(1_500)
    trace.mark(Stage.DECODE_DONE, clock)
    assert trace.duration(Stage.RAW_RECEIVE, Stage.DECODE_DONE) == 1_500


# -- stage traces ---------------------------------------------------------


def test_stage_durations_are_computed_exactly() -> None:
    clock = FakeClock()
    trace = TraceBuilder()
    for stage, gap in (
        (Stage.RAW_RECEIVE, 0),
        (Stage.DECODE_DONE, 800),
        (Stage.NORMALIZE_DONE, 1_200),
        (Stage.INGRESS_ASSIGNED, 300),
        (Stage.REDUCE_DONE, 2_000),
        (Stage.DECIDE_DONE, 16_000),
    ):
        clock.advance(gap)
        trace.mark(stage, clock)
    assert trace.duration(Stage.RAW_RECEIVE, Stage.DECODE_DONE) == 800
    assert trace.duration(Stage.DECODE_DONE, Stage.NORMALIZE_DONE) == 1_200
    assert trace.duration(Stage.REDUCE_DONE, Stage.DECIDE_DONE) == 16_000
    assert trace.duration(Stage.RAW_RECEIVE, Stage.DECIDE_DONE) == 20_300


def test_a_missing_stage_yields_none_not_zero() -> None:
    """A KEEP cycle has no dispatch; reporting 0 ns would corrupt the distribution."""
    clock = FakeClock()
    trace = TraceBuilder()
    trace.mark(Stage.RAW_RECEIVE, clock)
    clock.advance(5_000)
    trace.mark(Stage.DECIDE_DONE, clock)
    assert trace.at(Stage.DISPATCH_START) is None
    assert trace.duration(Stage.DECIDE_DONE, Stage.DISPATCH_START) is None
    assert trace.duration(Stage.DISPATCH_START, Stage.DISPATCH_RETURN) is None


def test_a_trace_can_be_reset_and_reused_without_allocating() -> None:
    clock = FakeClock()
    trace = TraceBuilder(market_id="m", event_kind="BookUpdate")
    stamps = trace.stamps
    trace.mark(Stage.RAW_RECEIVE, clock)
    trace.reset()
    assert trace.stamps is stamps, "the buffer is reused, not reallocated"
    assert trace.at(Stage.RAW_RECEIVE) is None
    assert trace.market_id == ""


def test_a_trace_is_mutable_by_design() -> None:
    """P4/P7 measured ~99 ns per frozen field; a hot-path rebuild per stage is not worth it."""
    trace = TraceBuilder()
    trace.market_id = "m"
    trace.action = "KEEP"
    assert trace.market_id == "m"


def test_the_snapshot_is_immutable() -> None:
    trace = TraceBuilder(market_id="m", event_id="e", ingress_ordinal=3)
    trace.mark(Stage.RAW_RECEIVE, FakeClock())
    snapshot = trace.snapshot()
    assert isinstance(snapshot, tuple)
    assert isinstance(snapshot[-1], tuple)


def test_setting_a_stage_from_an_existing_reading_avoids_a_second_call() -> None:
    clock = FakeClock()
    trace = TraceBuilder()
    reading = trace.mark(Stage.DECIDE_DONE, clock)
    trace.set(Stage.PREPARE_DONE, reading)
    assert trace.duration(Stage.DECIDE_DONE, Stage.PREPARE_DONE) == 0


# -- the sink -------------------------------------------------------------


def test_the_sink_never_blocks_when_full() -> None:
    sink = TelemetrySink(capacity=8)
    for index in range(1_000):
        sink.put((index,))
    assert len(sink) == 8


def test_drops_are_counted() -> None:
    sink = TelemetrySink(capacity=4)
    for index in range(10):
        sink.put((index,))
    assert sink.dropped == 6
    assert sink.accepted == 10


def test_the_sink_keeps_the_newest_records() -> None:
    sink = TelemetrySink(capacity=3)
    for index in range(6):
        sink.put((index,))
    assert [record[0] for record in sink] == [3, 4, 5]


def test_draining_empties_the_sink() -> None:
    sink = TelemetrySink(capacity=4)
    sink.put((1,))
    assert sink.drain() == [(1,)]
    assert len(sink) == 0


def test_a_non_positive_capacity_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        TelemetrySink(capacity=0)


# -- sampling -------------------------------------------------------------


def test_sampling_is_operational_configuration() -> None:
    assert SAMPLING_STATUS is ParameterStatus.OPERATIONAL


def test_the_default_policy_traces_everything() -> None:
    policy = SamplingPolicy()
    assert all(
        policy.should_trace(ingress_ordinal=n, event_kind="BookUpdate", forced=False)
        for n in range(100)
    )


def test_sampling_is_deterministic_and_reproducible() -> None:
    """A replayed run must sample exactly the same events, so no randomness is involved."""
    policy = SamplingPolicy(sample_every=10)
    first = [
        policy.should_trace(ingress_ordinal=n, event_kind="BookUpdate", forced=False)
        for n in range(200)
    ]
    second = [
        SamplingPolicy(sample_every=10).should_trace(
            ingress_ordinal=n, event_kind="BookUpdate", forced=False
        )
        for n in range(200)
    ]
    assert first == second
    assert sum(first) == 20


@pytest.mark.parametrize("kind", sorted(ALWAYS_TRACED_KINDS))
def test_rare_significant_events_bypass_sampling(kind: str) -> None:
    policy = SamplingPolicy(sample_every=1_000)
    assert policy.should_trace(ingress_ordinal=7, event_kind=kind, forced=False)


def test_anything_that_issued_a_request_bypasses_sampling() -> None:
    policy = SamplingPolicy(sample_every=1_000)
    assert policy.should_trace(ingress_ordinal=7, event_kind="BookUpdate", forced=True)


def test_an_invalid_sampling_rate_is_rejected() -> None:
    with pytest.raises(ValueError, match=">= 1"):
        SamplingPolicy(sample_every=0)


# -- distributions ---------------------------------------------------------


def test_quantiles_are_exact_nearest_rank() -> None:
    distribution = Distribution("t")
    for value in range(1, 101):
        distribution.add(value)
    summary = distribution.summary()
    assert summary["count"] == 100
    assert summary["p50"] == 50
    assert summary["p90"] == 90
    assert summary["p95"] == 95
    assert summary["p99"] == 99
    assert summary["max"] == 100


def test_an_empty_distribution_reports_zero_count_not_a_fake_quantile() -> None:
    assert Distribution("t").summary() == {"label": "t", "count": 0}


def test_quantile_rejects_an_empty_sample_set() -> None:
    with pytest.raises(ValueError, match="no samples"):
        quantile([], 0.5)
