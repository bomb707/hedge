"""BtcPrice: exact, float-free, and self-describing while O12 stays open."""

from __future__ import annotations

import dataclasses

import pytest

from maker5m.market.btc_price import MAX_BTC_SCALE_DECIMALS, BtcPrice
from maker5m.numeric import DomainError, NotRepresentableError, ParseError


def test_parses_exactly() -> None:
    assert BtcPrice.parse("64123.45", scale_decimals=2) == BtcPrice(6_412_345, 2)
    assert BtcPrice.parse("64123", scale_decimals=0) == BtcPrice(64_123, 0)
    assert BtcPrice.parse("0.00000001", scale_decimals=8).units == 1


def test_excess_precision_raises_rather_than_rounding() -> None:
    """Same fail-closed rule as the frozen scales: a wrong scale surfaces as a halt."""
    with pytest.raises(NotRepresentableError):
        BtcPrice.parse("64123.456", scale_decimals=2)


def test_excess_zero_digits_are_accepted() -> None:
    assert BtcPrice.parse("64123.4500", scale_decimals=2).units == 6_412_345


def test_malformed_input_is_rejected() -> None:
    with pytest.raises(ParseError):
        BtcPrice.parse("64_123.45", scale_decimals=2)
    with pytest.raises(ParseError):
        BtcPrice.parse("6.4e4", scale_decimals=2)


def test_negative_price_is_rejected() -> None:
    with pytest.raises(DomainError):
        BtcPrice(-1, 2)


def test_scale_is_bounded() -> None:
    with pytest.raises(DomainError):
        BtcPrice(1, MAX_BTC_SCALE_DECIMALS + 1)
    with pytest.raises(DomainError):
        BtcPrice(1, -1)
    with pytest.raises(DomainError):
        BtcPrice.parse("1", scale_decimals=-1)


def test_rescaling_up_is_exact() -> None:
    assert BtcPrice(6_412_345, 2).rescaled_to(4) == BtcPrice(641_234_500, 4)


def test_rescaling_down_refuses_to_lose_a_digit() -> None:
    assert BtcPrice(641_234_500, 4).rescaled_to(2) == BtcPrice(6_412_345, 2)
    with pytest.raises(DomainError):
        BtcPrice(641_234_501, 4).rescaled_to(2)


def test_rescaling_to_the_same_scale_is_identity() -> None:
    price = BtcPrice(1, 2)
    assert price.rescaled_to(2) is price


def test_comparison_across_scales_is_exact_not_a_raw_integer_compare() -> None:
    higher = BtcPrice(6_412_346, 2)  # 64123.46
    lower = BtcPrice(641_234_500, 4)  # 64123.45
    assert higher.compare(lower) == 1
    assert lower.compare(higher) == -1
    assert BtcPrice(6_412_345, 2).compare(lower) == 0
    # A raw integer comparison would have said the opposite, which is why one exists at all.
    assert higher.units < lower.units


def test_is_immutable_and_compares_by_value() -> None:
    price = BtcPrice(1, 2)
    with pytest.raises(dataclasses.FrozenInstanceError):
        price.units = 2  # type: ignore[misc]
    assert BtcPrice(1, 2) == BtcPrice(1, 2)


def test_string_form_is_exact() -> None:
    assert str(BtcPrice(6_412_345, 2)) == "64123.45"
    assert str(BtcPrice(64_123, 0)) == "64123"
