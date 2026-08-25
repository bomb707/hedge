"""Ledger state, fill application, and rebate separation."""

from __future__ import annotations

import dataclasses

import pytest

from maker5m.accounting import Fill, LedgerState, RebateMode
from maker5m.market import Outcome
from maker5m.numeric import (
    DomainError,
    MoneyUnits,
    PriceUnits,
    ShareUnits,
    money_from_whole,
    parse_money,
    parse_price,
    parse_share,
    share_from_whole,
)

WITHOUT = RebateMode.WITHOUT_REBATE


def fill(outcome: Outcome, shares: str, cost: str, fee: str = "0") -> Fill:
    return Fill(
        outcome=outcome,
        shares=parse_share(shares),
        cost=parse_money(cost),
        fee=parse_money(fee),
    )


def test_empty_ledger_is_all_zero() -> None:
    state = LedgerState()
    assert state.n_up == 0
    assert state.n_down == 0
    assert state.total_cost == 0
    assert state.net_inventory == 0
    assert state.pnl_if_up(WITHOUT) == 0
    assert state.pnl_if_down(WITHOUT) == 0


def test_up_only_fill() -> None:
    state = LedgerState().apply_fill(fill(Outcome.UP, "120", "72"))
    assert state.n_up == share_from_whole(120)
    assert state.n_down == 0
    assert state.cost_up == money_from_whole(72)
    assert state.total_cost == money_from_whole(72)
    assert state.net_inventory == share_from_whole(120)
    assert state.pnl_if_up(WITHOUT) == money_from_whole(48)
    assert state.pnl_if_down(WITHOUT) == money_from_whole(-72)


def test_down_only_fill() -> None:
    state = LedgerState().apply_fill(fill(Outcome.DOWN, "100", "50"))
    assert state.n_down == share_from_whole(100)
    assert state.net_inventory == share_from_whole(-100)
    assert state.pnl_if_down(WITHOUT) == money_from_whole(50)
    assert state.pnl_if_up(WITHOUT) == money_from_whole(-50)


def test_interleaved_fills_accumulate_exactly() -> None:
    state = LedgerState()
    for outcome, shares, cost in [
        (Outcome.UP, "60", "36"),
        (Outcome.DOWN, "40", "20"),
        (Outcome.UP, "60", "36"),
        (Outcome.DOWN, "60", "30"),
    ]:
        state = state.apply_fill(fill(outcome, shares, cost))
    assert state.n_up == share_from_whole(120)
    assert state.n_down == share_from_whole(100)
    assert state.cost_up == money_from_whole(72)
    assert state.cost_down == money_from_whole(50)
    assert state.net_inventory == share_from_whole(20)


def test_partial_fractional_fills_are_never_rounded() -> None:
    """Invariant I03: inventory uses true fractional filled quantities."""
    state = LedgerState()
    state = state.apply_fill(fill(Outcome.DOWN, "157.26", "78.63"))
    state = state.apply_fill(fill(Outcome.UP, "128.63", "81.037690"))
    assert state.net_inventory == parse_share("-28.63")
    state = state.apply_fill(fill(Outcome.UP, "13.63", "8.586900"))
    assert state.net_inventory == parse_share("-15")
    assert state.n_up == parse_share("142.26")


def test_many_small_fills_do_not_drift() -> None:
    """A float accumulator would drift here; integers cannot."""
    state = LedgerState()
    for _ in range(1_000):
        state = state.apply_fill(fill(Outcome.UP, "0.01", "0.0063"))
    assert state.n_up == share_from_whole(10)
    assert state.cost_up == parse_money("6.3")


def test_multiple_fills_at_different_costs() -> None:
    state = LedgerState()
    state = state.apply_fill(fill(Outcome.UP, "50", "30"))
    state = state.apply_fill(fill(Outcome.UP, "50", "35"))
    assert state.n_up == share_from_whole(100)
    assert state.cost_up == money_from_whole(65)


def test_fees_accumulate_and_reduce_both_branches() -> None:
    state = LedgerState().apply_fill(fill(Outcome.UP, "120", "72", fee="1.5"))
    assert state.fees == parse_money("1.5")
    assert state.pnl_if_up(WITHOUT) == money_from_whole(48) - parse_money("1.5")
    assert state.pnl_if_down(WITHOUT) == money_from_whole(-72) - parse_money("1.5")


def test_authoritative_cost_is_taken_from_the_venue_not_shares_times_price() -> None:
    """Section 5 of the P1 brief: the ledger follows the money, not the displayed price."""
    reported = Fill(
        outcome=Outcome.UP,
        shares=parse_share("13.63"),
        cost=parse_money("8.586901"),  # one unit away from shares * 0.63
        price=parse_price("0.63"),
    )
    state = LedgerState().apply_fill(reported)
    assert state.cost_up == parse_money("8.586901")


def test_price_is_informational_only() -> None:
    with_price = LedgerState().apply_fill(
        Fill(Outcome.UP, parse_share("10"), parse_money("6"), price=parse_price("0.6"))
    )
    without_price = LedgerState().apply_fill(Fill(Outcome.UP, parse_share("10"), parse_money("6")))
    assert dataclasses.astuple(with_price) == dataclasses.astuple(without_price)


# -- rebate separation ------------------------------------------------------------------


def test_estimated_and_realised_rebates_are_distinct_fields() -> None:
    state = LedgerState().apply_fill(fill(Outcome.UP, "120", "72"))
    state = state.accrue_estimated_rebate(parse_money("0.90"))
    state = state.accrue_realised_rebate(parse_money("0.10"))
    assert state.estimated_rebates == parse_money("0.90")
    assert state.realised_rebates == parse_money("0.10")
    assert state.rebate(RebateMode.WITHOUT_REBATE) == 0
    assert state.rebate(RebateMode.ESTIMATED_REBATE) == parse_money("0.90")
    assert state.rebate(RebateMode.REALISED_REBATE) == parse_money("0.10")


def test_accruing_one_rebate_never_touches_the_other() -> None:
    state = LedgerState().accrue_estimated_rebate(parse_money("1"))
    assert state.realised_rebates == 0
    state = state.accrue_realised_rebate(parse_money("2"))
    assert state.estimated_rebates == parse_money("1")


def test_estimate_can_be_replaced_by_a_model_that_recomputes_a_total() -> None:
    state = LedgerState().accrue_estimated_rebate(parse_money("1"))
    state = state.set_estimated_rebate(parse_money("0.25"))
    assert state.estimated_rebates == parse_money("0.25")


def test_rebate_modes_cannot_be_confused_in_pnl() -> None:
    state = (
        LedgerState()
        .apply_fill(fill(Outcome.UP, "120", "72"))
        .apply_fill(fill(Outcome.DOWN, "100", "50"))
        .accrue_estimated_rebate(money_from_whole(5))
        .accrue_realised_rebate(money_from_whole(1))
    )
    assert state.pnl_if_up(RebateMode.WITHOUT_REBATE) == money_from_whole(-2)
    assert state.pnl_if_up(RebateMode.ESTIMATED_REBATE) == money_from_whole(3)
    assert state.pnl_if_up(RebateMode.REALISED_REBATE) == money_from_whole(-1)


def test_pnl_requires_an_explicit_rebate_mode() -> None:
    """No default: a live estimate must never be reported as a settled figure."""
    with pytest.raises(TypeError):
        LedgerState().pnl_if_up()  # type: ignore[call-arg]


def test_pnl_if_dispatches_on_outcome() -> None:
    state = LedgerState().apply_fill(fill(Outcome.UP, "10", "6"))
    assert state.pnl_if(Outcome.UP, WITHOUT) == state.pnl_if_up(WITHOUT)
    assert state.pnl_if(Outcome.DOWN, WITHOUT) == state.pnl_if_down(WITHOUT)


# -- validation -------------------------------------------------------------------------


@pytest.mark.parametrize("shares", ["0", "-1"])
def test_fill_shares_must_be_strictly_positive(shares: str) -> None:
    with pytest.raises(DomainError):
        Fill(Outcome.UP, parse_share(shares), MoneyUnits(0))


def test_fill_cost_and_fee_must_be_non_negative() -> None:
    with pytest.raises(DomainError):
        Fill(Outcome.UP, parse_share("1"), parse_money("-0.01"))
    with pytest.raises(DomainError):
        Fill(Outcome.UP, parse_share("1"), parse_money("1"), fee=parse_money("-0.01"))


def test_fill_price_is_range_checked() -> None:
    with pytest.raises(DomainError):
        Fill(Outcome.UP, parse_share("1"), parse_money("1"), price=PriceUnits(1_000_001))


def test_ledger_rejects_negative_state() -> None:
    with pytest.raises(DomainError):
        LedgerState(n_up=ShareUnits(-1))
    with pytest.raises(DomainError):
        LedgerState(fees=MoneyUnits(-1))


def test_ledger_rejects_cost_without_shares() -> None:
    """Keeps the Term1/Term2 decomposition exact: it divides cost by share count."""
    with pytest.raises(DomainError):
        LedgerState(cost_up=money_from_whole(1))
    with pytest.raises(DomainError):
        LedgerState(cost_down=money_from_whole(1))


def test_ledger_state_is_immutable() -> None:
    state = LedgerState()
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.n_up = share_from_whole(1)  # type: ignore[misc]


def test_apply_fill_returns_a_new_state_and_leaves_the_old_one_untouched() -> None:
    before = LedgerState()
    after = before.apply_fill(fill(Outcome.UP, "10", "6"))
    assert before.n_up == 0
    assert after.n_up == share_from_whole(10)
    assert before is not after
