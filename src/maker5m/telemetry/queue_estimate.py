"""Queue-position **estimation**, and an honest account of why it is only an estimate.

Polymarket publishes no per-order queue index. What is observable is aggregate displayed size
at a price level. So everything here is an *estimate*, and the type says so: every value
carries a :class:`QueueConfidence`, and no field is ever named as though the venue's true queue
index were known.

The model, under price-time priority
------------------------------------
* **At submission**, the displayed size already resting at our exact price is the best locally
  observable estimate of quantity ahead. It is still an estimate: orders and cancels can reach
  the venue while our request is in transit.
* **A decrease** in aggregate displayed size at our price reduces the estimate by the observed
  decrease, floored at zero — those were fills or cancels, and under price-time priority most
  of them were ahead of us.
* **An increase does not raise it.** New same-price orders join *behind* an already resting
  order. This asymmetry is the whole point of the model.
* **Our own fill means we reached the front**, so the estimate becomes zero. A partial fill
  does not reset position: if the remainder is KEPT it keeps its slot, which is exactly the
  case P7's reconciler protects.
* **Any continuity loss invalidates it.** After a disconnect, a sequence gap, or a mandatory
  resnapshot, the prior estimate is not reconstructable and becomes ``UNKNOWN``. A fresh
  snapshot does not restore a continuous position, and pretending otherwise would manufacture
  evidence.

A slot belongs to an order, not to a price
------------------------------------------
An estimate is only ever opened for an order that was actually dispatched. A desired price that
the reconciler refused to submit — post-only would cross, below minimum size, off tick — owns no
queue position, because no order exists to hold one. Modelling intent as position would credit a
permanently blocked quote with a queue timestamp it never had, and would inflate every front-of-
queue statistic derived from it.

Aggregate book messages cannot reveal the internal ordering of same-price cancels and arrivals,
so the confidence never rises above ``ESTIMATED``. That ceiling is deliberate.

A known bias, stated rather than hidden
---------------------------------------
The decrease is measured against the **last observation**, which may include size added after
we arrived. If 25 join behind us and 28 subsequently disappear, the whole 28 is credited as
consumption ahead of us even though at most 15 ever were. The estimate is therefore
**optimistic**: it can report a better queue position than reality.

That direction matters, because an optimistic estimate inflates ``AT_FRONT`` rates and would
make the strategy look better positioned than it is. Two things bound it: ``ahead`` can never
exceed currently displayed size (an unambiguous fact, enforced below), and it never goes
negative. Closing the gap properly needs per-order queue data the venue does not publish, so
the bias is recorded in the P8 evidence rather than papered over — and it is one of the things
O08 must account for.
"""

from dataclasses import dataclass
from enum import Enum

from maker5m.numeric.units import PriceUnits, ShareUnits

__all__ = ["QueueConfidence", "QueueEstimate", "QueueSlot"]


class QueueConfidence(Enum):
    """How much the estimate can be trusted. Never ``EXACT`` — the venue does not tell us."""

    ESTIMATED = "ESTIMATED"
    """Derived from observed aggregate depth under price-time assumptions."""

    STALE = "STALE"
    """Continuity was interrupted; the prior estimate no longer tracks reality."""

    UNKNOWN = "UNKNOWN"
    """No basis for an estimate at all."""


@dataclass(frozen=True, slots=True)
class QueueEstimate:
    """One observation of where a **specific** order probably sits."""

    client_order_id: str
    """The order this estimate belongs to.

    Queue identity is per order, never per price level. Two orders at the same price do not
    share a queue timestamp, and a replacement is a different order however identical its
    price looks.
    """

    price: PriceUnits
    ahead: ShareUnits
    confidence: QueueConfidence
    displayed_at_submit: ShareUnits
    level_existed_before: bool
    """Whether the level already had resting size when we arrived.

    The distinction Canonical §10 turns on: arriving at a fresh level means owning the front of
    a new queue; arriving at a populated one means joining behind it.
    """

    @property
    def at_front(self) -> bool:
        return self.confidence is QueueConfidence.ESTIMATED and self.ahead == 0


@dataclass(slots=True)
class QueueSlot:
    """Tracks one order's estimated position for as long as that order holds its slot.

    A slot exists only while a corresponding order actually rests. It is opened when an order
    is dispatched and closed when that order stops resting — never opened because the strategy
    merely wished to quote somewhere.
    """

    client_order_id: str
    price: PriceUnits
    ahead: ShareUnits
    displayed_at_submit: ShareUnits
    level_existed_before: bool
    last_displayed: ShareUnits
    confidence: QueueConfidence = QueueConfidence.ESTIMATED
    reached_front: bool = False

    @classmethod
    def acquire(
        cls, *, client_order_id: str, price: PriceUnits, displayed_now: ShareUnits
    ) -> "QueueSlot":
        """Open a slot for one dispatched order.

        ``displayed_now`` must be the depth observed immediately before dispatch: that is the
        size the order would have queued behind. A slot inherits nothing from any earlier slot,
        including one at the same price, because it belongs to a different order.
        """
        return cls(
            client_order_id=client_order_id,
            price=price,
            ahead=displayed_now,
            displayed_at_submit=displayed_now,
            level_existed_before=displayed_now > 0,
            last_displayed=displayed_now,
        )

    def observe_depth(self, displayed_now: ShareUnits) -> None:
        """Update from a fresh observation of aggregate displayed size at our price."""
        if self.confidence is not QueueConfidence.ESTIMATED:
            return
        if self.reached_front:
            # Already at the front; later arrivals queue behind us.
            self.last_displayed = displayed_now
            return
        if displayed_now < self.last_displayed:
            consumed = self.last_displayed - displayed_now
            self.ahead = ShareUnits(max(0, self.ahead - consumed))
        # An increase is deliberately ignored: new same-price orders join behind us.
        # Whatever else is uncertain, we cannot be behind more size than is displayed.
        self.ahead = ShareUnits(min(self.ahead, displayed_now))
        self.last_displayed = displayed_now

    def record_own_fill(self) -> None:
        """A fill means we reached the front. A partial fill does not cost the slot."""
        self.ahead = ShareUnits(0)
        self.reached_front = True

    def invalidate(self, confidence: QueueConfidence = QueueConfidence.UNKNOWN) -> None:
        """Continuity lost. The estimate is not reconstructable from a fresh snapshot."""
        self.confidence = confidence

    def estimate(self) -> QueueEstimate:
        return QueueEstimate(
            client_order_id=self.client_order_id,
            price=self.price,
            ahead=self.ahead,
            confidence=self.confidence,
            displayed_at_submit=self.displayed_at_submit,
            level_existed_before=self.level_existed_before,
        )
