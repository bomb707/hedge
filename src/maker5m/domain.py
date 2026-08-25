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

__all__ = ["Outcome", "ParameterStatus"]


class Outcome(Enum):
    """Which side of the binary market a quantity belongs to."""

    UP = "UP"
    DOWN = "DOWN"

    @property
    def other(self) -> "Outcome":
        """The complementary outcome. One UP plus one DOWN settles to exactly $1.00."""
        return Outcome.DOWN if self is Outcome.UP else Outcome.UP


class ParameterStatus(Enum):
    """Confidence label every strategy parameter and component must carry (Canonical §1.2).

    Invariant I18: a value that is FITTED, OPEN, or OPERATIONAL must expose that label at
    runtime, so telemetry and the UI can show which numbers are not established. A component
    labelled ``OPEN`` must have a matching entry in ``docs/OPEN_ITEMS.md``.
    """

    CONFIRMED = "CONFIRMED"
    """Supported by the reconstructed evidence. May be encoded as a fixed rule."""

    FITTED = "FITTED"
    """Chosen by replay or small-sample fitting. Likely, not established."""

    OPEN = "OPEN"
    """Unresolved. Must stay configurable and must never become a silent assumption."""

    OPERATIONAL = "OPERATIONAL"
    """Engineering control, not proven target-wallet logic."""
