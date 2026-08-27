"""L3 classification is exhaustive and independent of the latency sampler.

**SUPPORTING UNIT TEST ONLY.** These prove a counting rule. What the resulting distribution
*says* about quoting comes from real markets in the corpus and from nowhere else.
"""

from __future__ import annotations

from typing import Any

import pytest

from maker5m.bot.quality import QualityAggregate
from maker5m.telemetry import SamplingPolicy, TelemetryAnalyzer
from maker5m.telemetry.analyzer import ClassificationMode
from maker5m.telemetry.observation import NOT_CAPTURED, OBS_DECIDE_DONE_NS, OBS_SEQ
from tests.persistence.builders import observation


def stream(count: int, *, sample_every: int) -> list[tuple[Any, ...]]:
    """One deterministic observation stream, stamped as a given sampler would have stamped it.

    Whether a cycle carries stage timings is a *captured fact* — P8 established that the
    analyzer must read it rather than recompute it — so a different `sample_every` is expressed
    here as a different set of stamped cycles, exactly as the hot path would have produced.
    """
    built: list[tuple[Any, ...]] = []
    for index in range(count):
        captured = list(observation(index, ordinal=index))
        captured[OBS_SEQ] = index
        if index % sample_every != 0:
            captured[OBS_DECIDE_DONE_NS] = NOT_CAPTURED
        built.append(tuple(captured))
    return built


def classify(count: int, sample_every: int) -> tuple[dict[str, int], QualityAggregate, Any]:
    aggregate = QualityAggregate(t0_ns=1_787_733_400_000_000_000)
    analyzer = TelemetryAnalyzer(
        sampling=SamplingPolicy(sample_every=sample_every),
        classification_mode=ClassificationMode.EVERY_DECISION,
    )
    analyzer.on_quote = aggregate.observe
    analyzer.run(stream(count, sample_every=sample_every))
    return dict(analyzer.counters.quality), aggregate, analyzer


@pytest.mark.parametrize("sample_every", [1, 10, 100])
def test_classification_does_not_depend_on_the_latency_sampler(sample_every: int) -> None:
    """§6. The same stream, three samplers, one distribution.

    P13's first corpus classified only cycles that were sampled or acting, so its `AT_FRONT`
    fraction had a denominator of "every acting cycle plus one in ten of the rest" — and acting
    cycles are exactly the ones where an order was being placed or replaced, which is when the
    queue position is worst. The rate moved with `sample_every`, which a rate about a market
    must not do.
    """
    baseline, baseline_aggregate, _ = classify(200, 1)
    quality, aggregate, _ = classify(200, sample_every)

    assert quality == baseline
    assert aggregate.total == baseline_aggregate.total
    assert aggregate.by_reason == baseline_aggregate.by_reason
    assert aggregate.by_outcome == baseline_aggregate.by_outcome


@pytest.mark.parametrize("sample_every", [1, 10, 100])
def test_every_decision_gets_two_classifications(sample_every: int) -> None:
    """§5. The denominator is exactly two per decision observation: UP and DOWN."""
    _, aggregate, analyzer = classify(200, sample_every)
    assert analyzer.processed == 200
    assert aggregate.classified == 400
    assert sum(analyzer.counters.actions.values()) == 400


@pytest.mark.parametrize("sample_every", [1, 10, 100])
def test_latency_sampling_still_follows_the_sampler(sample_every: int) -> None:
    """§3. Classification changed; timing did not. Sampled cycles still carry the timings."""
    _, _, analyzer = classify(200, sample_every)
    expected = len(range(0, 200, sample_every))
    assert analyzer.stages_captured == expected


def test_the_accepted_p8_mode_is_unchanged() -> None:
    """P8's own behaviour is the default and still classifies only sampled or acting cycles."""
    aggregate = QualityAggregate(t0_ns=1_787_733_400_000_000_000)
    analyzer = TelemetryAnalyzer(sampling=SamplingPolicy(sample_every=10))
    analyzer.on_quote = aggregate.observe
    analyzer.run(stream(200, sample_every=10))

    assert analyzer.classification_mode is ClassificationMode.SAMPLED_OR_ACTING
    assert aggregate.classified < 400
    assert sum(analyzer.counters.actions.values()) == 400, "actions were always exhaustive"


def test_a_cycle_is_never_classified_twice() -> None:
    """§4. A sampled cycle takes the latency branch and is classified once, not once per branch."""
    _, aggregate, analyzer = classify(50, 1)
    assert analyzer.processed == 50
    assert aggregate.classified == 100
