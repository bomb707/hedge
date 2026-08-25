"""Shadow queue measurement against real market data, with no orders sent.

``LIVE_TRADING_ENABLED`` is ``False``, so P8 cannot measure a real queue position. What it
*can* do is track where an order **would** have sat: when a desired price first appears, record
the displayed depth at that exact level; while the desired price is unchanged, keep the slot;
when it changes, reset at the new level.

That answers the question Canonical §10 turns on — *would this strategy arrive at a fresh price
level, or join behind an existing queue?* — without sending anything.

Every result is labelled ``SHADOW_ESTIMATE``. It is a statement about our own strategy's
intent against observed depth. It is **not** evidence of a venue queue position, and it is not
evidence about the target wallet.
"""

from dataclasses import dataclass, field
from typing import Final

from maker5m.domain import Outcome
from maker5m.numeric.units import PriceUnits, ShareUnits
from maker5m.telemetry.queue_estimate import QueueConfidence, QueueEstimate, QueueSlot

__all__ = ["SHADOW_LABEL", "ShadowQueueTracker"]

SHADOW_LABEL: Final[str] = "SHADOW_ESTIMATE"
"""Carried on every shadow result so it can never be mistaken for a real queue position."""


@dataclass(slots=True)
class ShadowQueueTracker:
    """Per-outcome shadow slots driven by desired price and observed depth."""

    slots: dict[Outcome, QueueSlot] = field(default_factory=dict)
    acquired: int = 0
    kept: int = 0
    lost: int = 0

    def on_desired(
        self, outcome: Outcome, price: PriceUnits | None, displayed_now: ShareUnits
    ) -> QueueEstimate | None:
        """Advance the shadow slot for one side.

        A new or changed desired price opens a fresh slot at the observed depth; an unchanged
        one keeps its slot and updates the estimate from current depth.
        """
        if price is None:
            self._close(outcome)
            return None

        slot = self.slots.get(outcome)
        if slot is None:
            slot = QueueSlot.acquire(price=price, displayed_now=displayed_now)
            self.slots[outcome] = slot
            self.acquired += 1
            return slot.estimate()

        if slot.price != price:
            # A price change costs the slot: a real order would have been cancelled and
            # replaced, losing its queue timestamp.
            self.lost += 1
            slot = QueueSlot.acquire(price=price, displayed_now=displayed_now)
            self.slots[outcome] = slot
            self.acquired += 1
            return slot.estimate()

        slot.observe_depth(displayed_now)
        self.kept += 1
        return slot.estimate()

    def on_fill(self, outcome: Outcome) -> None:
        """A fill means the front was reached. A partial fill keeps the slot."""
        slot = self.slots.get(outcome)
        if slot is not None:
            slot.record_own_fill()

    def invalidate(self, confidence: QueueConfidence = QueueConfidence.UNKNOWN) -> None:
        """Continuity lost: no prior shadow estimate survives a resnapshot."""
        for slot in self.slots.values():
            slot.invalidate(confidence)

    def _close(self, outcome: Outcome) -> None:
        if self.slots.pop(outcome, None) is not None:
            self.lost += 1

    def estimate(self, outcome: Outcome) -> QueueEstimate | None:
        slot = self.slots.get(outcome)
        return None if slot is None else slot.estimate()
