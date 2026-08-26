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
from maker5m.execution.reconciler import ReconcileAction, ReconcilePlan
from maker5m.feeds.pipeline import MarketDataPipeline
from maker5m.feeds.venue import VenueMarketRules
from maker5m.market.events import HealthStatus
from maker5m.numeric.units import ShareUnits
from maker5m.strategy.decision import DecisionResult
from maker5m.strategy.engine import StrategyEngine
from maker5m.telemetry.analyzer import TelemetryAnalyzer
from maker5m.telemetry.latency import perf_now_ns
from maker5m.telemetry.observation import (
    DEFAULT_OBSERVATION_CAPACITY,
    NOT_CAPTURED,
    ObservationBuffer,
)
from maker5m.telemetry.sampling import SamplingPolicy

__all__ = ["InstrumentedRun"]

QUEUE_LOSS_ACTIONS: Final = frozenset({ReconcileAction.REPLACE, ReconcileAction.CANCEL})
"""Reconciler actions that give up a live order's queue slot. KEEP is never one of them."""


def _risk_snapshot(controller: object) -> tuple[object, ...] | None:
    """Four primitives describing what risk currently permits, or ``None`` if unattached.

    The values are P9's own recorded verdict, read from the last record it wrote. They are not
    re-derived from the risk state: "HALTED implies no placement" is a rule P9 owns, and a second
    copy of it here would be a second thing to get wrong — and would quietly disagree the moment
    P9's recovery semantics changed.

    Duck-typed and defensive on purpose. This runs on the hot path in a package that must not
    depend on the risk package, and a telemetry read must never be able to raise into a trading
    cycle — a missing attribute here would otherwise stop the bot to record a number about it.
    """
    if controller is None:
        return None
    try:
        records = controller.trace.records  # type: ignore[attr-defined]
        if not records:
            return None
        latest = records[-1]
        return (
            latest.risk_sequence,
            latest.state.value,
            latest.allows_place,
            latest.allows_cancel,
        )
    except AttributeError:
        return None


@dataclass(slots=True)
class InstrumentedRun:
    """Times and classifies one shadow decision cycle per ingested event."""

    pipeline: MarketDataPipeline
    engine: StrategyEngine
    rules: VenueMarketRules
    executor: Executor = field(
        default_factory=lambda: Executor(adapter=VenueAdapter(RecordingTransport()))
    )
    buffer: ObservationBuffer = field(
        default_factory=lambda: ObservationBuffer(DEFAULT_OBSERVATION_CAPACITY)
    )
    sampling: SamplingPolicy = field(default_factory=SamplingPolicy)
    enabled: bool = True
    """When ``False`` the harness runs the same decisions and records nothing.

    This is what makes the OFF/ON overhead comparison a like-for-like measurement.
    """

    cycles: int = 0
    _shadow_seq: int = 0
    _seq: int = -1
    """Pre-incremented, so the first captured observation is sequence 0.

    The analyzer starts expecting 0; an off-by-one here would make every run report a phantom
    dropped observation before it had captured anything.
    """
    shadow_orders: bool = True
    """Treat a shadow PLACE as immediately resting.

    Without this the order table stays empty forever, every cycle re-plans a PLACE, and the
    KEEP path - the single most important behaviour in the system - is never exercised or
    measured. A real venue would acknowledge the order, so the shadow run models that. The
    resulting KEEP figures describe *our strategy's* behaviour against real market data; they
    are not evidence about the target wallet.
    """

    risk: object | None = None
    """An optional P9 ``RiskController``, read for its current verdict at capture time.

    Read, never driven: P11 records what risk permitted and has no opinion about it. Held as
    ``object`` so the telemetry package does not import the risk package on the hot path.
    """

    def observe(
        self,
        event_kind: str,
        raw_receive_ns: int,
        decision: DecisionResult,
        source_timestamp_ns: int | None = None,
    ) -> None:
        """Run one shadow execution cycle and capture what it did. Nothing analytical.

        Preparation, reconciliation, and the shadow order table are **simulation**: production
        performs them every cycle, so they run whether or not instrumentation is enabled and
        they are not telemetry cost. Everything that used to follow them — slot mutation,
        counting, classification, distributions — now happens downstream in
        :class:`~maker5m.telemetry.analyzer.TelemetryAnalyzer`.

        What is left here is the irreducible part: the displayed depth at our own price, which
        the book will have moved past by the time anything downstream looks, plus stage
        timestamps for the cycles deterministic sampling selected.
        """
        self.cycles += 1
        measuring = self.enabled
        stages = measuring and self.pipeline.merger.stages_measured

        decide_done = perf_now_ns() if stages else NOT_CAPTURED
        prepared = prepare_both_sides(decision, self.pipeline.merger.state, self.rules)
        prepare_done = perf_now_ns() if stages else NOT_CAPTURED

        orders = self.executor.orders
        live = {
            Outcome.UP: orders.current(Outcome.UP),
            Outcome.DOWN: orders.current(Outcome.DOWN),
        }
        plan = reconcile(prepared, live)
        reconcile_done = perf_now_ns() if stages else NOT_CAPTURED

        if self.shadow_orders:
            up_placed, down_placed, acted = self._advance_orders(plan)
        else:
            up_placed, down_placed, acted = None, None, False

        if not measuring:
            return

        if acted and not stages:
            # An action whose triggering event was not sampled. Record that it happened and
            # when; its earlier stages stay NOT_CAPTURED rather than being invented.
            reconcile_done = perf_now_ns()

        # Depth at our own price, on both sides. This is the one measurement that cannot be
        # deferred: the book is mutable and will have moved by the time the analyzer runs.
        books = self.pipeline.books
        up_bids = books.up.bids
        down_bids = books.down.bids
        up, down = plan.up, plan.down

        resting = up.live
        if resting is not None:
            up_depth = up_bids.get(resting.price, 0)
        elif up_placed is not None and up.prepared is not None:
            up_depth = up_bids.get(up.prepared.submission_price, 0)
        else:
            up_depth = 0

        resting = down.live
        if resting is not None:
            down_depth = down_bids.get(resting.price, 0)
        elif down_placed is not None and down.prepared is not None:
            down_depth = down_bids.get(down.prepared.submission_price, 0)
        else:
            down_depth = 0

        merger = self.pipeline.merger
        state = merger.state
        risk = self.risk
        self._seq += 1
        self.buffer.capture(
            (
                self._seq,
                merger.ordinal,
                event_kind,
                self.pipeline.clob_health.status is HealthStatus.HEALTHY,
                raw_receive_ns,
                decide_done,
                prepare_done,
                reconcile_done,
                merger.last_reduce_ns,
                merger.last_decide_ns,
                plan,
                up_depth,
                down_depth,
                up_placed,
                down_placed,
                decision.telemetry.eligibility,
                None,
                # -- P11: references to values already built, all immutable ------------------
                decision.telemetry,
                state.book,
                state.spot,
                state.last_event_timestamp,
                source_timestamp_ns,
                _risk_snapshot(risk),
            )
        )

    def _advance_orders(self, plan: ReconcilePlan) -> tuple[str | None, str | None, bool]:
        """Model venue acknowledgement so KEEP, REPLACE, and CANCEL can occur in shadow mode.

        Returns the client order id placed on (UP, DOWN) and whether the plan acted at all.
        ``acted`` falls out of a loop that already inspects every action, so knowing it costs
        nothing extra — and it is what lets an unsampled action still be timed.
        """
        placed: list[str | None] = [None, None]
        acted = False
        for index, side in enumerate(plan.sides):
            action = side.action
            if action is ReconcileAction.PLACE and side.prepared is not None:
                acted = True
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
                acted = True
                self.executor.orders.update(
                    side.live.client_order_id, status=OrderLifecycle.CANCELLED
                )
            elif action in QUEUE_LOSS_ACTIONS or action is ReconcileAction.PLACE:
                acted = True
        return placed[0], placed[1], acted

    def apply_shadow_fill(self, outcome: Outcome, filled: ShareUnits) -> None:
        """Model a fill against the shadow order resting on ``outcome``.

        Reaching the front is what a fill proves, so the estimate becomes zero. A *partial*
        fill leaves the order resting and keeps its slot — the case P7's reconciler protects by
        comparing remaining size, and therefore the case worth being able to measure. The fill
        enters the observation stream in order, so the analyzer sees it exactly where it
        happened.
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
        if not self.enabled:
            return
        self._seq += 1
        self.buffer.capture(
            (
                self._seq,
                self.pipeline.merger.ordinal,
                "ShadowFill",
                True,
                NOT_CAPTURED,
                NOT_CAPTURED,
                NOT_CAPTURED,
                NOT_CAPTURED,
                NOT_CAPTURED,
                NOT_CAPTURED,
                None,
                0,
                0,
                None,
                None,
                None,
                (outcome, order.client_order_id, remaining == 0),
            )
        )

    def analyze(self) -> TelemetryAnalyzer:
        """Fold the captured stream into the full measurement. Off the trading path.

        Idempotent within a run: the buffer is not drained, so this can be called again and
        will produce the same answer from the same observations.
        """
        analyzer = TelemetryAnalyzer(sampling=self.sampling)
        analyzer.run(self.buffer)
        return analyzer

    def summary(self) -> dict[str, object]:
        """The full measurement. Runs the downstream analyzer; never called on the hot path."""
        analyzer = self.analyze()
        return {
            "cycles": self.cycles,
            "instrumentation_enabled": self.enabled,
            "sampling_every": self.sampling.sample_every,
            "observations": {
                "capacity": self.buffer.capacity,
                "accepted": self.buffer.accepted,
                "buffered": len(self.buffer),
                "dropped": self.buffer.dropped,
                "gaps_seen_downstream": analyzer.gaps,
                "lost_downstream": analyzer.lost_observations,
            },
            "latency_ns": analyzer.latency.summary(),
            "counters": analyzer.counters.summary(),
            "shadow": analyzer.shadow.summary(),
            "cycles_with_stage_timing": analyzer.stages_captured,
            "real_order_rtt": "UNRUN / P14",
        }
