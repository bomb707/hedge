"""Realised settlement, including the mandatory dual-token accounting regression."""

from __future__ import annotations

import pytest

from maker5m.accounting import Fill, LedgerState, RebateMode, settle
from maker5m.market import Outcome
from maker5m.numeric import money_from_whole, parse_money, parse_share, share_from_whole

WITHOUT = RebateMode.WITHOUT_REBATE


def ledger(n_up: str, cost_up: str, n_down: str, cost_down: str, fees: str = "0") -> LedgerState:
    state = LedgerState()
    if parse_share(n_up) > 0:
        state = state.apply_fill(
            Fill(Outcome.UP, parse_share(n_up), parse_money(cost_up), parse_money(fees))
        )
    if parse_share(n_down) > 0:
        state = state.apply_fill(Fill(Outcome.DOWN, parse_share(n_down), parse_money(cost_down)))
    return state


# -- the mandatory regression ------------------------------------------------------------

MANDATORY = ledger(n_up="120", cost_up="72", n_down="100", cost_down="50")


def test_mandatory_regression_holding_more_of_the_winner_is_not_a_profit() -> None:
    """Canonical section 3.1, invariant I01. The load-bearing accounting fact.

    120 UP at $0.60 and 100 DOWN at $0.50. The wallet ends holding 20 more shares of the
    outcome that wins, and still loses $2, because the $50 spent on DOWN is part of the
    cost of the UP payout.
    """
    assert MANDATORY.n_up == share_from_whole(120)
    assert MANDATORY.n_down == share_from_whole(100)
    assert MANDATORY.cost_up == money_from_whole(72)
    assert MANDATORY.cost_down == money_from_whole(50)
    assert MANDATORY.total_cost == money_from_whole(122)
    assert MANDATORY.fees == 0

    up = settle(MANDATORY, Outcome.UP, rebate_mode=WITHOUT)
    assert up.gross_payout == money_from_whole(120)
    assert up.net_pnl == money_from_whole(-2)

    down = settle(MANDATORY, Outcome.DOWN, rebate_mode=WITHOUT)
    assert down.gross_payout == money_from_whole(100)
    assert down.net_pnl == money_from_whole(-22)

    # The point, stated as an assertion: inventory says "long the winner", economics say
    # "losing market". Both settlement branches are negative.
    assert MANDATORY.net_inventory == share_from_whole(20)
    assert MANDATORY.pnl_if_up(WITHOUT) < 0
    assert MANDATORY.pnl_if_down(WITHOUT) < 0


def test_mandatory_regression_matches_the_live_hypothetical_branches() -> None:
    for winner in (Outcome.UP, Outcome.DOWN):
        result = settle(MANDATORY, winner, rebate_mode=WITHOUT)
        assert result.net_pnl == MANDATORY.pnl_if(winner, WITHOUT)


# -- settlement scenarios ------------------------------------------------------------------


def test_break_even() -> None:
    state = ledger(n_up="100", cost_up="60", n_down="100", cost_down="40")
    assert settle(state, Outcome.UP, rebate_mode=WITHOUT).net_pnl == 0
    assert settle(state, Outcome.DOWN, rebate_mode=WITHOUT).net_pnl == 0


def test_winner_profitable() -> None:
    state = ledger(n_up="130", cost_up="70", n_down="100", cost_down="45")
    assert settle(state, Outcome.UP, rebate_mode=WITHOUT).net_pnl == money_from_whole(15)


def test_winner_still_losing() -> None:
    """Detailed section 28: a +30 favourite residual acquired too expensively still loses."""
    state = ledger(n_up="130", cost_up="81", n_down="100", cost_down="50")
    assert state.net_inventory == share_from_whole(30)
    assert settle(state, Outcome.UP, rebate_mode=WITHOUT).net_pnl == money_from_whole(-1)


def test_both_outcomes_negative() -> None:
    state = ledger(n_up="100", cost_up="70", n_down="100", cost_down="45")
    assert state.pnl_if_up(WITHOUT) < 0
    assert state.pnl_if_down(WITHOUT) < 0


def test_fees_and_rebates_enter_settlement_exactly_once() -> None:
    state = ledger(n_up="120", cost_up="72", n_down="100", cost_down="50", fees="0.25")
    state = state.accrue_realised_rebate(parse_money("0.98"))
    result = settle(state, Outcome.UP, rebate_mode=RebateMode.REALISED_REBATE)
    assert result.fees == parse_money("0.25")
    assert result.rebate == parse_money("0.98")
    assert result.net_pnl == money_from_whole(-2) - parse_money("0.25") + parse_money("0.98")


def test_settlement_records_which_rebate_figure_it_used() -> None:
    state = MANDATORY.accrue_estimated_rebate(money_from_whole(1))
    estimated = settle(state, Outcome.UP, rebate_mode=RebateMode.ESTIMATED_REBATE)
    realised = settle(state, Outcome.UP, rebate_mode=RebateMode.REALISED_REBATE)
    assert estimated.rebate_mode is RebateMode.ESTIMATED_REBATE
    assert estimated.net_pnl == money_from_whole(-1)
    assert realised.rebate_mode is RebateMode.REALISED_REBATE
    assert realised.net_pnl == money_from_whole(-2)


def test_settle_requires_an_explicit_rebate_mode() -> None:
    with pytest.raises(TypeError):
        settle(MANDATORY, Outcome.UP)  # type: ignore[call-arg]


def test_trading_pnl_excludes_fees_and_rebates() -> None:
    state = ledger(n_up="120", cost_up="72", n_down="100", cost_down="50", fees="1")
    state = state.accrue_realised_rebate(money_from_whole(3))
    result = settle(state, Outcome.UP, rebate_mode=RebateMode.REALISED_REBATE)
    assert result.trading_pnl == money_from_whole(-2)
    assert result.net_pnl == money_from_whole(-2) - money_from_whole(1) + money_from_whole(3)


def test_settling_an_empty_market_is_zero() -> None:
    assert settle(LedgerState(), Outcome.UP, rebate_mode=WITHOUT).net_pnl == 0
