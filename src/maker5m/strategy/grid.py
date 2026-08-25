"""The 5-share inventory lattice, and the two unresolved O04 target-selection readings.

The lattice itself is CONFIRMED (Canonical §12, Detailed §12). Order sizes are not fixed
nominal lots; each side is sized to land net inventory on a multiple of 5 shares::

    up_size   = up_target   - I
    down_size = I - down_target

**Which lattice point each side aims at is OPEN (O04).** The two frozen sources disagree
numerically on the DOWN side, and the modular fingerprint cannot arbitrate because both
readings satisfy it. Using the shared worked example ``I = -28.63``, ``L = 15``::

                                  UP target   UP size   DOWN target   DOWN size
    Canonical §12.1 formula          -15       13.63        -45         16.37
    Detailed §12 + d3_grid.png       -15       13.63        -30          1.37

Both readings are implemented as named policies. Neither is called correct, and the document
precedence rule is **not** treated as empirical proof — it only decides which one runs by
default so that later phases have something to execute. O04 closes on replay evidence against
the target wallet's reconstructed per-market size sequences, not on argument.

Grid rounding ties
------------------
Canonical §12.1 expresses the target as ``round(I / grid + L / grid) * grid``. In Python that
is banker's rounding, and it changes the result whenever ``(I ± L) / 5`` lands exactly on
``.5`` — which real fractional inventories do reach. Whether that is intended or incidental is
unstated, and P0 recorded it inside O04. So the tie rule here is an explicit named policy and
the built-in ``round`` is not used anywhere.

All arithmetic is integer ``ShareUnits``. Inventory is never rounded (I03); only *targets* are.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Final

from maker5m.domain import ParameterStatus
from maker5m.numeric.errors import DomainError
from maker5m.numeric.scales import SHARE_SCALE
from maker5m.numeric.units import ShareUnits
from maker5m.strategy.baselot import BaseLot

__all__ = [
    "GRID",
    "GRID_POLICY_STATUS",
    "REFERENCE_GRID_POLICY",
    "REFERENCE_GRID_ROUNDING",
    "GridPlan",
    "GridPolicy",
    "GridRounding",
    "is_on_grid",
    "plan_grid",
    "round_to_grid",
]

GRID: Final[int] = 5 * SHARE_SCALE
"""The lattice step: exactly 5 shares. CONFIRMED (Canonical §12, §30)."""


class GridRounding(Enum):
    """Tie rule for snapping an offset inventory to the lattice.

    Only reached when the offset lands exactly halfway between two lattice points. Named
    rather than inherited from Python's ``round`` so the choice is visible (O04).
    """

    HALF_EVEN = "HALF_EVEN"
    """Tie goes to the even lattice index. Matches Python's ``round``, chosen explicitly."""

    HALF_UP = "HALF_UP"
    """Tie goes to the higher lattice point."""

    HALF_DOWN = "HALF_DOWN"
    """Tie goes to the lower lattice point."""


class GridPolicy(Enum):
    """The two readings of the DOWN-side target. Neither is proven (O04)."""

    CANONICAL_OFFSET = "CANONICAL_OFFSET"
    """Both targets are ``I ± L`` snapped to the lattice — the literal Canonical §12.1 form."""

    OBSERVED_ADJACENT = "OBSERVED_ADJACENT"
    """UP as above; DOWN aims at the lattice point immediately below ``I`` — Detailed §12."""


REFERENCE_GRID_POLICY: Final = GridPolicy.CANONICAL_OFFSET
"""Runs by default because the precedence rule says Canonical wins a conflict.

That is a document rule, not evidence. See O04.
"""

REFERENCE_GRID_ROUNDING: Final = GridRounding.HALF_EVEN
GRID_POLICY_STATUS: Final = ParameterStatus.OPEN
"""O04. Must not be presented as CONFIRMED until replay evidence closes it."""


def is_on_grid(inventory: ShareUnits) -> bool:
    """Whether an inventory sits exactly on the 5-share lattice."""
    return inventory % GRID == 0


def round_to_grid(
    value: ShareUnits, rounding: GridRounding = REFERENCE_GRID_ROUNDING
) -> ShareUnits:
    """Snap to the nearest lattice point, with an explicit tie rule.

    Integer arithmetic throughout, and correct for negative inventories: ``divmod`` floors, so
    the remainder is always in ``[0, GRID)`` and the tie test is a comparison of
    ``2 * remainder`` against ``GRID``.
    """
    index, remainder = divmod(value, GRID)
    doubled = 2 * remainder

    if doubled == GRID:
        round_up = rounding is GridRounding.HALF_UP or (
            rounding is GridRounding.HALF_EVEN and index % 2 == 1
        )
    else:
        round_up = doubled > GRID

    if round_up:
        index += 1
    return ShareUnits(index * GRID)


def _canonical_up_target(
    inventory: ShareUnits, base_lot: BaseLot, rounding: GridRounding
) -> ShareUnits:
    """``round_to_grid(I + L)``, corrected to stay strictly above ``I``.

    The correction is Canonical §12.1's ``if t <= I: t += grid``. Without it a target equal to
    or below current inventory would yield a zero or negative buy size.
    """
    target = round_to_grid(ShareUnits(inventory + base_lot.shares), rounding)
    if target <= inventory:
        target = ShareUnits(target + GRID)
    return target


def _canonical_down_target(
    inventory: ShareUnits, base_lot: BaseLot, rounding: GridRounding
) -> ShareUnits:
    """``round_to_grid(I - L)``, corrected to stay strictly below ``I``.

    Canonical §12.1 gives the upward correction explicitly and says only that "equivalent
    logic should be used for the downward target". The strict mirror below is therefore the
    **reference interpretation, not evidence** — the mirror is recorded as unresolved inside
    O04 along with the rest of the DOWN-side question.
    """
    target = round_to_grid(ShareUnits(inventory - base_lot.shares), rounding)
    if target >= inventory:
        target = ShareUnits(target - GRID)
    return target


def _adjacent_down_target(inventory: ShareUnits) -> ShareUnits:
    """The lattice point immediately below ``I``, independent of ``L``.

    This is what Detailed §12 and ``d3_grid.png`` show: at ``I = -28.63`` the DOWN side aims
    at ``-30`` for a size of ``1.37``, while the same figure shows the UP side aiming at
    ``-15`` for ``13.63``. The asymmetry is what the sources depict; it is reproduced rather
    than rationalised.

    Behaviour when ``I`` already sits on the lattice is **not** shown by either source. The
    strict-below correction used here (target becomes ``I - 5``, size ``5``) is the minimal
    inference needed to avoid a zero-size order, and is recorded as unresolved under O04.
    """
    index = inventory // GRID  # floors, so this is the lattice point at or below I
    target = ShareUnits(index * GRID)
    if target >= inventory:
        target = ShareUnits(target - GRID)
    return target


@dataclass(frozen=True, slots=True)
class GridPlan:
    """Where each side aims and how large the resulting order is.

    Sizes are always strictly positive: a target that did not move inventory would not be an
    order. Every size satisfies the modular fingerprint of Canonical §12.2::

        up_size   ≡ (-I) mod 5
        down_size ≡ (+I) mod 5
    """

    policy: GridPolicy
    inventory: ShareUnits
    base_lot: BaseLot
    up_target: ShareUnits
    down_target: ShareUnits
    up_size: ShareUnits
    down_size: ShareUnits

    def __post_init__(self) -> None:
        if self.up_size <= 0 or self.down_size <= 0:
            raise DomainError(
                f"grid sizes must be strictly positive, got up={self.up_size} down={self.down_size}"
            )
        if not is_on_grid(self.up_target) or not is_on_grid(self.down_target):
            raise DomainError(
                f"grid targets must sit on the lattice, got "
                f"up={self.up_target} down={self.down_target}"
            )


def plan_grid(
    inventory: ShareUnits,
    base_lot: BaseLot,
    policy: GridPolicy = REFERENCE_GRID_POLICY,
    rounding: GridRounding = REFERENCE_GRID_ROUNDING,
) -> GridPlan:
    """Size both sides from true fractional inventory.

    ``inventory`` is passed explicitly rather than read from a state object, so the function
    stays pure and independently testable and no second copy of inventory exists. The
    authoritative value is always ``state.ledger.net_inventory`` (I02, I03).
    """
    up_target = _canonical_up_target(inventory, base_lot, rounding)
    if policy is GridPolicy.CANONICAL_OFFSET:
        down_target = _canonical_down_target(inventory, base_lot, rounding)
    else:
        down_target = _adjacent_down_target(inventory)

    return GridPlan(
        policy=policy,
        inventory=inventory,
        base_lot=base_lot,
        up_target=up_target,
        down_target=down_target,
        up_size=ShareUnits(up_target - inventory),
        down_size=ShareUnits(inventory - down_target),
    )
