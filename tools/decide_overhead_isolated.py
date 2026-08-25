"""Process-isolated decide overhead. The authoritative measurement.

``observe()`` runs *after* ``decide()``, so instrumentation cannot slow a decision that has
already finished. Yet the same-process benchmark reported decide p50 +7.7% before the telemetry
offload, and a smaller figure after. Both numbers are contaminated: allocator state, GC
scheduling, and cache residency from measured cycles carry into the *next* cycle's decision, and
no amount of interleaving inside one interpreter removes that.

So each configuration gets a fresh interpreter that does nothing else. Parent alternates the
launch order across pairs; every pair is reported, not just the aggregate.

Read-only. No venue, no credential, no order.
"""

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

PAIRS = 12
PASSES = 60
WARMUP_PASSES = 3
SAMPLE_EVERY = 10


def child_run(enabled: bool) -> dict[str, object]:
    """One configuration, alone in this interpreter. Imports happen here, not in the parent."""
    from tests.replay.corpus import SYNTHETIC_EVENTS, market

    from maker5m.execution import Executor, RecordingTransport, VenueAdapter
    from maker5m.feeds import BookTracker, IngressMerger, MarketDataPipeline
    from maker5m.feeds.venue import VenueMarketRules
    from maker5m.market import MarketState, reduce_event
    from maker5m.numeric import parse_price, parse_share
    from maker5m.strategy import BaseLot, StrategyEngine, default_config
    from maker5m.telemetry import InstrumentedRun, SamplingPolicy, perf_now_ns
    from maker5m.telemetry.metrics import quantile

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
        rules=VenueMarketRules(parse_price("0.01"), parse_share("5"), source="bench"),
        executor=Executor(adapter=VenueAdapter(RecordingTransport())),
        sampling=SamplingPolicy(SAMPLE_EVERY),
        enabled=enabled,
    )
    initial = MarketState.initial(definition)

    def sweep(collect: bool) -> list[int]:
        samples: list[int] = []
        state = initial
        for event in SYNTHETIC_EVENTS:
            kind = type(event).__name__
            start = perf_now_ns()
            state = reduce_event(state, event)
            merger.state = state
            merger.advance_ordinal()
            merger.stages_measured = enabled and harness.sampling.selects(merger.ordinal, kind)
            decision = engine.decide(state)
            decided = perf_now_ns()
            harness.observe(kind, start, decision)
            if collect:
                samples.append(decided - start)
        return samples

    # Both configurations are warmed identically before anything is recorded.
    for _ in range(WARMUP_PASSES):
        sweep(collect=False)

    decide_ns: list[int] = []
    for _ in range(PASSES):
        decide_ns += sweep(collect=True)

    ordered = sorted(decide_ns)
    return {
        "enabled": enabled,
        "count": len(ordered),
        "p50": quantile(ordered, 0.50),
        "p90": quantile(ordered, 0.90),
        "p95": quantile(ordered, 0.95),
        "p99": quantile(ordered, 0.99),
        "mean": int(statistics.fmean(ordered)),
    }


def spawn(enabled: bool) -> dict[str, object]:
    """A fresh interpreter per configuration. Same executable, same environment, same corpus."""
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--child", "on" if enabled else "off"],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    parsed: dict[str, object] = json.loads(result.stdout)
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", choices=("off", "on"))
    args = parser.parse_args()

    if args.child is not None:
        json.dump(child_run(args.child == "on"), sys.stdout)
        return

    pairs: list[dict[str, object]] = []
    off_p50: list[int] = []
    on_p50: list[int] = []
    for index in range(PAIRS):
        # Alternate which configuration starts, so a machine that warms or throttles across
        # the pair cannot systematically favour one side.
        if index % 2 == 0:
            off = spawn(enabled=False)
            on = spawn(enabled=True)
            order = "off_first"
        else:
            on = spawn(enabled=True)
            off = spawn(enabled=False)
            order = "on_first"
        off_value, on_value = off["p50"], on["p50"]
        assert isinstance(off_value, int) and isinstance(on_value, int)
        off_p50.append(off_value)
        on_p50.append(on_value)
        pairs.append(
            {
                "pair": index,
                "order": order,
                "off": off,
                "on": on,
                "delta_p50_ns": on_value - off_value,
                "delta_p50_percent": round((on_value - off_value) / off_value * 100, 2),
            }
        )

    off_median = int(statistics.median(off_p50))
    on_median = int(statistics.median(on_p50))
    absolute = on_median - off_median
    report = {
        "method": {
            "process_isolated": True,
            "pairs": PAIRS,
            "passes_per_process": PASSES,
            "warmup_passes_per_process": WARMUP_PASSES,
            "sample_every": SAMPLE_EVERY,
            "alternating_launch_order": True,
            "python": sys.version.split()[0],
        },
        "pairs": pairs,
        "aggregate": {
            "off_p50_median_ns": off_median,
            "on_p50_median_ns": on_median,
            "delta_ns": absolute,
            "delta_percent": round(absolute / off_median * 100, 2),
            "median_of_pair_deltas_ns": int(
                statistics.median(on - off for off, on in zip(off_p50, on_p50, strict=True))
            ),
            "median_of_pair_delta_percents": round(
                statistics.median(
                    (on - off) / off * 100 for off, on in zip(off_p50, on_p50, strict=True)
                ),
                2,
            ),
            "off_p50_by_pair": off_p50,
            "on_p50_by_pair": on_p50,
        },
    }
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    print()


if __name__ == "__main__":
    main()
