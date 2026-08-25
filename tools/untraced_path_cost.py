"""Where does an *unsampled* cycle's telemetry cost actually go?

The P8 performance limit is about the ordinary book update that sampling does not select. That
cycle still has to maintain state — shadow queue slots and action counters — because an
estimate that skipped unsampled depth changes would silently depend on the sampling rate.

This measures each surviving component, so "irreducible" is a number rather than an assertion.
Read-only. No venue, no credential, no order.
"""

import dataclasses
import json
import sys
import time

from tests.execution.builders import market, rules, state_at

from maker5m.domain import Outcome
from maker5m.execution import Executor, RecordingTransport, VenueAdapter, prepare_both_sides
from maker5m.execution.reconciler import ReconcileAction, reconcile
from maker5m.feeds import BookTracker, IngressMerger, MarketDataPipeline
from maker5m.market import MarketState
from maker5m.market.events import HealthStatus
from maker5m.numeric import parse_price, parse_share
from maker5m.strategy import BaseLot, StrategyEngine, default_config
from maker5m.strategy.decision import DesiredOrder, DesiredOrders
from maker5m.telemetry import InstrumentedRun, SamplingPolicy, perf_now_ns
from maker5m.telemetry.instrumented import QUEUE_LOSS_ACTIONS

REPEATS = 100_000
ROUNDS = 7


def steady_state_keep() -> tuple[InstrumentedRun, object, object]:
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


def best_ns(work: object) -> float:
    callable_work = work
    assert callable(callable_work)
    best: float | None = None
    for _ in range(ROUNDS):
        start = time.perf_counter_ns()
        for _ in range(REPEATS):
            callable_work()
        elapsed = (time.perf_counter_ns() - start) / REPEATS
        best = elapsed if best is None else min(best, elapsed)
    assert best is not None
    return round(best, 1)


def main() -> None:
    harness, plan, _decision = steady_state_keep()
    assert hasattr(plan, "up")
    up, down = plan.up, plan.down  # type: ignore[attr-defined]
    counters = harness.counters
    shadow = harness.shadow
    books = harness.pipeline.books
    sampling = harness.sampling
    health = harness.pipeline.clob_health

    def state_loop() -> None:
        for side in (up, down):
            action = side.action
            counters.count_action(action.value)
            resting = side.live
            if resting is not None:
                counters.cycles_with_live_order += 1
                if action is ReconcileAction.KEEP:
                    counters.keeps_with_live_order += 1
                    shadow.on_keep(
                        resting.client_order_id,
                        books.bid_size_at(side.outcome, resting.price),
                    )
                elif action in QUEUE_LOSS_ACTIONS:
                    counters.count_execution_queue_loss(side.reason.value)

    def count_action_twice() -> None:
        counters.count_action("KEEP")
        counters.count_action("KEEP")

    components = {
        "three_perf_counter_reads": best_ns(lambda: (perf_now_ns(), perf_now_ns(), perf_now_ns())),
        "sampling_decision": best_ns(lambda: sampling.selects(7, "BookUpdate")),
        "continuity_health_check": best_ns(lambda: health.status is HealthStatus.HEALTHY),
        "two_side_state_loop_total": best_ns(state_loop),
        "  of_which_count_action_x2": best_ns(count_action_twice),
        "  of_which_bid_size_at_x2": best_ns(
            lambda: (
                books.bid_size_at(Outcome.UP, up.live.price),
                books.bid_size_at(Outcome.DOWN, down.live.price),
            )
        ),
    }
    report = {
        "note": (
            "Best-of-rounds nanoseconds per call. Components of the work an UNSAMPLED cycle "
            "still performs in a measuring run. The state loop cannot be sampled away without "
            "making the queue estimate depend on the sampling rate."
        ),
        "repeats": REPEATS,
        "rounds": ROUNDS,
        "components_ns": components,
        "sum_of_top_level_ns": round(
            components["three_perf_counter_reads"]
            + components["sampling_decision"]
            + components["continuity_health_check"]
            + components["two_side_state_loop_total"],
            1,
        ),
    }
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    print()


if __name__ == "__main__":
    main()
