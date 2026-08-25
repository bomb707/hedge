"""O10's residual validation: real venue traffic must be exactly representable.

O10 was closed for the numeric kernel on Polymarket's published token decimals, leaving one
requirement for this phase — verify real ``btc-updown-5m-*`` traffic against the frozen
scales. These tests run that check against captured real messages, and prove the guard fails
closed rather than rounding when a value does not fit.
"""

from __future__ import annotations

import pytest

from maker5m.feeds import ExactnessError, PrecisionObserver, parse_venue_price, parse_venue_size
from maker5m.feeds.exactness import decimals_in, parse_venue_money
from maker5m.numeric import SCALE_DECIMALS, format_price, format_share
from tests.feeds.conftest import load

BOOK = load("polymarket_book.json")["message"]
CHANGE = load("polymarket_price_change.json")["message"]


def test_every_real_book_price_and_size_is_exactly_representable() -> None:
    price_obs = PrecisionObserver("price")
    size_obs = PrecisionObserver("size")
    for side in ("bids", "asks"):
        for level in BOOK[side]:
            price = parse_venue_price(level["price"], observer=price_obs)
            size = parse_venue_size(level["size"], observer=size_obs)
            # Round-trip proves exactness rather than merely "it parsed".
            assert format_price(price).rstrip("0").rstrip(".") == (
                level["price"].rstrip("0").rstrip(".") if "." in level["price"] else level["price"]
            )
            assert format_share(size).rstrip("0").rstrip(".") == (
                level["size"].rstrip("0").rstrip(".") if "." in level["size"] else level["size"]
            )
    assert price_obs.samples > 0
    assert size_obs.samples > 0
    assert price_obs.max_decimals <= SCALE_DECIMALS
    assert size_obs.max_decimals <= SCALE_DECIMALS


def test_every_real_price_change_value_is_exactly_representable() -> None:
    for entry in CHANGE["price_changes"]:
        parse_venue_price(entry["price"])
        parse_venue_size(entry["size"])
        for key in ("best_bid", "best_ask"):
            if entry.get(key) is not None:
                parse_venue_price(entry[key])


def test_observed_precision_is_well_within_the_frozen_scales() -> None:
    """The recorded evidence behind O10's residual closure."""
    price_obs = PrecisionObserver("polymarket_price")
    size_obs = PrecisionObserver("polymarket_size")
    for side in ("bids", "asks"):
        for level in BOOK[side]:
            parse_venue_price(level["price"], observer=price_obs)
            parse_venue_size(level["size"], observer=size_obs)
    assert price_obs.max_decimals <= SCALE_DECIMALS
    assert size_obs.max_decimals <= SCALE_DECIMALS
    # The summary is what the closure evidence is recorded from.
    assert price_obs.summary()["label"] == "polymarket_price"
    assert size_obs.summary()["samples"] == size_obs.samples


def test_the_venue_tick_and_min_size_are_exactly_representable() -> None:
    market = load("polymarket_discovery_clob.json")["market"]
    assert parse_venue_price(str(market["minimum_tick_size"])) > 0
    assert parse_venue_size(str(market["minimum_order_size"])) > 0


# -- the guard fails closed ---------------------------------------------------


def test_a_value_beyond_the_frozen_scale_raises_rather_than_rounding() -> None:
    with pytest.raises(ExactnessError, match="not exactly representable"):
        parse_venue_price("0.1234567")
    with pytest.raises(ExactnessError, match="not exactly representable"):
        parse_venue_size("1.0000001")
    with pytest.raises(ExactnessError):
        parse_venue_money("1.00000001")


def test_trailing_zeros_beyond_the_scale_are_still_accepted() -> None:
    """Excess digits that are all zero carry no information."""
    assert parse_venue_price("0.630000000") == parse_venue_price("0.63")


def test_a_float_is_refused_because_exactness_is_already_lost() -> None:
    with pytest.raises(ExactnessError, match="decimal string"):
        parse_venue_price(0.63)  # type: ignore[arg-type]
    with pytest.raises(ExactnessError, match="decimal string"):
        parse_venue_size(10.5)  # type: ignore[arg-type]


def test_a_negative_size_is_refused() -> None:
    with pytest.raises(ExactnessError):
        parse_venue_size("-1")


def test_decimals_in_counts_as_written() -> None:
    assert decimals_in("0.01") == 2
    assert decimals_in("80046.00000000") == 8
    assert decimals_in("5") == 0
