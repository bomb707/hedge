"""The execution loop: plan, reserve, dispatch.

Composes the pure pieces. The impure part is deliberately thin — everything worth reasoning
about (preparation, the post-only guard, reconciliation, replacement staleness, rate budget)
is a pure function tested without a clock or a socket.

Dispatch order does not affect the plan, and once the plan exists UP and DOWN are independent.
:meth:`Executor.run_cycle_async` therefore dispatches them **concurrently**: serialising one
behind the other adds a full round trip to whichever side goes second, and queue position is
decided in exactly that window (Canonical §10.1).

Reservation happens before any await
------------------------------------
Client order ids, ``PENDING_PLACE`` / ``PENDING_CANCEL`` state, and rate-limiter capacity are
all taken **synchronously**, before control returns to the event loop. If reservation happened
after the await, another cycle could observe no in-flight request and create a duplicate
order. The reserve step is therefore an ordinary function with no suspension point in it.

Concurrency is across independent outcomes, never within a replacement chain. A side's
replacement stays ``CANCEL_THEN_PLACE``: this cycle issues the cancel, and the placement waits
for the authoritative acknowledgement and a fresh reconciliation against *current* desired
state.

Whether individual requests or a batch endpoint is faster is a P8 measurement, not a strategy
claim, so no batch preference is hard-coded.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from maker5m.domain import Outcome
from maker5m.execution.adapter import AsyncVenueAdapter, VenueAdapter
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

__all__ = ["ExecutionCycle", "Executor", "ReservedRequest", "prepare_both_sides"]


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
class ReservedRequest:
    """A request whose state and rate capacity are already taken, awaiting dispatch.

    Produced synchronously so that nothing can observe an unreserved gap, then dispatched
    concurrently with its counterpart on the other outcome.
    """

    outcome: Outcome
    kind: RequestClass
    prepared: PreparedOrder | None = None
    venue_order_id: str | None = None
    client_order_id: str | None = None


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
        """Plan and dispatch one cycle **sequentially**. Test support, not production.

        Retained because a synchronous double makes the planning and state transitions easy to
        exercise. It must never be wired into production: it serialises the two outcomes, which
        adds a round trip to whichever side goes second. Production uses
        :meth:`run_cycle_async`, and ``Executor.adapter`` being a synchronous
        :class:`~maker5m.execution.adapter.VenueAdapter` is what keeps the two paths from being
        confused.
        """
        self.generation += 1
        plan = self.plan_cycle(decision, state, rules)
        records = [self._dispatch(side, now_ns) for side in plan.sides]
        return ExecutionCycle(plan=plan, records=tuple(records))

    def _dispatch(self, side: SideAction, now_ns: TimestampNs) -> ExecutionRecord:
        prepared = side.prepared
        live = side.live
        base = self._base_record(side)

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

    # -- the production async path ----------------------------------------------------------

    def reserve(
        self, plan: ReconcilePlan, now_ns: TimestampNs
    ) -> tuple[list[ReservedRequest], dict[Outcome, ExecutionRecord]]:
        """Take every identity, state transition, and rate token **before** any await.

        Synchronous by construction: there is no suspension point between deciding to act and
        recording that we are acting, so a concurrent cycle can never observe a gap and issue a
        duplicate.
        """
        reserved: list[ReservedRequest] = []
        records: dict[Outcome, ExecutionRecord] = {}

        for side in plan.sides:
            base = self._base_record(side)
            if not side.requires_request:
                records[side.outcome] = base
                continue

            request = (
                RequestClass.CANCEL
                if side.action in (ReconcileAction.CANCEL, ReconcileAction.REPLACE)
                else RequestClass.PLACE
            )
            rate_decision = self.bucket.acquire(request, now_ns)
            if rate_decision is RateDecision.DEFERRED:
                # Suppressed, never delayed behind a sleep. The next cycle re-decides.
                records[side.outcome] = replace(base, rate_decision=rate_decision)
                continue

            prepared = side.prepared
            live = side.live

            if side.action is ReconcileAction.PLACE and prepared is not None:
                client_order_id = self._client_order_id(side.outcome)
                self.orders.register_pending_place(
                    client_order_id=client_order_id,
                    outcome=side.outcome,
                    price=prepared.submission_price,
                    size=prepared.submission_size,
                    ingress_ordinal=self.generation,
                )
                reserved.append(
                    ReservedRequest(
                        outcome=side.outcome,
                        kind=RequestClass.PLACE,
                        prepared=prepared,
                        client_order_id=client_order_id,
                    )
                )
                records[side.outcome] = replace(
                    base, client_order_id=client_order_id, rate_decision=rate_decision
                )
                continue

            if live is None:
                records[side.outcome] = replace(base, rate_decision=rate_decision)
                continue

            self.orders.update(live.client_order_id, status=OrderLifecycle.PENDING_CANCEL)
            if side.action is ReconcileAction.REPLACE and prepared is not None:
                # Within one side replacement stays CANCEL_THEN_PLACE: the placement waits for
                # the authoritative acknowledgement and a fresh reconciliation.
                self.replacements.record(
                    PendingReplacement(
                        outcome=side.outcome,
                        cancelling_client_order_id=live.client_order_id,
                        target=prepared,
                        decision_generation=self.generation,
                    )
                )
            if live.venue_order_id is not None:
                reserved.append(
                    ReservedRequest(
                        outcome=side.outcome,
                        kind=RequestClass.CANCEL,
                        venue_order_id=live.venue_order_id,
                        client_order_id=live.client_order_id,
                    )
                )
            records[side.outcome] = replace(base, rate_decision=rate_decision)

        return reserved, records

    async def run_cycle_async(
        self,
        adapter: AsyncVenueAdapter,
        decision: DecisionResult,
        state: MarketState,
        rules: VenueMarketRules,
        now_ns: TimestampNs,
    ) -> ExecutionCycle:
        """Plan, reserve, then dispatch independent outcome requests **concurrently**.

        Completion order is irrelevant to the deterministic core: authenticated order and fill
        events re-enter through the P6 ingress merger and receive their ordinal there, so which
        coroutine returns first cannot influence replay (I20).
        """
        self.generation += 1
        plan = self.plan_cycle(decision, state, rules)
        reserved, records = self.reserve(plan, now_ns)

        if reserved:
            await asyncio.gather(*(self._send(adapter, request) for request in reserved))

        return ExecutionCycle(
            plan=plan, records=tuple(records[side.outcome] for side in plan.sides)
        )

    async def _send(self, adapter: AsyncVenueAdapter, request: ReservedRequest) -> None:
        """Dispatch one already-reserved request. Nothing is reserved in here."""
        if request.kind is RequestClass.PLACE and request.prepared is not None:
            await adapter.place(request.prepared)
        elif request.kind is RequestClass.CANCEL and request.venue_order_id is not None:
            await adapter.cancel(request.venue_order_id)

    def _base_record(self, side: SideAction) -> ExecutionRecord:
        prepared = side.prepared
        live = side.live
        return ExecutionRecord(
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
