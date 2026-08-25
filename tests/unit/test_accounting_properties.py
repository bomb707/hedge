"""Randomised property coverage of the accounting identities.

Implemented with a seeded ``random.Random`` rather than Hypothesis. The trade-off, recorded
deliberately: Hypothesis would give shrinking and a better-explored input space, but it is a
real dependency and P1 is meant to add none. A fixed seed keeps every run identical, which
also matters because a flaky arithmetic test would be worse than no test. If the input space
here ever proves too narrow, adding Hypothesis as a dev-only dependency is the right fix.

The identities asserted are the ones invariant I01 turns on, plus the Term1/Term2 identity
that Canonical section 35 makes mandatory.
"""

from __future__ import annotations

import dataclasses
import random
from fractions import Fraction

import pytest

from maker5m.accounting import Fill, LedgerState, RebateMode, decompose, settle
from maker5m.market import Outcome
from maker5m.numeric import MoneyUnits, ShareUnits, shares_at_par

SEED = 20260825
LEDGER_COUNT = 400
ALL_MODES = tuple(RebateMode)


def random_ledger(rng: random.Random) -> LedgerState:
    """A ledger built from a random sequence of fractional fills, fees, and rebates."""
    state = LedgerState()
    for _ in range(rng.randint(0, 40)):
        outcome = rng.choice((Outcome.UP, Outcome.DOWN))
        shares = ShareUnits(rng.randint(1, 40_000_000))
        # Cost is drawn independently of a price: the ledger must never assume
        # cost == shares * price (P1 brief section 5).
        cost = MoneyUnits(rng.randint(0, shares))
        fee = MoneyUnits(rng.randint(0, 1_000))
        state = state.apply_fill(Fill(outcome, shares, cost, fee))
    if rng.random() < 0.5:
        state = state.accrue_estimated_rebate(MoneyUnits(rng.randint(0, 2_000_000)))
    if rng.random() < 0.5:
        state = state.accrue_realised_rebate(MoneyUnits(rng.randint(0, 2_000_000)))
    return state


def ledgers() -> list[LedgerState]:
    rng = random.Random(SEED)
    return [random_ledger(rng) for _ in range(LEDGER_COUNT)]


LEDGERS = ledgers()


def test_the_generated_corpus_is_deterministic() -> None:
    """Same seed, same corpus - otherwise a failure here could not be reproduced."""
    assert [dataclasses.astuple(s) for s in ledgers()] == [dataclasses.astuple(s) for s in LEDGERS]


def test_the_corpus_actually_exercises_both_sides() -> None:
    """Guard against the generator silently degenerating into trivial cases."""
    assert any(s.n_up > 0 and s.n_down > 0 for s in LEDGERS)
    assert any(s.net_inventory > 0 for s in LEDGERS)
    assert any(s.net_inventory < 0 for s in LEDGERS)
    assert any(s.fees > 0 for s in LEDGERS)


@pytest.mark.parametrize("mode", ALL_MODES)
def test_hypothetical_pnl_identities_hold_everywhere(mode: RebateMode) -> None:
    for state in LEDGERS:
        rebate = state.rebate(mode)
        assert state.pnl_if_up(mode) == (
            shares_at_par(state.n_up) - state.total_cost - state.fees + rebate
        )
        assert state.pnl_if_down(mode) == (
            shares_at_par(state.n_down) - state.total_cost - state.fees + rebate
        )


def test_structural_identities_hold_everywhere() -> None:
    for state in LEDGERS:
        assert state.total_cost == state.cost_up + state.cost_down
        assert state.net_inventory == state.n_up - state.n_down


def test_every_stored_quantity_is_an_exact_integer() -> None:
    """No binary float may reach ledger state (ARCHITECTURE section 6)."""
    for state in LEDGERS:
        for value in dataclasses.astuple(state):
            assert type(value) is int


def test_term_identity_holds_for_both_winners_everywhere() -> None:
    for state in LEDGERS:
        for winner in (Outcome.UP, Outcome.DOWN):
            d = decompose(state, winner)
            assert d.term1 + d.term2 == d.trading_pnl
            assert isinstance(d.term1 + d.term2, Fraction)


@pytest.mark.parametrize("mode", ALL_MODES)
def test_settlement_agrees_with_the_live_branch_and_the_decomposition(
    mode: RebateMode,
) -> None:
    for state in LEDGERS:
        for winner in (Outcome.UP, Outcome.DOWN):
            result = settle(state, winner, rebate_mode=mode)
            d = decompose(state, winner)
            assert result.net_pnl == state.pnl_if(winner, mode)
            assert d.term1 + d.term2 - result.fees + result.rebate == result.net_pnl


def test_fill_application_is_order_independent() -> None:
    """Reordering the same fills must produce the same ledger, bit for bit.

    Recovery and reconciliation can replay fills in a different order than they arrived, so
    a ledger that depended on order would diverge after a reconnect.
    """
    rng = random.Random(SEED + 1)
    for _ in range(100):
        fills = [
            Fill(
                rng.choice((Outcome.UP, Outcome.DOWN)),
                ShareUnits(rng.randint(1, 10_000_000)),
                MoneyUnits(rng.randint(0, 5_000_000)),
                MoneyUnits(rng.randint(0, 500)),
            )
            for _ in range(rng.randint(2, 15))
        ]
        forward = LedgerState()
        for f in fills:
            forward = forward.apply_fill(f)
        shuffled = list(fills)
        rng.shuffle(shuffled)
        backward = LedgerState()
        for f in shuffled:
            backward = backward.apply_fill(f)
        assert dataclasses.astuple(forward) == dataclasses.astuple(backward)
