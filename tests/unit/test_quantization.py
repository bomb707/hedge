"""Tick quantization: explicit named tie policies, no hidden banker's rounding (O13)."""

from __future__ import annotations

import pytest

from maker5m.domain import ParameterStatus
from maker5m.numeric import PRICE_SCALE, DomainError, PriceUnits, parse_price
from maker5m.strategy import (
    REFERENCE_TICK_ROUNDING,
    TICK_ROUNDING_STATUS,
    RawCentre,
    TickRounding,
    complement,
    quantize_centre,
)

TICK = PriceUnits(10_000)  # 0.01


def centre_of(text: str) -> RawCentre:
    return RawCentre(parse_price(text), 1)


def test_the_tie_rule_is_recorded_as_open() -> None:
    """The sources give one example and never show a tie. O13."""
    assert TICK_ROUNDING_STATUS is ParameterStatus.OPEN


def test_the_documented_worked_example() -> None:
    """Detailed §10: C_raw = 0.6274 -> 0.63. The only example the sources give."""
    for policy in TickRounding:
        assert quantize_centre(centre_of("0.6274"), TICK, policy) == parse_price("0.63")


def test_the_worked_example_excludes_floor() -> None:
    """FLOOR would have quoted 0.62, so it is not a candidate and is not offered."""
    floored = parse_price("0.6274") // TICK * TICK
    assert floored == parse_price("0.62")
    assert all(quantize_centre(centre_of("0.6274"), TICK, p) != floored for p in TickRounding)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.6200", "0.62"),
        ("0.6201", "0.62"),
        ("0.6249", "0.62"),
        ("0.6251", "0.63"),
        ("0.6299", "0.63"),
        ("0.0000", "0.00"),
        ("1.0000", "1.00"),
    ],
)
def test_non_tie_values_are_policy_independent(raw: str, expected: str) -> None:
    for policy in TickRounding:
        assert quantize_centre(centre_of(raw), TICK, policy) == parse_price(expected)


@pytest.mark.parametrize(
    ("policy", "expected_62_5", "expected_63_5"),
    [
        (TickRounding.HALF_EVEN, "0.62", "0.64"),
        (TickRounding.HALF_UP, "0.63", "0.64"),
        (TickRounding.HALF_DOWN, "0.62", "0.63"),
    ],
)
def test_exact_half_tick_is_decided_by_the_named_policy(
    policy: TickRounding, expected_62_5: str, expected_63_5: str
) -> None:
    """0.625 and 0.635 are exact ties; the three policies genuinely differ."""
    assert quantize_centre(RawCentre(625_000, 1), TICK, policy) == parse_price(expected_62_5)
    assert quantize_centre(RawCentre(635_000, 1), TICK, policy) == parse_price(expected_63_5)


def test_half_unit_raw_centre_quantizes_exactly() -> None:
    """A CLOB mid of an odd bid+ask sum is a genuine half PriceUnit."""
    raw = RawCentre(1_250_001, 2)  # 0.6250005
    assert quantize_centre(raw, TICK, TickRounding.HALF_DOWN) == parse_price("0.63")


def test_reference_policy_is_half_even_and_is_not_pythons_round_by_accident() -> None:
    assert REFERENCE_TICK_ROUNDING is TickRounding.HALF_EVEN
    # round(62.5) is 62 in Python; we get the same answer, but by an explicit named rule.
    assert quantize_centre(RawCentre(625_000, 1), TICK) == parse_price("0.62")


def test_half_even_is_the_only_policy_that_keeps_zero_spread_when_both_sides_are_rounded() -> None:
    """Why HALF_EVEN is the reference default.

    Canonical §32 writes ``px_down = round_to_tick(1 - centre)``. Under that construction a
    tie breaks the complement identity for HALF_UP and HALF_DOWN at every tie point, and never
    for HALF_EVEN. This is a consistency argument, not evidence — O13 stays open.
    """
    ticks_in_one = PRICE_SCALE // TICK
    broken: dict[TickRounding, int] = {}
    for policy in TickRounding:
        count = 0
        for n in range(ticks_in_one):
            doubled = TICK * (2 * n + 1)  # exactly (n + 0.5) ticks, doubled
            up = quantize_centre(RawCentre(doubled, 2), TICK, policy)
            down = quantize_centre(RawCentre(2 * PRICE_SCALE - doubled, 2), TICK, policy)
            if up + down != PRICE_SCALE:
                count += 1
        broken[policy] = count
    assert broken[TickRounding.HALF_EVEN] == 0
    assert broken[TickRounding.HALF_UP] == ticks_in_one
    assert broken[TickRounding.HALF_DOWN] == ticks_in_one


def test_complementing_the_quantized_centre_is_exact_under_every_policy() -> None:
    """The construction P3 actually uses cannot be affected by the open tie rule."""
    ticks_in_one = PRICE_SCALE // TICK
    for policy in TickRounding:
        for n in range(ticks_in_one):
            up = quantize_centre(RawCentre(TICK * (2 * n + 1), 2), TICK, policy)
            assert up + complement(up) == PRICE_SCALE


@pytest.mark.parametrize("tick", [0, -1])
def test_non_positive_tick_is_rejected(tick: int) -> None:
    with pytest.raises(DomainError):
        quantize_centre(centre_of("0.5"), PriceUnits(tick))


def test_tick_that_does_not_divide_the_price_scale_is_rejected() -> None:
    with pytest.raises(DomainError):
        quantize_centre(centre_of("0.5"), PriceUnits(3_333))


def test_every_supported_tick_quantizes_exactly() -> None:
    from maker5m.numeric import SUPPORTED_TICK_SIZES, is_price_aligned

    for tick in SUPPORTED_TICK_SIZES:
        result = quantize_centre(centre_of("0.6274"), tick)
        assert is_price_aligned(result, tick)


def test_quantization_is_deterministic() -> None:
    raw = RawCentre(625_000, 1)
    for policy in TickRounding:
        first = quantize_centre(raw, TICK, policy)
        assert all(quantize_centre(raw, TICK, policy) == first for _ in range(50))
