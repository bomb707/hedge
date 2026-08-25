"""Exact parsing, representability, and the named conversions."""

from __future__ import annotations

import pytest

from maker5m.numeric import (
    MONEY_SCALE,
    PRICE_SCALE,
    SHARE_SCALE,
    DomainError,
    InexactError,
    MoneyUnits,
    NotRepresentableError,
    ParseError,
    PriceUnits,
    Rounding,
    ShareUnits,
    format_money,
    format_price,
    format_share,
    money_from_whole,
    notional_cost,
    parse_fixed_point,
    parse_money,
    parse_price,
    parse_share,
    price_from_whole,
    share_from_whole,
    shares_at_par,
    to_display_float,
)


def test_frozen_scales_are_exactly_one_million() -> None:
    """O10's resolution. Changing these invalidates every recorded journal."""
    assert SHARE_SCALE == 1_000_000
    assert MONEY_SCALE == 1_000_000
    assert PRICE_SCALE == 1_000_000


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", 0),
        ("-0", 0),
        ("1", 1_000_000),
        ("+1", 1_000_000),
        ("0.63", 630_000),
        ("13.63", 13_630_000),
        ("0.000001", 1),
        ("120", 120_000_000),
        ("-13.63", -13_630_000),
        ("0.100000", 100_000),
        ("999999.999999", 999_999_999_999),
    ],
)
def test_parse_share_is_exact(text: str, expected: int) -> None:
    assert parse_share(text) == expected


@pytest.mark.parametrize("text", ["1.0000000", "1.000000000000", "0.6300000", "-0.0000000"])
def test_excess_zero_digits_carry_no_information_and_are_accepted(text: str) -> None:
    assert parse_share(text) == parse_share(text.rstrip("0").rstrip("."))


@pytest.mark.parametrize("text", ["1.0000001", "0.0000009", "13.6300001", "-1.0000001"])
def test_excess_non_zero_digits_are_rejected_not_rounded(text: str) -> None:
    """Never silently round an authoritative ledger input (ARCHITECTURE section 6.3)."""
    with pytest.raises(NotRepresentableError):
        parse_share(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        " 1",
        "1 ",
        ".",
        ".5",
        "1.",
        "1..2",
        "1e5",
        "1E5",
        "1_000",
        "nan",
        "inf",
        "-inf",
        "0x10",
        "--1",
        "+-1",
        "1,000",
        "٣",  # Arabic-Indic digit three: str.isdigit() accepts it, we must not
        "²",  # superscript two
    ],
)
def test_malformed_input_is_rejected(text: str) -> None:
    with pytest.raises(ParseError):
        parse_share(text)


@pytest.mark.parametrize("value", [1, 1.0, None, b"1", ["1"]])
def test_non_string_input_is_rejected(value: object) -> None:
    """Adapters must hand over the venue's decimal string, never a float."""
    with pytest.raises(ParseError):
        parse_share(value)  # type: ignore[arg-type]


def test_negative_rejected_where_the_domain_forbids_it() -> None:
    assert parse_share("-1.5") == -1_500_000
    with pytest.raises(DomainError):
        parse_share("-1.5", allow_negative=False)
    with pytest.raises(DomainError):
        parse_money("-0.01", allow_negative=False)


@pytest.mark.parametrize(("text", "expected"), [("0", 0), ("0.63", 630_000), ("1", 1_000_000)])
def test_parse_price_is_exact(text: str, expected: int) -> None:
    assert parse_price(text) == expected


@pytest.mark.parametrize("text", ["-0.01", "1.000001", "2"])
def test_price_must_lie_in_the_unit_interval(text: str) -> None:
    with pytest.raises((DomainError, NotRepresentableError)):
        parse_price(text)


def test_whole_constructors() -> None:
    assert share_from_whole(120) == 120_000_000
    assert money_from_whole(72) == 72_000_000
    assert price_from_whole(63, 100) == 630_000
    assert price_from_whole(1, 2) == 500_000


def test_price_from_whole_refuses_an_inexact_ratio() -> None:
    with pytest.raises(InexactError):
        price_from_whole(1, 3)
    with pytest.raises(DomainError):
        price_from_whole(1, 0)
    with pytest.raises(DomainError):
        price_from_whole(3, 2)


def test_shares_at_par_is_the_named_share_to_money_boundary() -> None:
    """One winning share pays exactly $1.00."""
    assert shares_at_par(share_from_whole(120)) == money_from_whole(120)
    assert shares_at_par(ShareUnits(1)) == MoneyUnits(1)


@pytest.mark.parametrize(
    ("shares", "price", "expected"),
    [
        ("120", "0.60", 72_000_000),
        ("100", "0.50", 50_000_000),
        ("13.63", "0.63", 8_586_900),
        ("0", "0.63", 0),
    ],
)
def test_notional_cost_is_exact_on_the_tick_grid(shares: str, price: str, expected: int) -> None:
    got = notional_cost(parse_share(shares), parse_price(price), rounding=Rounding.EXACT)
    assert got == expected


def test_notional_cost_requires_an_explicit_rounding_mode_when_inexact() -> None:
    shares = ShareUnits(1)  # 0.000001 shares
    price = PriceUnits(500_000)  # 0.50 -> 0.0000005 dollars, below one MoneyUnit
    with pytest.raises(InexactError):
        notional_cost(shares, price, rounding=Rounding.EXACT)
    assert notional_cost(shares, price, rounding=Rounding.FLOOR) == 0
    assert notional_cost(shares, price, rounding=Rounding.CEILING) == 1


def test_notional_rounding_directions_are_absolute_not_magnitude_based() -> None:
    shares = ShareUnits(-1)
    price = PriceUnits(500_000)
    assert notional_cost(shares, price, rounding=Rounding.FLOOR) == -1
    assert notional_cost(shares, price, rounding=Rounding.CEILING) == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "0.000000"), (1, "0.000001"), (13_630_000, "13.630000"), (-2_000_000, "-2.000000")],
)
def test_formatting_round_trips_exactly(value: int, expected: str) -> None:
    assert format_share(ShareUnits(value)) == expected
    assert format_money(MoneyUnits(value)) == expected
    assert parse_share(expected) == value


def test_format_price_is_exact() -> None:
    assert format_price(parse_price("0.63")) == "0.630000"


def test_display_float_is_the_only_exit_and_is_clearly_lossy() -> None:
    assert to_display_float(630_000) == pytest.approx(0.63)


@pytest.mark.parametrize(
    ("text", "decimals", "expected"),
    [("64123", 0, 64123), ("64123.00", 0, 64123), ("1", 0, 1), ("0", 0, 0), ("-7", 0, -7)],
)
def test_parse_fixed_point_handles_a_zero_decimal_scale(
    text: str, decimals: int, expected: int
) -> None:
    """Regression: with no fractional digits at decimals=0 the padded fraction is empty."""
    assert parse_fixed_point(text, decimals=decimals) == expected


def test_parse_fixed_point_at_zero_decimals_still_rejects_real_precision() -> None:
    with pytest.raises(NotRepresentableError):
        parse_fixed_point("64123.5", decimals=0)


def test_parse_fixed_point_rejects_negative_decimals() -> None:
    with pytest.raises(DomainError):
        parse_fixed_point("1", decimals=-1)
