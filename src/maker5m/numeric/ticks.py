"""Tick grid helpers and the venue order-size quantisation primitive.

Two unrelated notions of "granularity" live here and must not be confused
(``docs/OPEN_ITEMS.md`` O04 context, and section 13 of the P1 brief):

* **Price tick** -- the venue's quotable price grid (``0.1`` / ``0.01`` / ``0.001`` /
  ``0.0001``). Canonical section 8.2 confirms ``0.01`` for this market universe.
* **Order-size quantisation** -- the official client rounds a *submitted* order size to two
  decimals. This is a transport concern applied on the way out.

Neither is the strategy's **5-share inventory lattice**, which is a P3 concept and appears
nowhere in this module. Nothing here rounds anything the ledger records.

Dynamic per-market tick handling is not implemented: P1 only fixes the representation.
"""

from typing import Final

from maker5m.numeric.errors import DomainError, InexactError
from maker5m.numeric.scales import PRICE_SCALE, SCALE_DECIMALS, SHARE_SCALE
from maker5m.numeric.units import PriceUnits, ShareUnits

__all__ = [
    "SUPPORTED_TICK_SIZES",
    "TICK_0_0001",
    "TICK_0_001",
    "TICK_0_0025",
    "TICK_0_005",
    "TICK_0_01",
    "TICK_0_1",
    "VENUE_ORDER_SIZE_DECIMALS",
    "is_price_aligned",
    "is_supported_tick",
    "price_to_ticks",
    "quantize_order_size",
    "ticks_to_price",
]

TICK_0_1: Final = PriceUnits(PRICE_SCALE // 10)
TICK_0_01: Final = PriceUnits(PRICE_SCALE // 100)
TICK_0_005: Final = PriceUnits(PRICE_SCALE // 200)
TICK_0_0025: Final = PriceUnits(PRICE_SCALE // 400)
TICK_0_001: Final = PriceUnits(PRICE_SCALE // 1_000)
TICK_0_0001: Final = PriceUnits(PRICE_SCALE // 10_000)

SUPPORTED_TICK_SIZES: Final = (
    TICK_0_1,
    TICK_0_01,
    TICK_0_005,
    TICK_0_0025,
    TICK_0_001,
    TICK_0_0001,
)
"""Tick sizes the official CLOB client currently supports. All exact at this scale.

Verified against ``polymarket-client==0.6.0``, which declares::

    TickSize = Literal["0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001"]

``0.005`` and ``0.0025`` were added to the venue after this set was first written. Both are
exactly representable at ``PRICE_SCALE = 1_000_000`` (``5_000`` and ``2_500`` price units), so
this is a venue-capability correction and **not** a numeric-scale problem — O10 stays closed.

These are the venue's *legal order price increments*. They are not the replica's quote grid,
which remains ``0.01`` from the frozen strategy evidence (Canonical §8.2). The two concepts
are kept apart deliberately; see :mod:`maker5m.feeds.venue`.
"""

VENUE_ORDER_SIZE_DECIMALS: Final = 2
"""Decimals the official client keeps when it quantises a *submitted* order size.

Transport-layer only. Never applied to a ledger input, and unrelated to the 5-share lattice.
"""


def is_supported_tick(tick: PriceUnits) -> bool:
    """Whether ``tick`` is one of the venue's documented tick sizes."""
    return tick in SUPPORTED_TICK_SIZES


def _require_positive_tick(tick: PriceUnits) -> None:
    if tick <= 0:
        raise DomainError(f"tick must be positive, got {tick}")


def is_price_aligned(price: PriceUnits, tick: PriceUnits) -> bool:
    """Whether ``price`` sits exactly on the ``tick`` grid.

    ``0.63`` is aligned to ``0.01``; ``0.631`` is not.
    """
    _require_positive_tick(tick)
    return price % tick == 0


def price_to_ticks(price: PriceUnits, tick: PriceUnits) -> int:
    """Exact count of ticks. Raises rather than rounding an off-grid price."""
    _require_positive_tick(tick)
    if price % tick:
        raise InexactError(f"price {price} is not aligned to tick {tick}")
    return price // tick


def ticks_to_price(ticks: int, tick: PriceUnits) -> PriceUnits:
    """Inverse of :func:`price_to_ticks`, range-checked to ``[0, 1]``."""
    _require_positive_tick(tick)
    value = ticks * tick
    if not 0 <= value <= PRICE_SCALE:
        raise DomainError(f"price out of [0, 1]: {ticks} ticks of {tick}")
    return PriceUnits(value)


def quantize_order_size(
    shares: ShareUnits, *, decimals: int = VENUE_ORDER_SIZE_DECIMALS
) -> ShareUnits:
    """Quantise a size for **submission**, truncating toward zero.

    Truncation, not rounding: a submitted order must never exceed the size the strategy
    intended, in either direction. The result is what the venue is asked for; what the
    ledger records is whatever the venue reports back as filled.

    This is a transport primitive. It is not the strategy's inventory lattice, and calling
    it on a ledger quantity would be a defect.
    """
    if not 0 <= decimals <= SCALE_DECIMALS:
        raise DomainError(f"decimals must lie in [0, {SCALE_DECIMALS}], got {decimals}")
    factor: int = 10**decimals
    step = SHARE_SCALE // factor
    magnitude = abs(shares) // step * step
    return ShareUnits(-magnitude if shares < 0 else magnitude)
