"""MarketState identity, derived phase, and immutability."""

from __future__ import annotations

import dataclasses

import pytest

from maker5m.domain import Outcome
from maker5m.market import (
    CANONICAL_PHASE_CONFIG,
    HealthState,
    HealthStatus,
    MarketState,
    Phase,
    TimestampNs,
)
from maker5m.market.errors import MarketDefinitionError
from maker5m.market.timebase import seconds
from maker5m.numeric import DomainError, PriceUnits
from tests.unit.builders import T0, at, definition, initial_state


def test_initial_state_is_flat_and_unobserved() -> None:
    state = initial_state()
    assert state.ledger.n_up == 0
    assert state.ledger.n_down == 0
    assert state.net_inventory == 0
    assert state.book is None
    assert state.spot is None
    assert state.orders == {}
    assert state.applied_fill_ids == frozenset()
    assert state.resolution is None
    assert state.health == HealthState()


def test_initial_ordinal_allows_ordinal_zero() -> None:
    assert initial_state().last_ingress_ordinal == -1


def test_initial_clock_is_parked_at_t0_so_the_phase_is_prearm() -> None:
    state = initial_state()
    assert state.last_event_timestamp == T0
    assert state.phase is Phase.PREARM


def test_phase_is_derived_from_the_latest_event_timestamp() -> None:
    state = dataclasses.replace(initial_state(), last_event_timestamp=at(240))
    assert state.phase is Phase.ENDGAME


def test_there_is_no_stored_phase_field_to_drift() -> None:
    """One source of truth: the phase cannot disagree with the event stream."""
    names = {f.name for f in dataclasses.fields(MarketState)}
    assert "phase" not in names
    assert isinstance(type(MarketState).__dict__.get("phase", None), type(None))
    assert isinstance(MarketState.phase, property)


def test_phase_at_timestamp_queries_an_arbitrary_time() -> None:
    state = initial_state()
    assert state.phase_at_timestamp(at(2)) is Phase.PREARM
    assert state.phase_at_timestamp(at(3)) is Phase.QUOTE
    assert state.phase_at_timestamp(at(300)) is Phase.DONE


def test_market_end_is_t0_plus_duration() -> None:
    assert definition().market_end == TimestampNs(T0 + seconds(300))


def test_token_mapping_round_trips() -> None:
    d = definition()
    assert d.token_id(Outcome.UP) == "token-up"
    assert d.token_id(Outcome.DOWN) == "token-down"
    assert d.outcome_of("token-up") is Outcome.UP
    assert d.outcome_of("token-down") is Outcome.DOWN


def test_unknown_token_is_rejected_not_defaulted() -> None:
    with pytest.raises(MarketDefinitionError):
        definition().outcome_of("token-from-another-market")


@pytest.mark.parametrize("field", ["market_id", "slug", "up_token_id", "down_token_id"])
def test_identity_fields_must_not_be_empty(field: str) -> None:
    with pytest.raises(MarketDefinitionError):
        definition(**{field: ""})


def test_up_and_down_tokens_must_differ() -> None:
    with pytest.raises(MarketDefinitionError):
        definition(up_token_id="same", down_token_id="same")


def test_negative_t0_is_rejected() -> None:
    with pytest.raises(DomainError):
        definition(t0=TimestampNs(-1))


def test_non_positive_tick_is_rejected() -> None:
    with pytest.raises(MarketDefinitionError):
        definition(tick=PriceUnits(0))


def test_state_and_definition_are_immutable() -> None:
    state = initial_state()
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.last_ingress_ordinal = 5  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.definition.slug = "other"  # type: ignore[misc]


def test_order_map_is_read_only() -> None:
    with pytest.raises(TypeError):
        initial_state().orders["x"] = None  # type: ignore[index]


def test_health_state_updates_one_component_at_a_time() -> None:
    from maker5m.market import HealthComponent

    health = HealthState()
    assert not health.all_healthy
    health = health.with_component(HealthComponent.CLOB_BOOK, HealthStatus.HEALTHY)
    assert health.clob_book is HealthStatus.HEALTHY
    assert health.spot_feed is HealthStatus.UNKNOWN
    health = health.with_component(HealthComponent.SPOT_FEED, HealthStatus.HEALTHY)
    health = health.with_component(HealthComponent.ORDER_STREAM, HealthStatus.HEALTHY)
    assert health.all_healthy
    assert health.status_of(HealthComponent.ORDER_STREAM) is HealthStatus.HEALTHY


def test_state_carries_the_canonical_section_24_1_market_fields() -> None:
    """Canonical section 24.1: identity, lifecycle, strike, resolution, tokens, tick."""
    state = initial_state()
    d = state.definition
    assert d.market_id and d.slug and d.up_token_id and d.down_token_id
    assert d.t0 == T0
    assert d.market_end > d.t0
    assert d.strike is not None
    assert d.tick > 0
    assert state.phase in set(Phase)
    assert state.resolution is None  # winner state; written by P10


def test_config_version_is_reachable_for_replay_identity() -> None:
    assert initial_state().definition.phase_config.version == "canonical-v1"
    assert definition().phase_config is CANONICAL_PHASE_CONFIG


def test_definition_rejects_an_impossible_lifecycle_via_phase_config() -> None:
    from maker5m.market import PhaseConfig

    with pytest.raises(MarketDefinitionError):
        PhaseConfig(seconds(3), seconds(240), seconds(280), seconds(100), "impossible")
