"""Up-space translation identities (Canonical §5)."""

from __future__ import annotations

import random

import pytest

from maker5m.domain import Outcome
from maker5m.numeric import PRICE_SCALE, DomainError, PriceUnits, parse_price
from maker5m.strategy import UpSpaceSide, complement, to_upspace, to_venue

TICK = PriceUnits(10_000)


def test_buy_up_maps_to_a_synthetic_bid_at_the_same_price() -> None:
    side, price = to_upspace(Outcome.UP, parse_price("0.63"))
    assert side is UpSpaceSide.BID
    assert price == parse_price("0.63")


def test_buy_down_maps_to_a_synthetic_ask_at_the_complement() -> None:
    side, price = to_upspace(Outcome.DOWN, parse_price("0.37"))
    assert side is UpSpaceSide.ASK
    assert price == parse_price("0.63")


def test_venue_translation_round_trips() -> None:
    for outcome, text in ((Outcome.UP, "0.63"), (Outcome.DOWN, "0.37")):
        side, up_price = to_upspace(outcome, parse_price(text))
        back_outcome, back_price = to_venue(side, up_price)
        assert back_outcome is outcome
        assert back_price == parse_price(text)


def test_side_knows_which_token_it_buys() -> None:
    assert UpSpaceSide.BID.outcome is Outcome.UP
    assert UpSpaceSide.ASK.outcome is Outcome.DOWN


# -- complement ---------------------------------------------------------------------------


def test_complement_endpoints() -> None:
    assert complement(PriceUnits(0)) == PRICE_SCALE
    assert complement(PriceUnits(PRICE_SCALE)) == 0


def test_complement_of_a_half_is_itself() -> None:
    assert complement(parse_price("0.5")) == parse_price("0.5")


def test_complement_is_an_involution_over_a_dense_sweep() -> None:
    """Every 1/10000 of the range, plus both endpoints."""
    for units in range(0, PRICE_SCALE + 1, 97):
        price = PriceUnits(units)
        assert complement(complement(price)) == price
    assert complement(complement(PriceUnits(PRICE_SCALE))) == PRICE_SCALE


def test_complement_is_an_involution_over_a_deterministic_random_sample() -> None:
    rng = random.Random(20260825)
    for _ in range(20_000):
        price = PriceUnits(rng.randint(0, PRICE_SCALE))
        assert complement(complement(price)) == price
        assert complement(price) + price == PRICE_SCALE


def test_complement_preserves_tick_alignment_for_every_supported_tick() -> None:
    """1.00 is a whole number of ticks, so the complement of an aligned price is aligned."""
    from maker5m.numeric import SUPPORTED_TICK_SIZES

    for tick in SUPPORTED_TICK_SIZES:
        for units in range(0, PRICE_SCALE + 1, tick * 7):
            assert complement(PriceUnits(units)) % tick == 0


@pytest.mark.parametrize("bad", [-1, PRICE_SCALE + 1, 2 * PRICE_SCALE])
def test_complement_rejects_a_non_probability(bad: int) -> None:
    with pytest.raises(DomainError):
        complement(PriceUnits(bad))


def test_translation_rejects_a_non_probability() -> None:
    with pytest.raises(DomainError):
        to_upspace(Outcome.UP, PriceUnits(PRICE_SCALE + 1))
    with pytest.raises(DomainError):
        to_venue(UpSpaceSide.ASK, PriceUnits(-1))
