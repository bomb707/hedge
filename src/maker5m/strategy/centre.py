"""Quote centre — the single largest OPEN item (O01).

Everything about the quote derives from one number, ``C``. Canonical §9 lists three candidate
sources and recommends starting from the CLOB midpoint because it introduces the fewest
unverified assumptions; it explicitly does **not** establish that as the target wallet's
choice. So the centre lives behind a replaceable interface, every implementation carries a
status label, and the CLOB-mid implementation is labelled ``OPEN`` rather than being allowed
to become "the strategy truth" by being the only one present.

``BINANCE_FV`` and ``BLEND`` are declared in :class:`CentreSource` but deliberately not
implemented: they need the TWAP model and sigma, which are P4 and O02.

An unavailable centre is a **normal** condition, not corruption — early in a market the book
may have no resting ask — so it is reported as an explicit result with a reason rather than
raised. The reason is kept because telemetry needs to distinguish "we chose not to quote"
from "we could not price" (Detailed §35's ``NOT_QUOTING`` classification).
"""

import math
from dataclasses import dataclass
from enum import Enum
from typing import Final, Protocol

from maker5m.domain import ParameterStatus
from maker5m.market.state import MarketState
from maker5m.numeric.errors import DomainError
from maker5m.numeric.scales import PRICE_SCALE

__all__ = [
    "CLOB_MID_STATUS",
    "CentreResult",
    "CentreSource",
    "CentreUnavailable",
    "ClobMidCentre",
    "QuoteCentre",
    "RawCentre",
]


class CentreSource(Enum):
    """Candidate centre sources from Canonical §9. Only ``CLOB_MID`` is implemented."""

    CLOB_MID = "CLOB_MID"
    BINANCE_FV = "BINANCE_FV"
    BLEND = "BLEND"


class CentreUnavailable(Enum):
    """Why no centre could be produced. A normal condition, not an error."""

    NO_BOOK = "NO_BOOK"
    NO_UP_BID = "NO_UP_BID"
    NO_UP_ASK = "NO_UP_ASK"


@dataclass(frozen=True, slots=True)
class RawCentre:
    """An exact centre before tick quantization, as a rational in ``PriceUnits``.

    Rational rather than integer because the CLOB midpoint of an odd bid+ask sum is a genuine
    half unit, and rounding it early would move the quantization decision somewhere
    undocumented. Normalised on construction so equality is meaningful, and small enough that
    the arithmetic downstream stays integer — no ``Fraction`` allocation on the decision path.
    """

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if self.denominator <= 0:
            raise DomainError(f"denominator must be positive, got {self.denominator}")
        if not 0 <= self.numerator <= self.denominator * PRICE_SCALE:
            raise DomainError(f"centre must lie in [0, 1], got {self.numerator}/{self.denominator}")
        divisor = math.gcd(self.numerator, self.denominator)
        if divisor > 1:
            object.__setattr__(self, "numerator", self.numerator // divisor)
            object.__setattr__(self, "denominator", self.denominator // divisor)

    @property
    def is_exact_price_unit(self) -> bool:
        """Whether the centre already lands on a whole ``PriceUnits`` value."""
        return self.denominator == 1


@dataclass(frozen=True, slots=True)
class CentreResult:
    """Either a centre or the reason there isn't one. Never both, never neither."""

    source: CentreSource
    centre: RawCentre | None = None
    unavailable: CentreUnavailable | None = None

    def __post_init__(self) -> None:
        if (self.centre is None) == (self.unavailable is None):
            raise DomainError("CentreResult must carry exactly one of centre or unavailable")

    @property
    def available(self) -> bool:
        return self.centre is not None


class QuoteCentre(Protocol):
    """The replaceable centre component (O01).

    A ``Protocol``, so an implementation is an ordinary object with no base class, no
    registry, and no dynamic dispatch framework on the decision path.
    """

    @property
    def source(self) -> CentreSource: ...

    @property
    def status(self) -> ParameterStatus: ...

    def compute(self, state: MarketState) -> CentreResult: ...


CLOB_MID_STATUS: Final = ParameterStatus.OPEN
"""O01. The recommended starting point, not an established choice."""


@dataclass(frozen=True, slots=True)
class ClobMidCentre:
    """``C = (best_up_bid + best_up_ask) / 2``, exactly.

    Derived from the observed UP top of book only. The DOWN book is deliberately not used:
    Canonical §5.2's mirror identity is conditional on all levels mapping exactly, and using
    it here would quietly fold an unverified assumption into the centre. A DOWN-derived or
    blended centre is a separate, explicitly configured implementation.

    A missing side yields an unavailable result. A midpoint is never invented from one side.
    """

    source: CentreSource = CentreSource.CLOB_MID
    status: ParameterStatus = CLOB_MID_STATUS

    def compute(self, state: MarketState) -> CentreResult:
        book = state.book
        if book is None:
            return CentreResult(self.source, unavailable=CentreUnavailable.NO_BOOK)
        if book.up_bid is None:
            return CentreResult(self.source, unavailable=CentreUnavailable.NO_UP_BID)
        if book.up_ask is None:
            return CentreResult(self.source, unavailable=CentreUnavailable.NO_UP_ASK)
        return CentreResult(
            self.source,
            centre=RawCentre(book.up_bid.price + book.up_ask.price, 2),
        )
