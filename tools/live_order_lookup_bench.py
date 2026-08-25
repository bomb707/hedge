"""O15 evidence: does ``LiveOrderTable.current()`` cost grow with retained history?

Measures the indexed lookup now in use against the original full-history scan, on the *same*
table, at several history depths. Reporting only the new numbers would show a fast lookup
without showing that the slope is gone, and the slope is the defect.

Read-only. No venue, no credential, no order.
"""

import json
import statistics
import sys
import time

from maker5m.domain import Outcome
from maker5m.execution.live_orders import LiveOrder, LiveOrderTable, OrderLifecycle
from maker5m.numeric.units import PriceUnits, ShareUnits

DEPTHS = (0, 50, 200, 500, 1_000, 2_000, 5_000, 10_000)
CALLS = 200
ROUNDS = 9
PRICE = PriceUnits(500_000)
SIZE = ShareUnits(15_000_000)


def scan_current(table: LiveOrderTable, outcome: Outcome) -> LiveOrder | None:
    """The original implementation, kept here purely as the measurement baseline."""
    occupying = sorted(
        (o for o in table.orders.values() if o.outcome is outcome and o.occupies_side),
        key=lambda o: o.client_order_id,
    )
    return occupying[0] if occupying else None


def build(retained: int) -> LiveOrderTable:
    """``retained`` terminal orders, plus exactly one live order on each side."""
    table = LiveOrderTable()
    for index in range(retained):
        client_order_id = f"dead-{index:08d}"
        outcome = Outcome.UP if index % 2 == 0 else Outcome.DOWN
        table.register_pending_place(
            client_order_id=client_order_id,
            outcome=outcome,
            price=PRICE,
            size=SIZE,
            ingress_ordinal=index,
        )
        table.update(client_order_id, status=OrderLifecycle.LIVE, venue_order_id=client_order_id)
        table.update(client_order_id, status=OrderLifecycle.CANCELLED)
    for outcome in Outcome:
        client_order_id = f"live-{outcome.value}"
        table.register_pending_place(
            client_order_id=client_order_id,
            outcome=outcome,
            price=PRICE,
            size=SIZE,
            ingress_ordinal=retained,
        )
        table.update(client_order_id, status=OrderLifecycle.LIVE, venue_order_id=client_order_id)
    return table


def time_ns_per_cycle(table: LiveOrderTable, scan: bool) -> int:
    """Best-of-rounds nanoseconds for one cycle's worth of lookups (both sides)."""
    best: int | None = None
    for _ in range(ROUNDS):
        start = time.perf_counter_ns()
        if scan:
            for _ in range(CALLS):
                scan_current(table, Outcome.UP)
                scan_current(table, Outcome.DOWN)
        else:
            for _ in range(CALLS):
                table.current(Outcome.UP)
                table.current(Outcome.DOWN)
        elapsed = (time.perf_counter_ns() - start) // CALLS
        best = elapsed if best is None else min(best, elapsed)
    assert best is not None
    return best


def slope_ns_per_order(points: list[tuple[int, int]]) -> float:
    """Least-squares slope: nanoseconds added per retained terminal order."""
    xs = [float(x) for x, _ in points]
    ys = [float(y) for _, y in points]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denominator


def main() -> None:
    rows = []
    for retained in DEPTHS:
        table = build(retained)
        before = time_ns_per_cycle(table, scan=True)
        after = time_ns_per_cycle(table, scan=False)
        # The two implementations must agree, or the benchmark is comparing different things.
        for outcome in Outcome:
            assert scan_current(table, outcome) == table.current(outcome), retained
        rows.append({"retained_terminal_orders": retained, "before_ns": before, "after_ns": after})

    report = {
        "calls_per_sample": CALLS,
        "rounds": ROUNDS,
        "note": "ns per cycle = two current() calls, one per outcome; best of rounds",
        "rows": rows,
        "slope_ns_per_retained_order": {
            "before": round(
                slope_ns_per_order([(r["retained_terminal_orders"], r["before_ns"]) for r in rows]),
                4,
            ),
            "after": round(
                slope_ns_per_order([(r["retained_terminal_orders"], r["after_ns"]) for r in rows]),
                4,
            ),
        },
    }
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    print()


if __name__ == "__main__":
    main()
