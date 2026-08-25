"""The 5-share lattice, both O04 policies, and every rounding boundary."""

from __future__ import annotations

import dataclasses

import pytest

from maker5m.domain import ParameterStatus
from maker5m.numeric import SHARE_SCALE, DomainError, ShareUnits, parse_share
from maker5m.strategy import (
    GRID,
    GRID_POLICY_STATUS,
    REFERENCE_GRID_POLICY,
    REFERENCE_GRID_ROUNDING,
    BaseLot,
    GridPlan,
    GridPolicy,
    GridRounding,
    is_on_grid,
    plan_grid,
    round_to_grid,
)

L15 = BaseLot.of(15)


def sh(text: str) -> ShareUnits:
    return parse_share(text)


def test_grid_is_exactly_five_shares() -> None:
    assert GRID == 5 * SHARE_SCALE == 5_000_000


def test_the_grid_policy_is_labelled_open() -> None:
    """O04: both readings are implemented; neither is proven."""
    assert GRID_POLICY_STATUS is ParameterStatus.OPEN


# -- the mandatory O04 worked examples -----------------------------------------------------


def test_canonical_offset_policy_worked_example() -> None:
    """Canonical §12.1 with I = -28.63, L = 15."""
    plan = plan_grid(sh("-28.63"), L15, GridPolicy.CANONICAL_OFFSET)
    assert plan.up_target == sh("-15")
    assert plan.up_size == sh("13.63")
    assert plan.down_target == sh("-45")
    assert plan.down_size == sh("16.37")


def test_observed_adjacent_policy_worked_example() -> None:
    """Detailed §12 and d3_grid.png with the same I = -28.63, L = 15."""
    plan = plan_grid(sh("-28.63"), L15, GridPolicy.OBSERVED_ADJACENT)
    assert plan.up_target == sh("-15")
    assert plan.up_size == sh("13.63")
    assert plan.down_target == sh("-30")
    assert plan.down_size == sh("1.37")


def test_the_two_policies_agree_on_the_up_side_and_disagree_on_the_down_side() -> None:
    """The exact shape of the O04 conflict, pinned so it cannot quietly disappear."""
    inventory = sh("-28.63")
    canonical = plan_grid(inventory, L15, GridPolicy.CANONICAL_OFFSET)
    observed = plan_grid(inventory, L15, GridPolicy.OBSERVED_ADJACENT)
    assert canonical.up_target == observed.up_target
    assert canonical.up_size == observed.up_size
    assert canonical.down_target != observed.down_target
    assert canonical.down_size != observed.down_size
    assert canonical.down_size - observed.down_size == sh("15")


def test_neither_policy_is_the_default_by_evidence() -> None:
    """Precedence decides which one runs, not which one is right."""
    assert REFERENCE_GRID_POLICY is GridPolicy.CANONICAL_OFFSET
    assert plan_grid(sh("-28.63"), L15).down_target == sh("-45")


# -- grid rounding boundaries --------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", "0"),
        ("5", "5"),
        ("-5", "-5"),
        ("2.49", "0"),
        ("2.51", "5"),
        ("-2.49", "0"),
        ("-2.51", "-5"),
        ("7.49", "5"),
        ("7.51", "10"),
        ("-28.63", "-30"),
        ("0.000001", "0"),
        ("4.999999", "5"),
        ("-0.000001", "0"),
    ],
)
def test_round_to_grid_non_ties(value: str, expected: str) -> None:
    assert round_to_grid(sh(value)) == sh(expected)


@pytest.mark.parametrize(
    ("value", "half_even", "half_up", "half_down"),
    [
        ("2.5", "0", "5", "0"),
        ("7.5", "10", "10", "5"),
        ("12.5", "10", "15", "10"),
        ("-2.5", "0", "0", "-5"),
        ("-7.5", "-10", "-5", "-10"),
        ("-27.5", "-30", "-25", "-30"),
    ],
)
def test_round_to_grid_ties_are_policy_dependent(
    value: str, half_even: str, half_up: str, half_down: str
) -> None:
    """Ties are reachable with real fractional inventories, so the rule must be named."""
    assert round_to_grid(sh(value), GridRounding.HALF_EVEN) == sh(half_even)
    assert round_to_grid(sh(value), GridRounding.HALF_UP) == sh(half_up)
    assert round_to_grid(sh(value), GridRounding.HALF_DOWN) == sh(half_down)


def test_reference_grid_rounding_is_named_not_inherited_from_python() -> None:
    assert REFERENCE_GRID_ROUNDING is GridRounding.HALF_EVEN


def test_is_on_grid() -> None:
    assert is_on_grid(sh("0"))
    assert is_on_grid(sh("-30"))
    assert is_on_grid(sh("25"))
    assert not is_on_grid(sh("-28.63"))
    assert not is_on_grid(sh("0.000001"))


# -- boundary correction --------------------------------------------------------------------


def test_targets_are_always_strictly_beyond_current_inventory() -> None:
    """A target at or beyond the wrong side of I would give a zero or negative size."""
    for policy in GridPolicy:
        for text in ("0", "5", "-5", "-30", "100", "-100", "2.5", "-2.5", "0.000001"):
            plan = plan_grid(sh(text), L15, policy)
            assert plan.up_target > plan.inventory
            assert plan.down_target < plan.inventory


def test_inventory_exactly_on_the_lattice() -> None:
    canonical = plan_grid(sh("-30"), L15, GridPolicy.CANONICAL_OFFSET)
    assert canonical.up_target == sh("-15")
    assert canonical.up_size == sh("15")
    assert canonical.down_target == sh("-45")
    assert canonical.down_size == sh("15")

    observed = plan_grid(sh("-30"), L15, GridPolicy.OBSERVED_ADJACENT)
    assert observed.down_target == sh("-35")
    assert observed.down_size == sh("5")


def test_zero_inventory() -> None:
    for policy in GridPolicy:
        plan = plan_grid(sh("0"), L15, policy)
        assert plan.up_target == sh("15")
        assert plan.up_size == sh("15")
    assert plan_grid(sh("0"), L15, GridPolicy.CANONICAL_OFFSET).down_target == sh("-15")
    assert plan_grid(sh("0"), L15, GridPolicy.OBSERVED_ADJACENT).down_target == sh("-5")


def test_positive_inventory_is_symmetric_under_the_canonical_policy() -> None:
    plan = plan_grid(sh("28.63"), L15, GridPolicy.CANONICAL_OFFSET)
    assert plan.up_target == sh("45")
    assert plan.up_size == sh("16.37")
    assert plan.down_target == sh("15")
    assert plan.down_size == sh("13.63")


@pytest.mark.parametrize("whole", [15, 20, 25])
def test_every_supported_base_lot_produces_a_valid_plan(whole: int) -> None:
    for policy in GridPolicy:
        plan = plan_grid(sh("-28.63"), BaseLot.of(whole), policy)
        assert plan.up_size > 0
        assert plan.down_size > 0
        assert is_on_grid(plan.up_target)
        assert is_on_grid(plan.down_target)


def test_very_small_fractional_offsets_are_handled_exactly() -> None:
    for policy in GridPolicy:
        plan = plan_grid(ShareUnits(1), L15, policy)  # 0.000001 shares
        assert plan.up_size > 0
        assert plan.down_size > 0
        assert is_on_grid(plan.up_target)
        assert is_on_grid(plan.down_target)


def test_inventories_near_the_hard_band_are_sized_normally_here() -> None:
    """band_hard is P4/P9 work; the grid itself imposes no limit."""
    for policy in GridPolicy:
        for text in ("99.99", "-99.99", "100", "-100", "150", "-150"):
            plan = plan_grid(sh(text), L15, policy)
            assert plan.up_size > 0
            assert plan.down_size > 0


# -- plan validation -------------------------------------------------------------------------


def test_grid_plan_rejects_a_non_positive_size() -> None:
    with pytest.raises(DomainError):
        GridPlan(
            policy=GridPolicy.CANONICAL_OFFSET,
            inventory=sh("0"),
            base_lot=L15,
            up_target=sh("0"),
            down_target=sh("-5"),
            up_size=sh("0"),
            down_size=sh("5"),
        )


def test_grid_plan_rejects_an_off_lattice_target() -> None:
    with pytest.raises(DomainError):
        GridPlan(
            policy=GridPolicy.CANONICAL_OFFSET,
            inventory=sh("0"),
            base_lot=L15,
            up_target=sh("15.5"),
            down_target=sh("-15"),
            up_size=sh("15.5"),
            down_size=sh("15"),
        )


def test_grid_plan_is_immutable() -> None:
    plan = plan_grid(sh("-28.63"), L15)
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.up_size = sh("1")  # type: ignore[misc]


def test_plan_grid_takes_inventory_explicitly_and_keeps_no_state() -> None:
    """Purity: no second copy of inventory exists anywhere."""
    first = plan_grid(sh("-28.63"), L15)
    second = plan_grid(sh("-28.63"), L15)
    assert first == second
