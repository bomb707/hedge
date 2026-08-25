"""Emit every queue-measurement result for a fixed corpus, so two versions can be compared.

The telemetry offload moved queue estimation, classification, and counting off the trading path
and into a downstream analyzer. That is a performance change and must not be a semantic one, so
this dumps the complete measurement for a deterministic corpus in a form that can be diffed
against the same dump taken from the synchronous implementation it replaced.

Runs against either shape: the current harness exposes ``analyze()``, the previous one exposed
its counters directly.

Read-only. No venue, no credential, no order.
"""

import json
import sys

from tests.replay.corpus import SYNTHETIC_EVENTS, market

from maker5m.execution import Executor, RecordingTransport, VenueAdapter
from maker5m.feeds import BookTracker, IngressMerger, MarketDataPipeline
from maker5m.feeds.venue import VenueMarketRules
from maker5m.market import MarketState, reduce_event
from maker5m.numeric import parse_price, parse_share
from maker5m.strategy import BaseLot, StrategyEngine, default_config
from maker5m.telemetry import InstrumentedRun, SamplingPolicy, perf_now_ns

PASSES = 12
SAMPLE_EVERY = 10


def main() -> None:
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
    harness = InstrumentedRun(
        pipeline=pipeline,
        engine=engine,
        rules=VenueMarketRules(parse_price("0.01"), parse_share("5"), source="snapshot"),
        executor=Executor(adapter=VenueAdapter(RecordingTransport())),
        sampling=SamplingPolicy(SAMPLE_EVERY),
    )
    # A live run is HEALTHY once both tokens have a snapshot; without this every side reads
    # STALE and the classification comparison would be vacuous.
    pipeline.clob_health.mark_snapshot(definition.t0)
    pipeline.spot_health.mark_snapshot(definition.t0)
    initial = MarketState.initial(definition)

    for _ in range(PASSES):
        state = initial
        for event in SYNTHETIC_EVENTS:
            kind = type(event).__name__
            state = reduce_event(state, event)
            merger.state = state
            merger.advance_ordinal()
            if hasattr(merger, "stages_measured"):
                merger.stages_measured = harness.sampling.selects(merger.ordinal, kind)
            harness.observe(kind, perf_now_ns(), engine.decide(state))

    # Deliberately duck-typed: this same script is run against the previous, synchronous
    # implementation inside a git worktree, where the results live on the harness itself.
    analyze = getattr(harness, "analyze", None)
    source: object = harness if analyze is None else analyze()
    shadow = source.shadow  # type: ignore[attr-defined]
    counters = source.counters  # type: ignore[attr-defined]
    latency = source.latency  # type: ignore[attr-defined]

    report = {
        "corpus": {"events": len(SYNTHETIC_EVENTS), "passes": PASSES, "cycles": harness.cycles},
        "sample_every": SAMPLE_EVERY,
        "shadow": {
            "acquired": shadow.acquired,
            "kept": shadow.kept,
            "lost": shadow.lost,
            "loss_reasons": dict(sorted(shadow.loss_reasons.items())),
        },
        "counters": {
            "actions": dict(sorted(counters.actions.items())),
            "quality": dict(sorted(counters.quality.items())),
            "reasons": dict(sorted(counters.reasons.items())),
            "cycles_with_live_order": counters.cycles_with_live_order,
            "keeps_with_live_order": counters.keeps_with_live_order,
            "keep_ratio": counters.keep_ratio,
            "execution_queue_loss_actions": counters.execution_queue_loss_actions,
            "execution_queue_loss_reasons": dict(
                sorted(counters.execution_queue_loss_reasons.items())
            ),
        },
        "queue_ahead_sequence": list(latency.queue_ahead.samples),
    }
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    print()


if __name__ == "__main__":
    main()
