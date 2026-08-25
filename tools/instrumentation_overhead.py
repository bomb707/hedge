"""Measure what instrumentation costs, on the same deterministic event stream.

The DEVELOPMENT_PLAN requires *evidence* that instrumentation does not measurably slow
``decide()``. That claim cannot be made from reading the source, so this runs the identical
stream twice — instrumentation off, then on — and reports both raw distributions plus the
delta.

Deterministic input, so the two runs differ only in whether measurement is taken.
"""

import json
import statistics
import sys

from tests.replay.corpus import SYNTHETIC_EVENTS, market

from maker5m.execution import Executor, RecordingTransport, VenueAdapter
from maker5m.feeds import BookTracker, IngressMerger, MarketDataPipeline
from maker5m.feeds.venue import VenueMarketRules
from maker5m.market import MarketState, reduce_event
from maker5m.numeric import parse_price, parse_share
from maker5m.strategy import BaseLot, StrategyEngine, default_config
from maker5m.telemetry import InstrumentedRun, perf_now_ns
from maker5m.telemetry.metrics import quantile

Stats = dict[str, int]
Deltas = dict[str, dict[str, float | None]]

REPEATS = 40
"""The corpus is 39 events; repeating it gives ~1,500 cycles per configuration."""


def run(enabled: bool) -> tuple[list[int], list[int]]:
    """Return (decide_ns samples, full internal cycle_ns samples)."""
    definition = market()
    config = default_config(BaseLot.of(15))
    engine = StrategyEngine(config)
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
        rules=VenueMarketRules(parse_price("0.01"), parse_share("5"), source="bench"),
        executor=Executor(adapter=VenueAdapter(RecordingTransport())),
        enabled=enabled,
    )

    decide_ns: list[int] = []
    cycle_ns: list[int] = []
    for _ in range(REPEATS):
        state = MarketState.initial(definition)
        for event in SYNTHETIC_EVENTS:
            start = perf_now_ns()
            state = reduce_event(state, event)
            merger.state = state
            decision = engine.decide(state)
            decided = perf_now_ns()
            harness.observe(type(event).__name__, start, decision)
            done = perf_now_ns()
            decide_ns.append(decided - start)
            cycle_ns.append(done - start)
    return decide_ns, cycle_ns


def summarize(samples: list[int]) -> dict[str, int]:
    ordered = sorted(samples)
    return {
        "count": len(ordered),
        "p50": quantile(ordered, 0.50),
        "p90": quantile(ordered, 0.90),
        "p95": quantile(ordered, 0.95),
        "p99": quantile(ordered, 0.99),
        "max": ordered[-1],
        "mean": int(statistics.fmean(ordered)),
    }


def main() -> None:
    off_decide, off_cycle = run(enabled=False)
    on_decide, on_cycle = run(enabled=True)

    measured = {
        "decide_ns": (summarize(off_decide), summarize(on_decide)),
        "cycle_ns": (summarize(off_cycle), summarize(on_cycle)),
    }
    sections: dict[str, dict[str, Stats | Deltas]] = {}
    for key, (off, on) in measured.items():
        deltas: Deltas = {
            stat: {
                "absolute_ns": on[stat] - off[stat],
                "percent": (
                    None if off[stat] == 0 else round((on[stat] - off[stat]) / off[stat] * 100, 1)
                ),
            }
            for stat in ("p50", "p90", "p95", "p99", "max")
        }
        sections[key] = {"off": off, "on": on, "delta": deltas}

    report: dict[str, object] = {
        "repeats": REPEATS,
        "events_per_repeat": len(SYNTHETIC_EVENTS),
        **sections,
    }
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    print()


if __name__ == "__main__":
    main()
