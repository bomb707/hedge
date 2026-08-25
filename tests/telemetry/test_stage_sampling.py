"""Stage sampling must gate the clock reads themselves, not just what is written down.

The decision has to be made before ``reduce_event`` and ``decide`` run, or those stages cannot
be timed at all. That puts a measurement concern upstream of the deterministic core, so these
tests pin the two things that could go wrong: that the default costs nothing and changes
nothing, and that the same trading logic runs either way.
"""

from __future__ import annotations

from maker5m.feeds import IngressMerger, MarketDataPipeline
from maker5m.feeds.polymarket import BookTracker
from maker5m.market import BtcPrice
from maker5m.strategy import BaseLot, StrategyEngine, default_config
from maker5m.telemetry.observation import NOT_CAPTURED
from tests.unit.builders import book, initial_state


def merger_for() -> IngressMerger:
    state = initial_state()
    return IngressMerger(
        engine=StrategyEngine(default_config(BaseLot.of(15))),
        state=state,
        clock=lambda: state.definition.t0,
        market_id=state.market_id,
    )


def test_the_default_takes_no_readings_at_all() -> None:
    """P5 replay and ordinary P6 capture must be untouched by measurement plumbing."""
    readings = 0

    def clock() -> int:
        nonlocal readings
        readings += 1
        return 1_000 + readings

    merger = merger_for()
    merger.perf_clock = clock
    merger.submit(book(0, 30, bid="0.61", ask="0.62"))

    assert readings == 0, "a default submit must not read the clock"
    assert merger.stages_measured is False
    assert merger.last_decide_ns == NOT_CAPTURED


def test_a_selected_event_records_both_stage_readings() -> None:
    readings: list[int] = []

    def clock() -> int:
        readings.append(len(readings))
        return 1_000 + len(readings)

    merger = merger_for()
    merger.perf_clock = clock
    merger.submit(book(0, 30, bid="0.61", ask="0.62"), measure_stages=True)

    assert len(readings) == 2, "one reading after reduce, one after decide"
    assert merger.stages_measured is True
    assert merger.last_reduce_ns != NOT_CAPTURED
    assert merger.last_decide_ns != NOT_CAPTURED
    assert merger.last_decide_ns > merger.last_reduce_ns


def test_an_unselected_event_clears_the_previous_reading() -> None:
    """A stale pair must never be read as though it belonged to this cycle."""
    merger = merger_for()
    merger.perf_clock = lambda: 5_000
    merger.submit(book(0, 30, bid="0.61", ask="0.62"), measure_stages=True)
    assert merger.last_decide_ns == 5_000

    merger.submit(book(1, 31, bid="0.61", ask="0.62"))
    assert merger.last_decide_ns == NOT_CAPTURED
    assert merger.stages_measured is False


def test_the_same_decision_is_produced_either_way() -> None:
    """Measurement may change whether a clock is read. It may not change trading logic."""
    timed = merger_for()
    timed.perf_clock = lambda: 7_000
    untimed = merger_for()

    events = [book(index, 30 + index, bid="0.61", ask="0.62") for index in range(6)]
    timed_decisions = [
        timed.submit(event, measure_stages=index % 2 == 0) for index, event in enumerate(events)
    ]
    untimed_decisions = [untimed.submit(event) for event in events]

    assert timed_decisions == untimed_decisions
    assert timed.state == untimed.state
    assert [step.decision for step in timed.steps] == [step.decision for step in untimed.steps]


def test_the_pipeline_selector_is_off_by_default() -> None:
    merger = merger_for()
    definition = merger.state.definition
    pipeline = MarketDataPipeline(
        merger=merger,
        books=BookTracker(definition.up_token_id, definition.down_token_id),
    )
    assert pipeline.stage_selector is None
    assert pipeline._measure(10, "BookUpdate") is False


def test_the_pipeline_selector_is_consulted_with_ordinal_and_kind() -> None:
    merger = merger_for()
    definition = merger.state.definition
    pipeline = MarketDataPipeline(
        merger=merger,
        books=BookTracker(definition.up_token_id, definition.down_token_id),
    )
    seen: list[tuple[int, str]] = []

    def selector(ordinal: int, kind: str) -> bool:
        seen.append((ordinal, kind))
        return True

    pipeline.stage_selector = selector

    pipeline.on_spot(BtcPrice(60_000_000_000, 8))
    assert seen and seen[-1][1] == "SpotTick"
    assert merger.stages_measured is True
