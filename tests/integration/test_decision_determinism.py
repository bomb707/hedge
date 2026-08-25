"""End-to-end determinism of the decision layer over a whole market lifecycle.

P5 replay drives this exact ``decide()`` against a recorded journal, so a decision that
varied between runs on the same state would make every downstream experiment worthless
(I20). This walks a full 300 s market, decides at every event, and asserts the entire
decision trajectory is reproducible.
"""

from __future__ import annotations

import dataclasses

from maker5m.domain import Outcome
from maker5m.market import Phase, reduce_event
from maker5m.numeric import parse_share
from maker5m.strategy import (
    BaseLot,
    GridPolicy,
    StrategyEngine,
    TickRounding,
    default_config,
)
from tests.integration.test_market_determinism import STREAM
from tests.unit.builders import initial_state

ENGINE = StrategyEngine(default_config())


def trajectory(engine: StrategyEngine) -> list[object]:
    """Decide after every event in a full-lifecycle stream."""
    state = initial_state()
    out: list[object] = []
    for event in STREAM:
        state = reduce_event(state, event)
        out.append(engine.decide(state))
    return out


def test_the_stream_covers_every_phase() -> None:
    state = initial_state()
    phases = {state.phase_at_timestamp(e.meta.timestamp) for e in STREAM}
    assert phases == {Phase.PREARM, Phase.QUOTE, Phase.ENDGAME, Phase.SETTLING, Phase.DONE}


def test_the_whole_decision_trajectory_is_reproducible() -> None:
    assert trajectory(ENGINE) == trajectory(ENGINE)


def test_a_second_engine_with_an_equal_config_agrees_exactly() -> None:
    assert trajectory(StrategyEngine(default_config())) == trajectory(ENGINE)


def test_a_different_config_gives_a_different_but_reproducible_trajectory() -> None:
    other = StrategyEngine(
        dataclasses.replace(default_config(), grid_policy=GridPolicy.OBSERVED_ADJACENT)
    )
    assert trajectory(other) == trajectory(other)
    assert trajectory(other) != trajectory(ENGINE)


def test_orders_only_ever_appear_in_quoting_phases() -> None:
    state = initial_state()
    for event in STREAM:
        state = reduce_event(state, event)
        result = ENGINE.decide(state)
        if state.phase in (Phase.PREARM, Phase.SETTLING, Phase.DONE):
            assert result.orders.is_empty, f"orders emitted in {state.phase}"
        assert result.orders.count <= 2


def test_every_emitted_order_is_a_buy_of_its_own_outcome() -> None:
    state = initial_state()
    for event in STREAM:
        state = reduce_event(state, event)
        orders = ENGINE.decide(state).orders
        if orders.up is not None:
            assert orders.up.outcome is Outcome.UP
            assert orders.up.size > 0
        if orders.down is not None:
            assert orders.down.outcome is Outcome.DOWN
            assert orders.down.size > 0


def test_zero_spread_holds_on_every_decision_that_quotes_both_sides() -> None:
    from maker5m.numeric import PRICE_SCALE

    state = initial_state()
    checked = 0
    for event in STREAM:
        state = reduce_event(state, event)
        orders = ENGINE.decide(state).orders
        if orders.up is not None and orders.down is not None:
            assert orders.up.price + orders.down.price == PRICE_SCALE
            checked += 1
    assert checked > 10


def test_every_emitted_size_satisfies_the_modular_fingerprint() -> None:
    """I04 holds through the whole decision layer, not just in the grid module."""
    from maker5m.strategy import GRID

    state = initial_state()
    for event in STREAM:
        state = reduce_event(state, event)
        result = ENGINE.decide(state)
        inventory = result.telemetry.economics.inventory
        if result.orders.up is not None:
            assert (result.orders.up.size + inventory) % GRID == 0
        if result.orders.down is not None:
            assert (result.orders.down.size - inventory) % GRID == 0


def test_economics_are_recorded_on_every_single_decision() -> None:
    state = initial_state()
    for event in STREAM:
        state = reduce_event(state, event)
        economics = ENGINE.decide(state).telemetry.economics
        assert economics.inventory == state.ledger.net_inventory
        assert economics.total_cost == state.ledger.total_cost


def test_the_configuration_cross_product_is_all_deterministic() -> None:
    for policy in GridPolicy:
        for rounding in TickRounding:
            for whole in (15, 20, 25):
                config = dataclasses.replace(
                    default_config(BaseLot.of(whole)),
                    grid_policy=policy,
                    tick_rounding=rounding,
                )
                engine = StrategyEngine(config)
                state = initial_state()
                for event in STREAM[:40]:
                    state = reduce_event(state, event)
                first = engine.decide(state)
                assert engine.decide(state) == first


def test_a_deep_excursion_still_quotes_the_inward_side() -> None:
    """I17: the hard band is a wall, never a reason to stop trading entirely."""
    from maker5m.accounting import Fill, LedgerState
    from maker5m.numeric import money_from_whole
    from tests.unit.builders import quoting_state

    ledger = LedgerState().apply_fill(Fill(Outcome.UP, parse_share("150"), money_from_whole(90)))
    state = dataclasses.replace(quoting_state(offset_s=60), ledger=ledger)
    result = ENGINE.decide(state)
    assert result.orders.up is None
    assert result.orders.down is not None
