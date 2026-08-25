"""The executor: dispatch, idempotency, the SDK boundary, and the long-stream KEEP test."""

from __future__ import annotations

import pytest

from maker5m.domain import Outcome
from maker5m.execution import (
    ExecutionError,
    Executor,
    LiveOrderTable,
    OrderIdentityError,
    OrderLifecycle,
    RateDecision,
    ReconcileAction,
    RecordingTransport,
    ReplacementPolicy,
    ReplacementTracker,
    TokenBucket,
    VenueAdapter,
)
from maker5m.execution.executor import ExecutionCycle
from maker5m.execution.prepare import PreparedOrder
from maker5m.execution.replacement import PendingReplacement
from maker5m.market.state import MarketState
from maker5m.market.timebase import NANOS_PER_SECOND, TimestampNs
from maker5m.strategy.decision import DesiredOrders
from tests.execution.builders import decision, desired, px, rules, sh, state_at

CLOCK = TimestampNs(1_000_000_000_000)


def at(step: int) -> TimestampNs:
    return TimestampNs(CLOCK + step * NANOS_PER_SECOND)


def build() -> tuple[Executor, RecordingTransport]:
    transport = RecordingTransport()
    return Executor(adapter=VenueAdapter(transport=transport)), transport


def run(
    executor: Executor, orders: DesiredOrders, state: MarketState, step: int = 0
) -> ExecutionCycle:
    return executor.run_cycle(decision(orders, state), state, rules(), at(step))


def a_prepared_up_order() -> PreparedOrder:
    """A concrete, submittable UP order taken from a real plan."""
    plan = Executor(adapter=VenueAdapter(RecordingTransport())).plan_cycle(
        decision(desired(), state_at()), state_at(), rules()
    )
    prepared = plan.up.prepared
    assert prepared is not None
    return prepared


# -- dispatch -----------------------------------------------------------


def test_a_first_cycle_places_both_sides() -> None:
    executor, transport = build()
    state = state_at()
    cycle = run(executor, desired(), state)
    assert cycle.plan.up.action is ReconcileAction.PLACE
    assert cycle.plan.down.action is ReconcileAction.PLACE
    assert len(transport.placements) == 2
    assert {p.side for p in transport.placements} == {"BUY"}
    assert {p.order_type for p in transport.placements} == {"GTC"}
    assert all(p.post_only for p in transport.placements)


def test_placements_carry_no_float() -> None:
    from decimal import Decimal

    executor, transport = build()
    run(executor, desired(), state_at())
    for placement in transport.placements:
        assert isinstance(placement.price, Decimal)
        assert isinstance(placement.size, Decimal)


def test_up_and_down_are_independent_so_they_may_dispatch_concurrently() -> None:
    """The plan does not depend on dispatch order; nothing serialises the two sides."""
    executor, _ = build()
    state = state_at()
    plan = executor.plan_cycle(decision(desired(), state), state, rules())
    assert plan.up.action is ReconcileAction.PLACE
    assert plan.down.action is ReconcileAction.PLACE
    assert plan.request_count == 2
    # Reversing the order of evaluation cannot change either side.
    again = executor.plan_cycle(decision(desired(), state), state, rules())
    assert (again.down, again.up) == (plan.down, plan.up)


def test_a_blocked_side_issues_no_request() -> None:
    executor, transport = build()
    state = state_at()
    # 0.65 is above the observed UP ask of 0.64.
    cycle = run(executor, desired(up_price="0.65"), state)
    assert cycle.plan.up.action is ReconcileAction.BLOCKED
    assert len(transport.placements) == 1, "only the DOWN side should have been placed"


def test_a_settling_style_withdrawal_cancels_both_sides() -> None:
    executor, transport = build()
    state = state_at()
    run(executor, desired(), state)
    for order in list(executor.orders):
        executor.orders.update(
            order.client_order_id,
            status=OrderLifecycle.LIVE,
            venue_order_id=f"v-{order.client_order_id}",
        )
    cycle = run(executor, desired(None, None, None, None), state, step=1)
    assert cycle.plan.up.action is ReconcileAction.CANCEL
    assert cycle.plan.down.action is ReconcileAction.CANCEL
    assert len(transport.cancels) == 2
    assert len(transport.placements) == 2, "no new orders after withdrawal"


# -- the long unchanged stream: the main queue-preservation acceptance test -------


def test_thousands_of_unchanged_decisions_produce_exactly_one_place_and_no_cancels() -> None:
    """The P7 acceptance test. Nothing but a genuine change may cost a queue slot."""
    executor, transport = build()
    state = state_at()
    orders = desired()

    counts: dict[ReconcileAction, int] = {}
    for step in range(5_000):
        cycle = run(executor, orders, state, step=step)
        for side in cycle.plan.sides:
            counts[side.action] = counts.get(side.action, 0) + 1
        # Acknowledge the first placements so they become resting orders.
        if step == 0:
            for order in list(executor.orders):
                executor.orders.update(
                    order.client_order_id,
                    status=OrderLifecycle.LIVE,
                    venue_order_id=f"v-{order.client_order_id}",
                )

    assert counts.get(ReconcileAction.PLACE, 0) == 2, "one PLACE per side, ever"
    assert counts.get(ReconcileAction.CANCEL, 0) == 0
    assert counts.get(ReconcileAction.REPLACE, 0) == 0
    assert counts[ReconcileAction.KEEP] >= 9_990
    assert len(transport.placements) == 2
    assert transport.cancels == []


def test_market_data_churn_alone_never_touches_a_resting_order() -> None:
    """A new book or spot event is not a reason to give up a queue position (I09, I10)."""
    executor, transport = build()
    orders = desired()
    state = state_at()
    run(executor, orders, state)
    for order in list(executor.orders):
        executor.orders.update(
            order.client_order_id, status=OrderLifecycle.LIVE, venue_order_id="v"
        )
    for step in range(1, 500):
        # The book moves around the resting price without invalidating it.
        moving = state_at(up_ask="0.70" if step % 2 else "0.69")
        cycle = executor.run_cycle(decision(orders, moving), moving, rules(), at(step))
        assert cycle.plan.up.action is ReconcileAction.KEEP
    assert transport.cancels == []
    assert len(transport.placements) == 2


# -- idempotency and in-flight protection ------------------------------------


def test_a_pending_place_never_produces_a_duplicate() -> None:
    executor, transport = build()
    state = state_at()
    orders = desired()
    run(executor, orders, state)
    assert len(transport.placements) == 2
    for step in range(1, 10):
        cycle = run(executor, orders, state, step=step)
        assert cycle.plan.up.action is ReconcileAction.WAIT
    assert len(transport.placements) == 2, "no duplicate while acknowledgement is pending"


def test_reusing_a_client_order_id_is_refused() -> None:
    table = LiveOrderTable()
    table.register_pending_place(
        client_order_id="dup", outcome=Outcome.UP, price=px("0.63"), size=sh("5"), ingress_ordinal=1
    )
    with pytest.raises(OrderIdentityError, match="already been used"):
        table.register_pending_place(
            client_order_id="dup",
            outcome=Outcome.UP,
            price=px("0.63"),
            size=sh("5"),
            ingress_ordinal=2,
        )


def test_a_duplicated_lifecycle_update_is_applied_once() -> None:
    table = LiveOrderTable()
    table.register_pending_place(
        client_order_id="c1", outcome=Outcome.UP, price=px("0.63"), size=sh("15"), ingress_ordinal=1
    )
    for _ in range(5):
        table.update(
            "c1",
            status=OrderLifecycle.PARTIALLY_FILLED,
            remaining_size=sh("10"),
            update_id="update-1",
        )
    order = table.require("c1")
    assert order.remaining_size == sh("10")
    assert order.status is OrderLifecycle.PARTIALLY_FILLED


def test_an_unknown_client_order_id_is_refused() -> None:
    with pytest.raises(OrderIdentityError, match="unknown"):
        LiveOrderTable().require("nope")


# -- replacement staleness ---------------------------------------------------


def test_a_superseded_replacement_is_discarded() -> None:
    """A late cancel acknowledgement must not place an obsolete price."""
    tracker = ReplacementTracker()
    tracker.record(
        PendingReplacement(Outcome.UP, "coid-1", a_prepared_up_order(), decision_generation=7)
    )
    assert tracker.is_stale(Outcome.UP, current_generation=8)
    assert tracker.take(Outcome.UP, current_generation=8) is None


def test_a_current_replacement_is_returned() -> None:
    tracker = ReplacementTracker()
    tracker.record(
        PendingReplacement(Outcome.UP, "coid-1", a_prepared_up_order(), decision_generation=7)
    )
    assert not tracker.is_stale(Outcome.UP, current_generation=7)
    taken = tracker.take(Outcome.UP, current_generation=7)
    assert taken is not None
    assert taken.cancelling_client_order_id == "coid-1"


def test_the_default_replacement_policy_is_cancel_then_place() -> None:
    assert ReplacementTracker().policy is ReplacementPolicy.CANCEL_THEN_PLACE


def test_place_then_cancel_is_declared_but_not_enabled() -> None:
    """It transiently exceeds the two-live-order model; P8/P13 must measure it first."""
    with pytest.raises(NotImplementedError, match="doubles exposure"):
        ReplacementTracker(policy=ReplacementPolicy.PLACE_THEN_CANCEL)


# -- the SDK boundary --------------------------------------------------------


def test_normal_submission_performs_no_metadata_lookup() -> None:
    """Canonical §22: no hidden REST request on the critical order path."""
    executor, transport = build()
    state = state_at()
    executor.adapter.prewarm(("token-up", "token-down"))
    before = transport.metadata_requests
    for step in range(20):
        run(executor, desired(), state, step=step)
    assert transport.metadata_requests == before, "submission triggered a metadata request"


def test_prewarm_happens_off_the_hot_path() -> None:
    executor, transport = build()
    executor.adapter.prewarm(("token-up", "token-down"))
    assert transport.prewarmed == [("token-up", "token-down")]


def test_the_accepted_order_path_does_not_wait_for_settlement() -> None:
    """post_order returns immediately; the SDK's settlement wait is never called.

    Checked as code rather than text: adapter.py names the method in a docstring precisely
    to record that it is deliberately not used.
    """
    from pathlib import Path

    import maker5m
    from tests.execution.builders import code_without_docstrings

    execution = Path(maker5m.__file__).parent / "execution"
    for path in execution.rglob("*.py"):
        code = code_without_docstrings(path)
        assert "wait_for_order_fill_settlement" not in code, f"{path.name} waits"
        assert "transaction_hash" not in code
        assert "wait()" not in code


def test_the_rate_limiter_suppresses_rather_than_delays() -> None:
    executor, transport = build()
    executor.bucket = TokenBucket(rate_per_second=8, burst=1, cancel_reserve=0)
    state = state_at()
    cycle = run(executor, desired(), state)
    decisions = [r.rate_decision for r in cycle.records]
    assert RateDecision.ALLOWED in decisions
    assert RateDecision.DEFERRED in decisions
    assert len(transport.placements) == 1, "the deferred side issued no request"


def test_a_non_submittable_order_cannot_reach_the_transport() -> None:
    executor, transport = build()
    state = state_at()
    run(executor, desired(up_price="0.65"), state)
    assert len(transport.placements) == 1
    blocked = executor.plan_cycle(
        decision(desired(up_price="0.65"), state), state, rules()
    ).up.prepared
    assert blocked is not None
    with pytest.raises(ExecutionError):
        executor.adapter.place(blocked)
