"""What does an *unsampled* cycle's telemetry capture actually cost?

The P8 performance limit is about the ordinary book update that sampling does not select. After
the telemetry offload, such a cycle no longer does any analysis at all — no slot mutation, no
counting, no classification, no distributions. What it still does is read the displayed depth at
our own price on each side, build one observation tuple, and append it to a bounded buffer.

The depth reads are the part that cannot be deferred. The book is mutable and moves continuously,
so the size resting at our own price has to be sampled at the moment the cycle sees it; there is
no later time at which the analyzer could recover it.

Read-only. No venue, no credential, no order.
"""

import dataclasses
import json
import sys
import time
from collections.abc import Callable

from tests.execution.builders import market, rules, state_at

from maker5m.domain import Outcome
from maker5m.execution import Executor, RecordingTransport, VenueAdapter, prepare_both_sides
from maker5m.execution.reconciler import ReconcileAction, ReconcilePlan, reconcile
from maker5m.feeds import BookTracker, IngressMerger, MarketDataPipeline
from maker5m.market import MarketState
from maker5m.market.events import HealthStatus
from maker5m.numeric import parse_price, parse_share
from maker5m.strategy import BaseLot, StrategyEngine, default_config
from maker5m.strategy.decision import DecisionResult, DesiredOrder, DesiredOrders
from maker5m.telemetry import InstrumentedRun, SamplingPolicy, perf_now_ns

REPEATS = 200_000
ROUNDS = 7


def steady_state_keep() -> tuple[InstrumentedRun, ReconcilePlan, DecisionResult]:
    """A harness with one order resting per side and a KEEP plan ready to replay."""
    definition = market()
    engine = StrategyEngine(default_config(BaseLot.of(15)))
    merger = IngressMerger(
        engine=engine,
        state=MarketState.initial(definition),
        clock=lambda: definition.t0,
        market_id=definition.market_id,
    )
    pipeline = MarketDataPipeline(
        merger=merger,
        books=BookTracker(definition.up_token_id, definition.down_token_id),
    )
    pipeline.clob_health.mark_snapshot(definition.t0)
    pipeline.spot_health.mark_snapshot(definition.t0)
    harness = InstrumentedRun(
        pipeline=pipeline,
        engine=engine,
        rules=rules(),
        executor=Executor(adapter=VenueAdapter(RecordingTransport())),
        sampling=SamplingPolicy(10),
    )
    state = state_at(up_bid="0.62", up_ask="0.64", down_bid="0.35", down_ask="0.38")
    merger.state = state
    decision = dataclasses.replace(
        engine.decide(state),
        orders=DesiredOrders(
            up=DesiredOrder(Outcome.UP, parse_price("0.62"), parse_share("15")),
            down=DesiredOrder(Outcome.DOWN, parse_price("0.35"), parse_share("15")),
        ),
    )
    pipeline.books.up.snapshot_seen = True
    pipeline.books.down.snapshot_seen = True
    pipeline.books.up.bids[620_000] = 40_000_000
    pipeline.books.down.bids[350_000] = 25_000_000
    for _ in range(3):
        harness.observe("BookUpdate", perf_now_ns(), decision)

    prepared = prepare_both_sides(decision, merger.state, harness.rules)
    live = {
        Outcome.UP: harness.executor.orders.current(Outcome.UP),
        Outcome.DOWN: harness.executor.orders.current(Outcome.DOWN),
    }
    plan = reconcile(prepared, live)
    assert all(side.action is ReconcileAction.KEEP for side in plan.sides)
    return harness, plan, decision


def best_ns(work: Callable[[], object]) -> float:
    best: float | None = None
    for _ in range(ROUNDS):
        start = time.perf_counter_ns()
        for _ in range(REPEATS):
            work()
        elapsed = (time.perf_counter_ns() - start) / REPEATS
        best = elapsed if best is None else min(best, elapsed)
    assert best is not None
    return round(best, 1)


def main() -> None:
    harness, plan, decision = steady_state_keep()
    up, down = plan.up, plan.down
    assert up.live is not None and down.live is not None
    up_price, down_price = up.live.price, down.live.price
    books = harness.pipeline.books
    up_bids, down_bids = books.up.bids, books.down.bids
    buffer = harness.buffer
    sampling = harness.sampling
    health = harness.pipeline.clob_health
    eligibility = decision.telemetry.eligibility

    def depth_reads() -> object:
        return up_bids.get(up_price, 0) + down_bids.get(down_price, 0)

    def observation() -> tuple[object, ...]:
        return (
            0,
            1,
            "BookUpdate",
            True,
            100,
            0,
            0,
            0,
            0,
            0,
            plan,
            40_000_000,
            25_000_000,
            None,
            None,
            eligibility,
            None,
        )

    def build_and_capture() -> object:
        buffer.capture(observation())
        return None

    components = {
        "sampling_decision": best_ns(lambda: sampling.selects(7, "BookUpdate")),
        "continuity_health_check": best_ns(lambda: health.status is HealthStatus.HEALTHY),
        "same_price_depth_reads_x2": best_ns(depth_reads),
        "observation_tuple_construction": best_ns(observation),
        "observation_build_and_buffer_insert": best_ns(build_and_capture),
        "three_perf_counter_reads_SAMPLED_ONLY": best_ns(
            lambda: (perf_now_ns(), perf_now_ns(), perf_now_ns())
        ),
    }
    unsampled = round(
        components["continuity_health_check"]
        + components["same_price_depth_reads_x2"]
        + components["observation_build_and_buffer_insert"],
        1,
    )
    report = {
        "note": (
            "Best-of-rounds nanoseconds per call, for work an UNSAMPLED cycle performs after "
            "the telemetry offload. No analysis remains on this path: no slot mutation, no "
            "counting, no classification, no distributions. The depth reads cannot be "
            "deferred because the book is mutable and will have moved by the time the "
            "analyzer runs. The perf-counter reads happen only on SAMPLED cycles and are "
            "excluded from the unsampled sum."
        ),
        "repeats": REPEATS,
        "rounds": ROUNDS,
        "components_ns": components,
        "sum_unsampled_ns": unsampled,
    }
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    print()


if __name__ == "__main__":
    main()
