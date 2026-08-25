"""Up-space translation (Canonical §5, Detailed §5).

One UP token plus one DOWN token settles to exactly ``$1.00``, so:

```text
BUY DOWN @ d   ==   SELL UP @ (1 - d)
```

which lets a two-token buy-only strategy be reasoned about as a single synthetic UP book:

| Venue action        | Up-space meaning   | Inventory effect |
|---------------------|--------------------|------------------|
| ``BUY UP @ p``      | synthetic BID @ p  | ``I += q``       |
| ``BUY DOWN @ d``    | synthetic ASK @ 1-d| ``I -= q``       |

The complement is exact integer arithmetic on ``PriceUnits``: ``PRICE_SCALE - p``. It is an
involution, it maps the endpoints to each other, and it preserves tick alignment for every
supported tick, because each of them divides ``PRICE_SCALE``.

Nothing here decides anything. ``I = n_up - n_down`` remains owned by the ledger (I02); these
are pure translations.
"""

from enum import Enum

from maker5m.domain import Outcome
from maker5m.numeric.errors import DomainError
from maker5m.numeric.scales import PRICE_SCALE
from maker5m.numeric.units import PriceUnits

__all__ = ["UpSpaceSide", "complement", "to_upspace", "to_venue"]


class UpSpaceSide(Enum):
    """Which side of the synthetic UP book a venue action represents."""

    BID = "BID"
    """``BUY UP``: raises inventory."""

    ASK = "ASK"
    """``BUY DOWN``: lowers inventory."""

    @property
    def outcome(self) -> Outcome:
        """The venue token this side is bought in."""
        return Outcome.UP if self is UpSpaceSide.BID else Outcome.DOWN


def _require_probability(price: PriceUnits) -> None:
    if not 0 <= price <= PRICE_SCALE:
        raise DomainError(f"price must lie in [0, 1], got {price}")


def complement(price: PriceUnits) -> PriceUnits:
    """``1 - price``, exactly.

    ``complement(0) == 1``, ``complement(1) == 0``, and
    ``complement(complement(p)) == p`` for every representable price.
    """
    _require_probability(price)
    return PriceUnits(PRICE_SCALE - price)


def to_upspace(outcome: Outcome, venue_price: PriceUnits) -> tuple[UpSpaceSide, PriceUnits]:
    """Translate a venue BUY into its synthetic Up-space side and price."""
    _require_probability(venue_price)
    if outcome is Outcome.UP:
        return UpSpaceSide.BID, venue_price
    return UpSpaceSide.ASK, complement(venue_price)


def to_venue(side: UpSpaceSide, up_price: PriceUnits) -> tuple[Outcome, PriceUnits]:
    """Translate a synthetic Up-space quote back into the venue BUY that expresses it."""
    _require_probability(up_price)
    if side is UpSpaceSide.BID:
        return Outcome.UP, up_price
    return Outcome.DOWN, complement(up_price)
