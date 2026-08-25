"""Comparing our state against an authoritative source, and refusing to repair it.

**SUPPORTING UNIT TEST ONLY.** No credential exists before P14, so every "venue" snapshot here
is constructed. The *empirical* status of real-account reconciliation is UNRUN / DEFERRED TO
P14, and these tests do not change that — they only pin the comparison logic that will consume
a real snapshot when one can be obtained.
"""

from __future__ import annotations

import pytest

from maker5m.accounting.ledger import LedgerState
from maker5m.domain import Outcome
from maker5m.execution.live_orders import LiveOrderTable, OrderLifecycle
from maker5m.numeric import ShareUnits, parse_money, parse_price, parse_share
from maker5m.risk import (
    OrderFinding,
    VenueExecution,
    VenueOpenOrder,
    VenuePosition,
    check_cost_ledger,
    check_position,
    reconcile_orders,
)

UP_TOKEN = "token-up"
DOWN_TOKEN = "token-down"


def ledger(up: str = "120", down: str = "100", cost_up: str = "72", cost_down: str = "50"):  # type: ignore[no-untyped-def]
    return LedgerState(
        n_up=parse_share(up),
        n_down=parse_share(down),
        cost_up=parse_money(cost_up),
        cost_down=parse_money(cost_down),
    )


# -- position ---------------------------------------------------------------------------------


def test_an_exact_match_is_a_match() -> None:
    snapshot = VenuePosition(UP_TOKEN, DOWN_TOKEN, parse_share("120"), parse_share("100"))
    check = check_position(ledger(), snapshot, up_token_id=UP_TOKEN, down_token_id=DOWN_TOKEN)
    assert check.matches
    assert check.up_difference == 0


def test_a_single_share_unit_difference_is_a_mismatch() -> None:
    """Fixed-point units, not a rounded display: this is the size a missed fill produces."""
    off_by_one = ShareUnits(int(parse_share("120")) + 1)
    snapshot = VenuePosition(UP_TOKEN, DOWN_TOKEN, off_by_one, parse_share("100"))
    check = check_position(ledger(), snapshot, up_token_id=UP_TOKEN, down_token_id=DOWN_TOKEN)
    assert not check.matches
    assert check.up_difference == 1


def test_a_snapshot_for_the_wrong_tokens_is_refused() -> None:
    """Comparing against another market's position is worse than not comparing at all."""
    snapshot = VenuePosition("other-up", "other-down", parse_share("120"), parse_share("100"))
    with pytest.raises(ValueError, match="position snapshot is for tokens"):
        check_position(ledger(), snapshot, up_token_id=UP_TOKEN, down_token_id=DOWN_TOKEN)


# -- cost ledger ------------------------------------------------------------------------------


def executions() -> dict[str, VenueExecution]:
    return {
        "x1": VenueExecution("x1", Outcome.UP, parse_share("120"), parse_money("72")),
        "x2": VenueExecution("x2", Outcome.DOWN, parse_share("100"), parse_money("50")),
    }


def test_cost_reconstructed_from_executions_matches() -> None:
    check = check_cost_ledger(ledger(), executions(), frozenset({"x1", "x2"}))
    assert check.matches
    assert check.up_cost_venue == parse_money("72")


def test_a_missing_execution_is_named() -> None:
    check = check_cost_ledger(ledger(), executions(), frozenset({"x1"}))
    assert not check.matches
    assert check.missing_execution_ids == ("x2",)
    assert check.duplicate_execution_ids == ()


def test_an_execution_we_applied_but_the_venue_does_not_report_is_named() -> None:
    """The two diagnoses call for opposite corrections, so they are reported apart."""
    check = check_cost_ledger(ledger(), executions(), frozenset({"x1", "x2", "ghost"}))
    assert not check.matches
    assert check.duplicate_execution_ids == ("ghost",)


def test_a_cost_disagreement_is_a_mismatch_even_with_every_id_present() -> None:
    wrong = ledger(cost_up="70")
    check = check_cost_ledger(wrong, executions(), frozenset({"x1", "x2"}))
    assert not check.matches
    assert check.up_cost_local == parse_money("70")
    assert check.up_cost_venue == parse_money("72")


# -- open orders ------------------------------------------------------------------------------


def table_with(client_order_id: str, status: OrderLifecycle) -> LiveOrderTable:
    table = LiveOrderTable()
    table.register_pending_place(
        client_order_id=client_order_id,
        outcome=Outcome.UP,
        price=parse_price("0.62"),
        size=parse_share("15"),
        ingress_ordinal=0,
    )
    table.update(client_order_id, status=status, venue_order_id="v1")
    return table


def venue_order(price: str = "0.62", size: str = "15") -> VenueOpenOrder:
    return VenueOpenOrder("c1", "v1", Outcome.UP, parse_price(price), parse_share(size))


def test_agreement_is_recognised() -> None:
    result = reconcile_orders(table_with("c1", OrderLifecycle.LIVE), {"c1": venue_order()})
    assert result.consistent
    assert result.findings == {"c1": OrderFinding.AGREED}


def test_a_price_disagreement_is_not_agreement() -> None:
    result = reconcile_orders(
        table_with("c1", OrderLifecycle.LIVE), {"c1": venue_order(price="0.61")}
    )
    assert not result.consistent
    assert result.findings["c1"] is OrderFinding.FIELDS_DIFFER


def test_a_remaining_size_disagreement_is_not_agreement() -> None:
    result = reconcile_orders(table_with("c1", OrderLifecycle.LIVE), {"c1": venue_order(size="10")})
    assert result.findings["c1"] is OrderFinding.FIELDS_DIFFER


def test_an_order_the_venue_does_not_report_is_local_only() -> None:
    result = reconcile_orders(table_with("c1", OrderLifecycle.LIVE), {})
    assert result.findings["c1"] is OrderFinding.LOCAL_ONLY
    assert not result.consistent


def test_an_order_we_have_no_record_of_is_venue_only() -> None:
    result = reconcile_orders(LiveOrderTable(), {"c1": venue_order()})
    assert result.findings["c1"] is OrderFinding.VENUE_ONLY
    assert not result.consistent


def test_an_unknown_local_order_defers_to_the_venue() -> None:
    result = reconcile_orders(table_with("c1", OrderLifecycle.UNKNOWN), {})
    assert result.findings["c1"] is OrderFinding.LOCAL_UNKNOWN
    assert not result.consistent


def test_a_terminal_local_order_the_venue_omits_is_agreement_by_silence() -> None:
    """A filled order not appearing in open orders is exactly what should happen."""
    result = reconcile_orders(table_with("c1", OrderLifecycle.FILLED), {})
    assert result.findings == {}
    assert result.consistent


def test_reconciliation_never_mutates_either_side() -> None:
    """It reports the shape of a disagreement. Resolving it is somebody else's job."""
    table = table_with("c1", OrderLifecycle.LIVE)
    before = dict(table.snapshot())
    venue = {"c1": venue_order(price="0.61")}
    reconcile_orders(table, venue)
    assert dict(table.snapshot()) == before
    assert venue == {"c1": venue_order(price="0.61")}
