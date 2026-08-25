"""Exact Term1 / Term2 decomposition and its identity with settlement."""

from __future__ import annotations

from fractions import Fraction

from maker5m.accounting import (
    Fill,
    LedgerState,
    RebateMode,
    average_price,
    decompose,
    settle,
)
from maker5m.market import Outcome
from maker5m.numeric import money_from_whole, parse_money, parse_share, share_from_whole

WITHOUT = RebateMode.WITHOUT_REBATE


def ledger(n_up: str, cost_up: str, n_down: str, cost_down: str) -> LedgerState:
    state = LedgerState()
    if parse_share(n_up) > 0:
        state = state.apply_fill(Fill(Outcome.UP, parse_share(n_up), parse_money(cost_up)))
    if parse_share(n_down) > 0:
        state = state.apply_fill(Fill(Outcome.DOWN, parse_share(n_down), parse_money(cost_down)))
    return state


CANONICAL = ledger(n_up="120", cost_up="72", n_down="100", cost_down="50")


def test_canonical_example_reproduces_the_document_exactly() -> None:
    """Canonical section 4.1: Term1 = -$10, Term2 = +$8, total -$2."""
    d = decompose(CANONICAL, Outcome.UP)
    assert d.matched_shares == share_from_whole(100)
    assert d.winner_residual_shares == share_from_whole(20)
    assert d.loser_residual_shares == 0
    assert d.residual_side is Outcome.UP
    assert d.term1 == money_from_whole(-10)
    assert d.term2 == money_from_whole(8)
    assert d.trading_pnl == money_from_whole(-2)


def test_identity_holds_for_an_up_winner() -> None:
    d = decompose(CANONICAL, Outcome.UP)
    assert d.term1 + d.term2 == d.trading_pnl


def test_identity_holds_for_a_down_winner_where_the_loser_residual_is_larger() -> None:
    """The correction to Canonical section 4.

    With DOWN winning, the bot holds 20 more shares of the *loser*. Canonical's literal
    ``Term2 = R * (1 - a_W)`` with ``R = n_W - n_L = -20`` gives ``-10``, so the terms would
    sum to ``-20`` against a true result of ``-22``. The residual is a loser residual: it
    pays nothing and cost ``a_L`` per share, so ``Term2 = -20 * 0.60 = -12``.
    """
    d = decompose(CANONICAL, Outcome.DOWN)
    assert d.matched_shares == share_from_whole(100)
    assert d.winner_residual_shares == 0
    assert d.loser_residual_shares == share_from_whole(20)
    assert d.residual_side is Outcome.UP
    assert d.term1 == money_from_whole(-10)
    assert d.term2 == money_from_whole(-12)
    assert d.trading_pnl == money_from_whole(-22)
    assert d.term1 + d.term2 == d.trading_pnl

    # And the naive Canonical form would have been wrong here, by exactly $2.
    naive_term2 = Fraction(d.winner_residual_shares - d.loser_residual_shares, 1_000_000) * (
        1_000_000 - d.average_price_winner
    )
    assert d.term1 + naive_term2 != d.trading_pnl


def test_equal_inventories_have_no_residual() -> None:
    state = ledger(n_up="100", cost_up="60", n_down="100", cost_down="45")
    d = decompose(state, Outcome.UP)
    assert d.winner_residual_shares == 0
    assert d.loser_residual_shares == 0
    assert d.residual_side is None
    assert d.term2 == 0
    assert d.term1 == d.trading_pnl == money_from_whole(-5)


def test_fractional_inventories_stay_exact() -> None:
    state = ledger(n_up="128.63", cost_up="81.037690", n_down="157.26", cost_down="78.63")
    for winner in (Outcome.UP, Outcome.DOWN):
        d = decompose(state, winner)
        assert d.term1 + d.term2 == d.trading_pnl
        assert isinstance(d.term1, Fraction)


def test_one_sided_market_decomposes() -> None:
    state = ledger(n_up="10", cost_up="6", n_down="0", cost_down="0")
    up = decompose(state, Outcome.UP)
    assert up.matched_shares == 0
    assert up.term1 == 0
    assert up.term2 == up.trading_pnl == money_from_whole(4)
    down = decompose(state, Outcome.DOWN)
    assert down.term2 == down.trading_pnl == money_from_whole(-6)


def test_empty_market_decomposes_to_zero() -> None:
    d = decompose(LedgerState(), Outcome.UP)
    assert d.term1 == d.term2 == d.trading_pnl == 0


def test_full_settlement_identity_including_fees_and_rebates() -> None:
    state = CANONICAL.accrue_realised_rebate(parse_money("0.98"))
    result = settle(state, Outcome.UP, rebate_mode=RebateMode.REALISED_REBATE)
    d = decompose(state, Outcome.UP)
    assert d.term1 + d.term2 - result.fees + result.rebate == result.net_pnl


def test_average_price_is_exact_and_rational() -> None:
    assert average_price(money_from_whole(72), share_from_whole(120)) == 600_000
    assert average_price(parse_money("1"), parse_share("3")) == Fraction(1_000_000, 3)
    assert average_price(money_from_whole(0), share_from_whole(0)) == 0


def test_terms_never_use_floating_point() -> None:
    d = decompose(CANONICAL, Outcome.UP)
    for value in (d.term1, d.term2, d.average_price_winner, d.average_price_loser):
        assert isinstance(value, Fraction)
        assert isinstance(value.numerator, int)
        assert isinstance(value.denominator, int)
