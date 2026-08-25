"""Run the P9 risk engine against a real BTC five-minute market.

Two modes, both on genuinely real market data:

* ``baseline`` — no faults. The verdict should stay SAFE for a healthy market apart from the
  normal phase-based no-quote behaviour, and any halt is a finding rather than a pass.
* ``faults`` — the market data stays real and live throughout while **local** failures are
  deliberately induced: the BTC adapter is paused, the CLOB socket is really dropped, and the
  continuity path is really forced into a resnapshot.

The distinction matters for what the evidence may claim. A paused adapter is not a venue
outage, and the manifest says ``CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`` rather than pretending
the venue failed. What is real is everything the bot observes: the book, the trades, the BTC
prices, the reconnect, the resubscription, and the snapshot.

Strictly read-only. ``LIVE_TRADING_ENABLED`` is ``False``, no credential is used, no
authenticated socket is opened, and no order of any size is sent.
"""

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from maker5m.execution import Executor, RecordingTransport, VenueAdapter
from maker5m.feeds.capture import capture_market
from maker5m.feeds.discovery import discover_market, slug_for
from maker5m.feeds.pipeline import MarketDataPipeline
from maker5m.market.events import HealthComponent, HealthStatus
from maker5m.market.timebase import NANOS_PER_SECOND, TimestampNs
from maker5m.risk import (
    ApiErrorMonitor,
    RiskConfig,
    RiskDecision,
    RiskEngine,
    RiskInputs,
    RiskReason,
    RiskState,
    risk_adjust,
)
from maker5m.safety import LIVE_TRADING_ENABLED
from maker5m.strategy import BaseLot, StrategyEngine, default_config
from maker5m.strategy.decision import DecisionResult
from maker5m.telemetry import InstrumentedRun, SamplingPolicy, perf_now_ns
from maker5m.telemetry.metrics import Distribution

MIN_LEAD_SECONDS = 45
SAMPLE_EVERY = 10


@dataclass(frozen=True, slots=True)
class Fault:
    """One controlled local fault, scheduled by offset from T0."""

    name: str
    start_offset_s: int
    end_offset_s: int
    kind: str
    """``pause_spot`` | ``drop_clob`` | ``force_resnapshot`` | ``signal:<RiskReason>``

    A ``signal:`` fault sets one risk input directly while the real CLOB and BTC streams keep
    flowing untouched. Some conditions cannot be asked of a live venue on demand — a clock
    cannot be made to drift, an account cannot be made to disagree — so their *integration* is
    exercised against real market observations with the fault signal induced locally. The
    market data stays real; only the signal is ours.
    """


BASELINE_FAULTS: tuple[Fault, ...] = ()

INJECTED_FAULTS: tuple[Fault, ...] = (
    # Long enough to exceed the 5 s OPERATIONAL spot staleness threshold with room to spare.
    Fault("btc_stale", 40, 60, "pause_spot"),
    Fault("clob_disconnect", 110, 111, "drop_clob"),
    Fault("continuity_uncertain", 190, 191, "force_resnapshot"),
    # Conditions a live venue cannot be asked to produce. Real market data keeps flowing
    # throughout; only the signal is injected. Spaced so each halt and recovery is separable.
    Fault("clock_drift", 210, 214, "signal:clock_drift"),
    Fault("order_state_uncertain", 222, 226, "signal:order_state_uncertain"),
    Fault("position_mismatch", 234, 238, "signal:position_mismatch"),
    Fault("cost_ledger_mismatch", 248, 252, "signal:cost_ledger_mismatch"),
    Fault("api_error_rate", 262, 266, "signal:api_errors_exceeded"),
    Fault("rate_limit_uncertain", 274, 278, "signal:rate_limit_uncertain"),
    Fault("resolution_ambiguous", 286, 290, "signal:resolution_ambiguous"),
)


@dataclass(slots=True)
class RiskTimeline:
    """Every risk transition, in ingress order, plus what happened around it."""

    transitions: list[dict[str, object]] = field(default_factory=list)
    cycles_by_state: dict[str, int] = field(default_factory=dict)
    halts_by_reason: dict[str, int] = field(default_factory=dict)
    evaluate_ns: Distribution = field(default_factory=lambda: Distribution("risk_evaluate_ns"))
    places_by_state: dict[str, int] = field(default_factory=dict)
    cancels_by_state: dict[str, int] = field(default_factory=dict)
    fault_events: list[dict[str, object]] = field(default_factory=list)
    previous: RiskState | None = None

    def record(
        self,
        state: RiskState,
        active: frozenset[RiskReason],
        latched: frozenset[RiskReason],
        ordinal: int,
        now_ns: TimestampNs,
    ) -> None:
        key = state.value
        self.cycles_by_state[key] = self.cycles_by_state.get(key, 0) + 1
        if state is not self.previous:
            self.transitions.append(
                {
                    "from": None if self.previous is None else self.previous.value,
                    "to": key,
                    "ingress_ordinal": ordinal,
                    "at_ns": int(now_ns),
                    "active": sorted(reason.value for reason in active),
                    "latched": sorted(reason.value for reason in latched),
                }
            )
            if state is RiskState.HALTED:
                for reason in active:
                    self.halts_by_reason[reason.value] = (
                        self.halts_by_reason.get(reason.value, 0) + 1
                    )
            self.previous = state

    def note_fault(self, name: str, phase: str, ordinal: int, now_ns: TimestampNs) -> None:
        self.fault_events.append(
            {"fault": name, "phase": phase, "ingress_ordinal": ordinal, "at_ns": int(now_ns)}
        )

    def summary(self) -> dict[str, object]:
        return {
            "cycles_by_state": dict(sorted(self.cycles_by_state.items())),
            "halts_by_reason": dict(sorted(self.halts_by_reason.items())),
            "transitions": self.transitions,
            "fault_events": self.fault_events,
            "shadow_places_by_state": dict(sorted(self.places_by_state.items())),
            "shadow_cancels_by_state": dict(sorted(self.cancels_by_state.items())),
            "risk_evaluate_ns": self.evaluate_ns.summary(),
        }


async def main(out: Path, faults: tuple[Fault, ...], label: str) -> None:
    if LIVE_TRADING_ENABLED:  # pragma: no cover - defensive
        raise SystemExit("refusing to run while live trading is enabled")
    out.mkdir(parents=True, exist_ok=True)

    now = int(time.time())
    t0 = ((now // 300) + 1) * 300
    if t0 - now < MIN_LEAD_SECONDS:
        t0 += 300
    slug = slug_for(t0)
    print(f"[{time.strftime('%H:%M:%S')}] {label} run on {slug} (T0 in {t0 - now}s)", flush=True)

    market = discover_market(slug)
    following = discover_market(slug_for(t0 + 300))
    config = default_config(BaseLot.of(15))
    t0_ns = market.definition.t0

    risk = RiskEngine(config=RiskConfig())
    api_errors = ApiErrorMonitor.from_config(RiskConfig())
    timeline = RiskTimeline()
    runs: list[InstrumentedRun] = []
    force_disconnect = asyncio.Event()
    active_faults: set[str] = set()
    fired: set[str] = set()
    resolved: set[str] = set()

    def offset_s(now_ns: TimestampNs) -> float:
        return (int(now_ns) - int(t0_ns)) / NANOS_PER_SECOND

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
                sampling=sampling,
            )
        )

    def injected_signals(now_ns: TimestampNs) -> dict[str, object]:
        """Risk inputs currently forced by a ``signal:`` fault."""
        elapsed = offset_s(now_ns)
        out: dict[str, object] = {}
        for fault in faults:
            if not fault.kind.startswith("signal:"):
                continue
            if fault.start_offset_s <= elapsed < fault.end_offset_s:
                field_name = fault.kind.split(":", 1)[1]
                out[field_name] = (
                    int(RiskConfig().clock_drift_limit_ns) * 4
                    if field_name == "clock_drift"
                    else True
                )
        return out

    def gate(kind: str, now_ns: TimestampNs) -> bool:
        """Suppress spot delivery while a ``pause_spot`` fault is scheduled."""
        elapsed = offset_s(now_ns)
        for fault in faults:
            if fault.kind != "pause_spot":
                continue
            if fault.start_offset_s <= elapsed < fault.end_offset_s and kind == "spot":
                return False
        return True

    last_evaluated = [-1]

    def run_faults(now_ns: TimestampNs, pipeline: MarketDataPipeline) -> None:
        elapsed = offset_s(now_ns)
        ordinal = pipeline.merger.ordinal
        for fault in faults:
            if fault.name not in fired and elapsed >= fault.start_offset_s:
                fired.add(fault.name)
                active_faults.add(fault.name)
                timeline.note_fault(fault.name, "injected", ordinal, now_ns)
                if fault.kind == "drop_clob":
                    force_disconnect.set()
                elif fault.kind == "force_resnapshot":
                    pipeline.on_uncertain(
                        HealthComponent.CLOB_BOOK, "controlled local fault injection"
                    )
            if (
                fault.name in active_faults
                and fault.name not in resolved
                and elapsed >= fault.end_offset_s
            ):
                resolved.add(fault.name)
                active_faults.discard(fault.name)
                timeline.note_fault(fault.name, "released", ordinal, now_ns)
                latching = {
                    "signal:order_state_uncertain": RiskReason.ORDER_STATE_UNCERTAIN,
                    "signal:position_mismatch": RiskReason.POSITION_MISMATCH,
                    "signal:cost_ledger_mismatch": RiskReason.COST_LEDGER_MISMATCH,
                }.get(fault.kind)
                if latching is not None:
                    # These outlive their condition on purpose, so releasing the signal is not
                    # enough. A reconciliation result has to be recorded deliberately, which is
                    # what the operator would be doing after establishing which side was wrong.
                    risk.reconciled(latching)
                    timeline.note_fault(fault.name, "reconciled", ordinal, now_ns)

    def evaluate_now(pipeline: MarketDataPipeline, now_ns: TimestampNs) -> RiskDecision:
        """Evaluate the verdict against health as it stands right now.

        Called from ``observe`` immediately before the intent is adjusted, so the verdict can
        never lag the condition by an event - a single PLACE slipping through between a feed
        going stale and the halt being noticed would defeat the whole mechanism.
        """
        signals = injected_signals(now_ns)
        inputs = RiskInputs(
            now_ns=now_ns,
            clob_status=pipeline.clob_health.status,
            clob_awaiting_snapshot=pipeline.clob_health.awaiting_snapshot,
            clob_last_message_at=pipeline.clob_health.last_message_at,
            spot_status=pipeline.spot_health.status,
            spot_last_message_at=pipeline.spot_health.last_message_at,
            order_stream_status=HealthStatus.UNKNOWN,
            order_stream_required=False,
            api_errors_exceeded=bool(signals.get("api_errors_exceeded"))
            or api_errors.exceeded(now_ns),
            clock_drift_ns=int(signals.get("clock_drift", 0)),  # type: ignore[call-overload]
            order_state_uncertain=bool(signals.get("order_state_uncertain")),
            position_mismatch=bool(signals.get("position_mismatch")),
            cost_ledger_mismatch=bool(signals.get("cost_ledger_mismatch")),
            rate_limit_uncertain=bool(signals.get("rate_limit_uncertain")),
            resolution_ambiguous=bool(signals.get("resolution_ambiguous")),
        )
        start = perf_now_ns()
        decision = risk.evaluate(inputs)
        timeline.evaluate_ns.add(perf_now_ns() - start)
        ordinal = pipeline.merger.ordinal
        last_evaluated[0] = ordinal
        timeline.record(decision.state, decision.active, decision.latched, ordinal, now_ns)
        return decision

    def on_tick(now_ns: TimestampNs, pipeline: MarketDataPipeline) -> None:
        """Advance the fault schedule, and evaluate when no event produced a verdict.

        A paused adapter means no events arrive, so a verdict that only fired on the next event
        would never fire at all while the feed is the thing that stopped.
        """
        run_faults(now_ns, pipeline)
        if pipeline.merger.ordinal != last_evaluated[0]:
            evaluate_now(pipeline, now_ns)

    def observe(kind: str, raw_ns: int, decision: DecisionResult) -> None:
        """One shadow execution cycle, with the verdict taken as of this event.

        Evaluated here rather than on the surrounding tick so the verdict cannot lag the
        condition by an event: a single PLACE slipping through between a feed going stale and
        the halt being noticed would defeat the whole mechanism.
        """
        run = runs[0]
        pipeline = run.pipeline
        verdict = evaluate_now(pipeline, pipeline.merger.state.last_event_timestamp)
        state = verdict.state.value
        before = run.executor.orders.open_count
        run.observe(kind, raw_ns, risk_adjust(decision, verdict))
        after = run.executor.orders.open_count
        if after > before:
            timeline.places_by_state[state] = timeline.places_by_state.get(state, 0) + 1
        elif after < before:
            timeline.cancels_by_state[state] = timeline.cancels_by_state.get(state, 0) + 1

    result = await capture_market(
        market,
        config,
        next_market=following,
        description=f"P9 {label} run on {slug}",
        on_pipeline=attach,
        observer=observe,
        gate=gate,
        on_tick=on_tick,
        force_clob_disconnect=force_disconnect if faults else None,
    )

    run = runs[0]
    measurement = run.summary()
    provenance = (
        "REAL_PUBLIC_MARKET_DATA" if not faults else "CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET"
    )
    manifest: dict[str, object] = {
        "phase": "P9",
        "kind": label.upper(),
        "provenance": provenance,
        "note": (
            "Real public Polymarket CLOB and real Binance BTC data throughout. "
            + (
                "No faults injected."
                if not faults
                else "Faults are deliberately induced LOCAL failures, not observed venue "
                "incidents. The market data itself is real and uninterrupted."
            )
        ),
        "slug": market.definition.slug,
        "market_id": market.definition.market_id,
        "t0_ns": int(t0_ns),
        "run_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "orders_sent": 0,
        "credentials_used": "none",
        "faults_scheduled": [
            {
                "name": fault.name,
                "kind": fault.kind,
                "start_offset_s": fault.start_offset_s,
                "end_offset_s": fault.end_offset_s,
            }
            for fault in faults
        ],
        "feed_counters": result.counters.summary(),
        "clock_health": result.clock_health.summary(),
        "cycles": measurement["cycles"],
        "observations": measurement["observations"],
        "risk": timeline.summary(),
        "risk_config": {
            "status": RiskConfig().status.value,
            "clob_stale_after_ns": int(RiskConfig().clob_stale_after),
            "spot_stale_after_ns": int(RiskConfig().spot_stale_after),
            "clock_drift_limit_ns": int(RiskConfig().clock_drift_limit_ns),
            "api_error_window_ns": int(RiskConfig().api_error_window),
            "api_error_threshold": RiskConfig().api_error_threshold,
            "recovery_confirmations": RiskConfig().recovery_confirmations,
        },
        "execution_counters": measurement["counters"],
        "latency_ns": measurement["latency_ns"],
        "api_errors": api_errors.summary(),
    }
    path = out / f"{market.definition.slug}.p9-{label}.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(manifest, indent=2, sort_keys=True))


def entry() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--mode", choices=("baseline", "faults"), default="baseline")
    args = parser.parse_args()
    faults = BASELINE_FAULTS if args.mode == "baseline" else INJECTED_FAULTS
    asyncio.run(main(args.out, faults, args.mode))


if __name__ == "__main__":
    entry()
