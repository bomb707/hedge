"""The execution loop: plan, rate-limit, dispatch.

Composes the pure pieces. The impure part is deliberately thin — everything worth reasoning
about (preparation, the post-only guard, reconciliation, replacement staleness, rate budget)
is a pure function tested without a clock or a socket.

Dispatch order does not affect the plan. UP and DOWN are independent once the plan exists, so
they may be dispatched concurrently; nothing here serialises them merely because it is easier
to write. Whether individual requests or a batch endpoint is faster is a P8 measurement, not a
strategy claim, so no batch preference is hard-coded.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from maker5m.domain import Outcome
from maker5m.execution.adapter import VenueAdapter
from maker5m.execution.live_orders import LiveOrder, LiveOrderTable, OrderLifecycle
from maker5m.execution.prepare import PreparedOrder, prepare_order
from maker5m.execution.rate_limit import RateDecision, RequestClass, TokenBucket
from maker5m.execution.reconciler import (
    ReconcileAction,
    ReconcilePlan,
    SideAction,
    reconcile,
)
from maker5m.execution.replacement import PendingReplacement, ReplacementTracker
from maker5m.execution.telemetry import ExecutionRecord
from maker5m.feeds.venue import VenueMarketRules
from maker5m.market.state import MarketState
from maker5m.market.timebase import TimestampNs
from maker5m.strategy.decision import DecisionResult

__all__ = ["ExecutionCycle", "Executor", "prepare_both_sides"]


def prepare_both_sides(
    decision: DecisionResult, state: MarketState, rules: VenueMarketRules
) -> dict[Outcome, PreparedOrder | None]:
    """Interpret both desired orders for the venue. Pure.

    Each side's passivity is proven against **its own** observed ask. The DOWN ask is never
    inferred from the UP book (I06, Canonical §5.2).
    """
    book = state.book
    definition = state.definition
    prepared: dict[Outcome, PreparedOrder | None] = {}
    for outcome, desired in (
        (Outcome.UP, decision.orders.up),
        (Outcome.DOWN, decision.orders.down),
    ):
        if desired is None:
            prepared[outcome] = None
            continue
        ask = None
        if book is not None:
            ask = book.up_ask if outcome is Outcome.UP else book.down_ask
        prepared[outcome] = prepare_order(
            desired,
            token_id=definition.token_id(outcome),
            venue_tick=rules.min_tick_size,
            min_order_size=rules.min_order_size,
            observed_ask=ask,
        )
    return prepared


@dataclass(frozen=True, slots=True)
class ExecutionCycle:
    """What one reconcile-and-dispatch cycle decided and did."""

    plan: ReconcilePlan
    records: tuple[ExecutionRecord, ...]

    def record_for(self, outcome: Outcome) -> ExecutionRecord:
        return next(r for r in self.records if r.outcome is outcome)


@dataclass(slots=True)
class Executor:
    """Turns a decision into venue requests, or into a recorded reason for not acting."""

    adapter: VenueAdapter
    orders: LiveOrderTable = field(default_factory=LiveOrderTable)
    bucket: TokenBucket = field(default_factory=TokenBucket)
    replacements: ReplacementTracker = field(default_factory=ReplacementTracker)
    generation: int = 0
    _next_id: int = 0

    def _client_order_id(self, outcome: Outcome) -> str:
        self._next_id += 1
        return f"m5m-{outcome.value.lower()}-{self._next_id:08d}"

    def plan_cycle(
        self, decision: DecisionResult, state: MarketState, rules: VenueMarketRules
    ) -> ReconcilePlan:
        """Pure planning step, exposed separately so it can be tested without dispatch."""
        prepared = prepare_both_sides(decision, state, rules)
        live: Mapping[Outcome, LiveOrder | None] = {
            Outcome.UP: self.orders.current(Outcome.UP),
            Outcome.DOWN: self.orders.current(Outcome.DOWN),
        }
        return reconcile(prepared, live)

    def run_cycle(
        self,
        decision: DecisionResult,
        state: MarketState,
        rules: VenueMarketRules,
        now_ns: TimestampNs,
    ) -> ExecutionCycle:
        """Plan and dispatch one cycle. ``now_ns`` is supplied, never read here."""
        self.generation += 1
        plan = self.plan_cycle(decision, state, rules)
        records = [self._dispatch(side, now_ns) for side in plan.sides]
        return ExecutionCycle(plan=plan, records=tuple(records))

    def _dispatch(self, side: SideAction, now_ns: TimestampNs) -> ExecutionRecord:
        prepared = side.prepared
        live = side.live
        base = ExecutionRecord(
            outcome=side.outcome,
            action=side.action,
            reason=side.reason,
            client_order_id=None if live is None else live.client_order_id,
            venue_order_id=None if live is None else live.venue_order_id,
            strategy_price=None if prepared is None else prepared.strategy_price,
            strategy_size=None if prepared is None else prepared.strategy_size,
            submitted_price=None if prepared is None else prepared.submission_price,
            submitted_size=None if prepared is None else prepared.submission_size,
            size_quantization_delta=(
                None if prepared is None else prepared.size_quantization_delta
            ),
            live_price=None if live is None else live.price,
            live_remaining=None if live is None else live.remaining_size,
            post_only_guard=side.preparation,
            supersedes_client_order_id=None if live is None else live.supersedes,
        )

        if not side.requires_request:
            # KEEP is the point of the exercise: no request, no lost queue position.
            # NOTHING, WAIT, and BLOCKED are equally quiet, for their own reasons.
            return base

        request = (
            RequestClass.CANCEL
            if side.action in (ReconcileAction.CANCEL, ReconcileAction.REPLACE)
            else RequestClass.PLACE
        )
        rate_decision = self.bucket.acquire(request, now_ns)
        if rate_decision is RateDecision.DEFERRED:
            # Suppressed, never delayed behind a sleep. The next cycle re-decides.
            return replace(base, rate_decision=rate_decision)

        if side.action is ReconcileAction.PLACE and prepared is not None:
            client_order_id = self._client_order_id(side.outcome)
            self.orders.register_pending_place(
                client_order_id=client_order_id,
                outcome=side.outcome,
                price=prepared.submission_price,
                size=prepared.submission_size,
                ingress_ordinal=self.generation,
            )
            self.adapter.place(prepared)
            return replace(base, client_order_id=client_order_id, rate_decision=rate_decision)

        if live is None:
            return replace(base, rate_decision=rate_decision)

        # CANCEL and REPLACE both begin with a cancel. Replacement is CANCEL_THEN_PLACE, so
        # the new order is placed only once the cancel is acknowledged and still current.
        self.orders.update(live.client_order_id, status=OrderLifecycle.PENDING_CANCEL)
        if live.venue_order_id is not None:
            self.adapter.cancel(live.venue_order_id)
        if side.action is ReconcileAction.REPLACE and prepared is not None:
            self.replacements.record(
                PendingReplacement(
                    outcome=side.outcome,
                    cancelling_client_order_id=live.client_order_id,
                    target=prepared,
                    decision_generation=self.generation,
                )
            )
        return replace(base, rate_decision=rate_decision)
