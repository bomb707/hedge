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
from maker5m.telemetry import InstrumentedRun
from maker5m.telemetry.observation import (
    NOT_CAPTURED,
    OBS_DECIDE_DONE_NS,
    OBS_PLAN,
    OBS_PREPARE_DONE_NS,
    OBS_RECONCILE_DONE_NS,
)
from maker5m.telemetry.sampling import ALWAYS_TRACED_KINDS, SAMPLING_STATUS, SamplingPolicy
from tests.telemetry.harness import analyzed, build, step, wants


def quoting(harness: InstrumentedRun, size: str, ask: str = "0.64", price: str = "0.63") -> None:
    step(harness, up=wants(price), up_bid=price, up_bid_size=size, up_ask=ask)


def test_unsampled_cycles_capture_no_stage_timings() -> None:
    """Sampling controls timing work, not just what gets written down.

    An unsampled cycle takes no perf-counter readings at all, so its stage fields are
    NOT_CAPTURED and the analyzer adds nothing to any latency distribution from it.
    """
    harness = build(sample_every=1_000_000)
    quoting(harness, "40")  # PLACE: an action, so it is timed whatever the sampler says
    before = len(analyzed(harness).latency.receive_to_reconcile)

    for size in ("39", "38", "37", "36", "35"):
        quoting(harness, size)

    result = analyzed(harness)
    assert result.counters.actions.get("KEEP", 0) == 5
    assert len(result.latency.receive_to_reconcile) == before, (
        "an unsampled cycle must not contribute a latency sample"
    )

    # The five KEEP cycles took no readings at all. The PLACE cycle is excluded because an
    # action is always timed, which the next test covers.
    untimed = [o for o in harness.buffer if o[OBS_RECONCILE_DONE_NS] == NOT_CAPTURED]
    assert len(untimed) == 5
    for observation in untimed:
        assert observation[OBS_PLAN] is not None
        assert observation[OBS_DECIDE_DONE_NS] == NOT_CAPTURED
        assert observation[OBS_PREPARE_DONE_NS] == NOT_CAPTURED


def test_unsampled_cycles_are_still_captured_and_still_maintain_queue_state() -> None:
    """The half sampling must never touch: depth movement the estimate depends on."""
    harness = build(sample_every=1_000_000)
    quoting(harness, "40")
    first = analyzed(harness).shadow.estimate(Outcome.UP)
    assert first is not None and first.ahead == parse_share("40")

    for size in ("35", "30", "22", "12"):
        quoting(harness, size)

    result = analyzed(harness)
    after = result.shadow.estimate(Outcome.UP)
    assert after is not None
    assert after.ahead == parse_share("12"), (
        "every unsampled decrease must still have been observed"
    )
    assert result.shadow.kept == 4
    assert harness.buffer.accepted == 5, "every cycle is captured; only analysis is thinned"
    assert harness.buffer.dropped == 0


def test_the_estimate_does_not_depend_on_the_sampling_rate() -> None:
    """The property that makes the state/emission split correct, stated directly."""
    results = []
    for sample_every in (1, 3, 10, 1_000_000):
        harness = build(sample_every=sample_every)
        quoting(harness, "40")
        for size in ("35", "30", "22", "12"):
            quoting(harness, size)
        estimate = analyzed(harness).shadow.estimate(Outcome.UP)
        assert estimate is not None
        results.append(
            (sample_every, estimate.ahead, analyzed(harness).counters.actions.get("KEEP", 0))
        )

    aheads = {ahead for _, ahead, _ in results}
    keeps = {keep for _, _, keep in results}
    assert len(aheads) == 1, f"queue estimate varied with sampling rate: {results}"
    assert len(keeps) == 1, f"action counts varied with sampling rate: {results}"


def test_sampled_cycles_do_get_full_stage_timing() -> None:
    """The corollary: with sampling off, every cycle is timed end to end and analysed."""
    harness = build(sample_every=1)
    quoting(harness, "40")
    for size in ("39", "38"):
        quoting(harness, size)

    result = analyzed(harness)
    assert result.stages_captured == 3
    assert len(result.latency.receive_to_reconcile) == 3
    assert len(result.latency.queue_ahead) >= 3
    for observation in harness.buffer:
        assert observation[OBS_DECIDE_DONE_NS] != NOT_CAPTURED
        assert observation[OBS_PREPARE_DONE_NS] != NOT_CAPTURED
        assert observation[OBS_RECONCILE_DONE_NS] != NOT_CAPTURED


def test_an_unsampled_action_is_still_recorded_as_an_action() -> None:
    """A network action must never be invisible just because its trigger was not sampled."""
    harness = build(sample_every=1_000_000)
    quoting(harness, "40")  # PLACE
    quoting(harness, "40", price="0.61")  # REPLACE: closes the slot
    quoting(harness, "40", price="0.61")  # PLACE again

    result = analyzed(harness)
    assert result.counters.actions.get("PLACE", 0) == 2
    assert result.counters.actions.get("REPLACE", 0) == 1
    assert result.shadow.acquired == 2
    # Actions carry a timestamp; their earlier stages are explicitly absent, not invented.
    acting = [o for o in harness.buffer if o[OBS_RECONCILE_DONE_NS] != NOT_CAPTURED]
    assert len(acting) == 3
    for observation in acting:
        assert observation[OBS_DECIDE_DONE_NS] == NOT_CAPTURED
        assert observation[OBS_PREPARE_DONE_NS] == NOT_CAPTURED
    # And no latency sample is fabricated from the missing stages.
    assert len(result.latency.receive_to_reconcile) == 0
    assert result.stages_captured == 0


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
