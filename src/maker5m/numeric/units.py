"""Fixed-point domain types, exact parsing, and the named cross-domain conversions.

Representation choice: ``typing.NewType`` over ``int``.

* ``ShareUnits``, ``MoneyUnits``, and ``PriceUnits`` are distinct to the type checker, so a
  value of one domain cannot be assigned to another. Because ``int + int`` widens to plain
  ``int``, any mixed-domain arithmetic also fails to type-check the moment its result is
  stored or passed anywhere annotated -- which under ``mypy --strict`` is everywhere.
* At runtime they are ordinary ``int`` objects: exact, immutable, hashable, cheap to
  compare, and free of any binary floating-point state.

Measured on CPython 3.12 (this machine): a raw ``int`` add is ~28 ns and re-wrapping the
result via ``ShareUnits(...)`` costs ~60 ns, against ~232 ns for a frozen-dataclass wrapper.
At this strategy's event rates -- of the order of 100 fills and a few thousand book updates
per 300 s market -- the difference is far below the noise floor, so the cheaper option was
taken and no wrapper class is justified.

Everything here is pure: no clock, no I/O, no randomness (invariant I20).
"""

import re
from typing import Final, Literal, NewType

from maker5m.numeric.errors import (
    DomainError,
    InexactError,
    NotRepresentableError,
    ParseError,
)
from maker5m.numeric.scales import (
    MONEY_SCALE,
    PRICE_SCALE,
    SCALE_DECIMALS,
    SHARE_SCALE,
)

__all__ = [
    "ZERO_MONEY",
    "ZERO_SHARES",
    "MoneyUnits",
    "PriceUnits",
    "Rounding",
    "RoundingMode",
    "ShareUnits",
    "format_money",
    "format_price",
    "format_share",
    "money_from_whole",
    "notional_cost",
    "parse_fixed_point",
    "parse_money",
    "parse_price",
    "parse_share",
    "price_from_whole",
    "share_from_whole",
    "shares_at_par",
    "to_display_float",
]

ShareUnits = NewType("ShareUnits", int)
"""A signed quantity of outcome tokens, in units of ``1 / SHARE_SCALE`` of a share.

Signed because net inventory ``I = n_up - n_down`` is a ``ShareUnits`` (invariant I02).
Fill quantities are separately required to be strictly positive.
"""

MoneyUnits = NewType("MoneyUnits", int)
"""A signed USDC amount in units of ``1 / MONEY_SCALE`` dollars.

Signed because PnL is a ``MoneyUnits``. Costs, fees, and rebates are separately required to
be non-negative.
"""

PriceUnits = NewType("PriceUnits", int)
"""A probability / share price in units of ``1 / PRICE_SCALE``, so ``1.0`` is ``PRICE_SCALE``."""


RoundingMode = Literal["FLOOR", "CEILING", "EXACT"]
"""The rounding modes accepted where scale is reduced. Checked by the type checker."""


ZERO_SHARES: Final = ShareUnits(0)
"""Named zero, so dataclass defaults need no call expression."""

ZERO_MONEY: Final = MoneyUnits(0)
"""Named zero, so dataclass defaults need no call expression."""


class Rounding:
    """Explicit rounding modes for the one operation that can reduce scale.

    There is no default anywhere: a caller that reduces scale must name the direction, so
    that rounding is always a documented decision at a named boundary
    (``docs/ARCHITECTURE_SSOT.md`` section 6.2).
    """

    FLOOR: Final = "FLOOR"
    """Toward negative infinity."""

    CEILING: Final = "CEILING"
    """Toward positive infinity."""

    EXACT: Final = "EXACT"
    """Raise :class:`InexactError` rather than round."""


# A plain decimal literal. Deliberately strict: no exponent, no underscores, no whitespace,
# no bare sign, no leading or trailing dot, and ASCII digits only -- ``str.isdigit()`` would
# accept characters such as superscripts and non-Latin digits.
_DECIMAL_RE: Final = re.compile(r"^[+-]?[0-9]+(?:\.[0-9]+)?$")

# ShareUnits * PriceUnits is money scaled by SHARE_SCALE * PRICE_SCALE; dividing by this
# divisor brings it back to MoneyUnits. Exact by construction for the frozen scales.
_NOTIONAL_DIVISOR: Final = SHARE_SCALE * PRICE_SCALE // MONEY_SCALE

# One share settles to exactly $1.00, so the shares -> money conversion is a pure rescale.
# Guarded here so that a future scale change cannot silently make it lossy.
if MONEY_SCALE % SHARE_SCALE != 0:  # pragma: no cover - frozen scales make this dead code
    raise AssertionError("MONEY_SCALE must be a whole multiple of SHARE_SCALE")
_PAR_RESCALE: Final = MONEY_SCALE // SHARE_SCALE


def _parse_fixed_point(text: object, *, field: str, decimals: int = SCALE_DECIMALS) -> int:
    """Parse a plain decimal string into exact integer units, or raise.

    Excess fractional digits are accepted only when every excess digit is ``0``; otherwise
    the value carries precision the frozen scale cannot hold and is rejected. Authoritative
    ledger inputs are never silently rounded.
    """
    if not isinstance(text, str):
        raise ParseError(f"{field}: expected a decimal string, got {type(text).__name__}")
    if _DECIMAL_RE.match(text) is None:
        raise ParseError(f"{field}: not a plain decimal string: {text!r}")

    negative = text[0] == "-"
    body = text[1:] if text[0] in "+-" else text
    integer_text, _, fraction_text = body.partition(".")

    if len(fraction_text) > decimals:
        excess = fraction_text[decimals:]
        if excess.strip("0"):
            raise NotRepresentableError(
                f"{field}: {text!r} needs more than {decimals} decimal places; "
                f"the frozen scale cannot represent it exactly"
            )
        fraction_text = fraction_text[:decimals]

    factor: int = 10**decimals
    # ``or "0"`` covers decimals == 0, where the padded fraction is the empty string.
    value = int(integer_text) * factor + int(fraction_text.ljust(decimals, "0") or "0")
    return -value if negative else value


def parse_fixed_point(text: str, *, decimals: int, field: str = "value") -> int:
    """Parse a plain decimal string into integer units at an arbitrary scale.

    The public form of the strict parser, for domains whose scale is not one of the three
    frozen ones -- currently only BTC prices, whose scale is still OPEN (O12). Exposed so
    that no second decimal parser is ever written: a parallel implementation would be a
    parallel numeric representation, and would drift from these rules.
    """
    if decimals < 0:
        raise DomainError(f"decimals must not be negative, got {decimals}")
    return _parse_fixed_point(text, field=field, decimals=decimals)


def _require_non_negative(value: int, *, field: str) -> None:
    if value < 0:
        raise DomainError(f"{field}: must not be negative, got {value}")


def parse_share(text: str, *, allow_negative: bool = True) -> ShareUnits:
    """Parse a share quantity. Signed by default because inventory is signed."""
    value = _parse_fixed_point(text, field="share")
    if not allow_negative:
        _require_non_negative(value, field="share")
    return ShareUnits(value)


def parse_money(text: str, *, allow_negative: bool = True) -> MoneyUnits:
    """Parse a USDC amount. Signed by default because PnL is signed."""
    value = _parse_fixed_point(text, field="money")
    if not allow_negative:
        _require_non_negative(value, field="money")
    return MoneyUnits(value)


def parse_price(text: str) -> PriceUnits:
    """Parse a share price / probability. Constrained to ``[0, 1]``."""
    value = _parse_fixed_point(text, field="price")
    if not 0 <= value <= PRICE_SCALE:
        raise DomainError(f"price: must lie in [0, 1], got {text!r}")
    return PriceUnits(value)


def share_from_whole(whole: int) -> ShareUnits:
    """Exact constructor from a whole number of shares."""
    return ShareUnits(whole * SHARE_SCALE)


def money_from_whole(whole: int) -> MoneyUnits:
    """Exact constructor from a whole number of dollars."""
    return MoneyUnits(whole * MONEY_SCALE)


def price_from_whole(numerator: int, denominator: int) -> PriceUnits:
    """Exact constructor from a rational probability, e.g. ``price_from_whole(63, 100)``."""
    if denominator <= 0:
        raise DomainError(f"price: denominator must be positive, got {denominator}")
    scaled = numerator * PRICE_SCALE
    if scaled % denominator:
        raise InexactError(
            f"price: {numerator}/{denominator} is not representable in {SCALE_DECIMALS} decimals"
        )
    value = scaled // denominator
    if not 0 <= value <= PRICE_SCALE:
        raise DomainError(f"price: must lie in [0, 1], got {numerator}/{denominator}")
    return PriceUnits(value)


def shares_at_par(shares: ShareUnits) -> MoneyUnits:
    """Settlement value of ``shares`` winning tokens: one share pays exactly ``$1.00``.

    The named boundary for the shares -> money conversion. It is numerically an identity
    while ``SHARE_SCALE == MONEY_SCALE``, but it must stay explicit: it is a genuine change
    of domain, and it is where a future scale change would need to be handled.
    """
    return MoneyUnits(shares * _PAR_RESCALE)


def notional_cost(shares: ShareUnits, price: PriceUnits, *, rounding: RoundingMode) -> MoneyUnits:
    """``shares * price`` as money, with an explicitly named rounding mode.

    **The ledger does not use this.** Authoritative cost is the collateral amount the venue
    reports, not a reconstruction from a displayed price (see the ``Fill`` contract). This
    helper exists for analytics, tests, and any future adapter that must construct a
    notional -- and it is exact for every price on the documented tick grid.
    """
    product = shares * price
    if product % _NOTIONAL_DIVISOR == 0:
        # Exact for every price on the documented tick grid, whatever the mode.
        return MoneyUnits(product // _NOTIONAL_DIVISOR)
    if rounding == Rounding.FLOOR:
        return MoneyUnits(product // _NOTIONAL_DIVISOR)
    if rounding == Rounding.CEILING:
        return MoneyUnits(-((-product) // _NOTIONAL_DIVISOR))
    raise InexactError(f"notional {shares} shares @ {price} is not an exact MoneyUnits amount")


def _format(value: int, decimals: int) -> str:
    sign = "-" if value < 0 else ""
    factor: int = 10**decimals
    whole, fraction = divmod(abs(value), factor)
    return f"{sign}{whole}.{fraction:0{decimals}d}"


def format_share(value: ShareUnits) -> str:
    """Exact decimal string for display and logs."""
    return _format(value, SCALE_DECIMALS)


def format_money(value: MoneyUnits) -> str:
    """Exact decimal string for display and logs."""
    return _format(value, SCALE_DECIMALS)


def format_price(value: PriceUnits) -> str:
    """Exact decimal string for display and logs."""
    return _format(value, SCALE_DECIMALS)


def to_display_float(value: int, *, decimals: int = SCALE_DECIMALS) -> float:
    """Convert to ``float`` **for display only**.

    The result is lossy and must never be converted back into state, compared for equality,
    or accumulated. Prefer :func:`format_money` and friends, which are exact. This exists so
    that charting and metrics have one obvious, clearly-labelled exit from the exact domain
    (``docs/ARCHITECTURE_SSOT.md`` section 6.3, rule 5).
    """
    factor: int = 10**decimals
    return value / factor
