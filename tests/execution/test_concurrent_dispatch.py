"""Proof that two independent outcome requests actually overlap on the transport.

The P7 suite contained a test named "...may dispatch concurrently" that only proved the
*plan* was independent of evaluation order. It said nothing about whether the two network
calls overlap, and the implementation at that point serialised them. That test has been
replaced by the barrier tests here.

The technique: each transport call announces that it has entered, then waits for the other
side to enter. If dispatch is sequential the second call is never started, the first waits
forever, and the test times out. If dispatch is concurrent both enter and the barrier
releases. No wall-clock timing is involved, so the proof is not flaky.
"""

from __future__ import annotations

import asyncio

import pytest

from maker5m.domain import Outcome
from maker5m.execution import (
    AsyncRecordingTransport,
    AsyncVenueAdapter,
    Executor,
    OrderLifecycle,
    ReconcileAction,
    TokenBucket,
)
from maker5m.market.timebase import TimestampNs
from tests.execution.builders import decision, desired, rules, state_at

NOW = TimestampNs(1_000_000_000_000)
TIMEOUT = 5.0
"""Generous: a sequential implementation deadlocks, it does not merely run slowly."""


class Barrier:
    """Releases only when both outcomes have entered the transport."""

    def __init__(self, expected: int = 2) -> None:
        self.entered: list[str] = []
        self._expected = expected
        self._all_in = asyncio.Event()

    async def __call__(self, kind: str, key: str) -> None:
        self.entered.append(f"{kind}:{key}")
        if len(self.entered) >= self._expected:
            self._all_in.set()
        await self._all_in.wait()


def build() -> tuple[Executor, AsyncRecordingTransport, AsyncVenueAdapter]:
    transport = AsyncRecordingTransport()
    from maker5m.execution import RecordingTransport, VenueAdapter

    executor = Executor(adapter=VenueAdapter(transport=RecordingTransport()))
    return executor, transport, AsyncVenueAdapter(transport=transport)


# -- the actual overlap proof ------------------------------------------------


@pytest.mark.asyncio
async def test_two_places_overlap_on_the_transport() -> None:
    """Both requests are inside the transport at the same moment."""
    executor, transport, adapter = build()
    transport.gate = Barrier()
    state = state_at()

    await asyncio.wait_for(
        executor.run_cycle_async(adapter, decision(desired(), state), state, rules(), NOW),
        timeout=TIMEOUT,
    )
    assert len(transport.placements) == 2
    assert len(transport.gate.entered) == 2, "both sides must have entered concurrently"


@pytest.mark.asyncio
async def test_two_cancels_overlap_on_the_transport() -> None:
    executor, transport, adapter = build()
    state = state_at()

    # Establish one live order per side, then withdraw both.
    await executor.run_cycle_async(adapter, decision(desired(), state), state, rules(), NOW)
    for order in list(executor.orders):
        executor.orders.update(
            order.client_order_id,
            status=OrderLifecycle.LIVE,
            venue_order_id=f"v-{order.client_order_id}",
        )
    transport.gate = Barrier()

    cycle = await asyncio.wait_for(
        executor.run_cycle_async(
            adapter, decision(desired(None, None, None, None), state), state, rules(), NOW
        ),
        timeout=TIMEOUT,
    )
    assert cycle.plan.up.action is ReconcileAction.CANCEL
    assert cycle.plan.down.action is ReconcileAction.CANCEL
    assert len(transport.cancels) == 2
    assert len(transport.gate.entered) == 2


@pytest.mark.asyncio
async def test_a_cancel_and_a_place_on_opposite_sides_overlap() -> None:
    """Mixed actions across independent outcomes are still concurrent."""
    executor, transport, adapter = build()
    state = state_at()

    # One live UP order only.
    await executor.run_cycle_async(
        adapter, decision(desired(down_price=None, down_size=None), state), state, rules(), NOW
    )
    for order in list(executor.orders):
        executor.orders.update(
            order.client_order_id, status=OrderLifecycle.LIVE, venue_order_id="v-up"
        )
    transport.gate = Barrier()

    # Withdraw UP while placing DOWN.
    cycle = await asyncio.wait_for(
        executor.run_cycle_async(
            adapter,
            decision(desired(up_price=None, up_size=None), state),
            state,
            rules(),
            NOW,
        ),
        timeout=TIMEOUT,
    )
    assert cycle.plan.up.action is ReconcileAction.CANCEL
    assert cycle.plan.down.action is ReconcileAction.PLACE
    assert len(transport.gate.entered) == 2
    assert {e.split(":")[0] for e in transport.gate.entered} == {"cancel", "place"}


@pytest.mark.asyncio
async def test_a_blocked_side_dispatches_nothing() -> None:
    """Only the legal request is sent, and the barrier is sized accordingly."""
    executor, transport, adapter = build()
    transport.gate = Barrier(expected=1)
    state = state_at()

    cycle = await asyncio.wait_for(
        executor.run_cycle_async(
            adapter, decision(desired(up_price="0.65"), state), state, rules(), NOW
        ),
        timeout=TIMEOUT,
    )
    assert cycle.plan.up.action is ReconcileAction.BLOCKED
    assert cycle.plan.down.action is ReconcileAction.PLACE
    assert len(transport.placements) == 1
    assert transport.gate.entered == ["place:token-down"]


# -- reservation happens before the await -------------------------------------


@pytest.mark.asyncio
async def test_state_is_registered_before_the_network_call_starts() -> None:
    """A concurrent cycle must never see a gap between deciding and recording."""
    executor, transport, adapter = build()
    state = state_at()
    observed: list[int] = []

    async def observe(kind: str, key: str) -> None:
        # Inside the transport: the order must already be PENDING_PLACE.
        observed.append(executor.orders.open_count)
        await asyncio.sleep(0)

    transport.gate = observe
    await executor.run_cycle_async(adapter, decision(desired(), state), state, rules(), NOW)
    assert observed == [2, 2], "both orders were registered before either request started"
    assert all(o.status is OrderLifecycle.PENDING_PLACE for o in executor.orders)


@pytest.mark.asyncio
async def test_a_slow_acknowledgement_cannot_produce_a_duplicate() -> None:
    """The classic race: the first request is still in flight when the next cycle runs."""
    executor, transport, adapter = build()
    state = state_at()
    release = asyncio.Event()

    async def hold(kind: str, key: str) -> None:
        await release.wait()

    transport.gate = hold
    first = asyncio.create_task(
        executor.run_cycle_async(adapter, decision(desired(), state), state, rules(), NOW)
    )
    await asyncio.sleep(0)  # let the first cycle reserve and enter the transport

    # A second cycle runs while the first is still awaiting the venue.
    second = await executor.run_cycle_async(
        adapter, decision(desired(), state), state, rules(), NOW
    )
    assert second.plan.up.action is ReconcileAction.WAIT
    assert second.plan.down.action is ReconcileAction.WAIT

    release.set()
    await first
    assert len(transport.placements) == 2, "no duplicate placement during the slow ack"


@pytest.mark.asyncio
async def test_rate_capacity_is_reserved_before_dispatch() -> None:
    executor, transport, adapter = build()
    executor.bucket = TokenBucket(rate_per_second=8, burst=1, cancel_reserve=0)
    state = state_at()

    await executor.run_cycle_async(adapter, decision(desired(), state), state, rules(), NOW)
    assert len(transport.placements) == 1, "the deferred side issued no request"
    assert executor.orders.open_count == 1, "and reserved no order state either"


@pytest.mark.asyncio
async def test_only_one_client_order_id_is_allocated_per_intended_placement() -> None:
    executor, _transport, adapter = build()
    state = state_at()
    await executor.run_cycle_async(adapter, decision(desired(), state), state, rules(), NOW)
    ids = [order.client_order_id for order in executor.orders]
    assert len(ids) == len(set(ids)) == 2


# -- ordering independence ------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_order_does_not_affect_recorded_state() -> None:
    """Whichever coroutine returns first, the cycle's records follow the plan's order."""
    executor, transport, adapter = build()
    state = state_at()

    async def reverse_order(kind: str, key: str) -> None:
        # Make the DOWN request finish first.
        if key == "token-up":
            await asyncio.sleep(0.01)

    transport.gate = reverse_order
    cycle = await executor.run_cycle_async(adapter, decision(desired(), state), state, rules(), NOW)
    assert [record.outcome for record in cycle.records] == [Outcome.UP, Outcome.DOWN]


@pytest.mark.asyncio
async def test_the_reserve_step_contains_no_await() -> None:
    """Structural: a suspension point inside reserve() would reopen the duplicate race."""
    import ast
    import inspect

    tree = ast.parse(inspect.cleandoc(inspect.getsource(Executor.reserve)))
    for node in ast.walk(tree):
        assert not isinstance(node, ast.Await), "reserve() must not await"
    assert not inspect.iscoroutinefunction(Executor.reserve)


def test_the_synchronous_cycle_is_labelled_test_support() -> None:
    """So production wiring cannot accidentally select the sequential path."""
    doc = Executor.run_cycle.__doc__ or ""
    assert "Test support" in doc
    assert "not production" in doc.lower()
    assert "run_cycle_async" in doc


def test_the_async_transport_protocol_is_genuinely_async() -> None:
    import inspect

    from maker5m.execution.adapter import AsyncRecordingTransport as T

    for name in ("place", "cancel", "prewarm"):
        assert inspect.iscoroutinefunction(getattr(T, name)), f"{name} is not a coroutine"


def test_the_official_sdk_exposes_a_real_async_write_api() -> None:
    """Recorded so a future SDK change that removes it is caught here, not in production."""
    import inspect

    from polymarket.clients.async_secure import AsyncSecureClient

    for name in ("create_limit_order", "post_order", "cancel_order"):
        assert inspect.iscoroutinefunction(getattr(AsyncSecureClient, name))


def test_live_trading_is_still_disabled() -> None:
    import maker5m

    assert maker5m.LIVE_TRADING_ENABLED is False
