"""The reconciler: KEEP is the default, and every action that gives up a queue slot is earned."""

from __future__ import annotations

import pytest

from maker5m.domain import Outcome
from maker5m.execution import (
    LiveOrder,
    OrderLifecycle,
    PreparationOutcome,
    PreparedOrder,
    ReconcileAction,
    SideAction,
    SideReason,
    prepare_order,
    reconcile,
)
from maker5m.market.events import BookLevel
from maker5m.numeric import TICK_0_01, parse_price, parse_share
from maker5m.strategy.decision import DesiredOrder
from tests.execution.builders import UP_TOKEN, px, sh


def prepared(price: str = "0.63", size: str = "15", ask: str = "0.64") -> PreparedOrder:
    return prepare_order(
        DesiredOrder(Outcome.UP, px(price), sh(size)),
        token_id=UP_TOKEN,
        venue_tick=TICK_0_01,
        min_order_size=sh("5"),
        observed_ask=BookLevel(parse_price(ask), parse_share("100")),
    )


def live(
    price: str = "0.63",
    remaining: str = "15",
    original: str = "15",
    status: OrderLifecycle = OrderLifecycle.LIVE,
) -> LiveOrder:
    return LiveOrder(
        client_order_id="coid-1",
        outcome=Outcome.UP,
        price=px(price),
        original_size=sh(original),
        remaining_size=sh(remaining),
        status=status,
        venue_order_id="venue-1",
    )


def up(prepared_order: PreparedOrder | None, live_order: LiveOrder | None) -> SideAction:
    return reconcile(
        {Outcome.UP: prepared_order, Outcome.DOWN: None},
        {Outcome.UP: live_order, Outcome.DOWN: None},
    ).up


# -- the basic table ----------------------------------------------------------


def test_no_desired_no_live_does_nothing() -> None:
    action = up(None, None)
    assert action.action is ReconcileAction.NOTHING
    assert action.reason is SideReason.NO_DESIRED_NO_LIVE


def test_desired_withdrawn_cancels_the_live_order() -> None:
    """SETTLING produces no desired orders, so this is how quoting stops."""
    action = up(None, live())
    assert action.action is ReconcileAction.CANCEL
    assert action.reason is SideReason.DESIRED_WITHDRAWN


def test_a_safe_desired_order_with_nothing_live_is_placed() -> None:
    action = up(prepared(), None)
    assert action.action is ReconcileAction.PLACE
    assert action.reason is SideReason.NO_LIVE_ORDER


def test_a_blocked_desired_order_with_nothing_live_is_not_placed() -> None:
    action = up(prepared("0.65", ask="0.64"), None)
    assert action.action is ReconcileAction.BLOCKED
    assert action.preparation is PreparationOutcome.WOULD_CROSS


# -- KEEP, the load-bearing rule ----------------------------------------------


def test_an_unchanged_order_is_kept() -> None:
    action = up(prepared("0.63", "15"), live("0.63", "15"))
    assert action.action is ReconcileAction.KEEP
    assert action.reason is SideReason.UNCHANGED


def test_a_changed_price_replaces() -> None:
    action = up(prepared("0.62", "15"), live("0.63", "15"))
    assert action.action is ReconcileAction.REPLACE
    assert action.reason is SideReason.PRICE_CHANGED


def test_a_changed_size_replaces() -> None:
    action = up(prepared("0.63", "20"), live("0.63", "15"))
    assert action.action is ReconcileAction.REPLACE
    assert action.reason is SideReason.SIZE_CHANGED


def test_a_partially_filled_order_whose_remainder_matches_is_kept() -> None:
    """The case a naive implementation gets wrong, handing away queue position for nothing.

    A 15.00 order fills 5.00. Inventory moves, so the strategy recomputes a desired size of
    10.00 — which is exactly what is already resting. Comparing the *original* size would
    cancel a perfectly good partially-filled order.
    """
    action = up(
        prepared("0.63", "10"),
        live("0.63", remaining="10", original="15", status=OrderLifecycle.PARTIALLY_FILLED),
    )
    assert action.action is ReconcileAction.KEEP
    assert action.reason is SideReason.UNCHANGED
    assert action.live is not None
    assert action.live.original_size == sh("15")
    assert action.live.remaining_size == sh("10")


def test_comparing_original_size_would_have_been_wrong() -> None:
    """Pins the distinction so a future refactor cannot quietly reintroduce the bug."""
    order = live("0.63", remaining="10", original="15", status=OrderLifecycle.PARTIALLY_FILLED)
    target = prepared("0.63", "10")
    assert order.remaining_size == target.submission_size
    assert order.original_size != target.submission_size
    assert up(target, order).action is ReconcileAction.KEEP


def test_a_partial_fill_whose_remainder_no_longer_matches_replaces() -> None:
    action = up(
        prepared("0.63", "12"),
        live("0.63", remaining="10", original="15", status=OrderLifecycle.PARTIALLY_FILLED),
    )
    assert action.action is ReconcileAction.REPLACE


# -- no unsafe replacement ------------------------------------------------------


def test_a_blocked_replacement_cancels_but_never_places() -> None:
    action = up(prepared("0.65", ask="0.64"), live("0.63", "15"))
    assert action.action is ReconcileAction.CANCEL
    assert action.reason is SideReason.UNSAFE_REPLACEMENT
    assert action.preparation is PreparationOutcome.WOULD_CROSS


def test_an_off_tick_replacement_cancels_but_never_places() -> None:
    action = up(prepared("0.631"), live("0.63", "15"))
    assert action.action is ReconcileAction.CANCEL
    assert action.reason is SideReason.UNSAFE_REPLACEMENT


# -- in-flight and unknown state -------------------------------------------------


@pytest.mark.parametrize("status", [OrderLifecycle.PENDING_PLACE, OrderLifecycle.PENDING_CANCEL])
def test_an_in_flight_order_makes_the_side_wait(status: OrderLifecycle) -> None:
    """Acting while a request is outstanding is how duplicates are created."""
    action = up(prepared(), live(status=status))
    assert action.action is ReconcileAction.WAIT
    assert action.reason is SideReason.IN_FLIGHT


def test_unknown_state_makes_the_side_wait() -> None:
    action = up(prepared(), live(status=OrderLifecycle.UNKNOWN))
    assert action.action is ReconcileAction.WAIT
    assert action.reason is SideReason.UNKNOWN_STATE


def test_a_pending_place_never_produces_a_second_place() -> None:
    action = up(prepared(), live(status=OrderLifecycle.PENDING_PLACE))
    assert action.action is not ReconcileAction.PLACE


def test_a_pending_cancel_never_produces_a_second_cancel() -> None:
    action = up(None, live(status=OrderLifecycle.PENDING_CANCEL))
    assert action.action is not ReconcileAction.CANCEL


# -- both sides -----------------------------------------------------------------


def test_both_sides_are_always_evaluated() -> None:
    """A fill changes total_cost and therefore both desired sizes (I08)."""
    down_prepared = prepare_order(
        DesiredOrder(Outcome.DOWN, px("0.36"), sh("15")),
        token_id="token-down",
        venue_tick=TICK_0_01,
        min_order_size=sh("5"),
        observed_ask=BookLevel(parse_price("0.38"), parse_share("100")),
    )
    plan = reconcile(
        {Outcome.UP: prepared("0.63", "10"), Outcome.DOWN: down_prepared},
        {Outcome.UP: live("0.63", "10"), Outcome.DOWN: None},
    )
    assert plan.up.action is ReconcileAction.KEEP
    assert plan.down.action is ReconcileAction.PLACE
    assert plan.action_for(Outcome.DOWN) is plan.down


def test_the_filled_side_keeps_while_the_other_replaces() -> None:
    down_live = LiveOrder(
        client_order_id="coid-d",
        outcome=Outcome.DOWN,
        price=px("0.35"),
        original_size=sh("15"),
        remaining_size=sh("15"),
        status=OrderLifecycle.LIVE,
    )
    down_prepared = prepare_order(
        DesiredOrder(Outcome.DOWN, px("0.36"), sh("15")),
        token_id="token-down",
        venue_tick=TICK_0_01,
        min_order_size=sh("5"),
        observed_ask=BookLevel(parse_price("0.38"), parse_share("100")),
    )
    plan = reconcile(
        {Outcome.UP: prepared("0.63", "10"), Outcome.DOWN: down_prepared},
        {
            Outcome.UP: live(
                "0.63", remaining="10", original="15", status=OrderLifecycle.PARTIALLY_FILLED
            ),
            Outcome.DOWN: down_live,
        },
    )
    assert plan.up.action is ReconcileAction.KEEP
    assert plan.down.action is ReconcileAction.REPLACE
    assert plan.request_count == 1


# -- purity ------------------------------------------------------------------------


def test_the_reconciler_is_pure_and_repeatable() -> None:
    args = ({Outcome.UP: prepared(), Outcome.DOWN: None}, {Outcome.UP: live(), Outcome.DOWN: None})
    first = reconcile(*args)
    for _ in range(50):
        assert reconcile(*args) == first


def test_reconciling_does_not_mutate_its_inputs() -> None:
    import dataclasses

    order = live()
    target = prepared()
    before = (dataclasses.astuple(order), dataclasses.astuple(target))
    reconcile({Outcome.UP: target, Outcome.DOWN: None}, {Outcome.UP: order, Outcome.DOWN: None})
    assert (dataclasses.astuple(order), dataclasses.astuple(target)) == before
