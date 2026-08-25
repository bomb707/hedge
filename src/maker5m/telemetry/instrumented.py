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

Simulation, measurement, and emission are three different things
----------------------------------------------------------------
Keeping them apart is what makes both the OFF/ON benchmark and the sampling policy honest:

* **Simulation** — preparation, reconciliation, and the shadow order table — runs on every
  cycle whether or not instrumentation is enabled. Production performs this work regardless, so
  charging it to telemetry would overstate what measurement costs. An earlier benchmark did
  exactly that, twice, and reported +133% and +217% overheads that were mostly simulation.
* **Measurement state** — shadow queue slots, action counters — runs on every cycle of a
  measuring run, sampled or not. Skipping a slot transition because its event was not sampled
  would corrupt the state itself, not merely thin the output.
* **Emission** — stage timestamps written into a trace, distribution samples, classification,
  and the sink — runs only for traced cycles. This is the expensive part, and it is what
  sampling is for.
"""

from collections.abc import Callable
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
from maker5m.execution.reconciler import ReconcileAction, ReconcilePlan, SideReason
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
from maker5m.telemetry.shadow import ShadowLossReason, ShadowQueueTracker
from maker5m.telemetry.sink import TelemetrySink

__all__ = ["InstrumentedRun", "LatencyBook"]

QUEUE_LOSS_ACTIONS: Final = frozenset({ReconcileAction.REPLACE, ReconcileAction.CANCEL})
"""Reconciler actions that give up a live order's queue slot. KEEP is never one of them."""

_SHADOW_LOSS_REASON: Final[dict[SideReason, ShadowLossReason]] = {
    SideReason.PRICE_CHANGED: ShadowLossReason.PRICE_CHANGE,
    SideReason.SIZE_CHANGED: ShadowLossReason.SIZE_CHANGE,
    SideReason.DESIRED_WITHDRAWN: ShadowLossReason.DESIRED_WITHDRAWN,
    SideReason.UNSAFE_REPLACEMENT: ShadowLossReason.UNSAFE_REPLACEMENT,
}
"""Every reconciler reason that can close a slot maps to an explicit shadow reason."""

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

    cycle_observer: Callable[[str, bool, bool], None] | None = None
    """Optional ``(event_kind, acting, traced)`` hook, for the overhead benchmark only.

    Called only inside the measuring path, so the instrumentation-off configuration never pays
    for it. It exists so the benchmark can label each cycle's sampling tier from a separate
    pass rather than by guessing, and is ``None`` in every real run.
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
        """Run one shadow execution cycle, timing and classifying it when enabled."""
        self.cycles += 1
        measuring = self.enabled

        decide_done = perf_now_ns() if measuring else 0
        prepared = prepare_both_sides(decision, self.pipeline.merger.state, self.rules)
        prepare_done = perf_now_ns() if measuring else 0

        live = {
            Outcome.UP: self.executor.orders.current(Outcome.UP),
            Outcome.DOWN: self.executor.orders.current(Outcome.DOWN),
        }
        plan = reconcile(prepared, live)
        reconcile_done = perf_now_ns() if measuring else 0

        # Simulation, not measurement: it mutates only the executor's order table, which is
        # exactly the input reconcile() reads next cycle. Gating it on `enabled` would leave
        # the OFF run reconciling against an empty table while the ON run reconciled against a
        # populated one, and the resulting "overhead" would be simulation cost, not telemetry.
        placements = self._advance_orders(plan) if self.shadow_orders else (None, None)

        if not measuring:
            return

        up, down = plan.up, plan.down

        # -- measurement state: every cycle, sampled or not ------------------------------
        # Everything from here to the `traced` gate runs on an ordinary unsampled book update,
        # so it is kept to what state correctness actually requires. Skipping a slot transition
        # because its event was not sampled would corrupt the state, not merely thin the output
        # - a depth decrease missed here is a decrease the estimate never learns about.
        healthy = self.pipeline.clob_health.status is HealthStatus.HEALTHY
        if not healthy:
            self.shadow.invalidate()

        acting = False
        counters = self.counters
        books = self.pipeline.books
        shadow = self.shadow
        for side, placed_id in ((up, placements[0]), (down, placements[1])):
            action = side.action
            counters.count_action(action.value)
            resting = side.live
            if resting is not None:
                counters.cycles_with_live_order += 1
                if action is ReconcileAction.KEEP:
                    counters.keeps_with_live_order += 1
                    shadow.on_keep(
                        resting.client_order_id,
                        books.bid_size_at(side.outcome, resting.price),
                    )
                elif action in QUEUE_LOSS_ACTIONS:
                    acting = True
                    counters.count_execution_queue_loss(side.reason.value)
                    shadow.on_lost(
                        resting.client_order_id,
                        _SHADOW_LOSS_REASON.get(side.reason, ShadowLossReason.OTHER),
                    )
            elif placed_id is not None and side.prepared is not None:
                # Only a dispatched order opens a slot, at the depth displayed right now.
                acting = True
                price = side.prepared.submission_price
                shadow.on_place(
                    client_order_id=placed_id,
                    outcome=side.outcome,
                    price=price,
                    displayed_now=books.bid_size_at(side.outcome, price),
                )
            elif action in QUEUE_LOSS_ACTIONS:
                acting = True
                counters.count_execution_queue_loss(side.reason.value)
            elif action is ReconcileAction.PLACE:
                # Shadow order placement is switched off; the plan still acts.
                acting = True

        # `acting` falls out of the loop above rather than costing two more property calls:
        # every action that consumes network capacity is already inspected there.
        traced = acting or self.sampling.selects(self.pipeline.merger.ordinal, event_kind)
        if self.cycle_observer is not None:
            self.cycle_observer(event_kind, acting, traced)

        if not traced:
            return

        # -- emission: traced cycles only ------------------------------------------------
        merger = self.pipeline.merger
        if merger.last_decide_ns and merger.last_reduce_ns:
            self.latency.decide_duration.add(merger.last_decide_ns - merger.last_reduce_ns)

        self.latency.by_kind(event_kind).add(decide_done - raw_receive_ns)
        self.latency.receive_to_reconcile.add(reconcile_done - raw_receive_ns)
        self.latency.prepare_duration.add(prepare_done - decide_done)
        self.latency.reconcile_duration.add(reconcile_done - prepare_done)

        cycle_ns = reconcile_done - raw_receive_ns
        if acting:
            self.latency.acting_cycle.add(cycle_ns)
        elif all(side.action is ReconcileAction.KEEP for side in plan.sides):
            # The common case, and the one that must cost nothing on the network.
            self.latency.keep_cycle.add(cycle_ns)

        for side in plan.sides:
            prepared_side = prepared.get(side.outcome)
            desired_price = None if prepared_side is None else prepared_side.submission_price
            # The slot of the order that actually rests here, or None. AT_FRONT is not
            # reachable without one, which is the whole point of the correction.
            estimate = self.shadow.estimate(side.outcome)

            classification = classify(
                action=side.action,
                side_reason=side.reason,
                desired_price=desired_price,
                estimate=estimate,
                preparation=side.preparation,
                strategy_reason=self._strategy_reason(decision, side.outcome),
                continuity_healthy=healthy,
            )
            self.counters.count_quality(classification.quality.value, classification.reason.value)
            if classification.queue_ahead is not None:
                self.latency.queue_ahead.add(int(classification.queue_ahead))

        trace = self.trace
        trace.reset()
        trace.event_kind = event_kind
        trace.ingress_ordinal = merger.ordinal
        trace.set(Stage.RAW_RECEIVE, raw_receive_ns)
        trace.set(Stage.DECIDE_DONE, decide_done)
        trace.set(Stage.PREPARE_DONE, prepare_done)
        trace.set(Stage.RECONCILE_DONE, reconcile_done)
        self.sink.put(trace.snapshot())

    def _advance_orders(self, plan: ReconcilePlan) -> tuple[str | None, str | None]:
        """Model venue acknowledgement so KEEP, REPLACE, and CANCEL can occur in shadow mode.

        Returns the client order id placed on (UP, DOWN) this cycle, or ``None`` per side. A
        fixed pair rather than a mapping: this runs on every cycle, and the common answer is
        ``(None, None)``, which should not cost an allocation.
        """
        placed: list[str | None] = [None, None]
        for index, side in enumerate(plan.sides):
            action = side.action
            if action is ReconcileAction.PLACE and side.prepared is not None:
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
                placed[index] = client_order_id
            elif action in QUEUE_LOSS_ACTIONS and side.live is not None:
                # P7 policy is CANCEL_THEN_PLACE, so a REPLACE retires the order here and the
                # replacement is placed by a later cycle. The slot dies with the old order.
                self.executor.orders.update(
                    side.live.client_order_id, status=OrderLifecycle.CANCELLED
                )
        return placed[0], placed[1]

    def apply_shadow_fill(self, outcome: Outcome, filled: ShareUnits) -> None:
        """Model a fill against the shadow order resting on ``outcome``.

        Reaching the front is what a fill proves, so the estimate becomes zero. A *partial*
        fill leaves the order resting and keeps its slot — the case P7's reconciler protects by
        comparing remaining size, and therefore the case worth being able to measure.
        """
        order = self.executor.orders.current(outcome)
        if order is None:
            return
        remaining = ShareUnits(max(0, order.remaining_size - filled))
        self.executor.orders.update(
            order.client_order_id,
            status=OrderLifecycle.FILLED if remaining == 0 else OrderLifecycle.PARTIALLY_FILLED,
            remaining_size=remaining,
        )
        self.shadow.on_fill(order.client_order_id, fully_filled=remaining == 0)

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
            "shadow": self.shadow.summary(),
            "real_order_rtt": "UNRUN / P14",
        }
