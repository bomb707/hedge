"""The O10 residual guard: every venue value must be exactly representable.

O10 was closed for the numeric kernel on Polymarket's published token decimals, with one
residual requirement owned by this phase — verify real ``btc-updown-5m-*`` traffic against the
frozen scales before live execution.

This module is that verification, running on every parsed message rather than as a one-off
audit. A value the frozen scales cannot hold raises :class:`ExactnessError` and stops the
feed. It is never rounded, and the P1 scales are never widened to accommodate it: that would
be a cross-phase contract change invalidating every recorded journal.

Observed precision is also *recorded* as it goes, so the closure evidence is a by-product of
normal operation rather than a separate study.
"""

from dataclasses import dataclass, field

from maker5m.feeds.errors import ExactnessError
from maker5m.numeric.errors import NumericError
from maker5m.numeric.units import MoneyUnits, PriceUnits, ShareUnits, parse_money
from maker5m.numeric.units import parse_price as _parse_price
from maker5m.numeric.units import parse_share as _parse_share

__all__ = ["PrecisionObserver", "decimals_in", "parse_venue_price", "parse_venue_size"]


def decimals_in(text: str) -> int:
    """How many decimal places a plain decimal string carries, as written."""
    _, _, fraction = text.partition(".")
    return len(fraction)


@dataclass(slots=True)
class PrecisionObserver:
    """Accumulates the precision evidence O10's closure needs."""

    label: str
    samples: int = 0
    max_decimals: int = 0
    min_decimals: int | None = None
    examples: dict[int, str] = field(default_factory=dict)

    def observe(self, text: str) -> None:
        places = decimals_in(text)
        self.samples += 1
        self.max_decimals = max(self.max_decimals, places)
        self.min_decimals = places if self.min_decimals is None else min(self.min_decimals, places)
        self.examples.setdefault(places, text)

    def summary(self) -> dict[str, object]:
        return {
            "label": self.label,
            "samples": self.samples,
            "min_decimals": self.min_decimals,
            "max_decimals": self.max_decimals,
            "examples": dict(sorted(self.examples.items())),
        }


def _guard(text: str, field_name: str, observer: PrecisionObserver | None) -> None:
    if not isinstance(text, str):
        raise ExactnessError(
            f"{field_name}: venue value must arrive as a decimal string, got "
            f"{type(text).__name__} — a JSON float has already lost exactness"
        )
    if observer is not None:
        observer.observe(text)


def parse_venue_price(
    text: str, *, field_name: str = "price", observer: PrecisionObserver | None = None
) -> PriceUnits:
    """Parse a venue price string exactly, or stop the feed."""
    _guard(text, field_name, observer)
    try:
        return _parse_price(text)
    except NumericError as exc:
        raise ExactnessError(
            f"{field_name}: {text!r} is not exactly representable by the frozen P1 price "
            f"scale ({exc}). O10's residual validation has failed; do not round."
        ) from exc


def parse_venue_size(
    text: str, *, field_name: str = "size", observer: PrecisionObserver | None = None
) -> ShareUnits:
    """Parse a venue size string exactly, or stop the feed."""
    _guard(text, field_name, observer)
    try:
        return _parse_share(text, allow_negative=False)
    except NumericError as exc:
        raise ExactnessError(
            f"{field_name}: {text!r} is not exactly representable by the frozen P1 share "
            f"scale ({exc}). O10's residual validation has failed; do not round."
        ) from exc


def parse_venue_money(
    text: str, *, field_name: str = "amount", observer: PrecisionObserver | None = None
) -> MoneyUnits:
    """Parse a venue money string exactly, or stop the feed."""
    _guard(text, field_name, observer)
    try:
        return parse_money(text)
    except NumericError as exc:
        raise ExactnessError(
            f"{field_name}: {text!r} is not exactly representable by the frozen P1 money "
            f"scale ({exc})."
        ) from exc
