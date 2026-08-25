"""Authenticated user events normalize onto the existing P2 contracts, secrets excluded."""

from __future__ import annotations

import pytest

from maker5m.accounting import Fill
from maker5m.domain import Outcome
from maker5m.execution import (
    ExecutionError,
    TakerFillViolation,
    normalize_order_update,
    normalize_trade,
)
from maker5m.market import EventMeta, Liquidity, OrderStateEvent, OrderStatus, OwnFill, TimestampNs
from maker5m.numeric import parse_money, parse_price, parse_share
from tests.execution.builders import DOWN_TOKEN, UP_TOKEN

META = EventMeta("0xcondition", "user-1", 0, TimestampNs(1_000))
TOKENS = {"up_token_id": UP_TOKEN, "down_token_id": DOWN_TOKEN}


# -- order updates ----------------------------------------------------


def test_an_order_update_becomes_a_p2_order_state_event() -> None:
    event = normalize_order_update(
        {
            "client_order_id": "coid-1",
            "order_id": "venue-1",
            "asset_id": UP_TOKEN,
            "status": "LIVE",
            "price": "0.63",
            "size_remaining": "15",
        },
        META,
        **TOKENS,
    )
    assert isinstance(event, OrderStateEvent)
    assert event.status is OrderStatus.ACKNOWLEDGED
    assert event.outcome is Outcome.UP
    assert event.price == parse_price("0.63")
    assert event.remaining == parse_share("15")
    assert event.venue_order_id == "venue-1"


@pytest.mark.parametrize(
    ("venue", "expected"),
    [
        ("LIVE", OrderStatus.ACKNOWLEDGED),
        ("MATCHED", OrderStatus.PARTIALLY_FILLED),
        ("FILLED", OrderStatus.FILLED),
        ("CANCELED", OrderStatus.CANCELLED),
        ("REJECTED", OrderStatus.REJECTED),
    ],
)
def test_venue_statuses_map_onto_the_internal_vocabulary(venue: str, expected: OrderStatus) -> None:
    event = normalize_order_update(
        {"client_order_id": "c", "asset_id": UP_TOKEN, "status": venue}, META, **TOKENS
    )
    assert event.status is expected


def test_an_unmapped_status_becomes_unknown_not_a_guess() -> None:
    """An unknown order state is a risk condition (Canonical §28.1), not a value to invent."""
    event = normalize_order_update(
        {"client_order_id": "c", "asset_id": UP_TOKEN, "status": "TELEPORTED"}, META, **TOKENS
    )
    assert event.status is OrderStatus.UNKNOWN


def test_a_token_from_another_market_is_refused() -> None:
    with pytest.raises(ExecutionError, match="neither side"):
        normalize_order_update(
            {"client_order_id": "c", "asset_id": "other", "status": "LIVE"}, META, **TOKENS
        )


def test_an_update_without_an_identity_is_refused() -> None:
    with pytest.raises(ExecutionError, match="client order id"):
        normalize_order_update({"asset_id": UP_TOKEN, "status": "LIVE"}, META, **TOKENS)


# -- trades -----------------------------------------------------------


def test_a_trade_becomes_a_p2_own_fill() -> None:
    fill_event, violation = normalize_trade(
        {
            "asset_id": UP_TOKEN,
            "size": "13.63",
            "price": "0.63",
            "maker_amount_filled": "8.5869",
            "liquidity": "MAKER",
            "client_order_id": "coid-1",
            "order_id": "venue-1",
        },
        META,
        **TOKENS,
    )
    assert isinstance(fill_event, OwnFill)
    assert isinstance(fill_event.fill, Fill)
    assert fill_event.fill.shares == parse_share("13.63")
    assert fill_event.fill.cost == parse_money("8.5869")
    assert fill_event.liquidity is Liquidity.MAKER
    assert violation is None


def test_cost_comes_from_the_venue_not_from_size_times_price() -> None:
    """Order construction and atomic rounding mean the two can differ; follow the money."""
    fill_event, _ = normalize_trade(
        {
            "asset_id": UP_TOKEN,
            "size": "13.63",
            "price": "0.63",
            "maker_amount_filled": "8.586901",
            "liquidity": "MAKER",
        },
        META,
        **TOKENS,
    )
    assert fill_event.fill.cost == parse_money("8.586901")


def test_a_taker_fill_is_surfaced_as_an_invariant_violation() -> None:
    """Canonical §11 calls this an execution bug; it must stay visible for P9 (I07)."""
    fill_event, violation = normalize_trade(
        {
            "asset_id": DOWN_TOKEN,
            "size": "5",
            "price": "0.36",
            "liquidity": "TAKER",
            "client_order_id": "coid-2",
        },
        META,
        **TOKENS,
    )
    assert fill_event.liquidity is Liquidity.TAKER, "the flag is preserved, never discarded"
    assert isinstance(violation, TakerFillViolation)
    assert violation.client_order_id == "coid-2"
    assert violation.outcome is Outcome.DOWN


def test_a_maker_fill_produces_no_violation() -> None:
    _, violation = normalize_trade(
        {"asset_id": UP_TOKEN, "size": "5", "price": "0.63", "liquidity": "MAKER"},
        META,
        **TOKENS,
    )
    assert violation is None


def test_a_float_amount_is_refused() -> None:
    """A float has already lost exactness before the normalizer sees it."""
    with pytest.raises(ExecutionError, match="decimal string"):
        normalize_trade({"asset_id": UP_TOKEN, "size": 5.0, "price": "0.63"}, META, **TOKENS)
    with pytest.raises(ExecutionError, match="decimal string"):
        normalize_trade({"asset_id": UP_TOKEN, "size": "5", "price": 0.63}, META, **TOKENS)


def test_a_trade_without_size_or_price_is_refused() -> None:
    with pytest.raises(ExecutionError, match="no size or price"):
        normalize_trade({"asset_id": UP_TOKEN}, META, **TOKENS)


def test_normalization_puts_no_credential_into_a_p2_event() -> None:
    event = normalize_order_update(
        {
            "client_order_id": "c",
            "asset_id": UP_TOKEN,
            "status": "LIVE",
            "api_key": "should-not-propagate",
        },
        META,
        **TOKENS,
    )
    assert "should-not-propagate" not in repr(event)
