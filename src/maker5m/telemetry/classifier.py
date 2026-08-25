"""Execution-quality classification (Detailed §35).

Separates "the strategy computed the right price" from "the order actually got a good queue
slot" — the distinction that tells a pricing problem apart from a latency problem.

```text
STALE               continuity or estimate cannot be trusted
NOT_QUOTING         no order intended, or intended but blocked/waiting/not live
OFF_PRICE           resting price differs from the current desired price
AT_FRONT            right price, healthy estimate, estimated ahead == 0
PRICE_OK_BUT_DEEP   right price, healthy estimate, estimated ahead > 0
```

A slot, not a wish
------------------
``AT_FRONT``, ``PRICE_OK_BUT_DEEP``, and ``OFF_PRICE`` all require an order that actually rests.
That is enforced structurally rather than by convention: there is no ``resting_price``
parameter to pass independently, so the resting price can only come from a live
:class:`QueueEstimate`, and a :class:`QueueEstimate` only exists while an order holds a slot.
A desired price the reconciler refused to submit therefore *cannot* be classified as being at
the front of anything.

**No numeric "deep" threshold is invented.** Any positive estimated queue-ahead classifies as
``PRICE_OK_BUT_DEEP`` while the actual quantity stays visible alongside. Choosing a threshold
is what O08 exists to answer, and inventing one here would pre-empt it.

The typed reason is always preserved. Collapsing ``CENTRE_UNAVAILABLE``, ``POST_ONLY_BLOCK``,
and ``RATE_DEFERRED`` into one unexplained ``NOT_QUOTING`` would destroy exactly the
information P11 and the UI need.
"""

from dataclasses import dataclass
from enum import Enum

from maker5m.execution.prepare import PreparationOutcome
from maker5m.execution.rate_limit import RateDecision
from maker5m.execution.reconciler import ReconcileAction, SideReason
from maker5m.numeric.units import PriceUnits, ShareUnits
from maker5m.telemetry.queue_estimate import QueueConfidence, QueueEstimate

__all__ = ["ExecutionQuality", "QualityReason", "QuoteClassification", "classify"]


class ExecutionQuality(Enum):
    """Detailed §35's classification."""

    AT_FRONT = "AT_FRONT"
    PRICE_OK_BUT_DEEP = "PRICE_OK_BUT_DEEP"
    OFF_PRICE = "OFF_PRICE"
    NOT_QUOTING = "NOT_QUOTING"
    STALE = "STALE"


class QualityReason(Enum):
    """Why, in terms the strategy and execution layers already use."""

    QUOTING = "QUOTING"
    CENTRE_UNAVAILABLE = "CENTRE_UNAVAILABLE"
    PHASE_NOT_QUOTING = "PHASE_NOT_QUOTING"
    ENDGAME_GATE = "ENDGAME_GATE"
    HARD_BAND = "HARD_BAND"
    POST_ONLY_BLOCK = "POST_ONLY_BLOCK"
    BELOW_MIN_SIZE = "BELOW_MIN_SIZE"
    OFF_VENUE_TICK = "OFF_VENUE_TICK"
    RATE_DEFERRED = "RATE_DEFERRED"
    IN_FLIGHT = "IN_FLIGHT"
    NO_LIVE_ORDER = "NO_LIVE_ORDER"
    CONTINUITY_LOST = "CONTINUITY_LOST"


@dataclass(frozen=True, slots=True)
class QuoteClassification:
    """One side's execution quality, with the numbers left visible."""

    quality: ExecutionQuality
    reason: QualityReason
    desired_price: PriceUnits | None = None
    resting_price: PriceUnits | None = None
    queue_ahead: ShareUnits | None = None
    confidence: QueueConfidence | None = None


_PREPARATION_REASON = {
    PreparationOutcome.WOULD_CROSS: QualityReason.POST_ONLY_BLOCK,
    PreparationOutcome.NO_BOOK: QualityReason.CENTRE_UNAVAILABLE,
    PreparationOutcome.OFF_VENUE_TICK: QualityReason.OFF_VENUE_TICK,
    PreparationOutcome.OUT_OF_VENUE_RANGE: QualityReason.OFF_VENUE_TICK,
    PreparationOutcome.BELOW_MIN_SIZE: QualityReason.BELOW_MIN_SIZE,
    PreparationOutcome.ZERO_AFTER_QUANTIZATION: QualityReason.BELOW_MIN_SIZE,
    PreparationOutcome.UNKNOWN_VENUE_RULES: QualityReason.CONTINUITY_LOST,
}

_SIDE_REASON = {
    SideReason.IN_FLIGHT: QualityReason.IN_FLIGHT,
    SideReason.UNKNOWN_STATE: QualityReason.CONTINUITY_LOST,
    SideReason.NO_DESIRED_NO_LIVE: QualityReason.PHASE_NOT_QUOTING,
    SideReason.DESIRED_WITHDRAWN: QualityReason.PHASE_NOT_QUOTING,
}


def classify(
    *,
    action: ReconcileAction,
    side_reason: SideReason,
    desired_price: PriceUnits | None,
    estimate: QueueEstimate | None,
    preparation: PreparationOutcome | None = None,
    rate_decision: RateDecision | None = None,
    strategy_reason: QualityReason | None = None,
    continuity_healthy: bool = True,
) -> QuoteClassification:
    """Classify one side. Pure.

    ``strategy_reason`` carries a P4 suppression cause (endgame gate, hard band, phase) when
    the strategy declined to quote, so that a NOT_QUOTING result still says *which* gate.

    ``estimate`` is the queue slot of the order actually resting on this side, or ``None``.
    The resting price is read from it, so no caller can assert a resting order without one.
    """
    resting_price = None if estimate is None else estimate.price
    if not continuity_healthy:
        return QuoteClassification(
            ExecutionQuality.STALE,
            QualityReason.CONTINUITY_LOST,
            desired_price,
            resting_price,
            None if estimate is None else estimate.ahead,
            None if estimate is None else estimate.confidence,
        )

    if estimate is not None and estimate.confidence is not QueueConfidence.ESTIMATED:
        return QuoteClassification(
            ExecutionQuality.STALE,
            QualityReason.CONTINUITY_LOST,
            desired_price,
            resting_price,
            estimate.ahead,
            estimate.confidence,
        )

    if rate_decision is RateDecision.DEFERRED:
        return QuoteClassification(
            ExecutionQuality.NOT_QUOTING,
            QualityReason.RATE_DEFERRED,
            desired_price,
            resting_price,
        )

    if action is ReconcileAction.WAIT:
        return QuoteClassification(
            ExecutionQuality.NOT_QUOTING,
            _SIDE_REASON.get(side_reason, QualityReason.IN_FLIGHT),
            desired_price,
            resting_price,
        )

    if action is ReconcileAction.BLOCKED or (
        preparation is not None and not preparation.submittable
    ):
        reason = (
            _PREPARATION_REASON.get(preparation, QualityReason.POST_ONLY_BLOCK)
            if preparation is not None
            else QualityReason.POST_ONLY_BLOCK
        )
        return QuoteClassification(
            ExecutionQuality.NOT_QUOTING, reason, desired_price, resting_price
        )

    if desired_price is None:
        return QuoteClassification(
            ExecutionQuality.NOT_QUOTING,
            strategy_reason or _SIDE_REASON.get(side_reason, QualityReason.PHASE_NOT_QUOTING),
            None,
            resting_price,
        )

    if estimate is None:
        # The strategy has a submittable price but nothing rests on this side. Wanting to quote
        # is not quoting, and there is no queue to be at the front of.
        return QuoteClassification(
            ExecutionQuality.NOT_QUOTING, QualityReason.NO_LIVE_ORDER, desired_price, None
        )

    if resting_price != desired_price:
        return QuoteClassification(
            ExecutionQuality.OFF_PRICE,
            QualityReason.QUOTING,
            desired_price,
            resting_price,
            estimate.ahead,
            estimate.confidence,
        )

    quality = (
        ExecutionQuality.AT_FRONT if estimate.ahead == 0 else ExecutionQuality.PRICE_OK_BUT_DEEP
    )
    return QuoteClassification(
        quality,
        QualityReason.QUOTING,
        desired_price,
        resting_price,
        estimate.ahead,
        estimate.confidence,
    )
