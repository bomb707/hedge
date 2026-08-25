"""Deterministic transitions: ordering, idempotency, validation, and ledger integration."""

from __future__ import annotations

import pytest

from maker5m.accounting import Fill, RebateMode
from maker5m.domain import Outcome
from maker5m.market import (
    BookUpdate,
    EventMeta,
    HealthComponent,
    HealthStatus,
    OrderStatus,
    Phase,
    PhaseEvent,
    TimestampNs,
    reduce_event,
    reduce_events,
)
from maker5m.market.errors import (
    DuplicateEventError,
    EventOrderError,
    InvalidPhaseTransitionError,
    WrongMarketError,
)
from maker5m.numeric import (
    DomainError,
    money_from_whole,
    parse_money,
    parse_price,
    parse_share,
    share_from_whole,
)
from tests.unit.builders import (
    MARKET_ID,
    at,
    book,
    health,
    initial_state,
    meta,
    order_state,
    own_fill,
    spot,
)

WITHOUT = RebateMode.WITHOUT_REBATE


# -- basic application --------------------------------------------------------------------


def test_book_update_is_stored_and_advances_the_clock() -> None:
    state = reduce_event(initial_state(), book(0, 10))
    assert state.book is not None
    assert state.last_event_timestamp == at(10)
    assert state.last_ingress_ordinal == 0
    assert state.phase is Phase.QUOTE


def test_spot_tick_is_stored_and_can_advance_state_alone() -> None:
    """Invariant I11: external spot must be able to drive the core without a CLOB update."""
    state = reduce_event(initial_state(), spot(0, 5))
    assert state.spot is not None
    assert state.book is None
    assert state.last_event_timestamp == at(5)


def test_health_event_updates_only_its_component() -> None:
    state = reduce_event(initial_state(), health(0, component=HealthComponent.SPOT_FEED))
    assert state.health.spot_feed is HealthStatus.HEALTHY
    assert state.health.clob_book is HealthStatus.UNKNOWN


def test_order_state_event_records_the_latest_status() -> None:
    state = initial_state()
    state = reduce_event(state, order_state(0, 10, client_order_id="c1"))
    assert state.orders["c1"].status is OrderStatus.ACKNOWLEDGED
    state = reduce_event(
        state, order_state(1, 11, client_order_id="c1", status=OrderStatus.CANCELLED)
    )
    assert state.orders["c1"].status is OrderStatus.CANCELLED
    assert state.orders["c1"].last_ingress_ordinal == 1
    assert len(state.orders) == 1


def test_order_records_accumulate_per_client_order_id() -> None:
    state = initial_state()
    state = reduce_event(state, order_state(0, 10, client_order_id="c1"))
    state = reduce_event(state, order_state(1, 11, client_order_id="c2"))
    assert set(state.orders) == {"c1", "c2"}


# -- ledger integration -------------------------------------------------------------------


def test_own_fill_updates_the_p1_ledger() -> None:
    state = reduce_event(initial_state(), own_fill(0, 10, shares="13.63", cost="8.5869"))
    assert state.ledger.n_up == parse_share("13.63")
    assert state.ledger.cost_up == parse_money("8.5869")
    assert state.net_inventory == parse_share("13.63")


def test_mandatory_accounting_example_flows_through_the_event_stream() -> None:
    """The P1 regression, driven end to end by events.

    120 UP at $0.60 and 100 DOWN at $0.50: the bot ends holding 20 more shares of the
    outcome that wins and still loses $2.
    """
    state = initial_state()
    state = reduce_event(state, own_fill(0, 10, outcome=Outcome.UP, shares="120", cost="72"))
    state = reduce_event(state, own_fill(1, 20, outcome=Outcome.DOWN, shares="100", cost="50"))
    assert state.ledger.total_cost == money_from_whole(122)
    assert state.net_inventory == share_from_whole(20)
    assert state.ledger.pnl_if_up(WITHOUT) == money_from_whole(-2)
    assert state.ledger.pnl_if_down(WITHOUT) == money_from_whole(-22)


def test_interleaved_fills_accumulate_exactly_once_each() -> None:
    state = initial_state()
    for i in range(10):
        outcome = Outcome.UP if i % 2 == 0 else Outcome.DOWN
        state = reduce_event(state, own_fill(i, 10 + i, outcome=outcome, shares="5", cost="3"))
    assert state.ledger.n_up == share_from_whole(25)
    assert state.ledger.n_down == share_from_whole(25)
    assert len(state.applied_fill_ids) == 10


def test_duplicate_fill_is_rejected_rather_than_double_accounted() -> None:
    """Double-accounting a fill would silently corrupt every downstream figure (I01)."""
    state = reduce_event(initial_state(), own_fill(0, 10, event_id="fill-a"))
    duplicate = own_fill(1, 11, event_id="fill-a")
    with pytest.raises(DuplicateEventError):
        reduce_event(state, duplicate)
    assert state.ledger.n_up == parse_share("13.63")


def test_two_identical_fills_with_distinct_ids_both_apply() -> None:
    """Identity is the event id, not the payload: a genuine repeat fill is real volume."""
    state = initial_state()
    state = reduce_event(state, own_fill(0, 10, event_id="fill-a"))
    state = reduce_event(state, own_fill(1, 11, event_id="fill-b"))
    assert state.ledger.n_up == parse_share("27.26")


def test_a_rejected_duplicate_leaves_the_prior_state_untouched() -> None:
    state = reduce_event(initial_state(), own_fill(0, 10, event_id="fill-a"))
    before = state
    with pytest.raises(DuplicateEventError):
        reduce_event(state, own_fill(1, 11, event_id="fill-a"))
    assert state == before


# -- ordering -----------------------------------------------------------------------------


def test_ordinal_must_strictly_increase() -> None:
    state = reduce_event(initial_state(), book(5, 10))
    with pytest.raises(EventOrderError):
        reduce_event(state, book(5, 11))
    with pytest.raises(EventOrderError):
        reduce_event(state, book(4, 11))


def test_equal_timestamps_are_ordered_by_ordinal_and_remain_deterministic() -> None:
    """Two feeds can tie on timestamp; the ingress ordinal is the total order."""
    state = initial_state()
    first = book(0, 10, bid="0.61")
    second = book(1, 10, bid="0.62")
    forward = reduce_events(state, [first, second])
    assert forward.book is not None
    assert forward.book.up_bid is not None
    assert forward.book.up_bid.price == parse_price("0.62")
    # The reverse ordinal order is not merely different - it is rejected.
    with pytest.raises(EventOrderError):
        reduce_events(state, [second, first])


def test_timestamp_must_not_decrease() -> None:
    state = reduce_event(initial_state(), book(0, 30))
    with pytest.raises(EventOrderError):
        reduce_event(state, book(1, 29))


def test_equal_timestamp_with_increasing_ordinal_is_accepted() -> None:
    state = reduce_event(initial_state(), book(0, 30))
    state = reduce_event(state, book(1, 30))
    assert state.last_ingress_ordinal == 1


def test_ordinal_zero_is_accepted_on_a_fresh_state() -> None:
    assert reduce_event(initial_state(), book(0, 10)).last_ingress_ordinal == 0


# -- market association --------------------------------------------------------------------


def test_event_for_another_market_is_rejected() -> None:
    foreign = BookUpdate(
        meta=EventMeta("other-market", "e1", 0, at(10)),
        up_bid=None,
        up_ask=None,
        down_bid=None,
        down_ask=None,
    )
    with pytest.raises(WrongMarketError):
        reduce_event(initial_state(), foreign)


# -- phase events --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset_s", "phase"),
    [
        (0, Phase.PREARM),
        (3, Phase.QUOTE),
        (240, Phase.ENDGAME),
        (280, Phase.SETTLING),
        (300, Phase.DONE),
    ],
)
def test_phase_event_matching_its_timestamp_is_accepted(offset_s: int, phase: Phase) -> None:
    state = reduce_event(initial_state(), PhaseEvent(meta(0, offset_s), phase))
    assert state.phase is phase


@pytest.mark.parametrize(
    ("offset_s", "claimed"),
    [
        (10, Phase.ENDGAME),  # QUOTE claimed as ENDGAME
        (250, Phase.QUOTE),  # ENDGAME claimed as QUOTE
        (299, Phase.DONE),  # SETTLING claimed as DONE
        (0, Phase.QUOTE),  # PREARM claimed as QUOTE
    ],
)
def test_phase_event_disagreeing_with_its_timestamp_is_rejected(
    offset_s: int, claimed: Phase
) -> None:
    """A phase event can never move the market to a phase the clock does not imply."""
    with pytest.raises(InvalidPhaseTransitionError):
        reduce_event(initial_state(), PhaseEvent(meta(0, offset_s), claimed))


def test_phase_cannot_be_driven_backwards_because_timestamps_cannot_decrease() -> None:
    state = reduce_event(initial_state(), PhaseEvent(meta(0, 250), Phase.ENDGAME))
    with pytest.raises(EventOrderError):
        reduce_event(state, PhaseEvent(meta(1, 100), Phase.QUOTE))


def test_phase_advances_without_any_phase_event() -> None:
    """The phase is derived, so a quiet market still ages correctly."""
    state = reduce_event(initial_state(), book(0, 10))
    assert state.phase is Phase.QUOTE
    state = reduce_event(state, book(1, 250))
    assert state.phase is Phase.ENDGAME


# -- immutability of prior states ----------------------------------------------------------


def test_prior_state_is_unaffected_by_a_later_transition() -> None:
    first = reduce_event(initial_state(), own_fill(0, 10, shares="10", cost="6"))
    second = reduce_event(first, own_fill(1, 11, shares="10", cost="6"))
    assert first.ledger.n_up == share_from_whole(10)
    assert second.ledger.n_up == share_from_whole(20)
    assert first.applied_fill_ids != second.applied_fill_ids
    assert first is not second


def test_order_map_of_a_prior_state_is_not_mutated() -> None:
    first = reduce_event(initial_state(), order_state(0, 10, client_order_id="c1"))
    second = reduce_event(first, order_state(1, 11, client_order_id="c2"))
    assert set(first.orders) == {"c1"}
    assert set(second.orders) == {"c1", "c2"}


def test_reduce_events_applies_nothing_when_a_later_event_is_rejected() -> None:
    """The fold raises; the caller still holds its original, untouched state."""
    state = initial_state()
    with pytest.raises(EventOrderError):
        reduce_events(state, [book(0, 10), book(0, 11)])
    assert state.last_ingress_ordinal == -1
    assert state.book is None


def test_fill_event_carries_the_authoritative_fill_unchanged() -> None:
    event = own_fill(0, 10)
    state = reduce_event(initial_state(), event)
    assert isinstance(event.fill, Fill)
    assert state.ledger.cost_up == event.fill.cost


def test_own_fill_of_both_outcomes_updates_net_inventory_sign() -> None:
    state = initial_state()
    state = reduce_event(state, own_fill(0, 10, outcome=Outcome.DOWN, shares="30", cost="15"))
    assert state.net_inventory == share_from_whole(-30)
    state = reduce_event(state, own_fill(1, 11, outcome=Outcome.UP, shares="45", cost="27"))
    assert state.net_inventory == share_from_whole(15)


def test_negative_timestamp_event_is_rejected_at_construction() -> None:
    with pytest.raises(DomainError):
        EventMeta(MARKET_ID, "e", 0, TimestampNs(-1))
