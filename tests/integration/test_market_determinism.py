"""The property everything downstream depends on: same events, same state, every time.

Invariant I20. P5 replay will drive this exact reducer with a recorded journal, so if the
same ordered stream could produce two different states, replay could not reproduce
production decisions and no OPEN item could ever be closed by evidence.
"""

from __future__ import annotations

import dataclasses

from maker5m.domain import Outcome
from maker5m.market import (
    Event,
    HealthComponent,
    OwnFill,
    Phase,
    PhaseEvent,
    reduce_event,
    reduce_events,
    snapshot,
)
from tests.unit.builders import (
    book,
    health,
    initial_state,
    meta,
    order_state,
    own_fill,
    spot,
)


def full_market_stream() -> list[Event]:
    """A stream spanning the whole lifecycle, touching every event type."""
    events: list[Event] = [PhaseEvent(meta(0, 0), Phase.PREARM)]
    ordinal = 1
    for second in range(3, 300, 7):
        events.append(book(ordinal, second, bid="0.61", ask="0.62"))
        ordinal += 1
        events.append(spot(ordinal, second))
        ordinal += 1
        if second % 21 == 3:
            outcome = Outcome.UP if (second // 7) % 2 == 0 else Outcome.DOWN
            events.append(own_fill(ordinal, second, outcome=outcome, shares="5", cost="3"))
            ordinal += 1
        if second % 35 == 3:
            events.append(order_state(ordinal, second, client_order_id=f"c{second}"))
            ordinal += 1
        if second % 49 == 3:
            events.append(health(ordinal, second, component=HealthComponent.SPOT_FEED))
            ordinal += 1
    events.append(PhaseEvent(meta(ordinal, 300), Phase.DONE))
    return events


STREAM = full_market_stream()


def test_the_stream_actually_covers_the_lifecycle() -> None:
    """Guard against the fixture degenerating into a trivial case."""
    phases = {initial_state().phase_at_timestamp(e.meta.timestamp) for e in STREAM}
    assert phases == {Phase.PREARM, Phase.QUOTE, Phase.ENDGAME, Phase.SETTLING, Phase.DONE}
    assert len(STREAM) > 100


def test_repeated_runs_produce_identical_final_state() -> None:
    runs = [reduce_events(initial_state(), STREAM) for _ in range(5)]
    for run in runs[1:]:
        assert run == runs[0]


def test_repeated_runs_produce_identical_snapshots() -> None:
    runs = [snapshot(reduce_events(initial_state(), STREAM)) for _ in range(5)]
    for run in runs[1:]:
        assert run == runs[0]


def test_every_intermediate_state_is_identical_across_runs() -> None:
    """Not just the endpoint: the whole trajectory must match, as replay compares steps."""

    def trajectory() -> list[tuple[int, object]]:
        state = initial_state()
        out: list[tuple[int, object]] = []
        for event in STREAM:
            state = reduce_event(state, event)
            out.append((state.last_ingress_ordinal, snapshot(state)))
        return out

    assert trajectory() == trajectory()


def test_reducing_stepwise_equals_reducing_as_a_fold() -> None:
    stepwise = initial_state()
    for event in STREAM:
        stepwise = reduce_event(stepwise, event)
    assert stepwise == reduce_events(initial_state(), STREAM)


def test_final_accounting_is_exact_and_each_fill_counted_once() -> None:
    state = reduce_events(initial_state(), STREAM)
    fills = [e for e in STREAM if isinstance(e, OwnFill)]
    assert len(state.applied_fill_ids) == len(fills)
    assert state.ledger.n_up + state.ledger.n_down == sum(e.fill.shares for e in fills)
    assert state.ledger.total_cost == sum(e.fill.cost for e in fills)


def test_the_initial_state_is_never_mutated_by_a_run() -> None:
    start = initial_state()
    before = dataclasses.astuple(start.ledger)
    reduce_events(start, STREAM)
    assert start.last_ingress_ordinal == -1
    assert start.book is None
    assert dataclasses.astuple(start.ledger) == before


def test_final_phase_is_done() -> None:
    assert reduce_events(initial_state(), STREAM).phase is Phase.DONE
