"""Exact Term1 / Term2 decomposition (Canonical section 4).

This is an **analytic view**, not hot-path state. It splits the realised trading result into
the matched-pair market-making leg and the terminal residual leg, which is how the strategy
is diagnosed: the target's Term 1 is frequently negative while Term 2 carries the profit,
so optimising pair prices alone would misread the strategy entirely (Detailed section 23).

Exactness
---------
Average acquisition price is genuinely rational, so the individual terms are computed with
:class:`fractions.Fraction`. No floating-point average appears. Their **sum** is always an
exact integer ``MoneyUnits`` amount, and that is the invariant worth asserting:

```text
term1 + term2 == gross_payout - total_cost          (exactly)
```

Fees and rebates sit outside the identity because Canonical section 4 defines the terms
from share counts and prices alone. The full settlement result is therefore::

    net_pnl == term1 + term2 - fees + rebate

``Fraction`` arithmetic is slower than ``int``; that is acceptable because nothing here runs
on the trading path. The hot-path ledger stays integer-only.

A correction to Canonical section 4
-----------------------------------
Canonical section 4 defines ``R = n_W - n_L`` and ``Term2 = R * (1 - a_W)``, and states the
decomposition is algebraically equivalent to the exact settlement accounting. That holds
only while ``n_W >= n_L``. When the bot ends holding **more of the loser** -- which happens
whenever the endgame favourite does not win -- the literal formula is wrong. Worked from the
document's own example with the outcome reversed (120 UP at 0.60, 100 DOWN at 0.50, DOWN
wins): it gives ``-10 + -10 = -20``, while the exact settlement result is ``-22``.

The residual is a *loser* residual in that case: it pays nothing and cost ``a_L`` per share.
The general form used here is::

    M      = min(n_W, n_L)
    Term1  = M * (1 - a_W - a_L)
    Term2  = (n_W - M) * (1 - a_W)  -  (n_L - M) * a_L

Exactly one of the two residual counts is non-zero, and for ``n_W >= n_L`` this reduces to
Canonical's ``R * (1 - a_W)``. The correction is required by the invariant the Canonical
document itself makes mandatory in section 35 ("Term 1 + Term 2 reproduces settlement PnL")
and is recorded in ``docs/OPEN_ITEMS.md`` and ``docs/ARCHITECTURE_SSOT.md`` section 10.
"""

from dataclasses import dataclass
from fractions import Fraction

from maker5m.accounting.ledger import LedgerState
from maker5m.domain import Outcome
from maker5m.numeric.scales import PAR_MONEY, SHARE_SCALE
from maker5m.numeric.units import MoneyUnits, ShareUnits, shares_at_par

__all__ = ["TermDecomposition", "average_price", "decompose"]


def average_price(cost: MoneyUnits, shares: ShareUnits) -> Fraction:
    """Average acquisition price in ``MoneyUnits`` per whole share, exactly.

    Zero shares yields zero: the ledger forbids cost without shares, so the term it feeds
    is multiplied by a zero share count in every case where this arises.
    """
    if shares == 0:
        return Fraction(0)
    return Fraction(cost * SHARE_SCALE, shares)


@dataclass(frozen=True, slots=True)
class TermDecomposition:
    """The exact split of trading PnL into matched-pair and residual legs."""

    winner: Outcome
    matched_shares: ShareUnits
    winner_residual_shares: ShareUnits
    loser_residual_shares: ShareUnits
    average_price_winner: Fraction
    average_price_loser: Fraction
    term1: Fraction
    term2: Fraction
    trading_pnl: MoneyUnits

    @property
    def residual_side(self) -> Outcome | None:
        """Which outcome the terminal residual is held in, or ``None`` if inventories match."""
        if self.winner_residual_shares > 0:
            return self.winner
        if self.loser_residual_shares > 0:
            return self.winner.other
        return None


def decompose(state: LedgerState, winner: Outcome) -> TermDecomposition:
    """Split the realised trading result of ``state`` under ``winner``.

    ``term1 + term2 == trading_pnl`` holds exactly, for every inventory configuration
    including the one where the loser holding exceeds the winner holding.
    """
    loser = winner.other
    n_w = state.shares(winner)
    n_l = state.shares(loser)
    cost_w = state.cost(winner)
    cost_l = state.cost(loser)

    matched = ShareUnits(min(n_w, n_l))
    residual_w = ShareUnits(n_w - matched)
    residual_l = ShareUnits(n_l - matched)

    avg_w = average_price(cost_w, n_w)
    avg_l = average_price(cost_l, n_l)

    term1 = Fraction(matched, SHARE_SCALE) * (PAR_MONEY - avg_w - avg_l)
    term2 = (
        Fraction(residual_w, SHARE_SCALE) * (PAR_MONEY - avg_w)
        - Fraction(residual_l, SHARE_SCALE) * avg_l
    )

    return TermDecomposition(
        winner=winner,
        matched_shares=matched,
        winner_residual_shares=residual_w,
        loser_residual_shares=residual_l,
        average_price_winner=avg_w,
        average_price_loser=avg_l,
        term1=term1,
        term2=term2,
        trading_pnl=MoneyUnits(shares_at_par(n_w) - state.total_cost),
    )
