"""Zero-synthetic-spread quote construction (Canonical §8.1, §29.2, Detailed §9).

The target's Up-space bid and ask sit at the same price::

    delta_ticks = 0
    bid_price = ask_price = round_tick(C)

which translates to two venue BUY orders::

    BUY UP   @ C
    BUY DOWN @ 1 - C

This module builds the DOWN price by **complementing the already-quantized centre**, not by
independently rounding ``1 - C_raw``. That choice is load-bearing. Canonical §32 writes the
latter::

    px_up   = round_to_tick(centre)
    px_down = round_to_tick(1.0 - centre)

and at an exact half tick that construction breaks zero spread under ``HALF_UP`` and
``HALF_DOWN``: at every one of the 100 tie points on the ``0.01`` grid the two prices stop
complementing each other and a one-tick synthetic spread appears, violating I05. Complementing
the quantized centre is exact under **every** tie policy, so the unresolved O13 tie rule
cannot leak into the CONFIRMED zero-spread property.

Zero spread is asserted at construction, so a future edit that reintroduces a spread fails
immediately rather than quietly changing the strategy.

Post-only safety is **not** applied here. If a zero-spread quote would cross the venue, P7
suppresses or adjusts it and records the deviation (I05, I06). P3 represents the strategy's
intent exactly.
"""

from dataclasses import dataclass

from maker5m.numeric.errors import DomainError
from maker5m.numeric.scales import PRICE_SCALE
from maker5m.numeric.units import PriceUnits
from maker5m.strategy.upspace import complement

__all__ = ["QuotePrices", "build_quote_prices"]


@dataclass(frozen=True, slots=True)
class QuotePrices:
    """The two venue prices expressing one zero-spread synthetic quote."""

    centre: PriceUnits
    """The tick-quantized Up-space centre. Both synthetic sides sit here."""

    up_buy_price: PriceUnits
    """``BUY UP`` price. Equals ``centre``."""

    down_buy_price: PriceUnits
    """``BUY DOWN`` price. Equals ``1 - centre``."""

    tick: PriceUnits
    """The grid the centre was quantized onto. Kept so alignment stays checkable."""

    def __post_init__(self) -> None:
        if self.tick <= 0:
            raise DomainError(f"tick must be positive, got {self.tick}")
        if self.centre % self.tick:
            raise DomainError(
                f"centre {self.centre} is not aligned to tick {self.tick}; "
                f"quantize it before building quote prices"
            )
        if self.up_buy_price != self.centre:
            raise DomainError("UP buy price must equal the centre (zero synthetic spread)")
        if self.up_buy_price + self.down_buy_price != PRICE_SCALE:
            raise DomainError(
                "UP and DOWN buy prices must sum to exactly 1.00; "
                f"got {self.up_buy_price} + {self.down_buy_price}"
            )

    @property
    def synthetic_bid(self) -> PriceUnits:
        """The Up-space bid: the UP buy price as-is."""
        return self.up_buy_price

    @property
    def synthetic_ask(self) -> PriceUnits:
        """The Up-space ask: ``1 - down_buy_price``. Always equal to the bid."""
        return complement(self.down_buy_price)

    @property
    def synthetic_spread(self) -> int:
        """Always exactly zero. Kept so telemetry can assert it rather than assume it."""
        return self.synthetic_ask - self.synthetic_bid


def build_quote_prices(centre: PriceUnits, tick: PriceUnits) -> QuotePrices:
    """Build both venue prices from a tick-quantized centre."""
    return QuotePrices(
        centre=centre,
        up_buy_price=centre,
        down_buy_price=complement(centre),
        tick=tick,
    )
