"""One complete real market, with the operator UI attached. P12's real-market gate.

Everything P11 ran, plus a snapshot published for the dashboard and an inbox polled for operator
commands. The UI is a **separate process** and this runner never learns whether it is alive: it
writes a file, it lists a directory, and it does neither of those on the trading path.

The acceptance test the whole design exists for is someone killing that process abruptly while
the market runs. Nothing here has to survive it, because nothing here is connected to it.

Original P11 docstring follows.

---

One complete real market, with durable telemetry persistence. P11's real-market gate.

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
    ArchiveIdentity,
    ArchiveVerificationError,
    BoundedChannel,
    Manifest,
    MarketIdentity,
    MetricsAccumulator,
    PersistenceWorker,
    TelemetryProvenance,
    TelemetryStore,
    archive_store,
    database_digest,
    open_verified_archive,
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
from maker5m.ui import (
    CommandBridge,
    CommandInbox,
    ControlIngress,
    HotCommandChannel,
    SnapshotChannel,
    SnapshotPublisher,
    drain_operator_commands,
)

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


async def main(
    out: Path,
    stall_window: tuple[int, int] | None,
    buffer_capacity: int,
    ui_dir: Path,
    bridge_stall: tuple[int, int] | None = None,
) -> None:
    if LIVE_TRADING_ENABLED:  # pragma: no cover - defensive
        raise SystemExit("refusing to run while live trading is enabled")
    out.mkdir(parents=True, exist_ok=True)
    ui_dir.mkdir(parents=True, exist_ok=True)
    (ui_dir / "inbox").mkdir(parents=True, exist_ok=True)

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
    audit_channel = BoundedChannel(capacity=256)
    # Plane-3 evidence from the hot side. The ingress owner appends immutable facts here and
    # never formats, writes or flushes anything; the run log is rendered later, off the loop.
    control_events = BoundedChannel(capacity=512)
    fill_channel = BoundedChannel(capacity=8_192)
    worker = PersistenceWorker(
        buffer=buffer,
        store=TelemetryStore(path=database),
        identity=identity,
        analyzer=analyzer,
        metrics=metrics,
        risk=risk_channel,
        fills=fill_channel,
        control_audit=audit_channel,
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

    # Plane 3 owns every filesystem call in the UI path. The bridge lists, reads, decodes and
    # unlinks command files on its own thread and writes the snapshot; the ingress owner does a
    # `popleft` on a deque and nothing else. "Does not wait for the UI process" was never the
    # requirement — a `listdir` can stall on the filesystem with no UI involved at all.
    snapshot_channel = SnapshotChannel(ui_dir / "snapshot.json")
    inbox = CommandInbox(ui_dir / "inbox")
    hot_commands = HotCommandChannel()
    bridge_stalling = {"active": False}
    bridge = CommandBridge(
        inbox=inbox,
        channel=hot_commands,
        snapshot=snapshot_channel,
        stall=(lambda: bool(bridge_stalling["active"])) if bridge_stall else None,
    )
    publisher = SnapshotPublisher(identity=identity, config=config, bridge=bridge, t0_ns=t0_ns)
    control_ingress = ControlIngress(
        controller=controller,
        publish=risk_channel.publish,
        audit=lambda command, outcome: audit_channel.publish((command, outcome)),
    )

    def on_persisted(record: Any, captured: Any) -> None:
        """Plane 3, on the worker thread. Reads nothing another thread can change.

        The decision and its latency both come out of the same immutable observation, so the
        figures belong to that cycle by construction; the verdict is joined by sequence from
        risk records this worker persisted. Nothing here reaches into the controller, the
        merger or the pipeline.
        """
        publisher.observe_decision(record, captured)
        publisher.deliver(
            "counters",
            {
                "decisions": worker.stats.decisions_written,
                "risk": worker.stats.risk_written,
                "dropped": buffer.dropped,
                "sink_errors": worker.store.sink_errors,
            },
        )
        publisher.deliver(
            "audit_counts",
            {
                "accepted": audit_channel.accepted,
                "persisted": worker.stats.control_records_written,
                "dropped": audit_channel.dropped,
            },
        )
        publisher.maybe_publish(time.time())

    def on_control_persisted(row: Any) -> None:
        """Command history is what durably exists, not what the ingress thread remembered."""
        publisher.deliver(
            "control_persisted",
            {
                "command_id": row.command_id,
                "kind": row.kind,
                "accepted": row.accepted,
                "ingress_ordinal": row.ingress_ordinal,
                "risk_sequence": row.risk_sequence,
                "risk_state": row.risk_state,
                "allows_place": row.allows_place,
                "detail": row.detail,
            },
        )

    worker.on_decision_record = on_persisted
    worker.on_risk_record = publisher.observe_risk
    worker.on_control_record = on_control_persisted

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

        # The whole of the hot side's UI work: a bounded pop from an in-memory deque, the
        # risk-signal application, and an append to a bounded channel. No syscall, no
        # serialization, no lock, no print. Stdout is I/O too: a write to a pipe nobody drains
        # blocks the ingress owner as thoroughly as a stalled stat.
        drain_operator_commands(
            hot_commands,
            control_ingress,
            ingress_ordinal=pipeline.merger.ordinal,
            now_ns=pipeline.merger.state.last_event_timestamp,
            report=control_events.publish,
        )

        offset = (int(now_ns) - int(t0_ns)) / NANOS_PER_SECOND
        if bridge_stall:
            begin, end = bridge_stall
            active = begin <= offset < end
            if active != bridge_stalling["active"]:
                bridge_stalling["active"] = active
                control_events.publish(("bridge", (round(offset, 1), active)))
        if not stall_window:
            return
        begin, end = stall_window
        if not stalling["active"] and begin <= offset < end:
            stalling["active"] = True
            stalling["started_at"] = offset
            control_events.publish(("sink", (round(offset, 1), True)))
        elif stalling["active"] and offset >= end:
            stalling["active"] = False
            stalling["ended_at"] = offset
            control_events.publish(("sink", (round(offset, 1), False)))

    bridge.start()
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
        bridge.stop(timeout=5)

    # Plane 3, main thread, after trading: render what the ingress owner recorded. The facts
    # were captured on the hot path in nanoseconds; the formatting and the write happen here.
    # Guarded, because this is evidence formatting running after the market has closed and a
    # bad format string must not be able to cost a captured market its manifest.
    try:
        for line in render_hot_events(_drained(control_events), dropped=control_events.dropped):
            print(line, flush=True)
    except Exception as error:  # pragma: no cover - defensive
        print(f"    could not render hot-side events: {error!r}", flush=True)

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
    stamped = manifest

    verification = verify_store(database, expected_sha256=digest)

    # Settlement and the verifier's answer are Plane-3 facts that arrive after trading. Both are
    # delivered to the read model and a final frame is written, so the last thing an operator
    # sees is the resolved market rather than a permanent "unknown".
    if settlement is not None:
        decision_summary = settlement.decision
        publisher.deliver(
            "settlement",
            {
                "state": decision_summary.state.value,
                "winning_outcome": (
                    None
                    if decision_summary.winning_outcome is None
                    else decision_summary.winning_outcome.value
                ),
                "authoritative_block": decision_summary.authoritative_block,
                "payout_numerators": list(
                    () if decision_summary.payout is None else decision_summary.payout.numerators
                ),
                "note": "redemption is disabled in this build; nothing was redeemed",
            },
        )
    publisher.deliver(
        "verification",
        {
            "status": verification.status.value,
            "complete": verification.status.value == "COMPLETE",
        },
    )
    # The closed market's own manifest, not the last live counters the read model happened to
    # hold. P12B's final snapshot said 82,335 decisions and one drop while its manifest said
    # 82,336 and none: the counters were a running estimate that stopped one record early.
    publisher.deliver(
        "closed",
        {
            "decision_count": stamped.decision_count,
            "risk_count": stamped.risk_count,
            "dropped_records": stamped.dropped_records,
            "sink_errors": stamped.sink_errors,
            "telemetry_complete": stamped.telemetry_complete,
            "verification_status": verification.status.value,
        },
    )
    publisher.deliver(
        "audit_counts",
        {
            "accepted": audit_channel.accepted,
            "persisted": worker.stats.control_records_written,
            "dropped": audit_channel.dropped,
        },
    )
    publisher.publish_now(time.time())
    bridge.publish_pending()
    time.sleep(0.2)

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

    # The sidecar is written last, and carries the identity the read path checks: the archive's
    # own hash, the raw database hash it must restore to, and the market it holds. A file cannot
    # contain its own hash, and an artifact with no identity is not evidence — so the identity
    # lives here and `open_verified_archive` refuses anything that does not match it.
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "database": database.name,
                "database_bytes": size,
                "database_sha256": digest,
                "archive": archive.archive_path.name,
                "archive_bytes": archive.archive_bytes,
                "archive_sha256": archive.archive_sha256,
                "archive_verified": archive.verified,
                "note": (
                    "Identity for the durable read path. The digests are of the closed database "
                    "and of its archive, and are held here rather than inside either: a file "
                    "cannot contain its own hash."
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

    # Prove the supported P12/P15 path works on this artifact, now, rather than promising it.
    readback = _verified_readback(archive.archive_path, sidecar, out)
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
        "verified_readback": readback,
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
        "ui": {
            "bridge": bridge.stats.summary(),
            "hot_channel": {
                "capacity": hot_commands.capacity,
                "accepted": hot_commands.accepted,
                "high_water": hot_commands.high_water,
            },
            "control_records_written": worker.stats.control_records_written,
            "audit_accepted": audit_channel.accepted,
            "audit_dropped": audit_channel.dropped,
            "hot_event_channel": {
                "capacity": control_events.capacity,
                "accepted": control_events.accepted,
                "dropped": control_events.dropped,
            },
            "snapshot_path": str(snapshot_channel.path),
            "inbox_path": str(inbox.directory),
            "snapshots_published": snapshot_channel.published,
            "snapshot_write_errors": snapshot_channel.write_errors,
            "commands": control_ingress.summary(),
        },
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


def render_hot_events(events: list[Any], *, dropped: int = 0) -> list[str]:
    """Format what the ingress owner recorded. Runs on the main thread, after the market.

    Separate from the runner's flow, and tested, because the P12C run that first exercised this
    crashed here on a subscript: an operator command arrives as `("command", outcome)`, so the
    payload *is* the outcome. Formatting evidence is the last thing that should be able to end a
    market run, and this is now the shape a test drives.
    """
    lines: list[str] = []
    for kind, payload in events:
        if kind == "command":
            lines.append(
                f"    operator {payload.kind} {payload.command_id}: "
                f"accepted={payload.accepted} ordinal={payload.ingress_ordinal} "
                f"risk_seq={payload.risk_sequence} state={payload.risk_state}"
            )
        elif kind in ("sink", "bridge"):
            at, active = payload
            what = "sink" if kind == "sink" else "UI bridge"
            state = "stalled (controlled local fault)" if active else "resumed"
            lines.append(f"    [+{at:.0f}s] {what} {state}")
    if dropped:
        lines.append(f"    {dropped} hot-side event(s) dropped")
    return lines


def _drained(channel: BoundedChannel) -> list[Any]:
    """Empty a bounded channel, keeping its drop accounting exact."""
    items: list[Any] = []
    while channel.records:
        items.append(channel.records.popleft())
        channel.drained += 1
    return items


def _verified_readback(archive: Path, sidecar: Path, out: Path) -> dict[str, object]:
    """Restore through the verified path and re-verify the store that comes back."""
    scratch = out / "readback.sqlite3"
    try:
        open_verified_archive(archive, ArchiveIdentity.from_sidecar(sidecar), scratch)
        result = verify_store(scratch)
        return {"opened": True, "verification": result.summary()}
    except ArchiveVerificationError as error:
        return {"opened": False, "error": str(error)}
    finally:
        scratch.unlink(missing_ok=True)


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
    parser.add_argument("--ui", type=Path, help="directory for the snapshot and command inbox")
    parser.add_argument(
        "--stall-bridge-from", type=int, help="seconds after T0 to stall the Plane-3 UI bridge"
    )
    parser.add_argument("--stall-bridge-to", type=int)
    args = parser.parse_args()
    window = (
        (args.stall_from, args.stall_to)
        if args.stall_from is not None and args.stall_to is not None
        else None
    )
    bridge_window = (
        (args.stall_bridge_from, args.stall_bridge_to)
        if args.stall_bridge_from is not None and args.stall_bridge_to is not None
        else None
    )
    asyncio.run(main(args.out, window, args.buffer, args.ui or (args.out / "ui"), bridge_window))
