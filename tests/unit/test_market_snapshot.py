"""The Plane 3 snapshot: immutable, deterministic, and leaking no mutable reference."""

from __future__ import annotations

import dataclasses
from enum import Enum

import pytest

from maker5m.accounting import LedgerState, RebateMode
from maker5m.domain import Outcome
from maker5m.market import (
    HealthComponent,
    HealthStatus,
    MarketSnapshot,
    Phase,
    reduce_events,
    snapshot,
)
from maker5m.numeric import money_from_whole, parse_price, share_from_whole
from tests.unit.builders import (
    T0,
    at,
    book,
    health,
    initial_state,
    order_state,
    own_fill,
    spot,
)


def populated() -> MarketSnapshot:
    state = reduce_events(
        initial_state(),
        [
            book(0, 10),
            spot(1, 11),
            own_fill(2, 12, outcome=Outcome.UP, shares="120", cost="72"),
            own_fill(3, 13, outcome=Outcome.DOWN, shares="100", cost="50"),
            order_state(4, 14, client_order_id="c2"),
            order_state(5, 15, client_order_id="c1"),
            health(6, 16, component=HealthComponent.CLOB_BOOK),
        ],
    )
    return snapshot(state)


def test_snapshot_exposes_lifecycle_and_identity() -> None:
    snap = populated()
    assert snap.market_id == "btc-updown-5m-0001"
    assert snap.phase is Phase.QUOTE
    assert snap.t0 == T0
    assert snap.market_end > snap.t0
    assert snap.last_event_timestamp == at(16)
    assert snap.last_ingress_ordinal == 6
    assert snap.phase_config_version == "canonical-v1"


def test_snapshot_exposes_observed_market_data() -> None:
    snap = populated()
    assert snap.up_bid is not None
    assert snap.up_bid.price == parse_price("0.62")
    assert snap.down_ask is not None
    assert snap.book_timestamp == at(10)
    assert snap.book_sequence == 0
    assert snap.spot is not None
    assert snap.spot_timestamp == at(11)
    assert snap.strike is not None


def test_snapshot_embeds_the_ledger_rather_than_precomputing_a_pnl() -> None:
    """A pre-computed PnL would have to pick a rebate mode silently; the ledger does not."""
    snap = populated()
    assert isinstance(snap.ledger, LedgerState)
    assert snap.ledger.total_cost == money_from_whole(122)
    assert snap.ledger.net_inventory == share_from_whole(20)
    assert snap.ledger.pnl_if_up(RebateMode.WITHOUT_REBATE) == money_from_whole(-2)


def test_snapshot_counts_applied_fills() -> None:
    assert populated().fill_count == 2


def test_snapshot_health_is_carried() -> None:
    assert populated().health.clob_book is HealthStatus.HEALTHY


def test_orders_are_a_deterministically_sorted_tuple() -> None:
    """Insertion order must not leak into the snapshot, or replay comparison breaks."""
    snap = populated()
    assert isinstance(snap.orders, tuple)
    assert [o.client_order_id for o in snap.orders] == ["c1", "c2"]


def test_snapshot_is_immutable() -> None:
    snap = populated()
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.market_id = "other"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.ledger.n_up = share_from_whole(1)  # type: ignore[misc]


def test_snapshot_holds_no_reference_that_can_mutate_authoritative_state() -> None:
    """Every non-scalar field is a frozen value object, proven by trying to mutate it."""
    snap = populated()
    checked = 0
    for f in dataclasses.fields(MarketSnapshot):
        value = getattr(snap, f.name)
        if value is None or isinstance(value, int | str | tuple | Enum):
            continue
        assert dataclasses.is_dataclass(value) and not isinstance(value, type), (
            f"snapshot field {f.name} is a {type(value).__name__}, which may be mutable"
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, dataclasses.fields(value)[0].name, None)
        checked += 1
    assert checked >= 3


def test_snapshotting_the_same_state_twice_is_identical() -> None:
    state = reduce_events(initial_state(), [book(0, 10), spot(1, 11), own_fill(2, 12)])
    assert snapshot(state) == snapshot(state)


def test_empty_state_snapshots_cleanly() -> None:
    snap = snapshot(initial_state())
    assert snap.phase is Phase.PREARM
    assert snap.up_bid is None
    assert snap.spot is None
    assert snap.book_timestamp is None
    assert snap.orders == ()
    assert snap.fill_count == 0
    assert snap.resolution is None
