"""Reconstruct every P8 measurement from the captured observation stream.

This is the half of P8 that used to run on the trading path. It is pure analysis: shadow queue
slots, execution-quality classification, action counting, latency distributions. Nothing here
touches a decision, an order, or any deterministic state, and nothing here has to finish before
the next market event can be handled.

Determinism
-----------
The ingress ordinal is the authoritative order, and observations are processed strictly in
capture sequence. The same ordered observations always produce identical slots, estimates,
classifications, and counts — that is what makes running this after the market instead of
during it a *relocation* rather than a change of meaning, and it is asserted by test against
the synchronous model it replaces.

Out-of-order input **fails closed** rather than being sorted into shape: a telemetry stream
that arrived out of order is a stream whose provenance is unknown, and quietly repairing it
would manufacture confidence.

Gaps
----
A missing capture sequence means an observation was dropped. The queue estimate depends on
having seen *every* depth change at our own price, so a gap makes the estimate unreconstructable
from that point. It is marked ``STALE`` rather than bridged. Trading was unaffected — the drop
happened in observation, not execution — but the measurement must say so.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final, NamedTuple

from maker5m.domain import Outcome
from maker5m.execution.reconciler import ReconcileAction, ReconcilePlan, SideAction, SideReason
from maker5m.numeric.units import ShareUnits
from maker5m.strategy.eligibility import EligibilityResult
from maker5m.telemetry.classifier import QualityReason, QuoteClassification, classify
from maker5m.telemetry.metrics import ActionCounters, Distribution
from maker5m.telemetry.observation import (
    NOT_CAPTURED,
    OBS_DECIDE_DONE_NS,
    OBS_DECIDE_STAGE_NS,
    OBS_DOWN_DEPTH,
    OBS_DOWN_PLACED_ID,
    OBS_ELIGIBILITY,
    OBS_EVENT_KIND,
    OBS_EVENT_TS,
    OBS_FILL,
    OBS_HEALTHY,
    OBS_INGRESS_ORDINAL,
    OBS_PLAN,
    OBS_PREPARE_DONE_NS,
    OBS_RAW_RECEIVE_NS,
    OBS_RECONCILE_DONE_NS,
    OBS_REDUCE_STAGE_NS,
    OBS_SEQ,
    OBS_TELEMETRY,
    OBS_UP_DEPTH,
    OBS_UP_PLACED_ID,
    Observation,
)
from maker5m.telemetry.queue_estimate import QueueConfidence
from maker5m.telemetry.sampling import SamplingPolicy
from maker5m.telemetry.shadow import ShadowLossReason, ShadowQueueTracker

__all__ = ["LatencyBook", "TelemetryAnalyzer", "TelemetryOrderError"]

QUEUE_LOSS_ACTIONS: Final = frozenset({ReconcileAction.REPLACE, ReconcileAction.CANCEL})
"""Reconciler actions that give up a live order's queue slot. KEEP is never one of them."""

_SHADOW_LOSS_REASON: Final[dict[SideReason, ShadowLossReason]] = {
    SideReason.PRICE_CHANGED: ShadowLossReason.PRICE_CHANGE,
    SideReason.SIZE_CHANGED: ShadowLossReason.SIZE_CHANGE,
    SideReason.DESIRED_WITHDRAWN: ShadowLossReason.DESIRED_WITHDRAWN,
    SideReason.UNSAFE_REPLACEMENT: ShadowLossReason.UNSAFE_REPLACEMENT,
}

_STRATEGY_REASON: Final[dict[str, QualityReason]] = {
    "PHASE_NOT_QUOTING": QualityReason.PHASE_NOT_QUOTING,
    "CENTRE_UNAVAILABLE": QualityReason.CENTRE_UNAVAILABLE,
    "ENDGAME_GATE": QualityReason.ENDGAME_GATE,
    "HARD_BAND": QualityReason.HARD_BAND,
}


class TelemetryOrderError(RuntimeError):
    """Observations arrived out of capture order. Fail closed; never reorder silently."""


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
    """Every triggering kind together. Kept for continuity with the accepted P8 evidence."""

    spot_receive_to_reconcile: Distribution = field(
        default_factory=lambda: Distribution("spot_receive_to_reconcile")
    )
    clob_receive_to_reconcile: Distribution = field(
        default_factory=lambda: Distribution("clob_receive_to_reconcile")
    )
    fill_receive_to_reconcile: Distribution = field(
        default_factory=lambda: Distribution("fill_receive_to_reconcile")
    )
    phase_receive_to_reconcile: Distribution = field(
        default_factory=lambda: Distribution("phase_receive_to_reconcile")
    )
    """Split by trigger, because a spot tick and a book update are not the same cycle.

    Added downstream, from timestamps the observation already carries — no clock is read on
    Plane 1 and the existing aggregate keeps its meaning. A merged CLOB-and-spot p99 answers a
    question nobody asked: the two arrive at different rates, through different sockets, and
    doing different work."""

    decide_duration: Distribution = field(default_factory=lambda: Distribution("decide_duration"))
    prepare_duration: Distribution = field(default_factory=lambda: Distribution("prepare_duration"))
    reconcile_duration: Distribution = field(
        default_factory=lambda: Distribution("reconcile_duration")
    )
    keep_cycle: Distribution = field(default_factory=lambda: Distribution("keep_cycle"))
    acting_cycle: Distribution = field(default_factory=lambda: Distribution("acting_cycle"))
    queue_ahead: Distribution = field(default_factory=lambda: Distribution("queue_ahead"))

    _by_kind: dict[str, Distribution] = field(default_factory=dict, repr=False)

    _by_kind_reconcile: dict[str, Distribution] = field(default_factory=dict, repr=False)

    def by_kind(self, event_kind: str) -> Distribution:
        if not self._by_kind:
            self._by_kind = {
                "SpotTick": self.spot_receive_to_decide,
                "BookUpdate": self.clob_receive_to_decide,
                "OwnFill": self.fill_receive_to_decide,
                "PhaseEvent": self.phase_receive_to_decide,
            }
        return self._by_kind.get(event_kind, self.clob_receive_to_decide)

    def by_kind_reconcile(self, event_kind: str) -> Distribution:
        if not self._by_kind_reconcile:
            self._by_kind_reconcile = {
                "SpotTick": self.spot_receive_to_reconcile,
                "BookUpdate": self.clob_receive_to_reconcile,
                "OwnFill": self.fill_receive_to_reconcile,
                "PhaseEvent": self.phase_receive_to_reconcile,
            }
        return self._by_kind_reconcile.get(event_kind, self.clob_receive_to_reconcile)

    def summary(self) -> dict[str, object]:
        return {
            d.label: d.summary()
            for d in (
                self.spot_receive_to_decide,
                self.clob_receive_to_decide,
                self.fill_receive_to_decide,
                self.phase_receive_to_decide,
                self.receive_to_reconcile,
                self.spot_receive_to_reconcile,
                self.clob_receive_to_reconcile,
                self.fill_receive_to_reconcile,
                self.phase_receive_to_reconcile,
                self.decide_duration,
                self.prepare_duration,
                self.reconcile_duration,
                self.keep_cycle,
                self.acting_cycle,
                self.queue_ahead,
            )
        }


class ClassificationMode(Enum):
    """How much of the stream gets an L3 classification."""

    SAMPLED_OR_ACTING = "SAMPLED_OR_ACTING"
    """P8's accepted behaviour: cycles that acted or were latency-sampled."""

    EVERY_DECISION = "EVERY_DECISION"
    """Both sides of every decision observation. What Canonical §34-L3 asks for."""


class QuoteEvent(NamedTuple):
    """One side's classification, with the coordinates a corpus needs to bucket it.

    A NamedTuple for the reason P8 already established for its hot types: construction is 76 ns
    against 1,791 ns for a frozen dataclass, and this is built twice per classified cycle.
    """

    ingress_ordinal: Any
    event_kind: Any
    event_timestamp_ns: Any
    phase: str | None
    outcome: str
    classification: QuoteClassification


def _phase_of(observation: Observation) -> str | None:
    """The phase the decision recorded, or ``None``. Never inferred from a clock."""
    telemetry = observation[OBS_TELEMETRY]
    phase = getattr(telemetry, "phase", None)
    return None if phase is None else str(getattr(phase, "value", phase))


@dataclass(slots=True)
class TelemetryAnalyzer:
    """Rebuilds the whole P8 measurement from observations, in order."""

    sampling: SamplingPolicy = field(default_factory=SamplingPolicy)
    """The policy that produced this stream, recorded for provenance.

    Deliberately **not** re-applied: whether a cycle was sampled is read from the observation
    itself. Recomputing it here would be a second opinion about a decision already made, and
    the two can drift.
    """

    shadow: ShadowQueueTracker = field(default_factory=ShadowQueueTracker)
    counters: ActionCounters = field(default_factory=ActionCounters)
    latency: LatencyBook = field(default_factory=LatencyBook)

    classification_mode: ClassificationMode = ClassificationMode.SAMPLED_OR_ACTING
    """Which cycles get an L3 classification. Latency sampling is unaffected either way.

    `SAMPLED_OR_ACTING` is P8's accepted behaviour and stays the default, so every existing
    measurement means exactly what it meant. `EVERY_DECISION` is P13's: both sides of every
    decision observation, so the L3 denominator is the market rather than a sample of it.

    Classification happens **once** per observation in both modes. The shadow queue state
    advances once per observation regardless, before either branch, because it is the model of
    where our order sits and cannot be allowed to depend on who is counting.
    """

    on_quote: Callable[[QuoteEvent], None] | None = field(default=None, repr=False)
    """Optional Plane-3 observer of every classification this analyzer makes.

    Added for P13, which needs the L3 distribution broken down by side, phase and time rather
    than as one market-wide total — and must not answer that with a second classifier. The
    classification handed out here is the same object `count_quality` receives; nothing is
    recomputed, and with no observer attached this costs one `is None` per side.
    """

    processed: int = 0
    gaps: int = 0
    lost_observations: int = 0
    stages_captured: int = 0
    _expected_seq: int = 0

    def run(self, observations: Iterable[Observation]) -> "TelemetryAnalyzer":
        for observation in observations:
            self.process(observation)
        return self

    def process(self, observation: Observation) -> None:
        """Fold one observation. Order is authoritative; gaps invalidate, never bridge."""
        seq = observation[OBS_SEQ]
        assert isinstance(seq, int)
        if seq < self._expected_seq:
            raise TelemetryOrderError(
                f"observation {seq} arrived after {self._expected_seq - 1}; the telemetry "
                "stream is out of order and will not be silently reordered"
            )
        if seq > self._expected_seq:
            # Observations were dropped. Depth changes at our own price went unseen, so the
            # queue estimate cannot be continued across the gap.
            self.gaps += 1
            self.lost_observations += seq - self._expected_seq
            self.shadow.invalidate(QueueConfidence.STALE)
        self._expected_seq = seq + 1
        self.processed += 1

        fill = observation[OBS_FILL]
        if fill is not None:
            assert isinstance(fill, tuple)
            client_order_id, fully_filled = fill[1], fill[2]
            assert isinstance(client_order_id, str) and isinstance(fully_filled, bool)
            self.shadow.on_fill(client_order_id, fully_filled=fully_filled)
            return

        plan = observation[OBS_PLAN]
        assert isinstance(plan, ReconcilePlan)
        healthy = observation[OBS_HEALTHY]
        assert isinstance(healthy, bool)
        if not healthy:
            self.shadow.invalidate()

        acting = self._advance_slots(
            plan,
            observation[OBS_UP_DEPTH],
            observation[OBS_DOWN_DEPTH],
            observation[OBS_UP_PLACED_ID],
            observation[OBS_DOWN_PLACED_ID],
        )

        event_kind = observation[OBS_EVENT_KIND]
        assert isinstance(event_kind, str)
        # Whether this cycle was sampled is a *captured fact*, not something to recompute.
        # Re-deriving it downstream from the ingress ordinal was an off-by-one waiting to
        # happen, and it happened: `next_meta` assigns an ordinal and then increments, so the
        # hot path sampled on N while the analyzer traced on N+1 and the two decisions almost
        # never agreed. A full real market produced stage timings for 126 cycles instead of
        # ~15,400. There is now one source of truth and no second opinion.
        sampled = observation[OBS_DECIDE_DONE_NS] != NOT_CAPTURED
        if acting or sampled:
            self._record_latency(observation, event_kind, plan, acting)
            self._classify(observation, plan, healthy)
        elif self.classification_mode is ClassificationMode.EVERY_DECISION:
            # Classification is not a measurement of *this run's* clock, so it has no reason to
            # follow the latency sampler. Canonical §34-L3 asks what fraction of quote
            # opportunities sat at the front, and a fraction whose denominator is "every acting
            # cycle plus one in ten of the others" answers a different question: acting cycles
            # are exactly the ones where an order was being placed or replaced, so sampling that
            # way over-weights the moments the queue position was worst.
            self._classify(observation, plan, healthy)

    # -- queue state -----------------------------------------------------------------------

    def _advance_slots(
        self,
        plan: ReconcilePlan,
        up_depth: object,
        down_depth: object,
        up_placed: object,
        down_placed: object,
    ) -> bool:
        """Exactly the transitions the synchronous model performed, in the same order."""
        acting = False
        counters = self.counters
        shadow = self.shadow
        for side, depth, placed_id in (
            (plan.up, up_depth, up_placed),
            (plan.down, down_depth, down_placed),
        ):
            assert isinstance(depth, int)
            action = side.action
            counters.count_action(action.value)
            resting = side.live
            if resting is not None:
                counters.cycles_with_live_order += 1
                if action is ReconcileAction.KEEP:
                    counters.keeps_with_live_order += 1
                    shadow.on_keep(resting.client_order_id, ShareUnits(depth))
                elif action in QUEUE_LOSS_ACTIONS:
                    acting = True
                    counters.count_execution_queue_loss(side.reason.value)
                    shadow.on_lost(
                        resting.client_order_id,
                        _SHADOW_LOSS_REASON.get(side.reason, ShadowLossReason.OTHER),
                    )
            elif placed_id is not None and side.prepared is not None:
                # Only a dispatched order opens a slot, at the depth displayed at dispatch.
                acting = True
                assert isinstance(placed_id, str)
                shadow.on_place(
                    client_order_id=placed_id,
                    outcome=side.outcome,
                    price=side.prepared.submission_price,
                    displayed_now=ShareUnits(depth),
                )
            elif action in QUEUE_LOSS_ACTIONS:
                acting = True
                counters.count_execution_queue_loss(side.reason.value)
            elif action is ReconcileAction.PLACE:
                acting = True
        return acting

    # -- emission --------------------------------------------------------------------------

    def _record_latency(
        self, observation: Observation, event_kind: str, plan: ReconcilePlan, acting: bool
    ) -> None:
        """Only from stages that were actually sampled. ``NOT_CAPTURED`` is never imputed."""
        raw = observation[OBS_RAW_RECEIVE_NS]
        decide_done = observation[OBS_DECIDE_DONE_NS]
        prepare_done = observation[OBS_PREPARE_DONE_NS]
        reconcile_done = observation[OBS_RECONCILE_DONE_NS]
        assert isinstance(raw, int) and isinstance(decide_done, int)
        assert isinstance(prepare_done, int) and isinstance(reconcile_done, int)
        if decide_done == NOT_CAPTURED or reconcile_done == NOT_CAPTURED:
            # An acting cycle whose triggering event was not sampled. The action is still
            # counted and classified above; its intermediate stages simply do not exist, and
            # inventing them would corrupt the distribution they went into.
            return
        self.stages_captured += 1

        reduce_ns = observation[OBS_REDUCE_STAGE_NS]
        decide_ns = observation[OBS_DECIDE_STAGE_NS]
        assert isinstance(reduce_ns, int) and isinstance(decide_ns, int)
        if reduce_ns != NOT_CAPTURED and decide_ns != NOT_CAPTURED:
            self.latency.decide_duration.add(decide_ns - reduce_ns)

        self.latency.by_kind(event_kind).add(decide_done - raw)
        self.latency.receive_to_reconcile.add(reconcile_done - raw)
        self.latency.by_kind_reconcile(event_kind).add(reconcile_done - raw)
        self.latency.prepare_duration.add(prepare_done - decide_done)
        self.latency.reconcile_duration.add(reconcile_done - prepare_done)

        cycle_ns = reconcile_done - raw
        if acting:
            self.latency.acting_cycle.add(cycle_ns)
        elif plan.up.action is ReconcileAction.KEEP and plan.down.action is ReconcileAction.KEEP:
            # The common case, and the one that must cost nothing on the network.
            self.latency.keep_cycle.add(cycle_ns)

    def _classify(self, observation: Observation, plan: ReconcilePlan, healthy: bool) -> None:
        eligibility = observation[OBS_ELIGIBILITY]
        for side in (plan.up, plan.down):
            prepared = side.prepared
            desired_price = None if prepared is None else prepared.submission_price
            estimate = self.shadow.estimate(side.outcome)
            classification = classify(
                action=side.action,
                side_reason=side.reason,
                desired_price=desired_price,
                estimate=estimate,
                preparation=side.preparation,
                strategy_reason=_strategy_reason(eligibility, side),
                continuity_healthy=healthy,
            )
            self.counters.count_quality(classification.quality.value, classification.reason.value)
            if classification.queue_ahead is not None:
                self.latency.queue_ahead.add(int(classification.queue_ahead))
            if self.on_quote is not None:
                self.on_quote(
                    QuoteEvent(
                        observation[OBS_INGRESS_ORDINAL],
                        observation[OBS_EVENT_KIND],
                        observation[OBS_EVENT_TS],
                        _phase_of(observation),
                        side.outcome.value,
                        classification,
                    )
                )

    # -- results ---------------------------------------------------------------------------

    def summary(self) -> dict[str, object]:
        return {
            "classification_mode": self.classification_mode.value,
            "observations_processed": self.processed,
            "observation_gaps": self.gaps,
            "observations_lost": self.lost_observations,
            "cycles_with_stage_timing": self.stages_captured,
            "latency_ns": self.latency.summary(),
            "counters": self.counters.summary(),
            "shadow": self.shadow.summary(),
        }


def _strategy_reason(eligibility: object, side: SideAction) -> QualityReason | None:
    """Recover *which* strategy gate suppressed a side, so NOT_QUOTING stays explanatory."""
    if eligibility is None:
        return None
    assert isinstance(eligibility, EligibilityResult)
    reasons = eligibility.up_reasons if side.outcome is Outcome.UP else eligibility.down_reasons
    if not reasons:
        return None
    return _STRATEGY_REASON.get(reasons[0].value)
