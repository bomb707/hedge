"""What does the operator UI cost the trading path? Process-isolated, on a real market.

Three configurations, each alone in a fresh interpreter:

* **off** — the accepted P11 stack, no bridge and no snapshot machinery;
* **healthy** — the corrected P12 bridge and snapshot publisher running normally;
* **stalled** — the same machinery with the bridge's filesystem work deliberately stopped.

The hot side does the same thing in all three: a `popleft` on a bounded deque. The point of the
stalled configuration is that the bridge being dead is indistinguishable from it being alive, as
measured from the trading path — which is the whole claim.

Original persistence-benchmark docstring follows.

---

What does durable persistence cost the trading path? Process-isolated, on a real market.

**REAL MARKET DATA.** The event stream is a complete P6 capture of a real `btc-updown-5m`
market, replayed from its journal. A synthetic loop is not used as the authoritative gate: P8
already learned that a corpus which acts on 51% of cycles produces tier numbers nothing like a
real market's 0.9%, and the number this benchmark exists to defend is the one a real market
would see.

Three configurations, each alone in a fresh interpreter:

* **off** — no persistence worker at all. The baseline.
* **healthy** — worker running, draining continuously into SQLite on real disk.
* **stalled** — worker running and deliberately consuming nothing, so the bounded buffer fills
  and overflows. This is the configuration the acceptance claim is about: **stalling Plane 3
  must not slow Plane 1.**

The method is P8C's, unchanged and for its reasons: a fresh process per configuration because
allocator state and GC scheduling carry between cycles inside one interpreter; launch order
alternated across pairs so a machine that warms or throttles cannot favour one side; identical
simulated work on every side, with only persistence switched.

Read-only. No venue, no credential, no order.
"""

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

PAIRS = 4
WARMUP_EVENTS = 2_000
CONFIGURATIONS = ("off", "healthy", "stalled")


def load_events(journal_path: Path, limit: int) -> list[Any]:
    """Stream real events out of a captured journal without decoding the decisions.

    The journals are 150-200 MB because every step stores a complete `DecisionResult`; decoding
    all of that would measure the decoder. Only the events are needed — the strategy recomputes
    its own decisions — so each line is parsed and only its event half is decoded.
    """
    from maker5m.replay.codec import _dec_event

    events: list[Any] = []
    with journal_path.open("rb") as handle:
        handle.readline()  # header
        for line in handle:
            if len(events) >= limit:
                break
            record = json.loads(line)
            events.append(_dec_event(record["event"]))
    return events


def _bench_config() -> Any:
    """The same config the run uses, so parameter projection costs what it costs."""
    from maker5m.strategy import BaseLot, default_config

    return default_config(BaseLot.of(15))


def _observe(publisher: Any, record: Any, controller: Any) -> None:
    """Plane 3: update the read model and offer a frame. Runs on the worker thread."""
    records = controller.trace.records
    verdict = records[-1] if records else None
    publisher.observe(record, verdict)
    import time as _time

    publisher.maybe_publish(_time.time())


def replayed_health(state: Any) -> Any:
    """P6's health as it actually was, replayed from the journal's own HealthEvents.

    The journal records real `HealthEvent`s — 2,206 of them in the measured market — because P6
    emits them as normalized events and P2 reduces them into `MarketState.health`. So the
    benchmark does not have to re-derive health, and must not: re-deriving would be a second
    staleness authority, and the previous version's alternative was worse still. It read
    `pipeline.clob_health`, which nothing in a replay ever updates, so every cycle saw CLOB
    UNKNOWN and awaiting-snapshot, P9 correctly halted, `risk_adjust` emptied every intent, and
    the benchmark measured a market that never quoted.

    `awaiting_snapshot` is derived from the recorded status rather than invented: P6 clears it
    when it emits HEALTHY for the book, which is exactly what a HEALTHY record means here.
    """
    from maker5m.market.events import HealthStatus
    from maker5m.risk.trace import HealthFrame

    health = state.health
    return HealthFrame(
        clob_status=health.clob_book,
        clob_awaiting_snapshot=health.clob_book is not HealthStatus.HEALTHY,
        spot_status=health.spot_feed,
        order_stream_status=HealthStatus.UNKNOWN,
        order_stream_required=False,
    )


def child_run(mode: str, journal: Path, limit: int) -> dict[str, object]:
    """One configuration, alone in this interpreter."""
    from maker5m.execution import Executor, RecordingTransport, VenueAdapter
    from maker5m.feeds import BookTracker, IngressMerger, MarketDataPipeline
    from maker5m.feeds.venue import VenueMarketRules
    from maker5m.market import MarketState, reduce_event
    from maker5m.market.events import BookUpdate, SpotTick
    from maker5m.numeric import parse_price, parse_share
    from maker5m.persistence import (
        BoundedChannel,
        MarketIdentity,
        PersistenceWorker,
        TelemetryProvenance,
        TelemetryStore,
    )
    from maker5m.replay.codec import _dec_header
    from maker5m.risk import RiskConfig, RiskEngine, RiskProvenance
    from maker5m.risk.engine import RiskDecision
    from maker5m.risk.overlay import risk_adjust
    from maker5m.risk.trace import RiskController
    from maker5m.strategy import BaseLot, StrategyEngine, default_config
    from maker5m.telemetry import InstrumentedRun, ObservationBuffer, SamplingPolicy, perf_now_ns
    from maker5m.telemetry.metrics import quantile
    from maker5m.telemetry.observation import OBS_PLAN

    with journal.open("rb") as handle:
        header = _dec_header(json.loads(handle.readline()))
    definition = header.market
    events = load_events(journal, limit)

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
        buffer=ObservationBuffer(capacity=65_536),
        sampling=SamplingPolicy(10),
        enabled=True,
    )

    # P9 runs in EVERY configuration, including off. Charging the risk evaluation itself to
    # persistence would inflate P11's number with work production performs regardless — the same
    # mistake P8's first benchmark made three times with simulation.
    controller = RiskController(
        engine=RiskEngine(config=RiskConfig()),
        provenance=RiskProvenance.REPLAY_OF_REAL_CAPTURE
        if hasattr(RiskProvenance, "REPLAY_OF_REAL_CAPTURE")
        else RiskProvenance.REAL_PUBLIC_MARKET_DATA,
    )
    harness.risk = controller
    risk_channel = BoundedChannel(capacity=400_000)

    from maker5m.ui import (
        CommandBridge,
        CommandInbox,
        ControlIngress,
        HotCommandChannel,
        SnapshotChannel,
        SnapshotPublisher,
    )

    worker: PersistenceWorker | None = None
    directory = tempfile.TemporaryDirectory()
    if mode != "off":
        worker = PersistenceWorker(
            buffer=harness.buffer,
            risk=risk_channel,
            fills=BoundedChannel(capacity=1_024),
            store=TelemetryStore(path=Path(directory.name) / "telemetry.sqlite3"),
            identity=MarketIdentity(
                market_id=definition.market_id,
                slug=definition.slug,
                condition_id=None,
                provenance=TelemetryProvenance.REPLAY_OF_REAL_CAPTURE.value,
            ),
            stall=(lambda: True) if mode == "stalled" else None,
        )
        worker.start()

    ui_root = Path(directory.name) / "ui"
    hot_commands = HotCommandChannel()
    bridge = None
    publisher = None
    if mode != "off":
        bridge = CommandBridge(
            inbox=CommandInbox(ui_root / "inbox"),
            channel=hot_commands,
            snapshot=SnapshotChannel(ui_root / "snapshot.json"),
            stall=(lambda: True) if mode == "stalled" else None,
        )
        publisher = SnapshotPublisher(
            identity=None, config=_bench_config(), bridge=bridge, t0_ns=int(definition.t0)
        )
        control = ControlIngress(controller=controller)
        del control
        bridge.start()
        if worker is not None:
            worker.on_record = lambda record: _observe(publisher, record, controller)

    decide_ns: list[int] = []
    cycle_ns: list[int] = []
    reconcile_ns: list[int] = []
    risk_states: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    state = MarketState.initial(definition)

    try:
        for index, event in enumerate(events):
            kind = type(event).__name__
            start = perf_now_ns()
            state = reduce_event(state, event)
            merger.state = state
            merger.advance_ordinal()
            merger.stages_measured = harness.sampling.selects(merger.ordinal, kind)
            decision = engine.decide(state)
            decided = perf_now_ns()
            record = controller.evaluate(
                replayed_health(state),
                as_of_ingress_ordinal=merger.ordinal,
                now_ns=state.last_event_timestamp,
            )
            if mode != "off":
                risk_channel.publish(record)
            verdict = RiskDecision(
                state=record.state,
                active=record.active,
                latched=record.latched,
                snapshot=controller.engine.snapshot,
            )
            source_ts = None
            if isinstance(event, BookUpdate | SpotTick):
                source_ts = None  # the captured journal carries no venue clock; never invented
            harness.observe(
                kind,
                start,
                risk_adjust(decision, verdict),
                source_ts,
                strategy_intent=decision.orders,
            )
            # The whole of the hot side's UI work, in every configuration that has one.
            for _pending in hot_commands.pop_all():
                pass
            risk_states[record.state.value] += 1
            last = harness.buffer.records[-1] if harness.buffer.records else None
            if last is not None:
                plan: Any = last[OBS_PLAN]
                actions[plan.up.action.value] += 1
                actions[plan.down.action.value] += 1
            finished = perf_now_ns()
            if index >= WARMUP_EVENTS:
                decide_ns.append(decided - start)
                cycle_ns.append(finished - start)
                reconcile_ns.append(finished - decided)
    finally:
        if bridge is not None:
            bridge.stop(timeout=5)
        if worker is not None:
            worker.stall = None
            worker.stop(timeout=30)
        directory.cleanup()

    def tiers(samples: list[int]) -> dict[str, int]:
        ordered = sorted(samples)
        return {
            "p50": quantile(ordered, 0.50),
            "p95": quantile(ordered, 0.95),
            "p99": quantile(ordered, 0.99),
            "mean": int(statistics.fmean(ordered)) if ordered else 0,
        }

    return {
        "mode": mode,
        "slug": definition.slug,
        "bridge": None if bridge is None else bridge.stats.summary(),
        "snapshots": 0 if publisher is None else publisher.published,
        "events": len(events),
        "measured": len(decide_ns),
        "decide": tiers(decide_ns),
        "full_cycle": tiers(cycle_ns),
        "receive_to_reconcile": tiers(reconcile_ns),
        "risk_states": dict(sorted(risk_states.items())),
        "actions": dict(sorted(actions.items())),
        "buffer_dropped": harness.buffer.dropped,
        "buffer_accepted": harness.buffer.accepted,
        "persisted": 0 if worker is None else worker.stats.decisions_written,
        "worker_high_water": 0 if worker is None else worker.stats.buffer_high_water,
        "sink_errors": 0 if worker is None else worker.store.sink_errors,
        "consume_errors": 0 if worker is None else worker.stats.consume_errors,
        "error_samples": [] if worker is None else list(worker.stats.error_samples),
        "store_error_samples": [] if worker is None else list(worker.store.error_samples),
        "observations_consumed": 0 if worker is None else worker.stats.observations_consumed,
        "fills_seen": 0 if worker is None else worker.stats.fills_seen,
        "rows_written": 0 if worker is None else worker.store.rows_written,
        "batches": 0 if worker is None else worker.store.batches,
        "transaction_ns": 0 if worker is None else worker.store.transaction_ns,
    }


def spawn(mode: str, journal: Path, limit: int) -> dict[str, object]:
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child",
            mode,
            "--journal",
            str(journal),
            "--limit",
            str(limit),
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    parsed: dict[str, object] = json.loads(result.stdout)
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", choices=CONFIGURATIONS)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=30_000)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if args.child is not None:
        json.dump(child_run(args.child, args.journal, args.limit), sys.stdout)
        return

    runs: list[dict[str, object]] = []
    for index in range(PAIRS):
        # Rotate which configuration goes first, so a throttling machine cannot bias one.
        order = CONFIGURATIONS[index % 3 :] + CONFIGURATIONS[: index % 3]
        for mode in order:
            runs.append(spawn(mode, args.journal, args.limit) | {"pair": index, "order": order})
        print(f"  pair {index + 1}/{PAIRS} done ({', '.join(order)})", flush=True)

    def medians(mode: str, metric: str, tier: str) -> int:
        values = [int(run[metric][tier]) for run in runs if run["mode"] == mode]  # type: ignore[index]
        return int(statistics.median(values))

    summary: dict[str, Any] = {
        "kind": "P11_PERSISTENCE_OVERHEAD",
        "provenance": "REPLAY_OF_REAL_CAPTURE",
        "journal": str(args.journal.name),
        "events_per_run": args.limit,
        "pairs": PAIRS,
        "note": (
            "Real captured market events replayed in fresh processes. The strategy, the "
            "reconciler and the shadow order table run identically in every configuration; "
            "only persistence changes."
        ),
        "medians": {
            metric: {
                mode: {tier: medians(mode, metric, tier) for tier in ("p50", "p95", "p99")}
                for mode in CONFIGURATIONS
            }
            for metric in ("decide", "full_cycle", "receive_to_reconcile")
        },
        "runs": runs,
    }
    base = summary["medians"]
    for metric in ("decide", "full_cycle"):
        off = base[metric]["off"]["p50"]
        for mode in ("healthy", "stalled"):
            delta = base[metric][mode]["p50"] - off
            summary.setdefault("overhead", {})[f"{metric}_{mode}_p50_ns"] = delta
            summary["overhead"][f"{metric}_{mode}_p50_pct"] = (
                round(100.0 * delta / off, 2) if off else None
            )

    text = json.dumps(summary, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(json.dumps({"medians": summary["medians"], "overhead": summary["overhead"]}, indent=2))


if __name__ == "__main__":
    main()
