"""The telemetry offload must be a performance change, not a semantic one.

Queue estimation, classification, and counting moved off the trading path into a downstream
analyzer. The golden file beside this test was produced by running
``tools/queue_semantics_snapshot.py`` against the **previous, synchronous** implementation
(commit ``c5cec7f``, in a git worktree), and this asserts the offloaded pipeline reproduces it
exactly: slot counts, typed loss reasons, every action and quality counter, and the whole
queue-ahead sequence element by element.

If this fails, the refactor changed what P8 measures, and no performance number it produces is
comparable to the ones already recorded.
"""

from __future__ import annotations

import json
from pathlib import Path

from maker5m.execution import Executor, RecordingTransport, VenueAdapter
from maker5m.feeds import BookTracker, IngressMerger, MarketDataPipeline
from maker5m.feeds.venue import VenueMarketRules
from maker5m.market import MarketState, reduce_event
from maker5m.numeric import parse_price, parse_share
from maker5m.strategy import BaseLot, StrategyEngine, default_config
from maker5m.telemetry import InstrumentedRun, SamplingPolicy, TelemetryAnalyzer, perf_now_ns
from tests.replay.corpus import SYNTHETIC_EVENTS, market

GOLDEN = json.loads(
    (Path(__file__).parent / "golden" / "synchronous_queue_semantics.json").read_text(
        encoding="utf-8"
    )
)


def run_corpus(passes: int, sample_every: int) -> tuple[InstrumentedRun, TelemetryAnalyzer]:
    definition = market()
    engine = StrategyEngine(default_config(BaseLot.of(15)))
    merger = IngressMerger(
        engine=engine,
        state=MarketState.initial(definition),
        clock=lambda: definition.t0,
        market_id=definition.market_id,
    )
    pipeline = MarketDataPipeline(
        merger=merger,
        books=BookTracker(definition.up_token_id, definition.down_token_id),
    )
    pipeline.clob_health.mark_snapshot(definition.t0)
    pipeline.spot_health.mark_snapshot(definition.t0)
    harness = InstrumentedRun(
        pipeline=pipeline,
        engine=engine,
        rules=VenueMarketRules(parse_price("0.01"), parse_share("5"), source="snapshot"),
        executor=Executor(adapter=VenueAdapter(RecordingTransport())),
        sampling=SamplingPolicy(sample_every),
    )
    initial = MarketState.initial(definition)
    for _ in range(passes):
        state = initial
        for event in SYNTHETIC_EVENTS:
            kind = type(event).__name__
            state = reduce_event(state, event)
            merger.state = state
            merger.advance_ordinal()
            merger.stages_measured = harness.sampling.selects(merger.ordinal, kind)
            harness.observe(kind, perf_now_ns(), engine.decide(state))
    return harness, harness.analyze()


def test_the_golden_file_describes_a_non_trivial_run() -> None:
    """Guard against the fixture degenerating into a corpus that proves nothing."""
    assert GOLDEN["corpus"]["cycles"] >= 400
    assert GOLDEN["shadow"]["acquired"] > 0
    assert GOLDEN["shadow"]["lost"] > 0
    assert len(GOLDEN["shadow"]["loss_reasons"]) >= 3
    assert set(GOLDEN["counters"]["actions"]) >= {"PLACE", "KEEP", "REPLACE", "CANCEL", "BLOCKED"}
    assert len(GOLDEN["queue_ahead_sequence"]) >= 200


def test_shadow_slot_accounting_is_unchanged() -> None:
    _, result = run_corpus(GOLDEN["corpus"]["passes"], GOLDEN["sample_every"])
    assert result.shadow.acquired == GOLDEN["shadow"]["acquired"]
    assert result.shadow.kept == GOLDEN["shadow"]["kept"]
    assert result.shadow.lost == GOLDEN["shadow"]["lost"]
    assert dict(sorted(result.shadow.loss_reasons.items())) == GOLDEN["shadow"]["loss_reasons"]


def test_every_counter_is_unchanged() -> None:
    _, result = run_corpus(GOLDEN["corpus"]["passes"], GOLDEN["sample_every"])
    counters = result.counters
    expected = GOLDEN["counters"]
    assert dict(sorted(counters.actions.items())) == expected["actions"]
    assert dict(sorted(counters.quality.items())) == expected["quality"]
    assert dict(sorted(counters.reasons.items())) == expected["reasons"]
    assert counters.cycles_with_live_order == expected["cycles_with_live_order"]
    assert counters.keeps_with_live_order == expected["keeps_with_live_order"]
    assert counters.keep_ratio == expected["keep_ratio"]
    assert counters.execution_queue_loss_actions == expected["execution_queue_loss_actions"]
    assert (
        dict(sorted(counters.execution_queue_loss_reasons.items()))
        == expected["execution_queue_loss_reasons"]
    )


def test_the_queue_ahead_sequence_matches_element_by_element() -> None:
    """Not just the distribution - the exact ordered sequence of estimates."""
    _, result = run_corpus(GOLDEN["corpus"]["passes"], GOLDEN["sample_every"])
    assert result.latency.queue_ahead.samples == GOLDEN["queue_ahead_sequence"]


def test_the_offloaded_run_drops_nothing_and_sees_no_gap() -> None:
    harness, result = run_corpus(GOLDEN["corpus"]["passes"], GOLDEN["sample_every"])
    assert harness.buffer.dropped == 0
    assert result.gaps == 0
    assert result.lost_observations == 0
    assert result.processed == harness.buffer.accepted == harness.cycles


def test_analysis_is_repeatable_from_the_same_observations() -> None:
    """Analysis is a fold over an ordered stream, so it must be reproducible."""
    harness, first = run_corpus(4, 10)
    for _ in range(3):
        again = harness.analyze()
        assert again.shadow.summary() == first.shadow.summary()
        assert again.counters.summary() == first.counters.summary()
        assert again.latency.queue_ahead.samples == first.latency.queue_ahead.samples
