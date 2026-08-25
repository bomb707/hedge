"""Eligibility by intersection, with typed reasons and a strictly one-sided hard band."""

from __future__ import annotations

import pytest

from maker5m.numeric import ShareUnits, parse_share
from maker5m.strategy import (
    DEFAULT_BAND_HARD,
    EligibilityReason,
    EligibilityResult,
    StrategyError,
    evaluate_eligibility,
)

HARD = DEFAULT_BAND_HARD


def sh(text: str) -> ShareUnits:
    return parse_share(text)


def evaluate(
    inventory: str = "0",
    *,
    quoting: bool = True,
    centre: bool = True,
    up: bool | None = None,
    down: bool | None = None,
    band_hard: ShareUnits = HARD,
) -> EligibilityResult:
    return evaluate_eligibility(
        quoting_phase=quoting,
        centre_available=centre,
        inventory=sh(inventory),
        band_hard=band_hard,
        endgame_up_allowed=up,
        endgame_down_allowed=down,
    )


def test_nothing_blocking_means_both_sides_live() -> None:
    result = evaluate()
    assert result.up_allowed and result.down_allowed
    assert result.up_reasons == () and result.down_reasons == ()


def test_non_quoting_phase_blocks_both_sides() -> None:
    result = evaluate(quoting=False)
    assert not result.up_allowed and not result.down_allowed
    assert result.up_reasons == (EligibilityReason.PHASE_NOT_QUOTING,)
    assert result.down_reasons == (EligibilityReason.PHASE_NOT_QUOTING,)


def test_unavailable_centre_blocks_both_sides_with_its_own_reason() -> None:
    result = evaluate(centre=False)
    assert result.up_reasons == (EligibilityReason.CENTRE_UNAVAILABLE,)
    assert result.down_reasons == (EligibilityReason.CENTRE_UNAVAILABLE,)


def test_endgame_gate_blocks_only_the_side_it_names() -> None:
    result = evaluate(up=False, down=True)
    assert not result.up_allowed
    assert result.down_allowed
    assert result.up_reasons == (EligibilityReason.ENDGAME_GATE,)


def test_endgame_none_means_the_gate_does_not_apply() -> None:
    """Outside ENDGAME the gate is absent, which is not the same as present-and-permitting."""
    assert evaluate(up=None, down=None).up_allowed


# -- the one-sided hard band ----------------------------------------------------------------


@pytest.mark.parametrize("inventory", ["100", "100.000001", "150", "1000"])
def test_hard_band_at_or_above_the_wall_blocks_up_only(inventory: str) -> None:
    """A4: the inward side stays live. ``abs(I) >= band_hard -> no orders`` would be wrong."""
    result = evaluate(inventory)
    assert not result.up_allowed
    assert result.up_reasons == (EligibilityReason.HARD_BAND,)
    assert result.down_allowed
    assert result.down_reasons == ()


@pytest.mark.parametrize("inventory", ["-100", "-100.000001", "-150", "-1000"])
def test_hard_band_at_or_below_the_wall_blocks_down_only(inventory: str) -> None:
    result = evaluate(inventory)
    assert not result.down_allowed
    assert result.down_reasons == (EligibilityReason.HARD_BAND,)
    assert result.up_allowed


@pytest.mark.parametrize("inventory", ["99.999999", "-99.999999", "0"])
def test_just_inside_the_wall_both_sides_stay_live(inventory: str) -> None:
    result = evaluate(inventory)
    assert result.up_allowed and result.down_allowed


def test_the_wall_is_inclusive_at_exactly_the_band() -> None:
    assert not evaluate("100").up_allowed
    assert evaluate("99.999999").up_allowed


# -- intersection ---------------------------------------------------------------------------


def test_endgame_permits_but_hard_band_blocks() -> None:
    result = evaluate("100", up=True, down=True)
    assert not result.up_allowed
    assert result.up_reasons == (EligibilityReason.HARD_BAND,)
    assert result.down_allowed


def test_hard_band_permits_but_endgame_blocks() -> None:
    result = evaluate("40", up=False, down=True)
    assert not result.up_allowed
    assert result.up_reasons == (EligibilityReason.ENDGAME_GATE,)
    assert result.down_allowed


def test_both_gates_block_the_same_side_and_both_reasons_are_recorded() -> None:
    """Reachable in ENDGAME with a DOWN favourite and inventory at the upper wall."""
    result = evaluate("100", up=False, down=True)
    assert not result.up_allowed
    assert result.up_reasons == (
        EligibilityReason.ENDGAME_GATE,
        EligibilityReason.HARD_BAND,
    )


def test_both_sides_blocked_for_different_reasons_is_unreachable_with_a_sane_config() -> None:
    """A property worth recording rather than a case worth faking.

    The hard band can only bite on one side at a time, and the endgame gate's two strict
    inequalities cannot both fail at once. Blocking UP needs ``I >= band_hard`` or
    ``I >= target + band``; blocking DOWN needs ``I <= -band_hard`` or ``I <= target - band``.
    With ``band_hard > tilt`` enforced by StrategyConfig, no inventory satisfies one of each.
    """
    from maker5m.domain import Outcome
    from maker5m.strategy import DEFAULT_ENDGAME_BAND, DEFAULT_ENDGAME_TILT, endgame_target

    step = 250_000  # 0.25 shares
    for favourite in (Outcome.UP, Outcome.DOWN):
        target = endgame_target(favourite, DEFAULT_ENDGAME_TILT)
        for units in range(-200 * 1_000_000, 200 * 1_000_000 + 1, step):
            inventory = ShareUnits(units)
            distance = inventory - target
            result = evaluate_eligibility(
                quoting_phase=True,
                centre_available=True,
                inventory=inventory,
                band_hard=HARD,
                endgame_up_allowed=distance < DEFAULT_ENDGAME_BAND,
                endgame_down_allowed=distance > -DEFAULT_ENDGAME_BAND,
            )
            assert result.up_allowed or result.down_allowed, (
                f"both sides blocked at I={inventory} with favourite {favourite}"
            )


def test_reasons_are_typed_not_free_text() -> None:
    result = evaluate("100")
    assert all(isinstance(r, EligibilityReason) for r in result.up_reasons)


def test_result_rejects_an_inconsistent_allowed_flag() -> None:
    with pytest.raises(StrategyError):
        EligibilityResult(
            up_allowed=True, down_allowed=True, up_reasons=(EligibilityReason.HARD_BAND,)
        )
    with pytest.raises(StrategyError):
        EligibilityResult(up_allowed=False, down_allowed=True)
