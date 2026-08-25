"""Numeric domain errors.

These are raised instead of rounding. A value the venue reports that cannot be represented
exactly in the frozen scales is a hard error that must halt new quoting, never a silent
adjustment to the authoritative ledger (``docs/ARCHITECTURE_SSOT.md`` section 6.3, and
invariants I01/I03).
"""

__all__ = [
    "DomainError",
    "InexactError",
    "NotRepresentableError",
    "NumericError",
    "ParseError",
]


class NumericError(Exception):
    """Base class for every fixed-point numeric failure."""


class ParseError(NumericError):
    """Input was not a well-formed plain decimal string."""


class NotRepresentableError(NumericError):
    """Input was well-formed but carries more precision than the frozen scale holds.

    Raised only when the excess digits are non-zero. Excess digits that are all zero carry
    no information and are accepted (``"1.0000000"`` is fine, ``"1.0000001"`` is not).
    """


class DomainError(NumericError):
    """Value is outside the domain the field allows (sign, range, or zero divisor)."""


class InexactError(NumericError):
    """An exact conversion was requested and the result would not be exact."""
