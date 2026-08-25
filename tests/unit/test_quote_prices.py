"""Zero synthetic spread, exactly, for every centre on every supported tick."""

from __future__ import annotations

import dataclasses

import pytest

from maker5m.numeric import (
    PRICE_SCALE,
    SUPPORTED_TICK_SIZES,
    DomainError,
    PriceUnits,
    parse_price,
)
from maker5m.strategy import QuotePrices, build_quote_prices, complement

TICK = PriceUnits(10_000)


def test_the_documented_example() -> None:
    """Detailed §9: C = 0.63 gives BUY UP @ 0.63 and BUY DOWN @ 0.37."""
    prices = build_quote_prices(parse_price("0.63"), TICK)
    assert prices.up_buy_price == parse_price("0.63")
    assert prices.down_buy_price == parse_price("0.37")


def test_synthetic_bid_equals_synthetic_ask_at_every_tick_on_the_grid() -> None:
    """The core property: delta_ticks = 0 (Canonical §8.1)."""
    for tick in SUPPORTED_TICK_SIZES:
        for units in range(0, PRICE_SCALE + 1, tick):
            prices = build_quote_prices(PriceUnits(units), tick)
            assert prices.synthetic_bid == prices.synthetic_ask
            assert prices.synthetic_spread == 0


def test_one_minus_the_down_price_is_exactly_the_centre() -> None:
    for units in range(0, PRICE_SCALE + 1, TICK):
        centre = PriceUnits(units)
        prices = build_quote_prices(centre, TICK)
        assert complement(prices.down_buy_price) == centre
        assert prices.up_buy_price + prices.down_buy_price == PRICE_SCALE


def test_endpoints() -> None:
    low = build_quote_prices(PriceUnits(0), TICK)
    assert low.up_buy_price == 0
    assert low.down_buy_price == PRICE_SCALE
    high = build_quote_prices(PriceUnits(PRICE_SCALE), TICK)
    assert high.up_buy_price == PRICE_SCALE
    assert high.down_buy_price == 0


def test_bid_and_ask_never_differ_by_a_single_integer_unit() -> None:
    for units in range(0, PRICE_SCALE + 1, 10_000):
        prices = build_quote_prices(PriceUnits(units), TICK)
        assert abs(prices.synthetic_ask - prices.synthetic_bid) == 0


def test_construction_rejects_a_centre_off_the_tick_grid() -> None:
    with pytest.raises(DomainError):
        build_quote_prices(parse_price("0.631"), TICK)


def test_construction_rejects_a_non_positive_tick() -> None:
    with pytest.raises(DomainError):
        build_quote_prices(parse_price("0.63"), PriceUnits(0))


def test_a_hand_built_spread_is_rejected_at_construction() -> None:
    """Guards against a future edit quietly reintroducing a spread (I05)."""
    with pytest.raises(DomainError):
        QuotePrices(
            centre=parse_price("0.63"),
            up_buy_price=parse_price("0.63"),
            down_buy_price=parse_price("0.38"),  # would leave a one-tick synthetic spread
            tick=TICK,
        )
    with pytest.raises(DomainError):
        QuotePrices(
            centre=parse_price("0.63"),
            up_buy_price=parse_price("0.62"),  # bid away from the centre
            down_buy_price=parse_price("0.37"),
            tick=TICK,
        )


def test_quote_prices_are_immutable() -> None:
    prices = build_quote_prices(parse_price("0.63"), TICK)
    with pytest.raises(dataclasses.FrozenInstanceError):
        prices.up_buy_price = parse_price("0.64")  # type: ignore[misc]


def test_the_soft_price_band_is_not_enforced() -> None:
    """Canonical §8.3, §29.9: a hard 0.11-0.89 cutoff would remove endgame fills."""
    for text in ("0.01", "0.05", "0.10", "0.90", "0.95", "0.99"):
        prices = build_quote_prices(parse_price(text), TICK)
        assert prices.synthetic_spread == 0
