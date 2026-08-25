"""Preparation: exact conversion, blocked-not-altered, and the post-only guard."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from maker5m.domain import Outcome
from maker5m.execution import (
    PreparationOutcome,
    PreparedOrder,
    prepare_order,
    price_to_decimal,
    size_to_decimal,
)
from maker5m.market.events import BookLevel
from maker5m.numeric import (
    TICK_0_0001,
    TICK_0_001,
    TICK_0_0025,
    TICK_0_005,
    TICK_0_01,
    TICK_0_1,
    PriceUnits,
    parse_price,
    parse_share,
)
from maker5m.strategy.decision import DesiredOrder
from tests.execution.builders import UP_TOKEN, px, sh


def ask(price: str | None) -> BookLevel | None:
    return None if price is None else BookLevel(parse_price(price), parse_share("100"))


def prep(
    price: str = "0.63",
    size: str = "15",
    *,
    tick: PriceUnits | None = TICK_0_01,
    min_size: str | None = "5",
    ask_price: str | None = "0.64",
    outcome: Outcome = Outcome.UP,
) -> PreparedOrder:
    return prepare_order(
        DesiredOrder(outcome, px(price), sh(size)),
        token_id=UP_TOKEN,
        venue_tick=tick,
        min_order_size=None if min_size is None else sh(min_size),
        observed_ask=ask(ask_price),
    )


# -- exact conversion, no float ---------------------------------------------


def test_a_safe_order_converts_exactly() -> None:
    prepared = prep("0.63", "15")
    assert prepared.outcome_status is PreparationOutcome.SAFE
    assert prepared.submittable
    assert prepared.submission_price == px("0.63")
    assert prepared.submission_size == sh("15")
    assert prepared.size_quantization_delta == 0


def test_the_sdk_boundary_uses_decimal_never_float() -> None:
    assert price_to_decimal(px("0.63")) == Decimal("0.630000")
    assert size_to_decimal(sh("13.63")) == Decimal("13.630000")
    assert isinstance(price_to_decimal(px("0.63")), Decimal)
    assert not isinstance(price_to_decimal(px("0.63")), float)


def test_decimal_conversion_is_exact_for_every_supported_tick() -> None:
    for tick in (TICK_0_1, TICK_0_01, TICK_0_005, TICK_0_0025, TICK_0_001, TICK_0_0001):
        assert Decimal(tick).scaleb(-6) == price_to_decimal(PriceUnits(tick))


# -- price is never altered --------------------------------------------------


def test_price_is_passed_through_untouched_when_safe() -> None:
    assert prep("0.63").price_unchanged


def test_an_off_venue_tick_price_is_blocked_not_rounded() -> None:
    """Moving a price changes queue position, which changes the strategy."""
    prepared = prep("0.631", tick=TICK_0_01)
    assert prepared.outcome_status is PreparationOutcome.OFF_VENUE_TICK
    assert not prepared.submittable
    assert prepared.submission_price == px("0.631"), "the intent must stay recoverable"
    assert prepared.price_unchanged


@pytest.mark.parametrize(
    ("price", "tick", "legal"),
    [
        ("0.63", TICK_0_01, True),
        ("0.63", TICK_0_005, True),
        ("0.63", TICK_0_0025, True),
        ("0.631", TICK_0_005, False),
        ("0.6325", TICK_0_0025, True),
        ("0.6325", TICK_0_005, False),
        ("0.631", TICK_0_001, True),
        ("0.6", TICK_0_1, True),
        ("0.63", TICK_0_1, False),
    ],
)
def test_venue_tick_alignment_by_integer_modulo(price: str, tick: PriceUnits, legal: bool) -> None:
    prepared = prep(price, tick=tick, ask_price="0.99")
    assert (prepared.outcome_status is not PreparationOutcome.OFF_VENUE_TICK) is legal


def test_prices_outside_the_venue_range_are_blocked() -> None:
    """The venue requires tick <= price <= 1 - tick, so 0.00 and 1.00 are illegal."""
    assert prep("0", ask_price="0.5").outcome_status is PreparationOutcome.OUT_OF_VENUE_RANGE
    assert prep("1", ask_price=None).outcome_status is PreparationOutcome.OUT_OF_VENUE_RANGE
    assert prep("0.01", ask_price="0.5").outcome_status is PreparationOutcome.SAFE


def test_unknown_venue_rules_block_submission() -> None:
    assert prep(tick=None).outcome_status is PreparationOutcome.UNKNOWN_VENUE_RULES
    assert prep(min_size=None).outcome_status is PreparationOutcome.UNKNOWN_VENUE_RULES


# -- size truncates, never rounds up ------------------------------------------


@pytest.mark.parametrize(
    ("strategy_size", "submitted"),
    [("13.630000", "13.63"), ("13.637891", "13.63"), ("15", "15"), ("5.999999", "5.99")],
)
def test_size_truncates_toward_zero(strategy_size: str, submitted: str) -> None:
    prepared = prep(size=strategy_size)
    assert prepared.submission_size == sh(submitted)
    assert prepared.submission_size <= prepared.strategy_size, "never round up"
    assert prepared.strategy_size == sh(strategy_size), "the strategy intent is preserved"


def test_the_quantization_delta_is_recorded_and_never_negative() -> None:
    prepared = prep(size="13.637891")
    assert prepared.size_quantization_delta == sh("0.007891")
    assert prepared.size_quantization_delta >= 0


def test_a_size_that_quantizes_to_zero_is_blocked() -> None:
    prepared = prep(size="0.009999")
    assert prepared.outcome_status is PreparationOutcome.ZERO_AFTER_QUANTIZATION
    assert prepared.submission_size == 0


@pytest.mark.parametrize(
    ("size", "status"),
    [
        ("4.99", PreparationOutcome.BELOW_MIN_SIZE),
        ("5.00", PreparationOutcome.SAFE),
        ("5.01", PreparationOutcome.SAFE),
    ],
)
def test_the_venue_minimum_blocks_rather_than_enlarges(
    size: str, status: PreparationOutcome
) -> None:
    """Enlarging to reach the minimum would overshoot the inventory lattice."""
    prepared = prep(size=size)
    assert prepared.outcome_status is status
    assert prepared.submission_size <= prepared.strategy_size


# -- the post-only guard ------------------------------------------------------


def test_a_price_below_the_observed_ask_is_passive() -> None:
    assert prep("0.63", ask_price="0.64").outcome_status is PreparationOutcome.SAFE


def test_a_price_equal_to_the_ask_is_blocked() -> None:
    """Equality is marketable, not passive."""
    assert prep("0.64", ask_price="0.64").outcome_status is PreparationOutcome.WOULD_CROSS


def test_a_price_above_the_ask_is_blocked() -> None:
    assert prep("0.65", ask_price="0.64").outcome_status is PreparationOutcome.WOULD_CROSS


def test_a_missing_ask_blocks_new_submission() -> None:
    """Passivity cannot be proven without the same outcome's ask."""
    assert prep(ask_price=None).outcome_status is PreparationOutcome.NO_BOOK


def test_the_down_side_is_judged_against_its_own_ask() -> None:
    """The DOWN ask is never inferred from the UP book (Canonical §5.2 is conditional)."""
    prepared = prepare_order(
        DesiredOrder(Outcome.DOWN, px("0.37"), sh("15")),
        token_id="token-down",
        venue_tick=TICK_0_01,
        min_order_size=sh("5"),
        observed_ask=ask("0.36"),
    )
    assert prepared.outcome_status is PreparationOutcome.WOULD_CROSS
    assert prepared.observed_ask == px("0.36")


# -- the desired order is never mutated ---------------------------------------


def test_preparation_never_mutates_the_desired_order() -> None:
    order = DesiredOrder(Outcome.UP, px("0.631"), sh("13.637891"))
    before = dataclasses.astuple(order)
    prepare_order(
        order,
        token_id=UP_TOKEN,
        venue_tick=TICK_0_01,
        min_order_size=sh("5"),
        observed_ask=ask("0.64"),
    )
    assert dataclasses.astuple(order) == before


def test_preparation_is_pure_and_repeatable() -> None:
    assert prep("0.63", "13.637891") == prep("0.63", "13.637891")
