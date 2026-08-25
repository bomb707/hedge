"""Queue estimation: the price-time model, and the limits of what can be claimed."""

from __future__ import annotations

import gc
import sys

import pytest

from maker5m.domain import Outcome
from maker5m.feeds import BookTracker, parse_market_message
from maker5m.numeric import parse_price, parse_share
from maker5m.telemetry import QueueConfidence, QueueSlot, ShadowQueueTracker
from maker5m.telemetry.shadow import SHADOW_LABEL, ShadowLossReason

UP = "token-up"
DOWN = "token-down"


def slot(price: str = "0.63", displayed: str = "0", order: str = "shadow-1") -> QueueSlot:
    return QueueSlot.acquire(
        client_order_id=order, price=parse_price(price), displayed_now=parse_share(displayed)
    )


# -- the model ------------------------------------------------------------


def test_an_empty_level_yields_an_estimate_of_zero() -> None:
    """Arriving at a fresh level means owning the front of a new queue (Canonical §10)."""
    s = slot(displayed="0")
    assert s.ahead == 0
    assert s.estimate().at_front
    assert s.estimate().level_existed_before is False


def test_an_existing_level_becomes_the_initial_estimate() -> None:
    s = slot(displayed="15")
    assert s.ahead == parse_share("15")
    assert not s.estimate().at_front
    assert s.estimate().level_existed_before is True


def test_a_decrease_reduces_the_estimate_by_the_observed_amount() -> None:
    s = slot(displayed="15")
    s.observe_depth(parse_share("10"))
    assert s.ahead == parse_share("10")


def test_repeated_decreases_accumulate() -> None:
    s = slot(displayed="15")
    s.observe_depth(parse_share("10"))
    s.observe_depth(parse_share("4"))
    assert s.ahead == parse_share("4")


def test_an_increase_after_placement_does_not_raise_the_estimate() -> None:
    """New same-price orders join behind an already resting one. This asymmetry is the model."""
    s = slot(displayed="15")
    s.observe_depth(parse_share("40"))
    assert s.ahead == parse_share("15")


def test_the_estimate_is_optimistic_after_an_increase_then_a_decrease() -> None:
    """A known, documented bias of aggregate-only data.

    15 rest ahead of us, 25 join behind, then 28 disappear. All 28 are credited as consumption
    ahead even though at most 15 ever were, so the estimate reports the front when reality may
    be worse. The direction matters - it inflates AT_FRONT - so it is asserted here rather than
    left as a surprise, and recorded in the P8 evidence.
    """
    s = slot(displayed="15")
    s.observe_depth(parse_share("40"))
    s.observe_depth(parse_share("12"))
    assert s.ahead == 0


def test_the_estimate_never_exceeds_currently_displayed_size() -> None:
    """Whatever else is uncertain, we cannot be behind more size than the venue displays."""
    s = slot(displayed="50")
    s.observe_depth(parse_share("3"))
    assert s.ahead <= parse_share("3")


def test_the_estimate_never_goes_negative() -> None:
    s = slot(displayed="5")
    s.observe_depth(parse_share("0"))
    s.observe_depth(parse_share("0"))
    assert s.ahead == 0
    assert s.ahead >= 0


def test_our_own_fill_means_the_front_was_reached() -> None:
    s = slot(displayed="15")
    s.record_own_fill()
    assert s.ahead == 0
    assert s.estimate().at_front


def test_a_partial_fill_does_not_cost_the_slot() -> None:
    """The remainder keeps its position - exactly the case P7's reconciler KEEPs."""
    s = slot(displayed="15")
    s.record_own_fill()
    s.observe_depth(parse_share("50"))
    assert s.ahead == 0
    assert s.estimate().at_front


def test_confidence_never_claims_exactness() -> None:
    """The venue publishes no queue index, so the ceiling is ESTIMATED by construction."""
    assert {c.value for c in QueueConfidence} == {"ESTIMATED", "STALE", "UNKNOWN"}
    assert not hasattr(QueueConfidence, "EXACT")
    assert slot().confidence is QueueConfidence.ESTIMATED


def test_continuity_loss_invalidates_the_estimate() -> None:
    s = slot(displayed="15")
    s.invalidate()
    assert s.confidence is QueueConfidence.UNKNOWN
    assert not s.estimate().at_front


def test_an_invalidated_slot_stops_updating() -> None:
    """A fresh snapshot does not restore a continuous position."""
    s = slot(displayed="15")
    s.invalidate(QueueConfidence.STALE)
    s.observe_depth(parse_share("1"))
    assert s.ahead == parse_share("15"), "no pretend recovery from a resnapshot"
    assert s.confidence is QueueConfidence.STALE


# -- same-outcome depth ----------------------------------------------------


def tracker_with_books() -> BookTracker:
    book = BookTracker(UP, DOWN)
    book.apply(
        parse_market_message(
            {
                "event_type": "book",
                "asset_id": UP,
                "bids": [{"price": "0.62", "size": "100"}, {"price": "0.63", "size": "15"}],
                "asks": [{"price": "0.66", "size": "40"}],
            }
        )
    )
    book.apply(
        parse_market_message(
            {
                "event_type": "book",
                "asset_id": DOWN,
                "bids": [{"price": "0.35", "size": "77"}],
                "asks": [{"price": "0.39", "size": "88"}],
            }
        )
    )
    return book


def test_depth_is_read_from_the_outcomes_own_ladder() -> None:
    book = tracker_with_books()
    assert book.size_at(Outcome.UP, "bid", parse_price("0.63")) == parse_share("15")
    assert book.size_at(Outcome.DOWN, "bid", parse_price("0.35")) == parse_share("77")


def test_the_down_queue_is_never_inferred_from_up() -> None:
    """0.63 exists on the UP ladder; its complement must not leak into the DOWN answer."""
    book = tracker_with_books()
    assert book.size_at(Outcome.UP, "bid", parse_price("0.63")) == parse_share("15")
    assert book.size_at(Outcome.DOWN, "bid", parse_price("0.63")) == 0
    assert book.size_at(Outcome.DOWN, "bid", parse_price("0.37")) == 0


def test_an_absent_level_reports_zero_depth() -> None:
    book = tracker_with_books()
    assert book.size_at(Outcome.UP, "bid", parse_price("0.55")) == 0


def test_an_unknown_side_is_rejected() -> None:
    from maker5m.feeds import FeedConformanceError

    with pytest.raises(FeedConformanceError):
        tracker_with_books().size_at(Outcome.UP, "middle", parse_price("0.63"))


# -- shadow tracking --------------------------------------------------------


def place(
    shadow: ShadowQueueTracker,
    order: str = "shadow-1",
    outcome: Outcome = Outcome.UP,
    price: str = "0.63",
    displayed: str = "0",
) -> None:
    shadow.on_place(
        client_order_id=order,
        outcome=outcome,
        price=parse_price(price),
        displayed_now=parse_share(displayed),
    )


def test_a_shadow_result_is_labelled_as_such() -> None:
    """It is our strategy's own order against observed depth, not a venue queue position."""
    assert SHADOW_LABEL == "SHADOW_ESTIMATE"


def test_a_place_opens_a_shadow_slot() -> None:
    shadow = ShadowQueueTracker()
    shadow.on_place(
        client_order_id="shadow-1",
        outcome=Outcome.UP,
        price=parse_price("0.63"),
        displayed_now=parse_share("0"),
    )
    estimate = shadow.estimate(Outcome.UP)
    assert estimate is not None
    assert estimate.at_front
    assert estimate.client_order_id == "shadow-1"
    assert shadow.acquired == 1


def test_keeping_a_slot_allocates_nothing() -> None:
    """The hot path must not build an estimate it throws away.

    ``on_keep`` runs on every cycle of a measuring run, including the ordinary unsampled book
    updates the P8 performance limit is about. Returning a six-field frozen dataclass here cost
    roughly 1 µs per side for a value the untraced caller discarded, so the contract is that
    keeping a slot updates state and allocates nothing. Readers ask for an estimate separately.
    """
    shadow = ShadowQueueTracker()
    place(shadow, displayed="50")
    sizes = [parse_share(str(50 - index)) for index in range(200)]

    gc.collect()
    before = sys.getallocatedblocks()
    for size in sizes:
        shadow.on_keep("shadow-1", size)
    after = sys.getallocatedblocks()

    # A handful of blocks is ordinary integer churn from the counters. A dataclass per call
    # would show up as hundreds, which is exactly what this is here to catch.
    allocated = after - before
    assert allocated < 20, f"on_keep allocated {allocated} blocks over 200 calls"
    assert shadow.kept == 200


def test_a_keep_preserves_the_shadow_slot() -> None:
    shadow = ShadowQueueTracker()
    place(shadow, displayed="15")
    for _ in range(10):
        shadow.on_keep("shadow-1", parse_share("15"))
    assert shadow.acquired == 1
    assert shadow.kept == 10
    assert shadow.lost == 0
    estimate = shadow.estimate(Outcome.UP)
    assert estimate is not None and estimate.client_order_id == "shadow-1"


def test_a_lost_slot_records_its_typed_reason() -> None:
    shadow = ShadowQueueTracker()
    place(shadow, displayed="15")
    shadow.on_lost("shadow-1", ShadowLossReason.PRICE_CHANGE)
    assert shadow.lost == 1
    assert shadow.loss_reasons == {"PRICE_CHANGE": 1}
    assert shadow.estimate(Outcome.UP) is None


def test_a_replacement_is_a_different_slot_identity() -> None:
    """Two orders at the same price do not share a queue timestamp."""
    shadow = ShadowQueueTracker()
    place(shadow, order="shadow-1", displayed="15")
    shadow.on_keep("shadow-1", parse_share("3"))
    shadow.on_lost("shadow-1", ShadowLossReason.PRICE_CHANGE)
    place(shadow, order="shadow-2", price="0.63", displayed="15")

    estimate = shadow.estimate(Outcome.UP)
    assert estimate is not None
    assert estimate.client_order_id == "shadow-2"
    # It inherits nothing: not the id, and not the 12 units of consumption the old slot saw.
    assert estimate.ahead == parse_share("15")


def test_a_partial_fill_keeps_the_slot_and_reaches_the_front() -> None:
    shadow = ShadowQueueTracker()
    place(shadow, displayed="20")
    shadow.on_fill("shadow-1")
    estimate = shadow.estimate(Outcome.UP)
    assert estimate is not None
    assert estimate.ahead == 0
    assert estimate.client_order_id == "shadow-1"
    assert shadow.lost == 0


def test_a_complete_fill_ends_the_slot() -> None:
    shadow = ShadowQueueTracker()
    place(shadow, displayed="20")
    shadow.on_fill("shadow-1", fully_filled=True)
    assert shadow.estimate(Outcome.UP) is None
    assert shadow.loss_reasons == {"FULLY_FILLED": 1}


def test_shadow_slots_invalidate_on_continuity_loss() -> None:
    shadow = ShadowQueueTracker()
    place(shadow, displayed="20")
    shadow.invalidate()
    estimate = shadow.estimate(Outcome.UP)
    assert estimate is not None
    assert estimate.confidence is QueueConfidence.UNKNOWN
    # The order still rests, so the slot is not lost - only its confidence is.
    assert shadow.lost == 0


def test_up_and_down_shadow_slots_are_independent() -> None:
    shadow = ShadowQueueTracker()
    place(shadow, order="up-1", outcome=Outcome.UP, price="0.63", displayed="15")
    place(shadow, order="down-1", outcome=Outcome.DOWN, price="0.36", displayed="0")
    up_estimate = shadow.estimate(Outcome.UP)
    down_estimate = shadow.estimate(Outcome.DOWN)
    assert up_estimate is not None and down_estimate is not None
    assert up_estimate.ahead == parse_share("15")
    assert down_estimate.ahead == 0


def test_keeping_an_unknown_order_does_nothing() -> None:
    """A slot only exists for an order that was actually placed."""
    shadow = ShadowQueueTracker()
    shadow.on_keep("never-placed", parse_share("5"))
    assert shadow.kept == 0
    assert shadow.acquired == 0


def test_shadow_loss_totals_reconcile_to_their_reasons() -> None:
    shadow = ShadowQueueTracker()
    for index, reason in enumerate(
        (
            ShadowLossReason.PRICE_CHANGE,
            ShadowLossReason.SIZE_CHANGE,
            ShadowLossReason.PRICE_CHANGE,
            ShadowLossReason.UNSAFE_REPLACEMENT,
        )
    ):
        order = f"shadow-{index}"
        place(shadow, order=order, displayed="5")
        shadow.on_lost(order, reason)
    assert shadow.lost == sum(shadow.loss_reasons.values())
    assert shadow.loss_reasons == {
        "PRICE_CHANGE": 2,
        "SIZE_CHANGE": 1,
        "UNSAFE_REPLACEMENT": 1,
    }
