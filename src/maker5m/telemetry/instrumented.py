"""The P8 measurement harness: real market data, shadow execution, no orders.

Wraps the P6 pipeline so every cycle is timed and classified, without changing what the
deterministic core does. Execution is **shadow only** — the reconciler runs and its plan is
recorded, but nothing is dispatched, because ``LIVE_TRADING_ENABLED`` is ``False``.

The measurement discipline that matters here:

* SpotTick-triggered and CLOB-triggered cycles are kept in **separate** distributions. Mixing
  them would hide the number Canonical §29.7 turns on — how fast the external feed can wake
  the decision path.
* KEEP cycles are measured separately and have no dispatch stage at all, which is the point:
  the common case should cost nothing on the network.
* Internal latency is never combined with venue round-trip time. Real order RTT is
  **unmeasured** at P8 and stays that way until P14.
"""

from dataclasses import dataclass, field
from typing import Final

from maker5m.domain import Outcome
from maker5m.execution import (
    Executor,
    RecordingTransport,
    VenueAdapter,
    prepare_both_sides,
    reconcile,
)
from maker5m.execution.live_orders import OrderLifecycle
from maker5m.execution.reconciler import ReconcileAction
from maker5m.feeds.pipeline import MarketDataPipeline
from maker5m.feeds.venue import VenueMarketRules
from maker5m.market.events import HealthStatus
from maker5m.numeric.units import ShareUnits
from maker5m.strategy.decision import DecisionResult
from maker5m.strategy.engine import StrategyEngine
from maker5m.telemetry.classifier import QualityReason, classify
from maker5m.telemetry.latency import Stage, TraceBuilder, perf_now_ns
from maker5m.telemetry.metrics import ActionCounters, Distribution
from maker5m.telemetry.sampling import SamplingPolicy
from maker5m.telemetry.shadow import ShadowQueueTracker
from maker5m.telemetry.sink import TelemetrySink

__all__ = ["InstrumentedRun", "LatencyBook"]

QUEUE_LOSS_ACTIONS: Final = frozenset({ReconcileAction.REPLACE, ReconcileAction.CANCEL})

_STRATEGY_REASON: Final[dict[str, QualityReason]] = {
    "PHASE_NOT_QUOTING": QualityReason.PHASE_NOT_QUOTING,
    "CENTRE_UNAVAILABLE": QualityReason.CENTRE_UNAVAILABLE,
    "ENDGAME_GATE": QualityReason.ENDGAME_GATE,
    "HARD_BAND": QualityReason.HARD_BAND,
}
"""Built once. A dict literal per call showed up clearly in the P8 overhead profile."""


@dataclass(slots=True)
class LatencyBook:
    """Every distribution the P8 evidence needs, kept separate by source."""

    spot_receive_to_decide: Distribution = field(
        default_factory=lambda: Distribution("spot_receive_to_decide")
    )
    clob_receive_to_decide: Distribution = field(
        default_factory=lambda: Distribution("clob_receive_to_decide")
    )
    fill_receive_to_decide: Distribution = field(
        default_factory=lambda: Distribution("fill_receive_to_decide")
    )
    phase_receive_to_decide: Distribution = field(
        default_factory=lambda: Distribution("phase_receive_to_decide")
    )
    receive_to_reconcile: Distribution = field(
        default_factory=lambda: Distribution("receive_to_reconcile")
    )
    decide_duration: Distribution = field(default_factory=lambda: Distribution("decide_duration"))
    prepare_duration: Distribution = field(default_factory=lambda: Distribution("prepare_duration"))
    reconcile_duration: Distribution = field(
        default_factory=lambda: Distribution("reconcile_duration")
    )
    keep_cycle: Distribution = field(default_factory=lambda: Distribution("keep_cycle"))
    acting_cycle: Distribution = field(default_factory=lambda: Distribution("acting_cycle"))
    queue_ahead: Distribution = field(default_factory=lambda: Distribution("queue_ahead"))

    _by_kind: dict[str, Distribution] = field(default_factory=dict, repr=False)

    def by_kind(self, event_kind: str) -> Distribution:
        """Route a sample to its source distribution.

        The mapping is built once. Constructing a dict literal per event showed up clearly in
        the P8 overhead profile - it is the kind of cost that is invisible in source and
        obvious in a measurement.
        """
        if not self._by_kind:
            self._by_kind = {
                "SpotTick": self.spot_receive_to_decide,
                "BookUpdate": self.clob_receive_to_decide,
                "OwnFill": self.fill_receive_to_decide,
                "PhaseEvent": self.phase_receive_to_decide,
            }
        return self._by_kind.get(event_kind, self.clob_receive_to_decide)

    def summary(self) -> dict[str, object]:
        return {
            d.label: d.summary()
            for d in (
                self.spot_receive_to_decide,
                self.clob_receive_to_decide,
                self.fill_receive_to_decide,
                self.phase_receive_to_decide,
                self.receive_to_reconcile,
                self.decide_duration,
                self.prepare_duration,
                self.reconcile_duration,
                self.keep_cycle,
                self.acting_cycle,
                self.queue_ahead,
            )
        }


@dataclass(slots=True)
class InstrumentedRun:
    """Times and classifies one shadow decision cycle per ingested event."""

    pipeline: MarketDataPipeline
    engine: StrategyEngine
    rules: VenueMarketRules
    executor: Executor = field(
        default_factory=lambda: Executor(adapter=VenueAdapter(RecordingTransport()))
    )
    shadow: ShadowQueueTracker = field(default_factory=ShadowQueueTracker)
    latency: LatencyBook = field(default_factory=LatencyBook)
    counters: ActionCounters = field(default_factory=ActionCounters)
    sink: TelemetrySink = field(default_factory=TelemetrySink)
    sampling: SamplingPolicy = field(default_factory=SamplingPolicy)
    enabled: bool = True
    """When ``False`` the harness runs the same decisions and records nothing.

    This is what makes the OFF/ON overhead comparison a like-for-like measurement.
    """

    trace: TraceBuilder = field(default_factory=TraceBuilder)
    cycles: int = 0
    _shadow_seq: int = 0
    shadow_orders: bool = True
    """Treat a shadow PLACE as immediately resting.

    Without this the order table stays empty forever, every cycle re-plans a PLACE, and the
    KEEP path - the single most important behaviour in the system - is never exercised or
    measured. A real venue would acknowledge the order, so the shadow run models that. The
    resulting KEEP figures describe *our strategy's* behaviour against real market data; they
    are not evidence about the target wallet.
    """

    def observe(self, event_kind: str, raw_receive_ns: int, decision: DecisionResult) -> None:
        """Run one shadow execution cycle, timing and classifying it when enabled.

        Preparation and reconciliation happen either way: production performs them on every
        cycle, so counting them as instrumentation would overstate what measurement costs. Only
        the timestamps, distributions, classification, shadow tracking, and sink are gated by
        ``enabled`` — which is what makes the OFF/ON comparison isolate instrumentation.
        """
        self.cycles += 1
        measuring = self.enabled

        trace = self.trace
        if measuring:
            trace.reset()
            trace.event_kind = event_kind
            trace.set(Stage.RAW_RECEIVE, raw_receive_ns)
            decide_done = trace.mark(Stage.DECIDE_DONE, perf_now_ns)

        prepared = prepare_both_sides(decision, self.pipeline.merger.state, self.rules)
        if measuring:
            prepare_done = trace.mark(Stage.PREPARE_DONE, perf_now_ns)

        live = {
            Outcome.UP: self.executor.orders.current(Outcome.UP),
            Outcome.DOWN: self.executor.orders.current(Outcome.DOWN),
        }
        plan = reconcile(prepared, live)
        if measuring:
            reconcile_done = trace.mark(Stage.RECONCILE_DONE, perf_now_ns)

        # Venue simulation, not measurement: it mutates only the executor's order table, which
        # is exactly the input reconcile() reads next cycle. Gating it on `enabled` would leave
        # the OFF run reconciling against an empty table while the ON run reconciled against a
        # populated one, and the resulting "overhead" would be simulation cost, not telemetry.
        if self.shadow_orders:
            self._apply_shadow(plan)

        if not measuring:
            return

        merger = self.pipeline.merger
        if merger.last_decide_ns and merger.last_reduce_ns:
            self.latency.decide_duration.add(merger.last_decide_ns - merger.last_reduce_ns)

        self.latency.by_kind(event_kind).add(decide_done - raw_receive_ns)
        self.latency.receive_to_reconcile.add(reconcile_done - raw_receive_ns)
        self.latency.prepare_duration.add(prepare_done - decide_done)
        self.latency.reconcile_duration.add(reconcile_done - prepare_done)

        acting = any(side.requires_request for side in plan.sides)
        cycle_ns = reconcile_done - raw_receive_ns
        if acting:
            self.latency.acting_cycle.add(cycle_ns)
        elif all(side.action is ReconcileAction.KEEP for side in plan.sides):
            # The common case, and the one that must cost nothing on the network.
            self.latency.keep_cycle.add(cycle_ns)

        healthy = self.pipeline.clob_health.status is HealthStatus.HEALTHY
        for side in plan.sides:
            self.counters.count_action(side.action.value)
            if side.live is not None:
                self.counters.cycles_with_live_order += 1
                if side.action is ReconcileAction.KEEP:
                    self.counters.keeps_with_live_order += 1
            if side.action in QUEUE_LOSS_ACTIONS:
                self.counters.count_queue_loss(side.reason.value)

            prepared_side = prepared.get(side.outcome)
            desired_price = None if prepared_side is None else prepared_side.submission_price
            displayed = ShareUnits(0)
            if desired_price is not None:
                displayed = self.pipeline.books.size_at(side.outcome, "bid", desired_price)
            estimate = self.shadow.on_desired(side.outcome, desired_price, displayed)

            classification = classify(
                action=side.action,
                side_reason=side.reason,
                desired_price=desired_price,
                resting_price=None if estimate is None else estimate.price,
                estimate=estimate,
                preparation=side.preparation,
                strategy_reason=self._strategy_reason(decision, side.outcome),
                continuity_healthy=healthy,
            )
            self.counters.count_quality(classification.quality.value, classification.reason.value)
            if estimate is not None and classification.queue_ahead is not None:
                self.latency.queue_ahead.add(int(classification.queue_ahead))

        self.counters.queue_slots_acquired = self.shadow.acquired
        self.counters.queue_slots_kept = self.shadow.kept

        forced = acting or event_kind in ("OwnFill", "OrderStateEvent", "PhaseEvent")
        if self.sampling.should_trace(
            ingress_ordinal=self.pipeline.merger.ordinal,
            event_kind=event_kind,
            forced=forced,
        ):
            self.sink.put(trace.snapshot())

    def _apply_shadow(self, plan: object) -> None:
        """Model venue acknowledgement so KEEP, REPLACE, and CANCEL can occur in shadow mode."""
        from maker5m.execution.reconciler import ReconcilePlan

        assert isinstance(plan, ReconcilePlan)
        for side in plan.sides:
            if side.action is ReconcileAction.PLACE and side.prepared is not None:
                self._shadow_seq += 1
                client_order_id = f"shadow-{self._shadow_seq:08d}"
                self.executor.orders.register_pending_place(
                    client_order_id=client_order_id,
                    outcome=side.outcome,
                    price=side.prepared.submission_price,
                    size=side.prepared.submission_size,
                    ingress_ordinal=self.cycles,
                )
                self.executor.orders.update(
                    client_order_id,
                    status=OrderLifecycle.LIVE,
                    venue_order_id=client_order_id,
                )
            elif side.action in QUEUE_LOSS_ACTIONS and side.live is not None:
                self.executor.orders.update(
                    side.live.client_order_id, status=OrderLifecycle.CANCELLED
                )

    def _strategy_reason(self, decision: DecisionResult, outcome: Outcome) -> QualityReason | None:
        """Recover *which* strategy gate suppressed a side, so NOT_QUOTING stays explanatory."""
        telemetry = decision.telemetry
        reasons = (
            telemetry.eligibility.up_reasons
            if outcome is Outcome.UP
            else telemetry.eligibility.down_reasons
        )
        if not reasons:
            return None
        return _STRATEGY_REASON.get(reasons[0].value)

    def summary(self) -> dict[str, object]:
        return {
            "cycles": self.cycles,
            "instrumentation_enabled": self.enabled,
            "sampling_every": self.sampling.sample_every,
            "latency_ns": self.latency.summary(),
            "counters": self.counters.summary(),
            "telemetry": {
                "buffered": len(self.sink),
                "accepted": self.sink.accepted,
                "dropped": self.sink.dropped,
            },
            "shadow": {
                "label": "SHADOW_ESTIMATE",
                "acquired": self.shadow.acquired,
                "kept": self.shadow.kept,
                "lost": self.shadow.lost,
            },
            "real_order_rtt": "UNRUN / P14",
        }
