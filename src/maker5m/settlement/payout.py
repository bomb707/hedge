"""What a resolved position is worth, computed the way the contract computes it.

``ConditionalTokens.redeemPositions`` pays, per index set:

```solidity
payout = balance * payoutNumerator / payoutDenominator
```

with Solidity's integer division, which truncates toward zero for the non-negative values
involved. This module reproduces that exactly with Python's ``//``, on the P1 fixed-point
integers. No float, no ``Decimal``, no rounding mode — the contract has none.

The word "paper"
----------------
Everything here computes what a redemption **would** pay. Nothing here proves collateral has
moved. Until a real transaction lands and a ``PayoutRedemption`` is observed, the honest names are
``expected_redeem_value`` and ``paper_settlement_pnl``; ``realised_redemption_amount`` does not
exist yet and is deliberately absent rather than approximated.
"""

from dataclasses import dataclass
from typing import Final

from maker5m.accounting.ledger import LedgerState, RebateMode
from maker5m.domain import Outcome
from maker5m.numeric.units import MoneyUnits, ShareUnits, shares_at_par
from maker5m.settlement.resolution import MarketResolutionTarget, PayoutVector

__all__ = ["PaperSettlement", "outcome_payout", "settle_on_paper"]


def outcome_payout(balance: ShareUnits, numerator: int, denominator: int) -> MoneyUnits:
    """One outcome's collateral payout, matching the contract's integer arithmetic.

    ``shares_at_par`` first, because a share settles to exactly $1.00 and the scales differ:
    doing the numerator/denominator step in share units and converting afterwards would truncate
    at a different place than the contract does.
    """
    if denominator <= 0:
        raise ValueError(f"payout denominator must be positive, got {denominator}")
    if numerator < 0:
        raise ValueError(f"payout numerator must not be negative, got {numerator}")
    if balance < 0:
        raise ValueError(f"balance must not be negative, got {balance}")
    return MoneyUnits(shares_at_par(balance) * numerator // denominator)


@dataclass(frozen=True, slots=True)
class PaperSettlement:
    """What settlement would yield, given an authoritative payout vector."""

    up_balance: ShareUnits
    down_balance: ShareUnits
    payout: PayoutVector
    up_payout: MoneyUnits
    down_payout: MoneyUnits
    expected_redeem_value: MoneyUnits
    """Gross collateral the payout vector entitles these balances to. Not cash received."""

    total_cost: MoneyUnits
    fees: MoneyUnits
    rebate: MoneyUnits
    rebate_mode: RebateMode
    paper_settlement_pnl: MoneyUnits
    """``expected_redeem_value - total_cost - fees + rebate``. Detailed §33's equation, and no
    alternative one."""

    winning_outcome: Outcome | None

    def summary(self) -> dict[str, object]:
        return {
            "up_balance": int(self.up_balance),
            "down_balance": int(self.down_balance),
            "payout": self.payout.summary(),
            "up_payout": int(self.up_payout),
            "down_payout": int(self.down_payout),
            "expected_redeem_value": int(self.expected_redeem_value),
            "total_cost": int(self.total_cost),
            "fees": int(self.fees),
            "rebate": int(self.rebate),
            "rebate_mode": self.rebate_mode.value,
            "paper_settlement_pnl": int(self.paper_settlement_pnl),
            "winning_outcome": None if self.winning_outcome is None else self.winning_outcome.value,
        }


def settle_on_paper(
    ledger: LedgerState,
    payout: PayoutVector,
    target: MarketResolutionTarget,
    *,
    rebate_mode: RebateMode,
) -> PaperSettlement:
    """Value the ledger's holdings against an authoritative payout vector.

    Works for any payout shape the contract can hold, including fractional ones, because it sums
    per-slot payouts rather than branching on a winner. For the binary singleton case it agrees
    with P1's ``pnl_if`` to the last ``MoneyUnit``, which is asserted by test — there is exactly
    one settlement equation in this codebase and this is not a second one.
    """
    if not payout.resolved:
        raise ValueError("cannot settle against an unresolved payout vector")
    if len(payout.numerators) < max(target.up_slot, target.down_slot) + 1:
        raise ValueError(
            f"payout vector has {len(payout.numerators)} slots; market maps UP to "
            f"{target.up_slot} and DOWN to {target.down_slot}"
        )

    up_payout = outcome_payout(ledger.n_up, payout.numerators[target.up_slot], payout.denominator)
    down_payout = outcome_payout(
        ledger.n_down, payout.numerators[target.down_slot], payout.denominator
    )
    gross = MoneyUnits(up_payout + down_payout)
    rebate = ledger.rebate(rebate_mode)
    pnl = MoneyUnits(gross - ledger.total_cost - ledger.fees + rebate)

    slot = payout.winning_slot
    return PaperSettlement(
        up_balance=ledger.n_up,
        down_balance=ledger.n_down,
        payout=payout,
        up_payout=up_payout,
        down_payout=down_payout,
        expected_redeem_value=gross,
        total_cost=ledger.total_cost,
        fees=ledger.fees,
        rebate=rebate,
        rebate_mode=rebate_mode,
        paper_settlement_pnl=pnl,
        winning_outcome=None if slot is None else target.outcome_for_slot(slot),
    )


BINARY_EQUALITY_NOTE: Final = (
    "For a binary singleton payout, settle_on_paper().paper_settlement_pnl equals "
    "LedgerState.pnl_if(winner, mode) exactly. Asserted in tests/settlement."
)
