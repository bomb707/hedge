"""Strategy domain errors.

Distinct from a *normal* absence of information. A book with no resting ask is an ordinary
early-market condition, not corruption, so the quote centre reports it as an explicit
unavailable result rather than raising. These exceptions are for genuinely invalid input:
a price outside ``[0, 1]``, an unsupported base lot, a centre that is not on the tick grid.
"""

__all__ = ["StrategyError", "UnsupportedBaseLotError"]


class StrategyError(Exception):
    """Base class for strategy-domain failures."""


class UnsupportedBaseLotError(StrategyError):
    """A base lot outside the confirmed observed set was requested."""
