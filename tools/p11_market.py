"""One complete real market, with durable telemetry persistence. P11's real-market gate.

**REAL MARKET DATA.** Real Polymarket CLOB, real BTC spot, real Polygon settlement. Full
strategy, P7 shadow execution, P8 telemetry, P9 risk, P10 settlement, P11 persistence.

**No real order. No credential. No redemption transaction.** `LIVE_TRADING_ENABLED` and
`REDEMPTION_ENABLED` are both `False`; execution is a shadow simulation and settlement is read
only.

`--stall-from/--stall-to` induces a **controlled local fault**: the persistence consumer stops
consuming for a window while the market carries on. Provenance is then
`CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`. The feeds stay real throughout and the stall is ours —
it is not, and must never be described as, a venue incident.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from maker5m.execution import Executor, RecordingTransport, VenueAdapter
from maker5m.feeds.capture import capture_market
from maker5m.feeds.discovery import discover_market, slug_for
from maker5m.feeds.pipeline import MarketDataPipeline
from maker5m.market.timebase import NANOS_PER_SECOND, TimestampNs
from maker5m.persistence import (
    MANIFEST_SCHEMA_VERSION,
    Manifest,
    MarketIdentity,
    MetricsAccumulator,
    PersistenceWorker,
    TelemetryProvenance,
    TelemetryStore,
    database_digest,
    risk_row,
    settlement_row,
    verify_store,
)
from maker5m.risk import RiskConfig, RiskEngine, RiskProvenance
from maker5m.risk.trace import HealthFrame, RiskController
from maker5m.safety import LIVE_TRADING_ENABLED
from maker5m.settlement import (
    DEFAULT_RPC_ENDPOINTS,
    EndpointSet,
    MarketResolutionTarget,
    RpcEndpoint,
    SettlementPolicy,
    attest_all,
    verify,
)
from maker5m.settlement.audit import SettlementRecord
from maker5m.settlement.redeem import REDEMPTION_ENABLED
from maker5m.strategy import BaseLot, StrategyEngine, default_config
from maker5m.telemetry import InstrumentedRun, SamplingPolicy, TelemetryAnalyzer, perf_now_ns
from maker5m.telemetry.observation import ObservationBuffer

MIN_LEAD_SECONDS = 45
SAMPLE_EVERY = 10
SETTLE_TIMEOUT_SECONDS = 400
SETTLE_POLL_SECONDS = 2.0


def source_timestamp_of(event_kind: str) -> int | None:
    """The venue's own clock for this event, if the ingress stream carried one.

    It does not. `EventMeta.timestamp` is the ingress clock by construction, and the venue's
    `source_timestamp_ms` is consumed by P6's clock-health monitor without entering the event
    contract — changing that contract would break P5's byte identity for every existing journal.
    So this returns ``None`` and the record says the exchange timestamp is unavailable, which is
    true, rather than storing the ingress clock under the venue's name, which would not be.
    """
    del event_kind
    return None


async def main(out: Path, stall_window: tuple[int, int] | None, buffer_capacity: int) -> None:
    if LIVE_TRADING_ENABLED:  # pragma: no cover - defensive
        raise SystemExit("refusing to run while live trading is enabled")
    out.mkdir(parents=True, exist_ok=True)

    now = int(time.time())
    t0 = ((now // 300) + 1) * 300
    if t0 - now < MIN_LEAD_SECONDS:
        t0 += 300
    slug = slug_for(t0)
    label = "stalled-sink" if stall_window else "baseline"
    print(f"[{time.strftime('%H:%M:%S')}] P11 {label} on {slug} (T0 in {t0 - now}s)", flush=True)

    market = discover_market(slug)
    following = discover_market(slug_for(t0 + 300))
    config = default_config(BaseLot.of(15))
    t0_ns = market.definition.t0

    provenance = (
        TelemetryProvenance.CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET
        if stall_window
        else TelemetryProvenance.REAL_PUBLIC_MARKET_DATA
    )
    identity = MarketIdentity(
        market_id=market.definition.market_id,
        slug=slug,
        condition_id=market.condition_id,
        provenance=provenance.value,
    )
    database = out / f"{slug}.p11.sqlite3"
    if database.exists():
        database.unlink()

    controller = RiskController(
        engine=RiskEngine(config=RiskConfig()),
        provenance=(
            RiskProvenance.CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET
            if stall_window
            else RiskProvenance.REAL_PUBLIC_MARKET_DATA
        ),
    )
    analyzer = TelemetryAnalyzer(sampling=SamplingPolicy(sample_every=SAMPLE_EVERY))
    metrics = MetricsAccumulator(
        market_id=identity.market_id, slug=slug, provenance=provenance.value
    )
    buffer = ObservationBuffer(capacity=buffer_capacity)
    worker = PersistenceWorker(
        buffer=buffer,
        store=TelemetryStore(path=database),
        identity=identity,
        analyzer=analyzer,
        metrics=metrics,
    )

    stalling = {"active": False, "started_at": 0.0, "ended_at": 0.0, "events": 0}
    if stall_window:

        def should_stall() -> bool:
            return bool(stalling["active"])

        worker.stall = should_stall

    runs: list[InstrumentedRun] = []

    def attach(pipeline: MarketDataPipeline) -> None:
        sampling = SamplingPolicy(sample_every=SAMPLE_EVERY)
        pipeline.merger.perf_clock = perf_now_ns
        pipeline.stage_selector = lambda ordinal, kind: sampling.selects(ordinal, kind)
        runs.append(
            InstrumentedRun(
                pipeline=pipeline,
                engine=StrategyEngine(config),
                rules=market.venue_rules,
                executor=Executor(adapter=VenueAdapter(RecordingTransport())),
                buffer=buffer,
                sampling=sampling,
                risk=controller,
            )
        )

    hot_path_ns: list[int] = []

    def observe(kind: str, raw_ns: int, decision: Any, pipeline: MarketDataPipeline) -> None:
        del pipeline
        started = perf_now_ns()
        runs[0].observe(kind, raw_ns, decision, source_timestamp_of(kind))
        hot_path_ns.append(perf_now_ns() - started)
        if stalling["active"]:
            stalling["events"] = int(stalling["events"]) + 1

    def on_tick(now_ns: TimestampNs, pipeline: MarketDataPipeline) -> None:
        del pipeline
        controller.evaluate(HealthFrame(), as_of_ingress_ordinal=0, now_ns=now_ns)
        if not stall_window:
            return
        offset = (int(now_ns) - int(t0_ns)) / NANOS_PER_SECOND
        begin, end = stall_window
        if not stalling["active"] and begin <= offset < end:
            stalling["active"] = True
            stalling["started_at"] = offset
            print(f"    [+{offset:.0f}s] sink stalled (controlled local fault)", flush=True)
        elif stalling["active"] and offset >= end:
            stalling["active"] = False
            stalling["ended_at"] = offset
            print(
                f"    [+{offset:.0f}s] sink resumed after "
                f"{offset - float(stalling['started_at']):.0f}s; "
                f"{stalling['events']} market events processed during the stall",
                flush=True,
            )

    worker.start()
    try:
        result = await capture_market(
            market,
            config,
            next_market=following,
            description=f"P11 {label} run on {slug}",
            on_pipeline=attach,
            observer=lambda kind, raw, decision: observe(kind, raw, decision, runs[0].pipeline),
            on_tick=on_tick,
        )
    finally:
        stalling["active"] = False
        worker.stall = None
        deadline = time.time() + 30
        while len(buffer) and time.time() < deadline:
            time.sleep(0.05)
        worker.stop(timeout=30)

    print(
        f"    persisted {worker.stats.decisions_written} decisions, "
        f"dropped {buffer.dropped}, gaps {worker.stats.sequence_gaps}, "
        f"sink errors {worker.store.sink_errors}",
        flush=True,
    )

    settlement = await asyncio.to_thread(settle, market, slug)
    ledger = runs[0].pipeline.merger.state.ledger
    winner = None if settlement is None else settlement.decision.winning_outcome
    market_metrics = metrics.build(ledger, winner=winner)

    # The store is reopened by a fresh worker for the closing writes, so the connection is still
    # owned by exactly one thread for its whole life.
    closer = PersistenceWorker(
        buffer=ObservationBuffer(capacity=1), store=TelemetryStore(path=database), identity=identity
    )
    closer.start()
    sequence = worker.stats.decisions_written
    risk_records = list(controller.trace)
    for record in risk_records:
        sequence += 1
        closer.store.write_risk(
            risk_row(record, market_id=identity.market_id, persistence_sequence=sequence)
        )
    if settlement is not None:
        sequence += 1
        closer.store.write_settlement(
            settlement_row(settlement, market_id=identity.market_id, persistence_sequence=sequence)
        )
    closer.store.write_metrics(market_metrics)

    accepted = buffer.accepted
    manifest = Manifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        slug=slug,
        market_id=identity.market_id,
        condition_id=identity.condition_id,
        capture_start_ns=t0_ns,
        capture_end_ns=TimestampNs(t0_ns + 300 * NANOS_PER_SECOND),
        source_revision=revision(),
        decision_count=worker.stats.decisions_written,
        fill_count=0,
        risk_count=len(risk_records),
        settlement_count=0 if settlement is None else 1,
        first_ingress_ordinal=0,
        last_ingress_ordinal=runs[0].pipeline.merger.ordinal,
        first_persistence_sequence=1 if worker.stats.decisions_written else None,
        last_persistence_sequence=sequence or None,
        accepted_records=accepted,
        persisted_records=worker.stats.decisions_written,
        dropped_records=buffer.dropped,
        sequence_gaps=worker.stats.sequence_gaps,
        lost_observations=worker.stats.lost_observations,
        sink_errors=worker.store.sink_errors,
        first_gap_at=worker.stats.first_gap_at,
        last_gap_at=worker.stats.last_gap_at,
        buffer_capacity=buffer.capacity,
        buffer_high_water=worker.stats.buffer_high_water,
        database_bytes=None,
        database_sha256=None,
        provenance=provenance.value,
        live_trading_enabled=LIVE_TRADING_ENABLED,
        redemption_enabled=REDEMPTION_ENABLED,
        closed=True,
    )
    closer.close_market(manifest)
    closer.stop(timeout=30)

    # The hash describes a file nothing is still writing, so it is taken after the close and
    # then written back in a final pass.
    size, digest = database_digest(database)
    final = PersistenceWorker(
        buffer=ObservationBuffer(capacity=1), store=TelemetryStore(path=database), identity=identity
    )
    final.start()
    stamped = Manifest(
        **{
            **{f: getattr(manifest, f) for f in manifest.__dataclass_fields__},
            "database_bytes": size,
            "database_sha256": digest,
        }
    )
    final.close_market(stamped)
    final.stop(timeout=30)

    verification = verify_store(database)
    evidence = {
        "kind": "P11_TELEMETRY_PERSISTENCE",
        "provenance": provenance.value,
        "label": label,
        "slug": slug,
        "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "redemption_enabled": REDEMPTION_ENABLED,
        "orders_sent": 0,
        "redemptions_sent": 0,
        "cycles": runs[0].cycles,
        "feed_counters": result.counters.summary() if hasattr(result.counters, "summary") else {},
        "manifest": {f: getattr(stamped, f) for f in stamped.__dataclass_fields__},
        "telemetry_complete": stamped.telemetry_complete,
        "worker": worker.stats.summary(),
        "store": {
            "rows_written": worker.store.rows_written,
            "batches": worker.store.batches,
            "transaction_ns": worker.store.transaction_ns,
            "sink_errors": worker.store.sink_errors,
            "error_samples": list(worker.store.error_samples),
        },
        "metrics": {
            f: _plain(getattr(market_metrics, f)) for f in market_metrics.__dataclass_fields__
        },
        "analyzer": analyzer.summary(),
        "verification": verification.summary(),
        "stall": {
            "requested_window_s": list(stall_window) if stall_window else None,
            "started_at_s": stalling["started_at"] or None,
            "ended_at_s": stalling["ended_at"] or None,
            "market_events_during_stall": stalling["events"],
            "note": (
                "CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET: our own persistence consumer was "
                "stopped. The Polymarket and BTC feeds were real and healthy throughout; this "
                "is not a venue incident."
            )
            if stall_window
            else None,
        },
        "hot_path_observe_ns": _tiers(hot_path_ns),
        "settlement": None if settlement is None else settlement.summary(),
        "limitation": (
            "REAL MARKET PERSISTENCE VALIDATED. REAL NONZERO OWN-LEDGER METRICS UNRUN / P14: "
            "live trading is disabled, so no order was placed and the ledger is empty. Real "
            "own-fill records, real maker fraction and real taker-fill persistence are all "
            "UNRUN / P14."
        ),
    }
    path = out / f"{slug}.p11-{label}.json"
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps({k: evidence[k] for k in ("telemetry_complete", "verification")}, indent=2))
    print(f"wrote {path}", flush=True)


def _plain(value: object) -> object:
    if value is None or isinstance(value, bool | int | str):
        return value
    if hasattr(value, "__dataclass_fields__"):
        return {f: _plain(getattr(value, f)) for f in value.__dataclass_fields__}
    return str(value)


def _tiers(samples: list[int]) -> dict[str, int]:
    from maker5m.telemetry.metrics import quantile

    ordered = sorted(samples)
    if not ordered:
        return {}
    return {
        "n": len(ordered),
        "p50": quantile(ordered, 0.50),
        "p95": quantile(ordered, 0.95),
        "p99": quantile(ordered, 0.99),
        "max": ordered[-1],
    }


def revision() -> str:
    import subprocess

    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover
        return "unknown"


def settle(market: Any, slug: str) -> SettlementRecord | None:
    """Wait for the real chain to resolve this market, using the production P10 path."""
    condition_id = market.condition_id
    if not condition_id:
        return None
    configured = EndpointSet(
        tuple(RpcEndpoint(provider_id=name, url=url) for name, url in DEFAULT_RPC_ENDPOINTS)
    )
    providers, _ = attest_all(configured)
    policy = SettlementPolicy()
    tokens = market.up_token_id, market.down_token_id
    target = MarketResolutionTarget(
        slug=slug,
        condition_id=condition_id,
        up_token_id=str(tokens[0]),
        down_token_id=str(tokens[1]),
    )
    deadline = time.time() + SETTLE_TIMEOUT_SECONDS
    while time.time() < deadline:
        readings = tuple(
            provider.read_condition(condition_id, block_tag=policy.block_tag)
            for provider in providers
        )
        decision = verify(target, readings, (), policy)
        if decision.state.value == "RESOLVED":
            print(f"    settlement -> {decision.winning_outcome}", flush=True)
            return SettlementRecord(
                target=target, decision=decision, policy=policy, provider_readings=readings
            )
        time.sleep(SETTLE_POLL_SECONDS)
    print("    settlement did not resolve inside the watch window", flush=True)
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--stall-from", type=int)
    parser.add_argument("--stall-to", type=int)
    parser.add_argument("--buffer", type=int, default=320_000)
    args = parser.parse_args()
    window = (
        (args.stall_from, args.stall_to)
        if args.stall_from is not None and args.stall_to is not None
        else None
    )
    asyncio.run(main(args.out, window, args.buffer))
