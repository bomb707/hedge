"""Market state and event errors.

Every one of these means the input stream is logically corrupted. They are raised, never
absorbed: a reducer that silently normalised a bad event would let the authoritative state
diverge from reality without anyone noticing, and that divergence would then be faithfully
reproduced by replay.
"""

__all__ = [
    "DuplicateEventError",
    "EventOrderError",
    "InvalidPhaseTransitionError",
    "MarketDefinitionError",
    "MarketStateError",
    "WrongMarketError",
]


class MarketStateError(Exception):
    """Base class for every market state or event failure."""


class MarketDefinitionError(MarketStateError):
    """A market identity or lifecycle configuration is impossible."""


class WrongMarketError(MarketStateError):
    """An event was routed to the state of a different market."""


class EventOrderError(MarketStateError):
    """The deterministic ingress order or the timestamp order was violated."""


class DuplicateEventError(MarketStateError):
    """An event was re-applied where doing so would double-account."""


class InvalidPhaseTransitionError(MarketStateError):
    """A phase event disagrees with the phase its own timestamp implies."""
