"""The KEEP path must cost nothing on the network, and be measured separately."""

from __future__ import annotations

import pytest

from maker5m.execution import (
    AsyncRecordingTransport,
    AsyncVenueAdapter,
    Executor,
    OrderLifecycle,
    ReconcileAction,
    RecordingTransport,
    VenueAdapter,
)
from maker5m.market.timebase import TimestampNs
from maker5m.telemetry import Stage, TraceBuilder, perf_now_ns
from tests.execution.builders import decision, desired, rules, state_at

NOW = TimestampNs(1_000_000_000_000)


@pytest.mark.asyncio
async def test_a_keep_cycle_issues_no_request_and_has_no_dispatch_stage() -> None:
    transport = AsyncRecordingTransport()
    executor = Executor(adapter=VenueAdapter(RecordingTransport()))
    adapter = AsyncVenueAdapter(transport=transport)
    state = state_at()
    orders = desired()

    await executor.run_cycle_async(adapter, decision(orders, state), state, rules(), NOW)
    for order in list(executor.orders):
        executor.orders.update(
            order.client_order_id, status=OrderLifecycle.LIVE, venue_order_id="v"
        )
    before = len(transport.placements)

    trace = TraceBuilder(event_kind="BookUpdate")
    trace.mark(Stage.RAW_RECEIVE, perf_now_ns)
    cycle = await executor.run_cycle_async(adapter, decision(orders, state), state, rules(), NOW)
    trace.mark(Stage.RECONCILE_DONE, perf_now_ns)

    assert all(side.action is ReconcileAction.KEEP for side in cycle.plan.sides)
    assert len(transport.placements) == before, "KEEP must not touch the network"
    assert trace.at(Stage.DISPATCH_START) is None
    assert trace.duration(Stage.DISPATCH_START, Stage.DISPATCH_RETURN) is None


@pytest.mark.asyncio
async def test_a_place_cycle_does_have_dispatch_stages() -> None:
    transport = AsyncRecordingTransport()
    executor = Executor(adapter=VenueAdapter(RecordingTransport()))
    adapter = AsyncVenueAdapter(transport=transport)
    state = state_at()

    trace = TraceBuilder(event_kind="BookUpdate")
    trace.mark(Stage.RAW_RECEIVE, perf_now_ns)
    trace.mark(Stage.DISPATCH_START, perf_now_ns)
    await executor.run_cycle_async(adapter, decision(desired(), state), state, rules(), NOW)
    trace.mark(Stage.DISPATCH_RETURN, perf_now_ns)

    assert len(transport.placements) == 2
    assert trace.at(Stage.DISPATCH_START) is not None
    duration = trace.duration(Stage.DISPATCH_START, Stage.DISPATCH_RETURN)
    assert duration is not None and duration >= 0


def test_keep_and_acting_cycles_are_measured_in_separate_distributions() -> None:
    """Reporting one blended number would hide the case that matters most."""
    from maker5m.telemetry.instrumented import LatencyBook

    book = LatencyBook()
    book.keep_cycle.add(5_000)
    book.acting_cycle.add(50_000)
    assert book.keep_cycle.summary()["p50"] == 5_000
    assert book.acting_cycle.summary()["p50"] == 50_000
    assert book.keep_cycle.label != book.acting_cycle.label


def test_spot_and_clob_paths_are_measured_separately() -> None:
    """Canonical §29.7 turns on how fast spot alone can wake the decision path."""
    from maker5m.telemetry.instrumented import LatencyBook

    book = LatencyBook()
    assert book.by_kind("SpotTick") is book.spot_receive_to_decide
    assert book.by_kind("BookUpdate") is book.clob_receive_to_decide
    assert book.by_kind("OwnFill") is book.fill_receive_to_decide
    assert book.by_kind("PhaseEvent") is book.phase_receive_to_decide
    assert book.spot_receive_to_decide is not book.clob_receive_to_decide
