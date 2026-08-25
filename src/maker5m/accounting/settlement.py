"""Realised settlement of a resolved market.

Exit is redemption, never selling or hedging (invariants I15, I16). This module computes
what redemption is worth; performing it is P10 work.
"""

from dataclasses import dataclass

from maker5m.accounting.ledger import LedgerState, RebateMode
from maker5m.domain import Outcome
from maker5m.numeric.units import MoneyUnits, shares_at_par

__all__ = ["SettlementResult", "settle"]


@dataclass(frozen=True, slots=True)
class SettlementResult:
    """The exact realised result of one market.

    ``net_pnl`` is the only valid measure of success. A positive winner residual is not a
    profit, and neither is holding more of the winning outcome (Canonical section 3.1).
    """

    winner: Outcome
    rebate_mode: RebateMode
    gross_payout: MoneyUnits
    total_cost: MoneyUnits
    fees: MoneyUnits
    rebate: MoneyUnits
    net_pnl: MoneyUnits

    @property
    def trading_pnl(self) -> MoneyUnits:
        """Payout minus acquisition cost, before fees and rebates.

        This is the figure the Term1/Term2 decomposition reproduces; fees and rebates sit
        outside that identity because Canonical section 4 defines the terms from prices and
        share counts alone.
        """
        return MoneyUnits(self.gross_payout - self.total_cost)


def settle(state: LedgerState, winner: Outcome, *, rebate_mode: RebateMode) -> SettlementResult:
    """Realise ``state`` under ``winner``. The loser's tokens pay nothing.

    ``rebate_mode`` has no default: settling with an *estimated* rebate produces an
    estimated result, and the caller must say which it wants.
    """
    gross_payout = shares_at_par(state.shares(winner))
    rebate = state.rebate(rebate_mode)
    return SettlementResult(
        winner=winner,
        rebate_mode=rebate_mode,
        gross_payout=gross_payout,
        total_cost=state.total_cost,
        fees=state.fees,
        rebate=rebate,
        net_pnl=MoneyUnits(gross_payout - state.total_cost - state.fees + rebate),
    )
