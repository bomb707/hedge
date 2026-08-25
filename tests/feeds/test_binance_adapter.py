"""Binance spot adapter: exact decimals, metadata-driven precision, fail-closed parsing."""

from __future__ import annotations

import json

import pytest

from maker5m.feeds import (
    FeedConformanceError,
    PrecisionObserver,
    agg_trade_stream,
    parse_agg_trade,
    parse_btc_price,
    parse_symbol_rules,
)
from maker5m.feeds.exactness import decimals_in
from maker5m.market.btc_price import BtcPrice
from tests.feeds.conftest import load


def test_stream_name() -> None:
    assert agg_trade_stream("BTCUSDT") == "btcusdt@aggTrade"


# -- exact decimal parsing ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "units", "scale"),
    [
        ("80046.00000000", 8004600000000, 8),
        ("80046.01", 8004601, 2),
        ("80046", 80046, 0),
        ("0.00000001", 1, 8),
        ("123456.78901234", 12345678901234, 8),
    ],
)
def test_prices_parse_exactly_at_their_own_precision(text: str, units: int, scale: int) -> None:
    """The scale comes from the string, so nothing is rounded and nothing is assumed."""
    price = parse_btc_price(text)
    assert price == BtcPrice(units, scale)
    assert str(price) == text


def test_a_float_is_refused() -> None:
    """A JSON float has already lost exactness before the adapter ever sees it."""
    with pytest.raises(FeedConformanceError, match="decimal string"):
        parse_btc_price(80046.01)  # type: ignore[arg-type]


@pytest.mark.parametrize("text", ["8e4", "80_046.00", "", "abc", "80046.0.0", " 80046"])
def test_malformed_prices_fail_closed(text: str) -> None:
    with pytest.raises(FeedConformanceError):
        parse_btc_price(text)


def test_absurd_precision_fails_closed() -> None:
    with pytest.raises(FeedConformanceError, match="beyond the sanity bound"):
        parse_btc_price("1." + "0" * 40)


# -- aggTrade messages ----------------------------------------------------------------------


def test_real_aggtrade_fixture_parses_exactly() -> None:
    fixture = load("binance_aggtrade.json")
    for message in fixture["messages"]:
        raw = json.dumps(message)
        parsed = parse_agg_trade(raw)
        assert str(parsed.price) == message["p"]
        assert parsed.source_sequence == message["a"]
        assert parsed.source_timestamp_ms == message["T"]


def test_real_aggtrade_prices_round_trip_through_btcprice() -> None:
    """O12 evidence: every captured value survives the representation exactly."""
    for message in load("binance_aggtrade.json")["messages"]:
        text = message["p"]
        assert str(parse_btc_price(text)) == text


def test_aggtrade_without_a_price_fails_closed() -> None:
    with pytest.raises(FeedConformanceError, match="no price field"):
        parse_agg_trade(json.dumps({"a": 1, "T": 2}))


def test_aggtrade_that_is_not_json_fails_closed() -> None:
    with pytest.raises(FeedConformanceError, match="not JSON"):
        parse_agg_trade("{not json")


def test_aggtrade_that_is_not_an_object_fails_closed() -> None:
    with pytest.raises(FeedConformanceError, match="not an object"):
        parse_agg_trade("[1,2,3]")


def test_precision_observer_records_what_arrived() -> None:
    observer = PrecisionObserver("binance_price")
    for message in load("binance_aggtrade.json")["messages"]:
        parse_agg_trade(json.dumps(message), observer)
    assert observer.samples == len(load("binance_aggtrade.json")["messages"])
    assert observer.max_decimals >= 1


# -- symbol metadata ------------------------------------------------------------------------


def test_real_exchangeinfo_fixture_yields_symbol_rules() -> None:
    """Precision is metadata-driven, which is the core O12 argument."""
    fixture = load("binance_exchangeinfo.json")
    rules = parse_symbol_rules(json.dumps({"symbols": fixture["symbols"]}), "BTCUSDT")
    assert rules.symbol == "BTCUSDT"
    assert decimals_in(rules.tick_size) == rules.tick_decimals
    assert rules.quote_precision >= rules.tick_decimals
    assert rules.tick_size.startswith("0.01")


def test_unknown_symbol_fails_closed() -> None:
    fixture = load("binance_exchangeinfo.json")
    with pytest.raises(FeedConformanceError, match="expected exactly 1"):
        parse_symbol_rules(json.dumps({"symbols": fixture["symbols"]}), "NOPEUSDT")


def test_missing_price_filter_fails_closed() -> None:
    payload = json.dumps(
        {"symbols": [{"symbol": "X", "quotePrecision": 8, "baseAssetPrecision": 8, "filters": []}]}
    )
    with pytest.raises(FeedConformanceError, match="no PRICE_FILTER"):
        parse_symbol_rules(payload, "X")


def test_unusable_payload_fails_closed() -> None:
    with pytest.raises(FeedConformanceError):
        parse_symbol_rules("{}", "BTCUSDT")
