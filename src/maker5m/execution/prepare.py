"""Turning strategy intent into an exact, legal venue submission — or refusing to.

The single rule that shapes this module: **the venue adapter's job is to reject an illegal
intent, never to alter it.**

Price is passed through untouched. If it does not sit on the venue's tick grid the order is
blocked, not rounded and not nudged one tick. Moving a price changes which queue the order
joins, and queue position is where this strategy's entire edge lives (Canonical §10.1) — so a
"helpful" adjustment in the transport layer would silently be a different strategy.

Size is the one quantity that must change, because the venue accepts only two decimals while
the strategy legitimately produces six after fractional fills. It is **truncated toward
zero**, never rounded up: submitting more than the strategy asked for would overshoot the
inventory lattice. Both quantities are preserved on the result so the strategy's intent stays
recoverable and the difference stays visible (I04, I03).
"""

from dataclasses import dataclass
from enum import Enum

from maker5m.domain import Outcome
from maker5m.market.events import BookLevel
from maker5m.numeric.scales import PRICE_SCALE
from maker5m.numeric.ticks import VENUE_ORDER_SIZE_DECIMALS, quantize_order_size
from maker5m.numeric.units import PriceUnits, ShareUnits
from maker5m.strategy.decision import DesiredOrder

__all__ = ["PreparationOutcome", "PreparedOrder", "prepare_order"]


class PreparationOutcome(Enum):
    """Why an intent is or is not submittable. Typed, never a free-text reason."""

    SAFE = "SAFE"
    """Legal for the venue and locally provable as passive."""

    NO_BOOK = "NO_BOOK"
    """No observed ask for this outcome, so passivity cannot be proven."""

    WOULD_CROSS = "WOULD_CROSS"
    """At or above the observed ask for the same outcome: marketable."""

    OFF_VENUE_TICK = "OFF_VENUE_TICK"
    """Not an integer multiple of the venue's legal price increment."""

    OUT_OF_VENUE_RANGE = "OUT_OF_VENUE_RANGE"
    """Outside ``[tick, 1 - tick]``, which the venue rejects."""

    BELOW_MIN_SIZE = "BELOW_MIN_SIZE"
    """Smaller than the venue's minimum order size."""

    ZERO_AFTER_QUANTIZATION = "ZERO_AFTER_QUANTIZATION"
    """Truncating to the venue's size precision left nothing to submit."""

    UNKNOWN_VENUE_RULES = "UNKNOWN_VENUE_RULES"
    """The venue's tick or minimum size has not been observed yet."""

    @property
    def submittable(self) -> bool:
        return self is PreparationOutcome.SAFE


@dataclass(frozen=True, slots=True)
class PreparedOrder:
    """One strategy intent, interpreted for the venue, with both quantities preserved."""

    outcome: Outcome
    token_id: str
    strategy_price: PriceUnits
    submission_price: PriceUnits
    strategy_size: ShareUnits
    submission_size: ShareUnits
    venue_tick: PriceUnits | None
    min_order_size: ShareUnits | None
    outcome_status: PreparationOutcome
    observed_ask: PriceUnits | None

    @property
    def submittable(self) -> bool:
        return self.outcome_status.submittable

    @property
    def size_quantization_delta(self) -> ShareUnits:
        """How much of the strategy's intent transport could not carry.

        Always ``>= 0``: submission never exceeds intent. The ledger stays authoritative over
        what the venue actually fills, and the next fill produces a fresh desired size, so
        this difference is recorded rather than corrected (P3's lattice is not bent to fit
        transport).
        """
        return ShareUnits(self.strategy_size - self.submission_size)

    @property
    def price_unchanged(self) -> bool:
        """Price is never altered to satisfy the venue. Asserted, not assumed."""
        return self.strategy_price == self.submission_price


def prepare_order(
    desired: DesiredOrder,
    *,
    token_id: str,
    venue_tick: PriceUnits | None,
    min_order_size: ShareUnits | None,
    observed_ask: BookLevel | None,
) -> PreparedOrder:
    """Interpret one desired order for the venue. Pure: no clock, no network, no mutation.

    ``observed_ask`` must be the ask for **this same outcome token**. A DOWN order's passivity
    is proven against the DOWN ask, never inferred from the UP book — Canonical §5.2's mirror
    identity is conditional, and post-only safety is a hard invariant (I06).
    """
    ask_price = None if observed_ask is None else observed_ask.price
    submission_size = quantize_order_size(desired.size, decimals=VENUE_ORDER_SIZE_DECIMALS)

    def result(status: PreparationOutcome) -> PreparedOrder:
        return PreparedOrder(
            outcome=desired.outcome,
            token_id=token_id,
            strategy_price=desired.price,
            # Never adjusted. A blocked order keeps the strategy's price so the intent stays
            # legible in telemetry.
            submission_price=desired.price,
            strategy_size=desired.size,
            submission_size=submission_size,
            venue_tick=venue_tick,
            min_order_size=min_order_size,
            outcome_status=status,
            observed_ask=ask_price,
        )

    if venue_tick is None or venue_tick <= 0:
        return result(PreparationOutcome.UNKNOWN_VENUE_RULES)
    if desired.price % venue_tick:
        return result(PreparationOutcome.OFF_VENUE_TICK)
    # The venue requires tick <= price <= 1 - tick; 0.00 and 1.00 are rejected outright.
    if not venue_tick <= desired.price <= PRICE_SCALE - venue_tick:
        return result(PreparationOutcome.OUT_OF_VENUE_RANGE)
    if submission_size <= 0:
        return result(PreparationOutcome.ZERO_AFTER_QUANTIZATION)
    if min_order_size is None:
        return result(PreparationOutcome.UNKNOWN_VENUE_RULES)
    if submission_size < min_order_size:
        # Never enlarged to reach the minimum: that would overshoot the inventory lattice.
        return result(PreparationOutcome.BELOW_MIN_SIZE)
    if ask_price is None:
        return result(PreparationOutcome.NO_BOOK)
    # Equality is marketable, not passive.
    if desired.price >= ask_price:
        return result(PreparationOutcome.WOULD_CROSS)
    return result(PreparationOutcome.SAFE)
