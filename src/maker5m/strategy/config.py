"""Immutable strategy configuration.

Composes the replaceable P3 components with the P4 regime parameters. Every value that is
not CONFIRMED carries a status label, so telemetry and the UI can show which numbers are not
established (I18).

There is no ``gamma`` and no ``band_skew``. They are zero in the canonical newer strategy,
and explicit restoring skew damages the terminal residual mechanism, so no such path exists
to be switched on (I12, Canonical §14, §29.3).
"""

from dataclasses import dataclass
from typing import Final

from maker5m.domain import ParameterStatus
from maker5m.numeric.scales import SHARE_SCALE
from maker5m.numeric.units import ShareUnits
from maker5m.strategy.baselot import BaseLot, BaseLotSelector, ConfiguredBaseLotSelector
from maker5m.strategy.centre import ClobMidCentre, QuoteCentre
from maker5m.strategy.errors import StrategyError
from maker5m.strategy.grid import (
    REFERENCE_GRID_POLICY,
    REFERENCE_GRID_ROUNDING,
    GridPolicy,
    GridRounding,
)
from maker5m.strategy.quantization import REFERENCE_TICK_ROUNDING, TickRounding

__all__ = [
    "BAND_HARD_STATUS",
    "DEFAULT_BAND_HARD",
    "DEFAULT_ENDGAME_BAND",
    "DEFAULT_ENDGAME_TILT",
    "ENDGAME_BAND_STATUS",
    "ENDGAME_TILT_STATUS",
    "StrategyConfig",
    "default_config",
]

DEFAULT_ENDGAME_TILT: Final[ShareUnits] = ShareUnits(30 * SHARE_SCALE)
"""Canonical §15.1. The *mechanism* is confirmed; this magnitude is not."""

ENDGAME_TILT_STATUS: Final = ParameterStatus.FITTED
"""O05. Fitted from replay, not established across a large sample."""

DEFAULT_ENDGAME_BAND: Final[ShareUnits] = ShareUnits(5 * SHARE_SCALE)
"""Canonical §15.2. Without a binding gate the target is operationally inert."""

ENDGAME_BAND_STATUS: Final = ParameterStatus.FITTED
"""O06. Mechanism confirmed, magnitude fitted."""

DEFAULT_BAND_HARD: Final[ShareUnits] = ShareUnits(100 * SHARE_SCALE)
"""Canonical §28, §30. A safety wall, never a mean-reversion control (I17)."""

BAND_HARD_STATUS: Final = ParameterStatus.CONFIRMED
"""Canonical §30 lists the ~100-share hard risk band as CONFIRMED."""


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """Everything ``decide()`` needs, and nothing it does not.

    Deliberately small. A knob added "because it might be useful later" is one nobody has
    justified against the frozen sources, and each widens the space of behaviours a replay
    has to account for.

    Validation is limited to what the sources actually establish: each regime parameter must
    be positive. No relationship *between* them is enforced, because none is stated.
    """

    quote_centre: QuoteCentre
    base_lot_selector: BaseLotSelector
    grid_policy: GridPolicy = REFERENCE_GRID_POLICY
    grid_rounding: GridRounding = REFERENCE_GRID_ROUNDING
    tick_rounding: TickRounding = REFERENCE_TICK_ROUNDING
    endgame_tilt: ShareUnits = DEFAULT_ENDGAME_TILT
    endgame_band: ShareUnits = DEFAULT_ENDGAME_BAND
    band_hard: ShareUnits = DEFAULT_BAND_HARD

    def __post_init__(self) -> None:
        if self.endgame_tilt <= 0:
            raise StrategyError(
                f"endgame_tilt must be positive, got {self.endgame_tilt}; "
                "a zero tilt would make the endgame regime inert"
            )
        if self.endgame_band <= 0:
            raise StrategyError(
                f"endgame_band must be positive, got {self.endgame_band}; the gate "
                "inequalities are strict, so a zero band blocks both sides at the target"
            )
        if self.band_hard <= 0:
            raise StrategyError(f"band_hard must be positive, got {self.band_hard}")
        # No relationship between band_hard and endgame_tilt is validated. The frozen sources
        # treat the endgame gate and the hard band as independent eligibility controls
        # (Canonical section 32), and requiring band_hard > endgame_tilt would be an
        # engineering rule of our own invention rather than a strategy rule. An unusual but
        # explicitly configured combination may legitimately suppress both sides at some
        # inventory; that is a deterministic strategy result, not corrupted state.


def default_config(base_lot: BaseLot | None = None) -> StrategyConfig:
    """The reference configuration: CLOB-mid centre and an explicitly configured base lot.

    "Reference" means *what runs by default*, not what the target wallet did. The centre
    source (O01), the base-lot rule (O03), the grid policy (O04), and the tick tie rule (O13)
    are all unresolved; this only picks the documented starting points so later phases have
    something to execute.
    """
    return StrategyConfig(
        quote_centre=ClobMidCentre(),
        base_lot_selector=ConfiguredBaseLotSelector(base_lot or BaseLot.of(15)),
    )
