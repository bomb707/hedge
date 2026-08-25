"""Exact dual-outcome position ledger for one binary market.

This is the authoritative accounting state of invariant I01. Every quantity is an integer
fixed-point value; no binary float and no ``Decimal`` appears anywhere in this module.

The central rule the ledger exists to enforce: **holding more of the eventual winner does
not imply profit**. Both hypothetical settlement values are first-class live state, kept
correct after every fill, not derived once at the end (Canonical section 3.2, and Detailed
section 48).

State transitions are immutable: each method returns a new :class:`LedgerState`. That makes
the fill sequence replayable and removes any possibility of hidden shared mutation
(invariant I20).
"""

from dataclasses import dataclass
from enum import Enum

from maker5m.domain import Outcome
from maker5m.numeric.errors import DomainError
from maker5m.numeric.scales import PRICE_SCALE
from maker5m.numeric.units import (
    ZERO_MONEY,
    ZERO_SHARES,
    MoneyUnits,
    PriceUnits,
    ShareUnits,
    shares_at_par,
)

__all__ = ["Fill", "LedgerState", "RebateMode"]


class RebateMode(Enum):
    """Which rebate figure a PnL calculation is allowed to use.

    Estimated and realised rebates are deliberately separate fields and there is no default
    mode anywhere: a caller must state which one it means, so a live estimate can never be
    silently reported as a settled figure (``docs/ARCHITECTURE_SSOT.md`` section 10, A6).
    """

    WITHOUT_REBATE = "WITHOUT_REBATE"
    ESTIMATED_REBATE = "ESTIMATED_REBATE"
    REALISED_REBATE = "REALISED_REBATE"


@dataclass(frozen=True, slots=True)
class Fill:
    """One authoritative execution report.

    ``cost`` is the collateral actually moved, taken from venue data. It is **not**
    reconstructed as ``shares * price``: order construction and atomic-amount rounding at
    the venue mean the two can differ, and the ledger must follow the money. ``price`` is
    carried for analysis and telemetry only and never participates in accounting.
    """

    outcome: Outcome
    shares: ShareUnits
    cost: MoneyUnits
    fee: MoneyUnits = ZERO_MONEY
    price: PriceUnits | None = None

    def __post_init__(self) -> None:
        if self.shares <= 0:
            raise DomainError(f"fill shares must be strictly positive, got {self.shares}")
        if self.cost < 0:
            raise DomainError(f"fill cost must not be negative, got {self.cost}")
        if self.fee < 0:
            # A negative fee is a rebate. It belongs in the rebate ledger, where estimated
            # and realised amounts stay distinguishable, not hidden inside the fee total.
            raise DomainError(
                f"fill fee must not be negative, got {self.fee}; record rebates separately"
            )
        if self.price is not None and not 0 <= self.price <= PRICE_SCALE:
            raise DomainError(f"fill price must lie in [0, 1], got {self.price}")


@dataclass(frozen=True, slots=True)
class LedgerState:
    """Complete dual-token accounting state (invariants I01, I02, I03)."""

    n_up: ShareUnits = ZERO_SHARES
    n_down: ShareUnits = ZERO_SHARES
    cost_up: MoneyUnits = ZERO_MONEY
    cost_down: MoneyUnits = ZERO_MONEY
    fees: MoneyUnits = ZERO_MONEY
    realised_rebates: MoneyUnits = ZERO_MONEY
    estimated_rebates: MoneyUnits = ZERO_MONEY

    def __post_init__(self) -> None:
        for name, value in (
            ("n_up", self.n_up),
            ("n_down", self.n_down),
            ("cost_up", self.cost_up),
            ("cost_down", self.cost_down),
            ("fees", self.fees),
            ("realised_rebates", self.realised_rebates),
            ("estimated_rebates", self.estimated_rebates),
        ):
            if value < 0:
                raise DomainError(f"{name} must not be negative, got {value}")
        # Collateral cannot be spent without receiving shares. Guarding this keeps the
        # Term1/Term2 decomposition exact, since it divides cost by share count.
        if self.n_up == 0 and self.cost_up != 0:
            raise DomainError("cost_up is non-zero while n_up is zero")
        if self.n_down == 0 and self.cost_down != 0:
            raise DomainError("cost_down is non-zero while n_down is zero")

    # -- derived state -----------------------------------------------------------------

    @property
    def total_cost(self) -> MoneyUnits:
        """Acquisition cost of **both** outcome tokens. The only cost figure that counts."""
        return MoneyUnits(self.cost_up + self.cost_down)

    @property
    def net_inventory(self) -> ShareUnits:
        """``I = n_up - n_down`` (invariant I02). Directional exposure, not an economic result."""
        return ShareUnits(self.n_up - self.n_down)

    def shares(self, outcome: Outcome) -> ShareUnits:
        return self.n_up if outcome is Outcome.UP else self.n_down

    def cost(self, outcome: Outcome) -> MoneyUnits:
        return self.cost_up if outcome is Outcome.UP else self.cost_down

    def rebate(self, mode: RebateMode) -> MoneyUnits:
        """The rebate figure for ``mode``. There is no default; the caller must choose."""
        if mode is RebateMode.WITHOUT_REBATE:
            return MoneyUnits(0)
        if mode is RebateMode.ESTIMATED_REBATE:
            return self.estimated_rebates
        return self.realised_rebates

    # -- hypothetical settlement -------------------------------------------------------

    def pnl_if_up(self, mode: RebateMode) -> MoneyUnits:
        """``n_up - total_cost - fees + rebate``, exactly (invariant I01)."""
        return MoneyUnits(
            shares_at_par(self.n_up) - self.total_cost - self.fees + self.rebate(mode)
        )

    def pnl_if_down(self, mode: RebateMode) -> MoneyUnits:
        """``n_down - total_cost - fees + rebate``, exactly (invariant I01)."""
        return MoneyUnits(
            shares_at_par(self.n_down) - self.total_cost - self.fees + self.rebate(mode)
        )

    def pnl_if(self, outcome: Outcome, mode: RebateMode) -> MoneyUnits:
        """Hypothetical settlement PnL should ``outcome`` win."""
        if outcome is Outcome.UP:
            return self.pnl_if_up(mode)
        return self.pnl_if_down(mode)

    # -- transitions -------------------------------------------------------------------

    def apply_fill(self, fill: Fill) -> "LedgerState":
        """Apply one execution report and return the resulting state.

        A fill on either side changes both hypothetical PnL branches, because ``total_cost``
        appears in both. Callers must recompute whatever depends on them (invariant I08 --
        the strategy consequence of that is P3/P4 work, not this module's).
        """
        if fill.outcome is Outcome.UP:
            return LedgerState(
                n_up=ShareUnits(self.n_up + fill.shares),
                n_down=self.n_down,
                cost_up=MoneyUnits(self.cost_up + fill.cost),
                cost_down=self.cost_down,
                fees=MoneyUnits(self.fees + fill.fee),
                realised_rebates=self.realised_rebates,
                estimated_rebates=self.estimated_rebates,
            )
        return LedgerState(
            n_up=self.n_up,
            n_down=ShareUnits(self.n_down + fill.shares),
            cost_up=self.cost_up,
            cost_down=MoneyUnits(self.cost_down + fill.cost),
            fees=MoneyUnits(self.fees + fill.fee),
            realised_rebates=self.realised_rebates,
            estimated_rebates=self.estimated_rebates,
        )

    def accrue_estimated_rebate(self, amount: MoneyUnits) -> "LedgerState":
        """Add to the running rebate *estimate*. Never touches the realised figure."""
        if amount < 0:
            raise DomainError(f"rebate accrual must not be negative, got {amount}")
        return self._with(estimated_rebates=MoneyUnits(self.estimated_rebates + amount))

    def set_estimated_rebate(self, amount: MoneyUnits) -> "LedgerState":
        """Replace the rebate *estimate* outright, for a model that recomputes a total."""
        if amount < 0:
            raise DomainError(f"rebate estimate must not be negative, got {amount}")
        return self._with(estimated_rebates=amount)

    def accrue_realised_rebate(self, amount: MoneyUnits) -> "LedgerState":
        """Add a rebate the venue has actually paid. Never touches the estimate."""
        if amount < 0:
            raise DomainError(f"realised rebate must not be negative, got {amount}")
        return self._with(realised_rebates=MoneyUnits(self.realised_rebates + amount))

    def _with(
        self,
        *,
        estimated_rebates: MoneyUnits | None = None,
        realised_rebates: MoneyUnits | None = None,
    ) -> "LedgerState":
        return LedgerState(
            n_up=self.n_up,
            n_down=self.n_down,
            cost_up=self.cost_up,
            cost_down=self.cost_down,
            fees=self.fees,
            realised_rebates=(
                self.realised_rebates if realised_rebates is None else realised_rebates
            ),
            estimated_rebates=(
                self.estimated_rebates if estimated_rebates is None else estimated_rebates
            ),
        )
