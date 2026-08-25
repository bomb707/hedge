"""Base lot ``L`` (Canonical §13, Detailed §4) — selection rule remains OPEN (O03).

The observed values ``15 / 20 / 25`` are CONFIRMED; **why** a given market uses one is not
known. Canonical §13 is explicit that ``L`` materially affects both PnL and winner-residual
alignment, and that it must not be permanently hard-coded.

So the selector is an interface. P3 ships only ``ConfiguredBaseLotSelector``, which returns
whatever it was configured with and says so. It does not infer ``L`` from volatility,
liquidity, equity, or time of day — those are the candidate drivers O03 must *test*, and
implementing one here would close the item by assumption.
"""

from dataclasses import dataclass
from typing import Final, Protocol

from maker5m.domain import ParameterStatus
from maker5m.market.state import MarketState
from maker5m.numeric.scales import SHARE_SCALE
from maker5m.numeric.units import ShareUnits
from maker5m.strategy.errors import UnsupportedBaseLotError

__all__ = [
    "BASE_LOT_SELECTION_STATUS",
    "SUPPORTED_BASE_LOTS",
    "BaseLot",
    "BaseLotSelector",
    "ConfiguredBaseLotSelector",
]

SUPPORTED_BASE_LOTS: Final[tuple[int, ...]] = (15, 20, 25)
"""Whole-share base lots observed in the reconstruction. CONFIRMED as a set."""

BASE_LOT_SELECTION_STATUS: Final = ParameterStatus.OPEN
"""O03. The values are confirmed; the rule that picks between them is not."""


@dataclass(frozen=True, slots=True)
class BaseLot:
    """A validated base lot, in ``ShareUnits``."""

    shares: ShareUnits

    def __post_init__(self) -> None:
        if self.shares % SHARE_SCALE:
            raise UnsupportedBaseLotError(
                f"base lot must be a whole number of shares, got {self.shares}"
            )
        whole = self.shares // SHARE_SCALE
        if whole not in SUPPORTED_BASE_LOTS:
            raise UnsupportedBaseLotError(
                f"base lot {whole} is not one of the observed values {SUPPORTED_BASE_LOTS}"
            )

    @classmethod
    def of(cls, whole_shares: int) -> "BaseLot":
        """Build from a whole number of shares, e.g. ``BaseLot.of(15)``."""
        return cls(ShareUnits(whole_shares * SHARE_SCALE))

    @property
    def whole_shares(self) -> int:
        return self.shares // SHARE_SCALE


class BaseLotSelector(Protocol):
    """The replaceable ``choose_base_lot(market_state)`` component of Canonical §13."""

    @property
    def status(self) -> ParameterStatus: ...

    def select(self, state: MarketState) -> BaseLot: ...


@dataclass(frozen=True, slots=True)
class ConfiguredBaseLotSelector:
    """Returns a fixed, explicitly configured base lot.

    Deliberately trivial. It exists so that ``L`` is a visible configuration value carrying an
    ``OPEN`` label rather than a constant buried in the sizing code — the distinction
    invariant I18 turns on.
    """

    base_lot: BaseLot
    status: ParameterStatus = BASE_LOT_SELECTION_STATUS

    def select(self, state: MarketState) -> BaseLot:
        return self.base_lot
