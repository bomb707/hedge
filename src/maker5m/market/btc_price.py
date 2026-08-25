"""External BTC price representation.

``PriceUnits`` cannot be reused here. It is a probability constrained to ``[0, 1]``, and a
BTC spot price is neither. Reusing it would be a silent domain error of exactly the kind the
distinct-newtype design exists to prevent.

The scale is **not frozen**. Unlike the share/money/price scales -- which O10 closed against
Polymarket's published ``COLLATERAL_TOKEN_DECIMALS`` and tick set -- this repository holds no
authoritative evidence for the precision of the external BTC feed. Guessing one would repeat
precisely the failure O10 existed to prevent: a scale too coarse to represent a real feed
value would either round the input silently or halt the bot in production.

So a ``BtcPrice`` is **self-describing**: it carries its own ``scale_decimals`` alongside its
integer units. That keeps the value exact and float-free while leaving the scale question
genuinely open for P6 to close from observed traffic (``docs/OPEN_ITEMS.md`` O12).
Comparison between two different scales is exact -- never a raw integer comparison, which
would be meaningless across scales.

Used for both the live spot price and the market strike, which are the same kind of quantity.
"""

from dataclasses import dataclass
from typing import Final

from maker5m.numeric.errors import DomainError
from maker5m.numeric.units import parse_fixed_point

__all__ = ["MAX_BTC_SCALE_DECIMALS", "BtcPrice"]

MAX_BTC_SCALE_DECIMALS: Final[int] = 18
"""Sanity bound only. Not a claim about the feed -- see O12."""


@dataclass(frozen=True, slots=True, order=False)
class BtcPrice:
    """An exact BTC price: ``units * 10**-scale_decimals``.

    Immutable and float-free. Two prices at the same scale compare as integers; at different
    scales they are normalised exactly before comparison.
    """

    units: int
    scale_decimals: int

    def __post_init__(self) -> None:
        if not 0 <= self.scale_decimals <= MAX_BTC_SCALE_DECIMALS:
            raise DomainError(
                f"scale_decimals must lie in [0, {MAX_BTC_SCALE_DECIMALS}], "
                f"got {self.scale_decimals}"
            )
        if self.units < 0:
            raise DomainError(f"BTC price must not be negative, got {self.units}")

    @classmethod
    def parse(cls, text: str, *, scale_decimals: int) -> "BtcPrice":
        """Parse a decimal string exactly, using the one strict parser in the project.

        Raises rather than rounding if the feed sends more precision than
        ``scale_decimals`` holds -- the same fail-closed rule as the frozen scales, which is
        what will surface a wrong scale choice as a halt instead of as silent corruption.
        """
        if not 0 <= scale_decimals <= MAX_BTC_SCALE_DECIMALS:
            raise DomainError(
                f"scale_decimals must lie in [0, {MAX_BTC_SCALE_DECIMALS}], got {scale_decimals}"
            )
        units = parse_fixed_point(text, decimals=scale_decimals, field="btc_price")
        return cls(units=units, scale_decimals=scale_decimals)

    def rescaled_to(self, scale_decimals: int) -> "BtcPrice":
        """Convert to another scale, exactly. Raises if the conversion would lose a digit."""
        if scale_decimals == self.scale_decimals:
            return self
        if scale_decimals > self.scale_decimals:
            factor: int = 10 ** (scale_decimals - self.scale_decimals)
            return BtcPrice(self.units * factor, scale_decimals)
        divisor: int = 10 ** (self.scale_decimals - scale_decimals)
        if self.units % divisor:
            raise DomainError(f"rescaling {self} to {scale_decimals} decimals would lose precision")
        return BtcPrice(self.units // divisor, scale_decimals)

    def compare(self, other: "BtcPrice") -> int:
        """Exact three-way comparison across scales: ``-1``, ``0``, or ``1``."""
        common = max(self.scale_decimals, other.scale_decimals)
        left = self.rescaled_to(common).units
        right = other.rescaled_to(common).units
        if left < right:
            return -1
        return 1 if left > right else 0

    def __str__(self) -> str:
        if self.scale_decimals == 0:
            return str(self.units)
        factor: int = 10**self.scale_decimals
        whole, fraction = divmod(self.units, factor)
        return f"{whole}.{fraction:0{self.scale_decimals}d}"
