"""Canonical §26 metrics and fill records.

**SUPPORTING UNIT TEST ONLY.** Constructed ledgers and constructed fills, which §43 and §42
explicitly permit for exact-accounting and schema work. Real nonzero own-ledger economics and
real own fills are UNRUN and belong to P14; nothing here claims otherwise.
"""

from __future__ import annotations

from dataclasses import fields
from fractions import Fraction

import pytest

from maker5m.accounting.decomposition import decompose
from maker5m.accounting.ledger import Fill, LedgerState
from maker5m.domain import Outcome
from maker5m.numeric.units import MoneyUnits, PriceUnits, ShareUnits
from maker5m.persistence import (
    FillProvenance,
    Liquidity,
    MarketMetrics,
    MetricsAccumulator,
    build_decision_record,
    build_fill_record,
)
from tests.persistence.builders import UP_TOKEN, fill_capture, identity, observation

CANONICAL_26 = (
    "gross_payout",
    "total_cost",
    "fees",
    "estimated_rebates",
    "realised_rebates",
    "net_pnl",
    "term1",
    "term2",
    "n_up",
    "n_down",
    "terminal_inventory",
    "terminal_residual_side",
    "terminal_residual_magnitude",
    "pnl_if_up_before_settlement",
    "pnl_if_down_before_settlement",
    "maker_fill_count",
    "taker_fill_count",
    "fill_count",
    "queue_ahead_sum",
    "queue_ahead_samples",
    "cancel_count",
    "replace_count",
    "stale_quote_count",
    "place_count",
    "quote_intent_count",
)


def accumulator() -> MetricsAccumulator:
    return MetricsAccumulator(market_id="0xmarket", slug="btc-updown-5m-1", provenance="TEST")


@pytest.mark.parametrize("name", CANONICAL_26)
def test_every_canonical_26_metric_is_present(name: str) -> None:
    assert name in {field.name for field in fields(MarketMetrics)}


def test_maker_fraction_and_average_queue_are_exact_pairs_not_decimals() -> None:
    """A UI may render a decimal. The record must not inherit that rounding choice."""
    unit = accumulator()
    for liquidity in (Liquidity.MAKER, Liquidity.MAKER, Liquidity.TAKER):
        unit.observe_fill(_fill_record(liquidity))
    metrics = unit.build(LedgerState(), winner=None)
    fraction = metrics.maker_fraction()
    assert fraction is not None
    assert (fraction.numerator, fraction.denominator) == (2, 3)
    assert fraction.value == Fraction(2, 3)


def test_average_queue_ahead_is_never_stored_pre_rounded() -> None:
    unit = accumulator()
    unit.queue_ahead_sum = 10
    unit.queue_ahead_samples = 3
    average = unit.build(LedgerState(), winner=None).average_queue_ahead()
    assert average is not None
    assert (average.numerator, average.denominator) == (10, 3)


def test_an_empty_market_reports_no_maker_fraction_rather_than_zero() -> None:
    metrics = accumulator().build(LedgerState(), winner=None)
    assert metrics.fill_count == 0
    assert metrics.maker_fraction() is None
    assert metrics.average_queue_ahead() is None


# -- Term1 / Term2 reuse the existing accounting ------------------------------------------------


def traded() -> LedgerState:
    """120 UP at 0.60, 100 DOWN at 0.50 — the worked example from the accounting module."""
    return (
        LedgerState()
        .apply_fill(
            Fill(
                outcome=Outcome.UP,
                shares=ShareUnits(120_000_000),
                cost=MoneyUnits(72_000_000),
                fee=MoneyUnits(1_000),
                price=PriceUnits(600_000),
            )
        )
        .apply_fill(
            Fill(
                outcome=Outcome.DOWN,
                shares=ShareUnits(100_000_000),
                cost=MoneyUnits(50_000_000),
                fee=MoneyUnits(2_000),
                price=PriceUnits(500_000),
            )
        )
    )


@pytest.mark.parametrize("winner", [Outcome.UP, Outcome.DOWN])
def test_term1_plus_term2_equals_gross_payout_minus_total_cost_exactly(winner: Outcome) -> None:
    ledger = traded()
    metrics = accumulator().build(ledger, winner=winner)
    assert metrics.term1 is not None and metrics.term2 is not None
    identity_value = metrics.term1.value + metrics.term2.value
    assert identity_value == metrics.gross_payout - metrics.total_cost
    assert identity_value.denominator == 1, "the sum of the terms is always an exact amount"


@pytest.mark.parametrize("winner", [Outcome.UP, Outcome.DOWN])
def test_net_pnl_equals_terms_minus_fees_plus_rebate_to_the_last_unit(winner: Outcome) -> None:
    ledger = traded()
    metrics = accumulator().build(ledger, winner=winner, rebate_mode="WITHOUT_REBATE")
    assert metrics.term1 is not None and metrics.term2 is not None
    expected = metrics.term1.value + metrics.term2.value - metrics.fees
    assert metrics.net_pnl == expected
    assert metrics.fees == 3_000


def test_the_metrics_do_not_restate_the_decomposition_formula(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the accounting module is the only source, replacing it must change the answer."""
    ledger = traded()
    real = accumulator().build(ledger, winner=Outcome.UP)
    assert real.term1 is not None

    from maker5m.accounting import decomposition

    expected = decompose(ledger, Outcome.UP)
    assert real.term1.value == expected.term1
    assert real.term2 is not None and real.term2.value == expected.term2
    assert real.matched_shares == expected.matched_shares
    assert decomposition.decompose is decompose


def test_a_loser_heavy_residual_is_reported_on_the_right_side() -> None:
    """The case Canonical §4's literal formula gets wrong, and the corrected form does not."""
    ledger = traded()
    metrics = accumulator().build(ledger, winner=Outcome.DOWN)
    assert metrics.terminal_residual_side == Outcome.UP.value
    assert metrics.terminal_residual_magnitude == 20_000_000


def test_an_unsettled_market_reports_no_terms_rather_than_zero() -> None:
    metrics = accumulator().build(traded(), winner=None)
    assert metrics.settled is False
    assert metrics.term1 is None and metrics.term2 is None
    assert metrics.winner is None
    assert metrics.terminal_inventory == 20_000_000


# -- rebate honesty ------------------------------------------------------------------------------


def test_the_rebate_mode_behind_a_reported_pnl_is_always_named() -> None:
    """O07 is open, so which rebate view produced a number is never left implicit."""
    ledger = traded()
    without = accumulator().build(ledger, winner=Outcome.UP, rebate_mode="WITHOUT_REBATE")
    estimated = accumulator().build(ledger, winner=Outcome.UP, rebate_mode="ESTIMATED_REBATE")
    assert without.rebate_mode == "WITHOUT_REBATE"
    assert estimated.rebate_mode == "ESTIMATED_REBATE"
    assert without.estimated_rebates == estimated.estimated_rebates
    assert without.realised_rebates == estimated.realised_rebates == 0


def test_an_estimated_rebate_never_becomes_a_realised_one() -> None:
    ledger = traded().set_estimated_rebate(MoneyUnits(5_000))
    metrics = accumulator().build(ledger, winner=Outcome.UP, rebate_mode="ESTIMATED_REBATE")
    assert metrics.estimated_rebates == 5_000
    assert metrics.realised_rebates == 0
    assert metrics.net_pnl != accumulator().build(ledger, winner=Outcome.UP).net_pnl


# -- action counting ------------------------------------------------------------------------------


def test_every_typed_action_is_counted_separately() -> None:
    from maker5m.execution.reconciler import ReconcileAction

    unit = accumulator()
    for action in ReconcileAction:
        record = build_decision_record(
            observation(action=action), identity(), persistence_sequence=1
        )
        unit.observe_decision(record)
    metrics = unit.build(LedgerState(), winner=None)
    total = (
        metrics.place_count
        + metrics.keep_count
        + metrics.cancel_count
        + metrics.replace_count
        + metrics.wait_count
        + metrics.nothing_count
        + metrics.blocked_count
    )
    assert total == 2 * len(ReconcileAction), "two sides per decision, every action counted"
    assert metrics.cancel_count == 2
    assert metrics.replace_count == 2


def test_quote_count_ambiguity_is_exposed_rather_than_resolved_silently() -> None:
    """Canonical §26 says 'quote count' and does not define it. Both readings are stored."""
    from maker5m.execution.reconciler import ReconcileAction

    unit = accumulator()
    for _ in range(5):
        unit.observe_decision(
            build_decision_record(
                observation(action=ReconcileAction.KEEP),
                identity(),
                persistence_sequence=1,
            )
        )
    metrics = unit.build(LedgerState(), winner=None)
    assert metrics.place_count == 0
    assert metrics.quote_intent_count == 10
    assert metrics.keep_count == 10


# -- fill records ---------------------------------------------------------------------------------


def _fill_record(liquidity: Liquidity = Liquidity.MAKER) -> object:
    return build_fill_record(fill_capture(liquidity), identity(), persistence_sequence=1)


def test_a_fill_record_captures_both_ledger_states_rather_than_reversing_the_fill() -> None:
    record = _fill_record()
    assert record.total_cost_before == 0  # type: ignore[attr-defined]
    assert record.total_cost_after == 4_900_000  # type: ignore[attr-defined]
    assert record.n_up_before == 0  # type: ignore[attr-defined]
    assert record.n_up_after == 10_000_000  # type: ignore[attr-defined]
    assert record.fees_before == 0  # type: ignore[attr-defined]
    assert record.fees_after == 1_234  # type: ignore[attr-defined]
    assert record.pnl_if_up_before != record.pnl_if_up_after  # type: ignore[attr-defined]


def test_a_large_token_id_survives_as_text() -> None:
    record = _fill_record()
    assert record.token_id == UP_TOKEN  # type: ignore[attr-defined]
    assert int(record.token_id) > 2**63  # type: ignore[attr-defined]


def test_a_shadow_fill_is_never_recorded_as_a_venue_fill() -> None:
    record = _fill_record()
    assert record.provenance == FillProvenance.SHADOW_MODEL.value  # type: ignore[attr-defined]
    assert record.provenance != FillProvenance.REAL_VENUE.value  # type: ignore[attr-defined]


@pytest.mark.parametrize("liquidity", list(Liquidity))
def test_liquidity_is_recorded_exactly_as_reported(liquidity: Liquidity) -> None:
    """P9 owns the halt on a taker fill. P11 records it and does not repair it."""
    record = _fill_record(liquidity)
    assert record.liquidity == liquidity.value  # type: ignore[attr-defined]


def test_unknown_liquidity_is_counted_apart_from_maker_and_taker() -> None:
    unit = accumulator()
    unit.observe_fill(_fill_record(Liquidity.UNKNOWN))
    metrics = unit.build(LedgerState(), winner=None)
    assert metrics.unknown_liquidity_fill_count == 1
    assert metrics.maker_fill_count == 0
    fraction = metrics.maker_fraction()
    assert fraction is not None
    assert fraction.numerator == 0, "unknown liquidity is not silently counted as maker"
