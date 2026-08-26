"""Durable schema coverage and exactness.

**SUPPORTING UNIT TEST ONLY.** Schema shape, arithmetic, and round-trip fidelity. Nothing here
is evidence about a market — the real-market gate is `docs/evidence/P11-TELEMETRY-PERSISTENCE.md`.
"""

from __future__ import annotations

from dataclasses import fields
from fractions import Fraction

import pytest

from maker5m.execution.reconciler import ReconcileAction
from maker5m.numeric.units import MoneyUnits, ShareUnits
from maker5m.persistence import (
    DECISION_SCHEMA_VERSION,
    DecisionRecord,
    ExactRatio,
    FillProvenance,
    build_decision_record,
)
from tests.persistence.builders import identity, observation, telemetry

# -- Canonical §25: every decision field is present -------------------------------------------

CANONICAL_25_DECISION = {
    "market": ("market_id", "slug"),
    "phase": ("phase",),
    "local monotonic timestamp": ("local_monotonic_ns",),
    "exchange timestamp if available": ("exchange_timestamp_ns",),
    "spot": ("spot_price_units", "spot_price_scale_decimals"),
    "spot age": ("spot_age_ns",),
    "CLOB best bid/ask": ("up_best_bid", "up_best_ask", "down_best_bid", "down_best_ask"),
    "book age": ("book_age_ns",),
    "centre": ("raw_centre", "quantized_centre", "centre_source", "centre_status"),
    "I": ("inventory",),
    "n_up": ("n_up",),
    "n_down": ("n_down",),
    "cost_up": ("cost_up",),
    "cost_down": ("cost_down",),
    "total_cost": ("total_cost",),
    "pnl_if_up": ("pnl_if_up_without_rebate", "pnl_if_up_estimated_rebate"),
    "pnl_if_down": ("pnl_if_down_without_rebate", "pnl_if_down_estimated_rebate"),
    "favourite": ("favourite",),
    "target_I": ("target_inventory",),
    "L": ("base_lot", "base_lot_status"),
}


@pytest.mark.parametrize(("canonical", "names"), sorted(CANONICAL_25_DECISION.items()))
def test_every_canonical_25_decision_field_exists(canonical: str, names: tuple[str, ...]) -> None:
    present = {field.name for field in fields(DecisionRecord)}
    missing = [name for name in names if name not in present]
    assert not missing, f"Canonical §25 '{canonical}' is not persisted: {missing}"


def test_both_sides_are_recorded_separately_and_completely() -> None:
    """§25 wants desired, existing, queue and reason *per side*, not one merged action."""
    record = build_decision_record(observation(), identity(), persistence_sequence=1, event_id="e")
    for side in (record.up, record.down):
        assert side.action and side.reason
        assert side.desired_price is not None and side.desired_size is not None
        assert side.live_client_order_id is not None
        assert side.live_remaining_size is not None
        assert side.live_status is not None
    assert record.up.outcome != record.down.outcome


def test_a_decision_record_refuses_an_unknown_schema_version() -> None:
    record = build_decision_record(observation(), identity(), persistence_sequence=1, event_id="e")
    values = {field.name: getattr(record, field.name) for field in fields(DecisionRecord)}
    values["schema_version"] = DECISION_SCHEMA_VERSION + 1
    with pytest.raises(ValueError, match="unsupported decision schema"):
        DecisionRecord(**values)


# -- ages: absence is not zero -----------------------------------------------------------------


def test_ages_derive_from_event_timestamps_not_a_wall_clock() -> None:
    record = build_decision_record(observation(), identity(), persistence_sequence=1, event_id="e")
    assert record.spot_age_ns == 2_000_000_000
    assert record.book_age_ns == 1_000_000_000
    assert record.event_timestamp_ns == 1_787_733_400_000_000_000


def test_a_missing_source_is_recorded_as_unknown_not_as_zero_age() -> None:
    record = build_decision_record(
        observation(with_book=False, with_spot=False),
        identity(),
        persistence_sequence=1,
        event_id="e",
    )
    assert record.spot_age_ns is None
    assert record.book_age_ns is None
    assert record.spot_price_units is None
    assert record.up_best_bid is None


def test_an_absent_exchange_timestamp_is_null_not_the_ingress_clock() -> None:
    """The ingress clock must never be passed off as the venue's."""
    without = build_decision_record(observation(), identity(), persistence_sequence=1, event_id="e")
    assert without.exchange_timestamp_ns is None

    with_venue = build_decision_record(
        observation(source_timestamp_ns=1_787_733_399_500_000_000),
        identity(),
        persistence_sequence=2,
        event_id="e",
    )
    assert with_venue.exchange_timestamp_ns == 1_787_733_399_500_000_000
    assert with_venue.exchange_timestamp_ns != with_venue.event_timestamp_ns


# -- economics are carried, never recomputed ---------------------------------------------------


def test_the_ledger_projection_is_carried_through_unchanged() -> None:
    """A second PnL formula here would be a second thing to get wrong."""
    from tests.persistence.builders import economics

    numbers = economics(
        inventory=ShareUnits(7_000_000),
        n_up=ShareUnits(20_000_000),
        n_down=ShareUnits(13_000_000),
        cost_up=MoneyUnits(9_800_000),
        cost_down=MoneyUnits(6_100_000),
        total_cost=MoneyUnits(15_900_000),
        fees=MoneyUnits(12_345),
        estimated_rebates=MoneyUnits(6_789),
        pnl_if_up_without_rebate=MoneyUnits(4_100_000),
        pnl_if_down_without_rebate=MoneyUnits(-2_900_000),
    )
    record = build_decision_record(
        observation(decision_telemetry=telemetry(economics=numbers)),
        identity(),
        persistence_sequence=1,
        event_id="e",
    )
    assert record.inventory == 7_000_000
    assert record.total_cost == 15_900_000
    assert record.pnl_if_up_without_rebate == 4_100_000
    assert record.pnl_if_down_without_rebate == -2_900_000
    assert record.fees == 12_345


def test_both_rebate_views_survive_because_o07_is_open() -> None:
    record = build_decision_record(observation(), identity(), persistence_sequence=1, event_id="e")
    names = {field.name for field in fields(DecisionRecord)}
    assert {"estimated_rebates", "realised_rebates"} <= names
    assert {"pnl_if_up_without_rebate", "pnl_if_up_estimated_rebate"} <= names
    assert record.estimated_rebates == record.realised_rebates == 0


# -- exactness ---------------------------------------------------------------------------------


def test_an_exact_ratio_round_trips_a_fraction_without_a_float() -> None:
    value = Fraction(123_456_789, 1_000_000_007)
    stored = ExactRatio.of(value)
    assert stored.value == value
    assert stored.numerator == 123_456_789
    assert stored.denominator == 1_000_000_007


def test_an_exact_ratio_refuses_a_zero_denominator() -> None:
    with pytest.raises(ValueError, match="non-zero denominator"):
        ExactRatio(numerator=1, denominator=0)


def test_no_authoritative_economic_field_is_a_float() -> None:
    record = build_decision_record(observation(), identity(), persistence_sequence=1, event_id="e")
    for field in fields(DecisionRecord):
        value = getattr(record, field.name)
        assert not isinstance(value, float), f"{field.name} is a float"


# -- fill provenance ---------------------------------------------------------------------------


def test_real_and_modelled_fills_can_never_be_confused() -> None:
    assert len({member.value for member in FillProvenance}) == len(FillProvenance)
    assert len(FillProvenance) == 2, "a third kind would need its own honesty argument"
    assert {member.name for member in FillProvenance} == {"REAL_VENUE", "SHADOW_MODEL"}


def test_the_risk_verdict_is_recorded_when_one_is_attached() -> None:
    record = build_decision_record(
        observation(risk=(12, "HALTED", False, True)),
        identity(),
        persistence_sequence=1,
        event_id="e",
    )
    assert record.risk_sequence == 12
    assert record.risk_state == "HALTED"
    assert record.risk_allows_place is False
    assert record.risk_allows_cancel is True


def test_every_typed_action_survives_into_the_record() -> None:
    for action in ReconcileAction:
        record = build_decision_record(
            observation(action=action), identity(), persistence_sequence=1, event_id="e"
        )
        assert record.up.action == action.value
