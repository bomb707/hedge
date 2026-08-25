"""O15: the occupancy index must make ``current()`` independent of retained history.

The first P8 measurement found ``current()`` filtering and sorting every order the table had
ever held, twice per cycle — about 258 ns per retained order, or ~265 µs by the end of a market
that placed 1,049 orders. History retention is *required* for idempotency, so the fix indexes
the occupying orders rather than pruning the history.

These tests hold both halves of that: the index is correct and deterministic, and the history
it sits alongside is still complete and still idempotent.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

from maker5m.domain import Outcome
from maker5m.execution.errors import OrderIdentityError
from maker5m.execution.live_orders import LiveOrder, LiveOrderTable, OrderLifecycle
from maker5m.numeric import PriceUnits, ShareUnits, parse_price, parse_share

if TYPE_CHECKING:
    from _collections_abc import dict_items, dict_values

PRICE = parse_price("0.63")
SIZE = parse_share("15")

TERMINAL = (OrderLifecycle.FILLED, OrderLifecycle.CANCELLED, OrderLifecycle.REJECTED)
OCCUPYING = (
    OrderLifecycle.PENDING_PLACE,
    OrderLifecycle.LIVE,
    OrderLifecycle.PARTIALLY_FILLED,
    OrderLifecycle.PENDING_CANCEL,
    OrderLifecycle.UNKNOWN,
)


def place(
    table: LiveOrderTable,
    client_order_id: str,
    outcome: Outcome = Outcome.UP,
    *,
    price: PriceUnits = PRICE,
    size: ShareUnits = SIZE,
) -> None:
    table.register_pending_place(
        client_order_id=client_order_id,
        outcome=outcome,
        price=price,
        size=size,
        ingress_ordinal=0,
    )


def with_history(retained: int) -> LiveOrderTable:
    """``retained`` terminal orders plus exactly one live order per outcome."""
    table = LiveOrderTable()
    for index in range(retained):
        client_order_id = f"dead-{index:08d}"
        outcome = Outcome.UP if index % 2 == 0 else Outcome.DOWN
        place(table, client_order_id, outcome)
        table.update(client_order_id, status=OrderLifecycle.LIVE, venue_order_id=client_order_id)
        table.update(client_order_id, status=OrderLifecycle.CANCELLED)
    for outcome in Outcome:
        client_order_id = f"live-{outcome.value}"
        place(table, client_order_id, outcome)
        table.update(client_order_id, status=OrderLifecycle.LIVE, venue_order_id=client_order_id)
    return table


# -- correctness of the index ------------------------------------------------------------


@pytest.mark.parametrize("status", OCCUPYING)
def test_every_occupying_status_is_indexed(status: OrderLifecycle) -> None:
    table = LiveOrderTable()
    place(table, "a")
    table.update("a", status=status)
    assert table.current(Outcome.UP) is not None
    assert table.open_count == 1


@pytest.mark.parametrize("status", TERMINAL)
def test_every_terminal_status_leaves_the_index(status: OrderLifecycle) -> None:
    table = LiveOrderTable()
    place(table, "a")
    table.update("a", status=status)
    assert table.current(Outcome.UP) is None
    assert table.open_count == 0
    assert table.get("a") is not None, "history must still hold it"


def test_an_order_that_becomes_occupying_again_is_reindexed() -> None:
    """Membership is recomputed from status, so an unusual transition cannot desynchronise it."""
    table = LiveOrderTable()
    place(table, "a")
    table.update("a", status=OrderLifecycle.CANCELLED)
    assert table.current(Outcome.UP) is None
    table.update("a", status=OrderLifecycle.UNKNOWN)
    current = table.current(Outcome.UP)
    assert current is not None and current.client_order_id == "a"


def test_the_two_sides_are_indexed_separately() -> None:
    table = LiveOrderTable()
    place(table, "u", Outcome.UP)
    place(table, "d", Outcome.DOWN)
    up, down = table.current(Outcome.UP), table.current(Outcome.DOWN)
    assert up is not None and up.client_order_id == "u"
    assert down is not None and down.client_order_id == "d"


def test_a_replacement_race_returns_the_earliest_deterministically() -> None:
    """Two orders occupying one side: the earliest by client order id must win, every time."""
    table = LiveOrderTable()
    for client_order_id in ("c-003", "c-001", "c-002"):
        place(table, client_order_id)
        table.update(client_order_id, status=OrderLifecycle.PENDING_CANCEL)
    for _ in range(10):
        current = table.current(Outcome.UP)
        assert current is not None and current.client_order_id == "c-001"
    assert [o.client_order_id for o in table.occupying(Outcome.UP)] == ["c-001", "c-002", "c-003"]


def test_the_index_agrees_with_a_full_scan_after_a_long_mixed_history() -> None:
    """The property the optimisation must preserve, checked against the naive definition."""
    table = LiveOrderTable()
    statuses = OCCUPYING + TERMINAL
    for index in range(400):
        client_order_id = f"o-{index:04d}"
        outcome = Outcome.UP if index % 3 == 0 else Outcome.DOWN
        place(table, client_order_id, outcome)
        table.update(client_order_id, status=statuses[index % len(statuses)])
        for outcome in Outcome:
            scanned = sorted(
                (o for o in table.orders.values() if o.outcome is outcome and o.occupies_side),
                key=lambda o: o.client_order_id,
            )
            assert table.occupying(outcome) == tuple(scanned)
            assert table.current(outcome) == (scanned[0] if scanned else None)


# -- history and idempotency survive -------------------------------------------------------


def test_ten_thousand_terminal_orders_are_all_still_addressable() -> None:
    table = with_history(10_000)
    assert len(table.orders) == 10_002
    for index in (0, 1, 4_999, 9_999):
        order = table.get(f"dead-{index:08d}")
        assert order is not None
        assert order.status is OrderLifecycle.CANCELLED


def test_a_reused_client_order_id_is_still_refused_after_ten_thousand_orders() -> None:
    table = with_history(10_000)
    with pytest.raises(OrderIdentityError):
        place(table, "dead-00000001")


def test_a_duplicated_update_is_still_applied_once() -> None:
    table = with_history(1_000)
    live = table.current(Outcome.UP)
    assert live is not None
    for _ in range(5):
        table.update(
            live.client_order_id,
            status=OrderLifecycle.PARTIALLY_FILLED,
            remaining_size=parse_share("10"),
            update_id="fill-1",
        )
    after = table.get(live.client_order_id)
    assert after is not None
    assert after.remaining_size == parse_share("10")
    assert after.status is OrderLifecycle.PARTIALLY_FILLED
    assert table.current(Outcome.UP) == after


def test_a_late_duplicated_cancel_does_not_resurrect_a_slot() -> None:
    table = with_history(100)
    live = table.current(Outcome.UP)
    assert live is not None
    table.update(live.client_order_id, status=OrderLifecycle.CANCELLED, update_id="cancel-1")
    assert table.current(Outcome.UP) is None
    table.update(live.client_order_id, status=OrderLifecycle.CANCELLED, update_id="cancel-1")
    assert table.current(Outcome.UP) is None


# -- the cost itself -----------------------------------------------------------------------


def test_current_does_not_iterate_the_whole_history() -> None:
    """Structural: ``current()`` must not touch ``orders`` beyond the ids it was handed.

    A timing assertion alone would be flaky on a shared machine, so this counts accesses.
    """
    table = with_history(5_000)
    seen: list[str] = []

    class CountingOrders(dict[str, LiveOrder]):
        def values(self) -> dict_values[str, LiveOrder]:
            seen.append("values")
            return super().values()

        def items(self) -> dict_items[str, LiveOrder]:
            seen.append("items")
            return super().items()

        def __iter__(self) -> Iterator[str]:
            seen.append("iter")
            return super().__iter__()

    table.orders = CountingOrders(table.orders)
    for _ in range(50):
        table.current(Outcome.UP)
        table.current(Outcome.DOWN)
    assert seen == [], f"current() walked the history: {set(seen)}"


def test_lookup_cost_does_not_grow_with_retained_history() -> None:
    """The measured slope, not just the structure. Generous bound; the real ratio is ~5000x."""

    def cost_ns(table: LiveOrderTable) -> int:
        best = None
        for _ in range(7):
            start = time.perf_counter_ns()
            for _ in range(200):
                table.current(Outcome.UP)
                table.current(Outcome.DOWN)
            elapsed = time.perf_counter_ns() - start
            best = elapsed if best is None else min(best, elapsed)
        assert best is not None
        return best

    small = cost_ns(with_history(100))
    large = cost_ns(with_history(10_000))
    assert large < small * 4, (
        f"100 orders: {small} ns, 10,000 orders: {large} ns - cost still tracks history size"
    )
