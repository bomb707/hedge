"""Market-data adapter errors. All fail closed.

A feed that guessed at a malformed message, invented a missing field, or rounded a value it
could not represent would corrupt authoritative state silently — and that corruption would
then be faithfully reproduced by replay. Every one of these is raised, never swallowed.
"""

__all__ = [
    "DiscoveryError",
    "ExactnessError",
    "FeedConformanceError",
    "FeedError",
    "TransportError",
]


class FeedError(Exception):
    """Base class for every market-data adapter failure."""


class FeedConformanceError(FeedError):
    """A venue message did not match the documented shape this adapter implements."""


class ExactnessError(FeedError):
    """A venue value cannot be represented exactly by the frozen P1 numeric contract.

    This is the O10 guard firing. It is a hard stop: rounding the value would corrupt the
    ledger, and widening the P1 scales would be a cross-phase contract change requiring
    explicit review (``docs/OPEN_ITEMS.md`` O10).
    """


class DiscoveryError(FeedError):
    """A market could not be identified unambiguously.

    Zero matches, several matches, or missing required metadata all land here. A market is
    never invented to keep the pipeline moving.
    """


class TransportError(FeedError):
    """A transport-level failure: connect, subscribe, heartbeat, or unexpected close."""
