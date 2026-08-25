"""Event contracts: immutability, validation, and the P1 types being reused."""

from __future__ import annotations

import dataclasses

import pytest

from maker5m.accounting import Fill
from maker5m.domain import Outcome
from maker5m.market import (
    BookLevel,
    BookUpdate,
    EventMeta,
    Liquidity,
    OrderStateEvent,
    OrderStatus,
    OwnFill,
    Phase,
    PhaseEvent,
    TimestampNs,
)
from maker5m.market.btc_price import BtcPrice
from maker5m.market.errors import MarketDefinitionError
from maker5m.numeric import DomainError, PriceUnits, ShareUnits, parse_price, parse_share
from tests.unit.builders import book, meta, own_fill, spot


def test_every_event_is_immutable() -> None:
    for event in (book(1), spot(2), own_fill(3), PhaseEvent(meta(4), Phase.QUOTE)):
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.meta = meta(99)  # type: ignore[misc]


def test_event_meta_validation() -> None:
    with pytest.raises(MarketDefinitionError):
        EventMeta("", "e1", 0, TimestampNs(1))
    with pytest.raises(MarketDefinitionError):
        EventMeta("m", "", 0, TimestampNs(1))
    with pytest.raises(MarketDefinitionError):
        EventMeta("m", "e1", -1, TimestampNs(1))
    with pytest.raises(DomainError):
        EventMeta("m", "e1", 0, TimestampNs(-1))


def test_ordinal_zero_is_valid() -> None:
    assert EventMeta("m", "e0", 0, TimestampNs(0)).ingress_ordinal == 0


def test_book_level_is_range_checked() -> None:
    with pytest.raises(DomainError):
        BookLevel(PriceUnits(1_000_001), parse_share("1"))
    with pytest.raises(DomainError):
        BookLevel(parse_price("0.5"), ShareUnits(-1))


def test_book_carries_both_sides_rather_than_deriving_down_from_up() -> None:
    """Canonical section 5.2's mirror identity is conditional; post-only safety is not."""
    update = book(1)
    assert update.best(Outcome.UP, "bid") == update.up_bid
    assert update.best(Outcome.UP, "ask") == update.up_ask
    assert update.best(Outcome.DOWN, "bid") == update.down_bid
    assert update.best(Outcome.DOWN, "ask") == update.down_ask


def test_book_best_rejects_an_unknown_side() -> None:
    with pytest.raises(DomainError):
        book(1).best(Outcome.UP, "middle")


def test_book_may_be_empty_on_a_side() -> None:
    empty = BookUpdate(meta(1), None, None, None, None)
    assert empty.best(Outcome.UP, "bid") is None


def test_own_fill_composes_the_authoritative_p1_fill() -> None:
    """No parallel fill model: the event carries the accepted accounting type unchanged."""
    event = own_fill(1)
    assert isinstance(event.fill, Fill)
    assert event.fill.shares == parse_share("13.63")
    assert event.liquidity is Liquidity.UNKNOWN


def test_own_fill_records_liquidity_so_a_taker_fill_is_detectable() -> None:
    """Invariant I07: a taker fill is an execution bug. P2 records it; P9 acts."""
    event = OwnFill(meta(1), own_fill(1).fill, liquidity=Liquidity.TAKER)
    assert event.liquidity is Liquidity.TAKER


def test_order_state_validation() -> None:
    with pytest.raises(MarketDefinitionError):
        OrderStateEvent(meta(1), "", OrderStatus.ACKNOWLEDGED)
    with pytest.raises(DomainError):
        OrderStateEvent(meta(1), "c", OrderStatus.ACKNOWLEDGED, price=PriceUnits(1_000_001))
    with pytest.raises(DomainError):
        OrderStateEvent(meta(1), "c", OrderStatus.ACKNOWLEDGED, remaining=ShareUnits(-1))


def test_order_status_vocabulary_includes_unknown() -> None:
    """An unmappable venue status must be representable, not guessed."""
    assert OrderStatus.UNKNOWN in set(OrderStatus)


def test_spot_uses_a_btc_price_not_a_probability() -> None:
    """PriceUnits is constrained to [0, 1]; a BTC price is not a probability."""
    tick = spot(1)
    assert isinstance(tick.price, BtcPrice)
    assert str(tick.price) == "64123.45"
