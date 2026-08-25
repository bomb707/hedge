"""Shared domain primitives.

A leaf module: it imports nothing from the project, so anything may depend on it.

``Outcome`` lives here rather than in :mod:`maker5m.market` because P2 established the true
dependency direction between the two Plane 2 packages. ``market`` needs ``Fill`` and
``LedgerState`` from ``accounting`` -- the event stream carries fills and the market state
embeds the ledger -- while ``accounting`` needed only this one enum from ``market``. Keeping
``Outcome`` under ``market`` therefore created an import cycle. The dependency now runs
``market -> accounting -> domain -> numeric``, and both packages re-export ``Outcome`` so
call sites are unaffected. See ``docs/ARCHITECTURE_SSOT.md`` section 8.
"""

from enum import Enum

__all__ = ["Outcome"]


class Outcome(Enum):
    """Which side of the binary market a quantity belongs to."""

    UP = "UP"
    DOWN = "DOWN"

    @property
    def other(self) -> "Outcome":
        """The complementary outcome. One UP plus one DOWN settles to exactly $1.00."""
        return Outcome.DOWN if self is Outcome.UP else Outcome.UP
