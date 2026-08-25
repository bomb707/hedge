"""Polymarket adapter: real-message conformance, separate books, no invented sequence."""

from __future__ import annotations

import json

import pytest

from maker5m.feeds import (
    BookTracker,
    ExactnessError,
    FeedConformanceError,
    MarketEventKind,
    PrecisionObserver,
    parse_market_message,
    subscribe_payload,
)
from maker5m.numeric import PriceUnits, ShareUnits, parse_price, parse_share
from tests.feeds.conftest import load

BOOK_FIXTURE = load("polymarket_book.json")
CHANGE_FIXTURE = load("polymarket_price_change.json")
UP = BOOK_FIXTURE["_up_token_id"]
DOWN = BOOK_FIXTURE["_down_token_id"]


def test_subscription_is_read_only_and_carries_no_credential() -> None:
    payload = json.loads(subscribe_payload((UP, DOWN)))
    assert payload == {"assets_ids": [UP, DOWN], "type": "market"}
    assert "auth" not in payload
    assert set(payload) == {"assets_ids", "type"}


# -- real book message ----------------------------------------------------------------------


def test_real_book_fixture_parses() -> None:
    parsed = parse_market_message(BOOK_FIXTURE["message"])
    assert parsed.kind is MarketEventKind.BOOK
    assert parsed.book is not None
    assert parsed.book.asset_id in (UP, DOWN)
    assert parsed.book.bids or parsed.book.asks
    assert parsed.book.tick_size == parse_price("0.01")


def test_real_book_levels_are_exactly_representable() -> None:
    """The O10 residual guard, exercised on real captured traffic."""
    price_obs = PrecisionObserver("price")
    size_obs = PrecisionObserver("size")
    parsed = parse_market_message(
        BOOK_FIXTURE["message"], price_observer=price_obs, size_observer=size_obs
    )
    assert parsed.book is not None
    raw = BOOK_FIXTURE["message"]
    for side, levels in (("bids", parsed.book.bids), ("asks", parsed.book.asks)):
        for (price, size), entry in zip(levels, raw[side], strict=True):
            assert price == parse_price(entry["price"])
            assert size == parse_share(entry["size"])
    assert price_obs.max_decimals <= 6
    assert size_obs.max_decimals <= 6


def test_the_venue_hash_is_recorded_but_is_not_a_sequence() -> None:
    """The venue publishes no documented monotonic sequence; none is fabricated."""
    parsed = parse_market_message(BOOK_FIXTURE["message"])
    assert parsed.book is not None
    assert parsed.book.venue_hash == BOOK_FIXTURE["message"]["hash"]
    assert not hasattr(parsed.book, "sequence")


# -- real price_change message --------------------------------------------------------------


def test_real_price_change_fixture_parses() -> None:
    parsed = parse_market_message(CHANGE_FIXTURE["message"])
    assert parsed.kind is MarketEventKind.PRICE_CHANGE
    assert parsed.price_changes
    raw_changes = CHANGE_FIXTURE["message"]["price_changes"]
    for change, raw in zip(parsed.price_changes, raw_changes, strict=True):
        assert change.asset_id == raw["asset_id"]
        assert change.price == parse_price(raw["price"])
        assert change.size == parse_share(raw["size"])
        assert change.side == raw["side"]


def test_price_change_carries_best_bid_ask_when_present() -> None:
    parsed = parse_market_message(CHANGE_FIXTURE["message"])
    raw_changes = CHANGE_FIXTURE["message"]["price_changes"]
    for change, raw in zip(parsed.price_changes, raw_changes, strict=True):
        if raw.get("best_bid") is not None:
            assert change.best_bid == parse_price(raw["best_bid"])
        if raw.get("best_ask") is not None:
            assert change.best_ask == parse_price(raw["best_ask"])


def test_real_last_trade_price_fixture_is_recognised_not_dropped() -> None:
    fixture = load("polymarket_last_trade_price.json")
    parsed = parse_market_message(fixture["message"])
    assert parsed.kind is MarketEventKind.LAST_TRADE_PRICE
    assert parsed.unhandled_kind is None


# -- documented-shape events not seen in the capture window ---------------------------------


def test_tick_size_change_is_parsed_and_never_discarded() -> None:
    """No real sample was observed in the capture window; shape follows the documentation."""
    parsed = parse_market_message(
        {
            "event_type": "tick_size_change",
            "asset_id": UP,
            "market": "0xabc",
            "old_tick_size": "0.01",
            "new_tick_size": "0.001",
            "timestamp": "1787646150118",
        }
    )
    assert parsed.kind is MarketEventKind.TICK_SIZE_CHANGE
    assert parsed.tick_size is not None
    assert parsed.tick_size.old_tick_size == parse_price("0.01")
    assert parsed.tick_size.new_tick_size == parse_price("0.001")


def test_best_bid_ask_is_parsed_when_the_feature_is_enabled() -> None:
    parsed = parse_market_message(
        {
            "event_type": "best_bid_ask",
            "asset_id": UP,
            "best_bid": "0.62",
            "best_ask": "0.64",
            "timestamp": "1787646150118",
        }
    )
    assert parsed.kind is MarketEventKind.BEST_BID_ASK
    assert parsed.price_changes[0].best_bid == parse_price("0.62")


# -- failure handling -----------------------------------------------------------------------


def test_an_unknown_event_type_is_surfaced_not_swallowed() -> None:
    parsed = parse_market_message({"event_type": "teleport", "timestamp": "1"})
    assert parsed.kind is None
    assert parsed.unhandled_kind == "teleport"


@pytest.mark.parametrize(
    "message",
    [
        {"no_event_type": 1},
        {"event_type": 5},
        {"event_type": "book"},
        {"event_type": "price_change"},
        {"event_type": "tick_size_change", "asset_id": "x"},
    ],
)
def test_malformed_messages_fail_closed(message: dict[str, object]) -> None:
    with pytest.raises(FeedConformanceError):
        parse_market_message(message)


def test_a_non_object_message_fails_closed() -> None:
    with pytest.raises(FeedConformanceError, match="not an object"):
        parse_market_message([1, 2, 3])


def test_a_float_price_fails_closed_rather_than_rounding() -> None:
    """O10: a value that arrived as a float has already lost exactness."""
    with pytest.raises(ExactnessError, match="decimal string"):
        parse_market_message(
            {
                "event_type": "book",
                "asset_id": UP,
                "bids": [{"price": 0.62, "size": "10"}],
                "asks": [],
            }
        )


def test_a_price_beyond_the_frozen_scale_fails_closed() -> None:
    with pytest.raises(ExactnessError, match="not exactly representable"):
        parse_market_message(
            {
                "event_type": "book",
                "asset_id": UP,
                "bids": [{"price": "0.6200001", "size": "10"}],
                "asks": [],
            }
        )


# -- book tracking --------------------------------------------------------------------------


def is_ready(book: BookTracker) -> bool:
    """Read through a function so mypy does not narrow the flag across a mutating call."""
    return book.ready


def tracker() -> BookTracker:
    return BookTracker(UP, DOWN)


def test_up_and_down_books_are_tracked_separately() -> None:
    """Canonical §5.2's mirror identity is conditional; the adapter records reality."""
    book = tracker()
    book.apply(
        parse_market_message(
            {
                "event_type": "book",
                "asset_id": UP,
                "bids": [{"price": "0.62", "size": "100"}],
                "asks": [{"price": "0.64", "size": "50"}],
            }
        )
    )
    assert book.up.best_bid() is not None
    assert book.down.best_bid() is None, "the DOWN book must not be derived from UP"
    assert not is_ready(book)

    book.apply(
        parse_market_message(
            {
                "event_type": "book",
                "asset_id": DOWN,
                "bids": [{"price": "0.30", "size": "70"}],
                "asks": [{"price": "0.33", "size": "80"}],
            }
        )
    )
    assert is_ready(book)
    down_bid = book.down.best_bid()
    assert down_bid is not None
    assert down_bid.price == parse_price("0.30")
    # Deliberately NOT the complement of the UP side.
    assert down_bid.price != PriceUnits(1_000_000 - parse_price("0.64"))


def test_price_change_updates_the_correct_token_only() -> None:
    book = tracker()
    book.apply(parse_market_message({"event_type": "book", "asset_id": UP, "bids": [], "asks": []}))
    book.apply(
        parse_market_message({"event_type": "book", "asset_id": DOWN, "bids": [], "asks": []})
    )
    book.apply(
        parse_market_message(
            {
                "event_type": "price_change",
                "price_changes": [{"asset_id": UP, "price": "0.61", "size": "25", "side": "BUY"}],
            }
        )
    )
    up_bid = book.up.best_bid()
    assert up_bid is not None
    assert up_bid.size == parse_share("25")
    assert book.down.best_bid() is None


def test_a_zero_size_change_removes_the_level() -> None:
    book = tracker()
    book.apply(
        parse_market_message(
            {
                "event_type": "book",
                "asset_id": UP,
                "bids": [{"price": "0.62", "size": "100"}],
                "asks": [],
            }
        )
    )
    assert book.up.best_bid() is not None
    book.apply(
        parse_market_message(
            {
                "event_type": "price_change",
                "price_changes": [{"asset_id": UP, "price": "0.62", "size": "0", "side": "BUY"}],
            }
        )
    )
    assert book.up.best_bid() is None


def test_best_bid_and_ask_pick_the_right_extremes() -> None:
    book = tracker()
    book.apply(
        parse_market_message(
            {
                "event_type": "book",
                "asset_id": UP,
                "bids": [
                    {"price": "0.60", "size": "10"},
                    {"price": "0.62", "size": "20"},
                    {"price": "0.61", "size": "30"},
                ],
                "asks": [
                    {"price": "0.66", "size": "10"},
                    {"price": "0.64", "size": "20"},
                ],
            }
        )
    )
    bid, ask = book.up.best_bid(), book.up.best_ask()
    assert bid is not None and ask is not None
    assert bid.price == parse_price("0.62")
    assert bid.size == ShareUnits(parse_share("20"))
    assert ask.price == parse_price("0.64")


def test_an_unknown_token_is_counted_not_applied() -> None:
    book = tracker()
    book.apply(
        parse_market_message({"event_type": "book", "asset_id": "999", "bids": [], "asks": []})
    )
    assert book.unknown_token_messages == 1
    assert not is_ready(book)


def test_clearing_drops_all_state_and_requires_a_fresh_snapshot() -> None:
    book = tracker()
    for token in (UP, DOWN):
        book.apply(
            parse_market_message(
                {
                    "event_type": "book",
                    "asset_id": token,
                    "bids": [{"price": "0.5", "size": "1"}],
                    "asks": [],
                }
            )
        )
    assert is_ready(book)
    book.clear()
    assert not is_ready(book)
    assert book.up.best_bid() is None
