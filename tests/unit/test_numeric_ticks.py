"""Tick grid alignment and the venue order-size quantisation primitive."""

from __future__ import annotations

import pytest

from maker5m.numeric import (
    SUPPORTED_TICK_SIZES,
    TICK_0_0001,
    TICK_0_001,
    TICK_0_01,
    TICK_0_1,
    VENUE_ORDER_SIZE_DECIMALS,
    DomainError,
    InexactError,
    PriceUnits,
    ShareUnits,
    is_price_aligned,
    is_supported_tick,
    parse_price,
    parse_share,
    price_to_ticks,
    quantize_order_size,
    ticks_to_price,
)


def test_every_documented_tick_size_is_exact() -> None:
    assert TICK_0_1 == 100_000
    assert TICK_0_01 == 10_000
    assert TICK_0_001 == 1_000
    assert TICK_0_0001 == 100
    assert all(is_supported_tick(t) for t in SUPPORTED_TICK_SIZES)


def test_unsupported_tick_is_reported_as_such() -> None:
    assert not is_supported_tick(PriceUnits(3_333))


@pytest.mark.parametrize(
    ("price", "tick", "aligned"),
    [
        ("0.63", TICK_0_01, True),
        ("0.631", TICK_0_01, False),
        ("0.631", TICK_0_001, True),
        ("0.6", TICK_0_1, True),
        ("0.63", TICK_0_1, False),
        ("0.6301", TICK_0_0001, True),
        ("0", TICK_0_01, True),
        ("1", TICK_0_01, True),
    ],
)
def test_price_alignment(price: str, tick: PriceUnits, aligned: bool) -> None:
    assert is_price_aligned(parse_price(price), tick) is aligned


def test_tick_conversion_round_trips() -> None:
    price = parse_price("0.63")
    assert price_to_ticks(price, TICK_0_01) == 63
    assert ticks_to_price(63, TICK_0_01) == price


def test_off_grid_price_raises_rather_than_rounding() -> None:
    with pytest.raises(InexactError):
        price_to_ticks(parse_price("0.631"), TICK_0_01)


def test_tick_helpers_reject_a_non_positive_tick() -> None:
    with pytest.raises(DomainError):
        is_price_aligned(parse_price("0.63"), PriceUnits(0))


def test_ticks_to_price_is_range_checked() -> None:
    with pytest.raises(DomainError):
        ticks_to_price(101, TICK_0_01)


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ("13.63", "13.63"),
        ("13.639999", "13.63"),
        ("13.631", "13.63"),
        ("0.009", "0"),
        ("-13.639", "-13.63"),
        ("5", "5"),
    ],
)
def test_order_size_quantisation_truncates_toward_zero(size: str, expected: str) -> None:
    """A submitted order must never exceed the intended size in either direction."""
    assert quantize_order_size(parse_share(size)) == parse_share(expected)


def test_order_size_quantisation_default_matches_the_official_client() -> None:
    assert VENUE_ORDER_SIZE_DECIMALS == 2


def test_order_size_quantisation_rejects_an_impossible_precision() -> None:
    with pytest.raises(DomainError):
        quantize_order_size(ShareUnits(1), decimals=7)
    with pytest.raises(DomainError):
        quantize_order_size(ShareUnits(1), decimals=-1)


def test_order_size_quantisation_is_not_the_strategy_grid() -> None:
    """Guard against the two ideas being conflated (P1 brief section 13).

    The 5-share inventory lattice is P3 work and lives nowhere in the numeric kernel. This
    asserts the transport primitive leaves a non-lattice size alone rather than snapping it
    to a multiple of five.
    """
    assert quantize_order_size(parse_share("13.63")) == parse_share("13.63")
