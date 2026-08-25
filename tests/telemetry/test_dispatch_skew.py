"""Dispatch-start skew between the two outcomes, on the corrected concurrent path.

Load-bearing after the P7 correction: a sequential implementation is immediately visible here
as a skew of roughly one full request duration, while a concurrent one shows near-zero skew.

Mock transport only. No order is sent and no venue round-trip is measured — real order RTT
remains unmeasured until P14.
"""

from __future__ import annotations

import asyncio
import statistics

import pytest

from maker5m.execution import (
    AsyncRecordingTransport,
    AsyncVenueAdapter,
    Executor,
    RecordingTransport,
    VenueAdapter,
)
from maker5m.market.timebase import TimestampNs
from maker5m.telemetry import Distribution, perf_now_ns, quantile
from tests.execution.builders import decision, desired, rules, state_at

NOW = TimestampNs(1_000_000_000_000)


class SkewRecorder:
    """Records the latency-clock reading at which each request entered the transport."""

    def __init__(self) -> None:
        self.starts: dict[str, int] = {}

    async def __call__(self, kind: str, key: str) -> None:
        self.starts[key] = perf_now_ns()
        await asyncio.sleep(0.002)  # a realistic request duration

    @property
    def skew_ns(self) -> int:
        readings = sorted(self.starts.values())
        return readings[-1] - readings[0]


async def one_two_sided_cycle() -> int:
    transport = AsyncRecordingTransport()
    recorder = SkewRecorder()
    transport.gate = recorder
    executor = Executor(adapter=VenueAdapter(RecordingTransport()))
    adapter = AsyncVenueAdapter(transport=transport)
    state = state_at()
    await executor.run_cycle_async(adapter, decision(desired(), state), state, rules(), NOW)
    assert len(recorder.starts) == 2
    return recorder.skew_ns


@pytest.mark.asyncio
async def test_dispatch_start_skew_is_far_below_one_request_duration() -> None:
    """Both requests start together, not one after the other.

    The request itself takes 2 ms in this double. A sequential implementation would show skew
    of about that; a concurrent one shows the cost of scheduling a second coroutine.
    """
    skew = await one_two_sided_cycle()
    assert skew < 1_000_000, f"dispatch-start skew {skew} ns suggests sequential dispatch"


@pytest.mark.asyncio
async def test_dispatch_start_skew_distribution_is_recorded() -> None:
    """The measurement P8 owes the record, over repeated two-sided cycles."""
    distribution = Distribution("dispatch_start_skew_ns")
    for _ in range(30):
        distribution.add(await one_two_sided_cycle())

    summary = distribution.summary()
    assert summary["count"] == 30
    ordered = sorted(distribution.samples)
    assert quantile(ordered, 0.50) < 1_000_000
    assert statistics.median(ordered) >= 0


@pytest.mark.asyncio
async def test_a_single_sided_cycle_has_no_skew_to_measure() -> None:
    transport = AsyncRecordingTransport()
    recorder = SkewRecorder()
    transport.gate = recorder
    executor = Executor(adapter=VenueAdapter(RecordingTransport()))
    adapter = AsyncVenueAdapter(transport=transport)
    state = state_at()
    await executor.run_cycle_async(
        adapter,
        decision(desired(up_price=None, up_size=None), state),
        state,
        rules(),
        NOW,
    )
    assert len(recorder.starts) == 1
