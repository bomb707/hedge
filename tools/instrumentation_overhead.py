"""What does instrumentation actually cost? Paired, interleaved, and reported by tier.

An earlier version of this benchmark produced three confidently wrong answers (+133%, +49%,
+217%). Two of them charged *simulation* work — preparation, reconciliation, the shadow order
table — to instrumentation, because the off configuration returned before doing it. The third
compared an off run reconciling against an empty order table with an on run reconciling against
several hundred orders. None of those errors was visible by reading the source.

So the method is now explicit, and it is the method that carries the result:

* **Identical simulated work on both sides.** The off configuration runs the same preparation,
  reconciliation, and shadow order-table maintenance. Only measurement is switched off.
* **Identical starting state.** A fresh harness per pass: same empty order table, same shadow
  state, same config, same deterministic corpus in the same order.
* **Interleaved.** Off and on alternate within each repeat, and the order flips between repeats,
  so a machine that warms or throttles cannot bias one configuration.
* **Ordinals advance**, as they do in a live run. An earlier draft left the ingress ordinal
  pinned at zero, so every cycle satisfied ``ordinal % sample_every == 0`` and the benchmark
  reported no unsampled cycles at all — the very tier the acceptance limit is about.
* **Warmed up.** Discarded passes first, so neither side pays for a cold interpreter.
* **Reported per repeat**, not only as a pooled aggregate: a single number cannot show whether
  the two configurations were measured under comparable conditions.
* **Split by sampling tier**, because they are genuinely different costs. An ordinary unsampled
  book update is the one that must be cheap; a sampled cycle and an always-traced action cycle
  legitimately cost more, and are reported separately rather than averaged in.
* **Two streams, because one of them is unrepresentative.** The replay corpus exercises every
  action, which is what makes it a good functional stream — but 51% of its cycles act, against
  0.9% in the real market. Tier numbers taken from it would be dominated by action cycles and
  perturbed by their allocations. So tiers are also measured on a *steady-state* stream: one
  order resting per side, the desired price unchanged, depth churning underneath. That is what
  production overwhelmingly looks like (measured KEEP ratio 0.993), and it is the stream the
  performance limit should be read against. Both are reported.

Read-only. No venue, no credential, no order.
"""

import dataclasses
import json
import statistics
import sys

from tests.execution.builders import state_at
from tests.replay.corpus import SYNTHETIC_EVENTS, market

from maker5m.domain import Outcome
from maker5m.execution import Executor, RecordingTransport, VenueAdapter
from maker5m.execution.reconciler import ReconcilePlan
from maker5m.feeds import BookTracker, IngressMerger, MarketDataPipeline
from maker5m.feeds.venue import VenueMarketRules
from maker5m.market import MarketState, reduce_event
from maker5m.numeric import parse_price, parse_share
from maker5m.strategy import BaseLot, StrategyEngine, default_config
from maker5m.strategy.decision import DesiredOrder, DesiredOrders
from maker5m.telemetry import InstrumentedRun, SamplingPolicy, perf_now_ns
from maker5m.telemetry.metrics import quantile
from maker5m.telemetry.observation import OBS_EVENT_KIND, OBS_INGRESS_ORDINAL, OBS_PLAN

Stats = dict[str, int]

PASSES = 40
"""Corpus repeats inside one pass; 39 events each, so ~1,560 cycles per pass."""

REPEATS = 8
"""Independent off/on pairs. Each is reported separately."""

WARMUP_PASSES = 2
STEADY_REPEATS = 4
SAMPLE_EVERY = 10
"""The production sampling policy this run is characterising."""

UNSAMPLED = "unsampled_ordinary"
SAMPLED = "sampled_ordinary"
ACTION = "always_traced_action"


def build(enabled: bool) -> tuple[InstrumentedRun, StrategyEngine, IngressMerger, MarketState]:
    """A fresh harness. Identical for both configurations except for ``enabled``."""
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
    harness = InstrumentedRun(
        pipeline=pipeline,
        engine=engine,
        rules=VenueMarketRules(parse_price("0.01"), parse_share("5"), source="bench"),
        executor=Executor(adapter=VenueAdapter(RecordingTransport())),
        sampling=SamplingPolicy(SAMPLE_EVERY),
        enabled=enabled,
    )
    return harness, engine, merger, MarketState.initial(definition)


STEADY_CYCLES = 4_000
"""Cycles per steady-state pass: one resting order per side, book churning underneath."""


def steady_state_pass(enabled: bool) -> tuple[list[int], list[str]]:
    """Time a production-shaped stream: KEEP after KEEP, with real depth movement.

    Returns (cycle_ns, tier label per cycle). The tier is derived from the harness itself
    rather than assumed, so a change in the sampling policy cannot silently mislabel it.
    """
    harness, engine, merger, _initial = build(enabled)
    pipeline = harness.pipeline
    definition = market()
    pipeline.clob_health.mark_snapshot(definition.t0)
    pipeline.spot_health.mark_snapshot(definition.t0)

    state = state_at(up_bid="0.62", up_ask="0.64", down_bid="0.35", down_ask="0.38")
    merger.state = state
    orders = DesiredOrders(
        up=DesiredOrder(Outcome.UP, parse_price("0.62"), parse_share("15")),
        down=DesiredOrder(Outcome.DOWN, parse_price("0.35"), parse_share("15")),
    )
    pipeline.books.up.snapshot_seen = True
    pipeline.books.down.snapshot_seen = True

    cycle_ns: list[int] = []
    for index in range(STEADY_CYCLES):
        # Depth at our own price moves every cycle; the price we want does not.
        pipeline.books.up.bids[620_000] = 40_000_000 - (index % 30) * 1_000_000
        pipeline.books.down.bids[350_000] = 25_000_000 - (index % 17) * 1_000_000
        merger.advance_ordinal()
        merger.stages_measured = harness.sampling.selects(merger.ordinal, "BookUpdate")
        start = perf_now_ns()
        # A real decide() every cycle, so the denominator is a genuine full cycle rather than
        # observe() alone. The pinned desired orders keep the stream in steady state; building
        # them costs the same on both configurations.
        decision = dataclasses.replace(engine.decide(state), orders=orders)
        harness.observe("BookUpdate", start, decision)
        cycle_ns.append(perf_now_ns() - start)

    return cycle_ns, tiers_from(harness)


def one_pass(enabled: bool, passes: int = PASSES) -> tuple[list[int], list[int], InstrumentedRun]:
    """Return (decide_ns, cycle_ns) per cycle, in cycle order, plus the harness."""
    harness, engine, merger, initial = build(enabled)
    decide_ns: list[int] = []
    cycle_ns: list[int] = []
    for _ in range(passes):
        state = initial
        for event in SYNTHETIC_EVENTS:
            start = perf_now_ns()
            state = reduce_event(state, event)
            merger.state = state
            merger.advance_ordinal()
            merger.stages_measured = harness.sampling.selects(merger.ordinal, type(event).__name__)
            decision = engine.decide(state)
            decided = perf_now_ns()
            harness.observe(type(event).__name__, start, decision)
            done = perf_now_ns()
            decide_ns.append(decided - start)
            cycle_ns.append(done - start)
    return decide_ns, cycle_ns, harness


def tiers_from(harness: InstrumentedRun) -> list[str]:
    """Label each captured cycle by sampling tier, read back off the observation stream.

    Derived from the observations themselves rather than from a hook on the hot path: the tier
    is a property of the cycle, and reading it back afterwards keeps the measured path free of
    benchmark scaffolding.
    """
    labels: list[str] = []
    for observation in harness.buffer:
        plan = observation[OBS_PLAN]
        ordinal = observation[OBS_INGRESS_ORDINAL]
        kind = observation[OBS_EVENT_KIND]
        assert isinstance(ordinal, int) and isinstance(kind, str)
        if isinstance(plan, ReconcilePlan) and plan.request_count > 0:
            labels.append(ACTION)
        elif harness.sampling.selects(ordinal, kind):
            labels.append(SAMPLED)
        else:
            labels.append(UNSAMPLED)
    return labels


def summarize(samples: list[int]) -> Stats:
    if not samples:
        return {"count": 0}
    ordered = sorted(samples)
    return {
        "count": len(ordered),
        "p50": quantile(ordered, 0.50),
        "p90": quantile(ordered, 0.90),
        "p95": quantile(ordered, 0.95),
        "p99": quantile(ordered, 0.99),
        "max": ordered[-1],
        "mean": int(statistics.fmean(ordered)),
    }


def delta(off: Stats, on: Stats) -> dict[str, dict[str, float | int | None]]:
    if not off.get("count") or not on.get("count"):
        return {}
    out: dict[str, dict[str, float | int | None]] = {}
    for stat in ("p50", "p90", "p95", "p99"):
        absolute = on[stat] - off[stat]
        out[stat] = {
            "absolute_ns": absolute,
            "percent": None if off[stat] == 0 else round(absolute / off[stat] * 100, 1),
        }
    return out


def main() -> None:
    for _ in range(WARMUP_PASSES):
        one_pass(enabled=False, passes=4)
        one_pass(enabled=True, passes=4)

    off_decide: list[int] = []
    off_cycle: list[int] = []
    on_decide: list[int] = []
    on_cycle: list[int] = []
    per_repeat: list[dict[str, object]] = []

    for repeat in range(REPEATS):
        # Flip the order every repeat so thermal drift cannot favour one configuration.
        if repeat % 2 == 0:
            off = one_pass(enabled=False)
            on = one_pass(enabled=True)
            order = "off_first"
        else:
            on = one_pass(enabled=True)
            off = one_pass(enabled=False)
            order = "on_first"
        labels = tiers_from(on[2])
        off_decide += off[0]
        off_cycle += off[1]
        on_decide += on[0]
        on_cycle += on[1]
        per_repeat.append(
            {
                "repeat": repeat,
                "order": order,
                "decide_p50": {"off": summarize(off[0])["p50"], "on": summarize(on[0])["p50"]},
                "cycle_p50": {"off": summarize(off[1])["p50"], "on": summarize(on[1])["p50"]},
            }
        )

    def by_tier(samples: list[int], labels_: list[str], tier: str) -> list[int]:
        """Cycle *i* of every repeat shares the label of cycle *i* of the labelling pass."""
        width = len(labels_)
        return [value for index, value in enumerate(samples) if labels_[index % width] == tier]

    def tier_table(
        off_samples: list[int], on_samples: list[int], labels_: list[str]
    ) -> dict[str, object]:
        table: dict[str, object] = {}
        for tier in (UNSAMPLED, SAMPLED, ACTION):
            off_stats = summarize(by_tier(off_samples, labels_, tier))
            on_stats = summarize(by_tier(on_samples, labels_, tier))
            table[tier] = {"off": off_stats, "on": on_stats, "delta": delta(off_stats, on_stats)}
        return table

    tiers = tier_table(off_cycle, on_cycle, labels)

    # The stream the performance limit should be read against.
    steady_off: list[int] = []
    steady_on: list[int] = []
    steady_labels: list[str] = []
    for repeat in range(STEADY_REPEATS):
        if repeat % 2 == 0:
            off_run, _ = steady_state_pass(enabled=False)
            on_run, steady_labels = steady_state_pass(enabled=True)
        else:
            on_run, steady_labels = steady_state_pass(enabled=True)
            off_run, _ = steady_state_pass(enabled=False)
        steady_off += off_run
        steady_on += on_run

    overall_decide_off = summarize(off_decide)
    overall_decide_on = summarize(on_decide)
    overall_cycle_off = summarize(off_cycle)
    overall_cycle_on = summarize(on_cycle)

    report: dict[str, object] = {
        "method": {
            "passes_per_run": PASSES,
            "repeats": REPEATS,
            "warmup_passes": WARMUP_PASSES,
            "sample_every": SAMPLE_EVERY,
            "interleaved": True,
            "identical_simulated_work_both_sides": True,
        },
        "cycle_counts_by_tier": {
            tier: sum(1 for label in labels if label == tier)
            for tier in (UNSAMPLED, SAMPLED, ACTION)
        },
        "per_repeat": per_repeat,
        "decide_ns": {
            "off": overall_decide_off,
            "on": overall_decide_on,
            "delta": delta(overall_decide_off, overall_decide_on),
        },
        "cycle_ns": {
            "off": overall_cycle_off,
            "on": overall_cycle_on,
            "delta": delta(overall_cycle_off, overall_cycle_on),
        },
        "cycle_ns_by_tier_replay_corpus": tiers,
        "steady_state": {
            "note": (
                "One order resting per side, desired price unchanged, depth churning. "
                "This is the production-shaped stream; the P8 performance limit is read here."
            ),
            "cycles_per_pass": STEADY_CYCLES,
            "repeats": STEADY_REPEATS,
            "cycle_counts_by_tier": {
                tier: sum(1 for label in steady_labels if label == tier)
                for tier in (UNSAMPLED, SAMPLED, ACTION)
            },
            "cycle_ns_by_tier": tier_table(steady_off, steady_on, steady_labels),
        },
    }
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    print()


if __name__ == "__main__":
    main()
