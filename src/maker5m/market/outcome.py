"""The binary outcome identifier.

Placed in :mod:`maker5m.market` rather than :mod:`maker5m.accounting` to respect the
dependency direction in ``docs/ARCHITECTURE_SSOT.md`` section 8: ``accounting`` may import
``market``, not the reverse, and ``MarketState`` will need this type at P2. It is a shared
domain primitive only -- ``MarketState``, the event contracts, and the phase machine remain
P2 work and are not present here.
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
