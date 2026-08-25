"""Shadow queue measurement against real market data, with no orders sent.

``LIVE_TRADING_ENABLED`` is ``False``, so P8 cannot measure a real queue position. What it
*can* do is track where an order **would** have sat, provided the order would actually have
existed.

The rule this module exists to enforce
--------------------------------------
A shadow queue slot follows the **executable order lifecycle**, not strategy intent:

```text
PLACE                       acquire a slot at the depth displayed immediately before dispatch
KEEP                        preserve the slot, update it from current depth
partial fill + KEEP         preserve the same slot; ahead becomes zero
CANCEL                      close the slot
REPLACE                     close the slot and grant nothing - P7 is CANCEL_THEN_PLACE, so a
                            new slot begins only when a later cycle actually reaches PLACE
BLOCKED / WAIT / NOTHING    no slot at all
continuity loss             the slot survives but its confidence does not
```

The first P8 implementation keyed slots on *desired price* and advanced them whenever the
strategy wanted to quote. That was wrong, and materially so: the first real market produced
about 119,116 ``POST_ONLY_BLOCK`` sides, every one of which acquired and then aged a queue
estimate for an order that was never submitted. Blocked intent accumulated depth-decrease
credit and reported itself at the front of a queue it had never joined.

Identity is therefore the client order id, never ``(outcome, price)``. Two orders at the same
price are two queues; a replacement is a new order no matter how identical its price looks.

Every result is labelled ``SHADOW_ESTIMATE``. It is a statement about our own strategy's orders
against observed depth. It is **not** evidence of a venue queue position, and it is not
evidence about the target wallet.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from maker5m.domain import Outcome
from maker5m.numeric.units import PriceUnits, ShareUnits
from maker5m.telemetry.queue_estimate import QueueConfidence, QueueEstimate, QueueSlot

__all__ = ["SHADOW_LABEL", "ShadowLossReason", "ShadowQueueTracker"]

SHADOW_LABEL: Final[str] = "SHADOW_ESTIMATE"
"""Carried on every shadow result so it can never be mistaken for a real queue position."""


class ShadowLossReason(Enum):
    """Why a shadow slot ceased to exist. Typed so totals can be reconciled to reasons."""

    PRICE_CHANGE = "PRICE_CHANGE"
    SIZE_CHANGE = "SIZE_CHANGE"
    DESIRED_WITHDRAWN = "DESIRED_WITHDRAWN"
    UNSAFE_REPLACEMENT = "UNSAFE_REPLACEMENT"
    SETTLING = "SETTLING"
    CONTINUITY_LOSS = "CONTINUITY_LOSS"
    FULLY_FILLED = "FULLY_FILLED"
    OTHER = "OTHER"


@dataclass(slots=True)
class ShadowQueueTracker:
    """Shadow slots keyed by shadow order id, driven by the executable order lifecycle."""

    slots: dict[str, QueueSlot] = field(default_factory=dict)
    """Live slots only. A closed slot is removed; its counters remain."""

    active: dict[Outcome, str] = field(default_factory=dict)
    """The one order id currently holding a slot on each side."""

    acquired: int = 0
    kept: int = 0
    lost: int = 0
    loss_reasons: dict[str, int] = field(default_factory=dict)

    # -- lifecycle -----------------------------------------------------------------------

    def on_place(
        self,
        *,
        client_order_id: str,
        outcome: Outcome,
        price: PriceUnits,
        displayed_now: ShareUnits,
    ) -> None:
        """An order was dispatched. Open its slot at the depth displayed before dispatch.

        Any slot still recorded for this side is closed first: reaching PLACE while another
        order holds the side means that order is gone, and its slot must not linger.
        """
        existing = self.active.get(outcome)
        if existing is not None:
            self._close(existing, ShadowLossReason.OTHER)
        slot = QueueSlot.acquire(
            client_order_id=client_order_id, price=price, displayed_now=displayed_now
        )
        self.slots[client_order_id] = slot
        self.active[outcome] = client_order_id
        self.acquired += 1

    def on_keep(self, client_order_id: str, displayed_now: ShareUnits) -> None:
        """The order still rests unchanged. Preserve the slot and refresh from depth.

        Returns nothing on purpose. This runs on every cycle of a measuring run, including the
        ordinary unsampled book updates that must stay cheap, and building a six-field frozen
        :class:`QueueEstimate` here cost about 1 µs per side for a value the untraced path
        immediately discarded. Readers ask for an estimate when they actually need one.
        """
        slot = self.slots.get(client_order_id)
        if slot is None:
            return
        slot.observe_depth(displayed_now)
        self.kept += 1

    def on_lost(self, client_order_id: str, reason: ShadowLossReason) -> None:
        """The order stopped resting. The queue timestamp is gone with it."""
        self._close(client_order_id, reason)

    def on_fill(self, client_order_id: str, *, fully_filled: bool = False) -> None:
        """A fill means the front was reached.

        A partial fill keeps the slot — that is precisely the case P7's reconciler protects by
        comparing *remaining* size. A complete fill ends the order, so the slot ends too.
        """
        slot = self.slots.get(client_order_id)
        if slot is None:
            return
        slot.record_own_fill()
        if fully_filled:
            self._close(client_order_id, ShadowLossReason.FULLY_FILLED)

    def invalidate(self, confidence: QueueConfidence = QueueConfidence.UNKNOWN) -> None:
        """Continuity lost: no prior estimate survives a resnapshot.

        The order still rests, so the slot is not closed — but nothing about its position can
        be reconstructed from a fresh snapshot, and pretending otherwise would manufacture
        evidence.
        """
        for slot in self.slots.values():
            slot.invalidate(confidence)

    def _close(self, client_order_id: str, reason: ShadowLossReason) -> None:
        slot = self.slots.pop(client_order_id, None)
        if slot is None:
            return
        for outcome, held in list(self.active.items()):
            if held == client_order_id:
                del self.active[outcome]
        self.lost += 1
        self.loss_reasons[reason.value] = self.loss_reasons.get(reason.value, 0) + 1

    # -- views ---------------------------------------------------------------------------

    def active_slot(self, outcome: Outcome) -> QueueSlot | None:
        """The slot currently held on this side, or ``None`` when nothing rests there."""
        client_order_id = self.active.get(outcome)
        return None if client_order_id is None else self.slots.get(client_order_id)

    def estimate(self, outcome: Outcome) -> QueueEstimate | None:
        slot = self.active_slot(outcome)
        return None if slot is None else slot.estimate()

    def summary(self) -> dict[str, object]:
        return {
            "label": SHADOW_LABEL,
            "shadow_slots_acquired": self.acquired,
            "shadow_slot_cycles_kept": self.kept,
            "shadow_slot_losses": self.lost,
            "shadow_slot_loss_reasons": dict(sorted(self.loss_reasons.items())),
            "shadow_slots_open": len(self.slots),
        }
