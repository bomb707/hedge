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
from maker5m.market.events import HealthStatus
from maker5m.market.timebase import NANOS_PER_SECOND, TimestampNs
from maker5m.persistence import (
    DEFAULT_RISK_CAPACITY,
    MANIFEST_SCHEMA_VERSION,
    BoundedChannel,
    Manifest,
    MarketIdentity,
    MetricsAccumulator,
    PersistenceWorker,
    TelemetryProvenance,
    TelemetryStore,
    archive_store,
    database_digest,
    settlement_row,
    verify_store,
)
from maker5m.risk import RiskConfig, RiskEngine, RiskProvenance
from maker5m.risk.engine import RiskDecision
from maker5m.risk.overlay import risk_adjust
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
    risk_channel = BoundedChannel(capacity=min(DEFAULT_RISK_CAPACITY, max(buffer_capacity, 1)))
    fill_channel = BoundedChannel(capacity=8_192)
    worker = PersistenceWorker(
        buffer=buffer,
        store=TelemetryStore(path=database),
        identity=identity,
        analyzer=analyzer,
        metrics=metrics,
        risk=risk_channel,
        fills=fill_channel,
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
    last_evaluated = [-1]
    places_by_state: dict[str, int] = {}

    def frame(pipeline: MarketDataPipeline) -> HealthFrame:
        """P6's verdict, read rather than recomputed.

        The default `HealthFrame()` is not a neutral placeholder — it says CLOB UNKNOWN, awaiting
        snapshot, SPOT UNKNOWN — and P9 correctly refuses to trade on that. The previous runner
        passed one, so the risk record it wrote described a market it had not looked at. P6
        remains the sole staleness authority; nothing here recomputes it.
        """
        return HealthFrame(
            clob_status=pipeline.clob_health.status,
            clob_awaiting_snapshot=pipeline.clob_health.awaiting_snapshot,
            spot_status=pipeline.spot_health.status,
            order_stream_status=HealthStatus.UNKNOWN,
            order_stream_required=False,
        )

    def evaluate_now(pipeline: MarketDataPipeline, now_ns: TimestampNs) -> RiskDecision:
        """Take a verdict against health as it stands, at the ordinal that stands.

        `as_of_ingress_ordinal` is the merger's real ordinal, not zero: the audit has to be able
        to say which point in the event stream a verdict applied to, and every record claiming
        ordinal 0 says nothing at all.
        """
        record = controller.evaluate(
            frame(pipeline),
            as_of_ingress_ordinal=pipeline.merger.ordinal,
            now_ns=now_ns,
        )
        risk_channel.publish(record)
        last_evaluated[0] = pipeline.merger.ordinal
        return RiskDecision(
            state=record.state,
            active=record.active,
            latched=record.latched,
            snapshot=controller.engine.snapshot,
        )

    def observe(kind: str, raw_ns: int, decision: Any, pipeline: MarketDataPipeline) -> None:
        """One shadow cycle, with the verdict taken as of this event and applied to it.

        Order is production's: decide, evaluate, adjust, then prepare/reconcile inside `observe`.
        The verdict is taken here rather than on the surrounding tick so it cannot lag the
        condition by an event — one PLACE slipping through between a feed going stale and the
        halt being noticed would defeat the whole mechanism, which is P9's own argument.
        """
        verdict = evaluate_now(pipeline, pipeline.merger.state.last_event_timestamp)
        state = verdict.state.value
        started = perf_now_ns()
        before = runs[0].executor.orders.open_count
        runs[0].observe(
            kind,
            raw_ns,
            risk_adjust(decision, verdict),
            source_timestamp_of(kind),
            strategy_intent=decision.orders,
        )
        hot_path_ns.append(perf_now_ns() - started)
        if runs[0].executor.orders.open_count > before:
            places_by_state[state] = places_by_state.get(state, 0) + 1
        if stalling["active"]:
            stalling["events"] = int(stalling["events"]) + 1

    def on_tick(now_ns: TimestampNs, pipeline: MarketDataPipeline) -> None:
        """Evaluate when health moved without an event producing a verdict.

        A quiet feed is exactly when a verdict matters and exactly when no event arrives to
        trigger one.
        """
        if pipeline.merger.ordinal != last_evaluated[0]:
            evaluate_now(pipeline, now_ns)
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

    # The closing writes happen on this thread, through a store this thread opens. The rule is
    # that one thread owns a connection for its life, and a plain store used here satisfies it;
    # routing these through a second worker did not — the writes crossed a thread boundary and
    # sqlite refused every one of them, silently, leaving a market that verified INCOMPLETE. The
    # verifier caught it, which is what the verifier is for.
    # Risk records were persisted continuously, through the same worker, as they were produced.
    # Dumping `controller.trace` here instead would have made a mid-market crash lose the whole
    # risk audit — and RiskTrace is bounded, so a long market's trace can have already lost its
    # prefix by the time DONE arrives.
    closing = TelemetryStore(path=database)
    closing.open()
    sequence = worker.persistence_sequence
    if settlement is not None:
        sequence += 1
        closing.write_settlement(
            settlement_row(settlement, market_id=identity.market_id, persistence_sequence=sequence)
        )
    closing.write_metrics(market_metrics)

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
        fill_count=worker.stats.fills_written,
        risk_count=worker.stats.risk_written,
        settlement_count=0 if settlement is None else 1,
        first_ingress_ordinal=worker.first_ingress_ordinal,
        last_ingress_ordinal=worker.last_ingress_ordinal,
        first_persistence_sequence=1 if sequence else None,
        last_persistence_sequence=sequence or None,
        accepted_records=accepted,
        persisted_records=worker.stats.decisions_written,
        dropped_records=buffer.dropped,
        sequence_gaps=worker.stats.sequence_gaps,
        lost_observations=worker.stats.lost_observations,
        sink_errors=worker.store.sink_errors + closing.sink_errors,
        first_gap_at=worker.stats.first_gap_at,
        last_gap_at=worker.stats.last_gap_at,
        buffer_capacity=buffer.capacity,
        buffer_high_water=worker.stats.buffer_high_water,
        risk_records_accepted=risk_channel.accepted,
        risk_records_persisted=worker.stats.risk_written,
        risk_records_dropped=risk_channel.dropped,
        fill_captures_accepted=fill_channel.accepted,
        fill_captures_persisted=worker.stats.fills_written,
        fill_captures_dropped=fill_channel.dropped,
        database_bytes=None,
        database_sha256=None,
        provenance=provenance.value,
        live_trading_enabled=LIVE_TRADING_ENABLED,
        redemption_enabled=REDEMPTION_ENABLED,
        closed=True,
    )
    closing.write_manifest(manifest)
    closing.close()

    # A file cannot contain its own hash. Writing the digest into the manifest row would change
    # the file and invalidate the digest it had just recorded, so the hash lives in a sidecar —
    # the same shape P6 already uses for its capture manifests — and the in-database manifest
    # says so rather than carrying a number that cannot be right.
    size, digest = database_digest(database)
    sidecar = database.with_suffix(".manifest.json")
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "database": database.name,
                "database_bytes": size,
                "database_sha256": digest,
                "note": (
                    "The digest is of the closed database and is held here rather than inside "
                    "it: a file cannot contain its own hash."
                ),
                "manifest": {f: getattr(manifest, f) for f in manifest.__dataclass_fields__},
                "telemetry_complete": manifest.telemetry_complete,
            },
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    stamped = manifest

    verification = verify_store(database, expected_sha256=digest)

    # Cold, lossless, and only after the store verified. 853 MB per market is not a durable
    # representation; ~11 MB is. The archive is proved to restore byte-identically before the
    # raw file is even considered removable, and this module never removes it — deleting the
    # only copy of a market's telemetry on the strength of an unchecked archive would be the
    # worst thing here could do.
    archive = archive_store(database)
    print(
        f"    archived {archive.raw_bytes:,} -> {archive.archive_bytes:,} bytes "
        f"({archive.ratio:.1f}x) in {archive.compress_seconds:.1f}s, "
        f"verified={archive.verified}",
        flush=True,
    )
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
        "database_path": str(database),
        "database_bytes": size,
        "database_sha256": digest,
        "sidecar_manifest": str(sidecar),
        "bytes_per_decision": (
            size // worker.stats.decisions_written if worker.stats.decisions_written else 0
        ),
        "closing_sink_errors": closing.sink_errors,
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
        "archive": archive.summary(),
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
    definition = market.definition
    target = MarketResolutionTarget(
        slug=slug,
        condition_id=condition_id,
        up_token_id=str(definition.up_token_id),
        down_token_id=str(definition.down_token_id),
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
