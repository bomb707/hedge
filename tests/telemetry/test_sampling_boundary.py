"""Sampling must thin *emission* and never thin *state*.

Two different failures live here. If sampling only controlled what reached the sink, unsampled
cycles would still pay for timestamps, classification, and distribution updates, and the
sampling policy would be decoration. If sampling also skipped slot transitions, the queue
estimate would silently depend on the sampling rate — an unsampled depth decrease is a decrease
the estimate never learns about.
"""

from __future__ import annotations

from maker5m.domain import Outcome
from maker5m.numeric import parse_share
from maker5m.telemetry.sampling import ALWAYS_TRACED_KINDS, SAMPLING_STATUS, SamplingPolicy
from tests.telemetry.harness import build, step, wants


def quoting(harness: object, size: str, ask: str = "0.64") -> None:
    from maker5m.telemetry import InstrumentedRun

    assert isinstance(harness, InstrumentedRun)
    step(harness, up=wants("0.63"), up_bid="0.63", up_bid_size=size, up_ask=ask)


def test_unsampled_cycles_emit_nothing() -> None:
    """No trace, no latency sample. Emission is what sampling is for."""
    harness = build(sample_every=1_000_000)
    quoting(harness, "40")  # PLACE: acting cycles are always traced
    before_sink = harness.sink.accepted
    before_latency = len(harness.latency.receive_to_reconcile)
    before_queue = len(harness.latency.queue_ahead)

    for size in ("39", "38", "37", "36", "35"):
        quoting(harness, size)

    assert harness.counters.actions.get("KEEP", 0) == 5
    assert harness.sink.accepted == before_sink, "an unsampled cycle must not reach the sink"
    assert len(harness.latency.receive_to_reconcile) == before_latency, (
        "an unsampled cycle must not add a latency sample"
    )
    assert len(harness.latency.queue_ahead) == before_queue


def test_unsampled_cycles_still_maintain_queue_state() -> None:
    """The half sampling must never touch: depth movement the estimate depends on."""
    harness = build(sample_every=1_000_000)
    quoting(harness, "40")
    estimate = harness.shadow.estimate(Outcome.UP)
    assert estimate is not None and estimate.ahead == parse_share("40")

    for size in ("35", "30", "22", "12"):
        quoting(harness, size)

    after = harness.shadow.estimate(Outcome.UP)
    assert after is not None
    assert after.ahead == parse_share("12"), (
        "every unsampled decrease must still have been observed"
    )
    assert harness.shadow.kept == 4
    assert harness.sink.accepted == 1, "only the PLACE cycle was traced"


def test_the_estimate_does_not_depend_on_the_sampling_rate() -> None:
    """The property that makes the state/emission split correct, stated directly."""
    results = []
    for sample_every in (1, 3, 10, 1_000_000):
        harness = build(sample_every=sample_every)
        quoting(harness, "40")
        for size in ("35", "30", "22", "12"):
            quoting(harness, size)
        estimate = harness.shadow.estimate(Outcome.UP)
        assert estimate is not None
        results.append((sample_every, estimate.ahead, harness.counters.actions.get("KEEP", 0)))

    aheads = {ahead for _, ahead, _ in results}
    keeps = {keep for _, _, keep in results}
    assert len(aheads) == 1, f"queue estimate varied with sampling rate: {results}"
    assert len(keeps) == 1, f"action counts varied with sampling rate: {results}"


def test_sampled_cycles_do_emit() -> None:
    """The corollary: with sampling off, every cycle produces a trace and a sample."""
    harness = build(sample_every=1)
    quoting(harness, "40")
    for size in ("39", "38"):
        quoting(harness, size)
    assert harness.sink.accepted == 3
    assert len(harness.latency.receive_to_reconcile) == 3
    assert len(harness.latency.queue_ahead) >= 3


def test_safety_relevant_kinds_are_never_sampled_away() -> None:
    policy = SamplingPolicy(1_000_000)
    for kind in ("OwnFill", "OrderStateEvent", "PhaseEvent", "HealthEvent"):
        assert kind in ALWAYS_TRACED_KINDS
        assert policy.selects(7, kind), f"{kind} must always be traced"
    assert not policy.selects(7, "BookUpdate")
    # And anything issuing a request is forced by the caller.
    assert policy.should_trace(ingress_ordinal=7, event_kind="BookUpdate", forced=True)


def test_the_sampling_policy_is_operational_configuration() -> None:
    assert SAMPLING_STATUS.value == "OPERATIONAL"
