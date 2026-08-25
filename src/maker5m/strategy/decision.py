"""Strategy intent and the decision record.

``DesiredOrder`` is *intent*, not an order. It carries strategy data only — outcome, price,
size. No client order id, no venue order id, no post-only flag, no queue position, no
timestamps. Those belong to P7 and P8, and putting them here would tangle strategy with
execution, which is exactly what the module boundaries exist to prevent.

Every intent is a **BUY of an outcome token**. There is no side field because there is no
other kind: the strategy never sells, hedges, flattens, merges, splits, or converts
(I15, I16, Canonical §18). A suppressed side is simply ``None``.

P4 never emits a cancel. "No desired order on this side" is strategy intent; translating
desired-none against a live order into a CANCEL is the reconciler's job in P7 (I09).
"""

from dataclasses import dataclass

from maker5m.accounting.ledger import LedgerState, RebateMode
from maker5m.domain import Outcome, ParameterStatus
from maker5m.market.phase import Phase
from maker5m.numeric.errors import DomainError
from maker5m.numeric.scales import PRICE_SCALE
from maker5m.numeric.units import MoneyUnits, PriceUnits, ShareUnits
from maker5m.strategy.baselot import BaseLot
from maker5m.strategy.centre import CentreSource, CentreUnavailable, RawCentre
from maker5m.strategy.eligibility import EligibilityResult
from maker5m.strategy.grid import GridPolicy, GridRounding
from maker5m.strategy.quantization import TickRounding

__all__ = [
    "DecisionEconomics",
    "DecisionResult",
    "DecisionTelemetry",
    "DesiredOrder",
    "DesiredOrders",
    "EndgameTelemetry",
    "economics_of",
]


@dataclass(frozen=True, slots=True)
class DesiredOrder:
    """One side's strategy intent: buy ``size`` of ``outcome`` at ``price``."""

    outcome: Outcome
    price: PriceUnits
    size: ShareUnits

    def __post_init__(self) -> None:
        if not 0 <= self.price <= PRICE_SCALE:
            raise DomainError(f"order price must lie in [0, 1], got {self.price}")
        if self.size <= 0:
            raise DomainError(f"order size must be strictly positive, got {self.size}")


@dataclass(frozen=True, slots=True)
class DesiredOrders:
    """At most two strategy intents: one BUY UP and one BUY DOWN (Canonical §23)."""

    up: DesiredOrder | None = None
    down: DesiredOrder | None = None

    def __post_init__(self) -> None:
        if self.up is not None and self.up.outcome is not Outcome.UP:
            raise DomainError("the up slot must hold an UP order")
        if self.down is not None and self.down.outcome is not Outcome.DOWN:
            raise DomainError("the down slot must hold a DOWN order")

    @property
    def count(self) -> int:
        return (self.up is not None) + (self.down is not None)

    @property
    def is_empty(self) -> bool:
        return self.up is None and self.down is None


@dataclass(frozen=True, slots=True)
class DecisionEconomics:
    """Exact settlement economics, recomputed on every decision (I01, Canonical §32).

    Taken straight from the P1 ledger. Nothing here is reconstructed from a displayed price,
    and no accounting formula is restated — a second copy would be a second thing to get
    wrong.

    Both rebate views are carried because O07 is open: hiding the difference between "no
    rebate" and "our estimate of the rebate" would let an unvalidated model quietly decide
    whether a market looks profitable (A6).
    """

    inventory: ShareUnits
    n_up: ShareUnits
    n_down: ShareUnits
    cost_up: MoneyUnits
    cost_down: MoneyUnits
    total_cost: MoneyUnits
    fees: MoneyUnits
    estimated_rebates: MoneyUnits
    realised_rebates: MoneyUnits
    pnl_if_up_without_rebate: MoneyUnits
    pnl_if_down_without_rebate: MoneyUnits
    pnl_if_up_estimated_rebate: MoneyUnits
    pnl_if_down_estimated_rebate: MoneyUnits


def economics_of(ledger: LedgerState) -> DecisionEconomics:
    """Project the authoritative ledger into the decision record."""
    return DecisionEconomics(
        inventory=ledger.net_inventory,
        n_up=ledger.n_up,
        n_down=ledger.n_down,
        cost_up=ledger.cost_up,
        cost_down=ledger.cost_down,
        total_cost=ledger.total_cost,
        fees=ledger.fees,
        estimated_rebates=ledger.estimated_rebates,
        realised_rebates=ledger.realised_rebates,
        pnl_if_up_without_rebate=ledger.pnl_if_up(RebateMode.WITHOUT_REBATE),
        pnl_if_down_without_rebate=ledger.pnl_if_down(RebateMode.WITHOUT_REBATE),
        pnl_if_up_estimated_rebate=ledger.pnl_if_up(RebateMode.ESTIMATED_REBATE),
        pnl_if_down_estimated_rebate=ledger.pnl_if_down(RebateMode.ESTIMATED_REBATE),
    )


@dataclass(frozen=True, slots=True)
class EndgameTelemetry:
    """What the endgame regime decided, and the economics it is judged against.

    The settlement edges use the **estimated** rebate, matching Canonical §32's formula. The
    no-rebate view of the same quantities is in :class:`DecisionEconomics`, so the two are
    never conflated.
    """

    favourite: Outcome
    target_inventory: ShareUnits
    distance_to_target: ShareUnits
    tilt: ShareUnits
    tilt_status: ParameterStatus
    band: ShareUnits
    band_status: ParameterStatus
    gate_up_allowed: bool
    gate_down_allowed: bool
    settlement_edge_favourite: MoneyUnits
    settlement_edge_underdog: MoneyUnits


@dataclass(frozen=True, slots=True)
class DecisionTelemetry:
    """Everything needed to explain and replay one decision.

    Deterministic and free of wall-clock, queue, transport, and network data — those are P8
    and P11. Candidate prices and sizes are recorded whether or not the side was emitted, so
    the A5 invariant (ENDGAME changes eligibility only) is directly checkable from the record
    rather than inferred.

    Candidate favourite price and size are retained exactly, which is what Canonical §17's
    "incremental cost of acquiring more favourite shares" needs. No money conversion is
    performed: authoritative cost is the venue's collateral movement (P1 ``Fill``), and
    pre-transport size is not yet quantized for submission (P7).
    """

    phase: Phase

    centre_source: CentreSource
    centre_status: ParameterStatus
    raw_centre: RawCentre | None
    centre_unavailable: CentreUnavailable | None
    tick_rounding: TickRounding
    tick_rounding_status: ParameterStatus
    quantized_centre: PriceUnits | None
    tick: PriceUnits

    grid_policy: GridPolicy
    grid_policy_status: ParameterStatus
    grid_rounding: GridRounding
    base_lot: BaseLot | None
    base_lot_status: ParameterStatus

    candidate_up_price: PriceUnits | None
    candidate_up_size: ShareUnits | None
    candidate_down_price: PriceUnits | None
    candidate_down_size: ShareUnits | None

    eligibility: EligibilityResult
    band_hard: ShareUnits
    band_hard_status: ParameterStatus
    endgame: EndgameTelemetry | None
    economics: DecisionEconomics


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """The output of one ``StrategyEngine.decide`` call."""

    orders: DesiredOrders
    telemetry: DecisionTelemetry
