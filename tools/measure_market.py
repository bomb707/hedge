"""Produce the P8 measurement evidence: one real market, instrumented, no orders.

Not part of the ``maker5m`` package: a one-shot operator script that runs the P6 feeds, the P4
decision engine, P7 **shadow** reconciliation, and P8 instrumentation over a full 5-minute
market.

Strictly read-only. ``LIVE_TRADING_ENABLED`` is ``False``, no credential is used, no
authenticated socket is opened, and nothing is ever dispatched — the reconciler's plan is
computed and recorded, never sent. Real venue order RTT is therefore **unmeasured** and stays
that way until P14.

Reproduce with::

    .venv/bin/python tools/measure_market.py <output-directory>

It waits for the next suitable ``T0``, so a run takes roughly nine minutes.
"""

import asyncio
import json
import sys
import time
from pathlib import Path

from maker5m.execution import Executor, RecordingTransport, VenueAdapter
from maker5m.feeds.capture import capture_market
from maker5m.feeds.discovery import discover_market, slug_for
from maker5m.feeds.pipeline import MarketDataPipeline
from maker5m.market.timebase import TimestampNs
from maker5m.safety import LIVE_TRADING_ENABLED
from maker5m.strategy import BaseLot, default_config
from maker5m.strategy.decision import DecisionResult
from maker5m.strategy.engine import StrategyEngine
from maker5m.telemetry import InstrumentedRun, SamplingPolicy, perf_now_ns

MIN_LEAD_SECONDS = 45

SAMPLE_EVERY = 10
"""Deterministic trace sampling for high-frequency book events (OPERATIONAL).

The first P8 run at ``sample_every = 1`` overflowed the 65,536-slot buffer and dropped 84,978
traces - correct drop-oldest behaviour, but it meant the retained traces were only the tail of
the market. Sampling one in ten book updates keeps a representative spread across the whole
window. Fills, order updates, phase boundaries, and anything that issued a request are always
traced regardless, and every aggregate metric is still computed on **every** cycle - only the
per-event trace records are thinned.
"""


async def main(out: Path) -> None:
    if LIVE_TRADING_ENABLED:  # pragma: no cover - defensive
        raise SystemExit("refusing to run a measurement while live trading is enabled")
    out.mkdir(parents=True, exist_ok=True)

    now = int(time.time())
    t0 = ((now // 300) + 1) * 300
    if t0 - now < MIN_LEAD_SECONDS:
        t0 += 300
    slug = slug_for(t0)
    print(f"[{time.strftime('%H:%M:%S')}] measuring {slug} (T0 in {t0 - now}s)", flush=True)

    market = discover_market(slug)
    following = discover_market(slug_for(t0 + 300))
    prearm_ready = TimestampNs(time.time_ns())
    config = default_config(BaseLot.of(15))

    runs: list[InstrumentedRun] = []

    def attach(pipeline: MarketDataPipeline) -> None:
        # Opt into merger-level timing so decide() itself is measured, not just receive->decide.
        pipeline.merger.perf_clock = perf_now_ns
        runs.append(
            InstrumentedRun(
                pipeline=pipeline,
                engine=StrategyEngine(config),
                rules=market.venue_rules,
                executor=Executor(adapter=VenueAdapter(RecordingTransport())),
                sampling=SamplingPolicy(sample_every=SAMPLE_EVERY),
            )
        )

    def observe(kind: str, raw_ns: int, decision: DecisionResult) -> None:
        runs[0].observe(kind, raw_ns, decision)

    result = await capture_market(
        market,
        config,
        next_market=following,
        prearm_ready_ns=prearm_ready,
        description=f"P8 instrumented shadow measurement of {slug}",
        on_pipeline=attach,
        observer=observe,
    )

    run = runs[0]
    manifest: dict[str, object] = {
        "phase": "P8",
        "kind": "INSTRUMENTED_SHADOW_MEASUREMENT",
        "note": (
            "Shadow execution against real market data. No order was sent and no credential "
            "was used. Queue figures are SHADOW_ESTIMATE, not venue queue positions."
        ),
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "orders_sent": 0,
        "slug": slug,
        "market_id": market.definition.market_id,
        "t0_ns": market.definition.t0,
        "run_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "journal_steps": result.journal.step_count,
        "feed_counters": result.counters.summary(),
        "measurement": run.summary(),
    }
    (out / f"{slug}.p8-measurement.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(json.dumps(manifest, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
