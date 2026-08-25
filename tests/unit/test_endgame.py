"""Favourite determination from the raw centre, the target, and the exact gate boundaries."""

from __future__ import annotations

import pytest

from maker5m.domain import Outcome
from maker5m.numeric import PRICE_SCALE, SHARE_SCALE, ShareUnits, parse_price, parse_share
from maker5m.strategy import (
    DEFAULT_ENDGAME_BAND,
    DEFAULT_ENDGAME_TILT,
    RawCentre,
    endgame_target,
    evaluate_endgame,
    favourite_from_centre,
    quantize_centre,
)

TICK = parse_price("0.01")
TILT = DEFAULT_ENDGAME_TILT
BAND = DEFAULT_ENDGAME_BAND


def sh(text: str) -> ShareUnits:
    return parse_share(text)


# -- favourite from the RAW centre ---------------------------------------------------------


@pytest.mark.parametrize(
    ("centre", "expected"),
    [
        ("0.00", Outcome.DOWN),
        ("0.10", Outcome.DOWN),
        ("0.49", Outcome.DOWN),
        ("0.499999", Outcome.DOWN),
        ("0.50", Outcome.DOWN),
        ("0.500001", Outcome.UP),
        ("0.51", Outcome.UP),
        ("0.90", Outcome.UP),
        ("1.00", Outcome.UP),
    ],
)
def test_favourite_below_at_and_above_a_half(centre: str, expected: Outcome) -> None:
    """Canonical §32 writes ``centre > 0.5``, so an exact half is DOWN (A1)."""
    assert favourite_from_centre(RawCentre(parse_price(centre), 1)) is expected


def test_the_mandatory_raw_versus_quantized_divergence() -> None:
    """raw 0.504 quantizes to 0.50, but the favourite must still be UP.

    Canonical §32 computes the raw centre, derives prices from it, and only then compares
    ``centre > 0.5``. Deciding a 30-share terminal residual from a rounding artefact would be
    a real behavioural change, so the comparison uses the unrounded value.
    """
    raw = RawCentre(parse_price("0.504"), 1)
    assert quantize_centre(raw, TICK) == parse_price("0.50")
    assert favourite_from_centre(raw) is Outcome.UP
    # And the reverse direction: raw just below a half that would round up to 0.50.
    raw_down = RawCentre(parse_price("0.496"), 1)
    assert quantize_centre(raw_down, TICK) == parse_price("0.50")
    assert favourite_from_centre(raw_down) is Outcome.DOWN


def test_favourite_is_exact_on_a_half_unit_rational_centre() -> None:
    """A non-integer rational centre either side of a half, with no float anywhere."""
    assert favourite_from_centre(RawCentre(PRICE_SCALE + 1, 2)) is Outcome.UP
    assert favourite_from_centre(RawCentre(PRICE_SCALE - 1, 2)) is Outcome.DOWN
    assert favourite_from_centre(RawCentre(PRICE_SCALE, 2)) is Outcome.DOWN


# -- target -------------------------------------------------------------------------------


def test_default_tilt_is_thirty_shares() -> None:
    assert DEFAULT_ENDGAME_TILT == 30 * SHARE_SCALE
    assert DEFAULT_ENDGAME_BAND == 5 * SHARE_SCALE


def test_up_favourite_targets_positive_tilt() -> None:
    assert endgame_target(Outcome.UP, TILT) == sh("30")


def test_down_favourite_targets_negative_tilt() -> None:
    assert endgame_target(Outcome.DOWN, TILT) == sh("-30")


# -- gate boundaries ------------------------------------------------------------------------
#
# target = +30, band = 5:  up_allowed iff I < 35 ;  down_allowed iff I > 25


@pytest.mark.parametrize(
    ("inventory", "up", "down"),
    [
        ("24.999999", True, False),
        ("25", True, False),
        ("25.000001", True, True),
        ("30", True, True),
        ("34.999999", True, True),
        ("35", False, True),
        ("35.000001", False, True),
        ("0", True, False),
        ("100", False, True),
    ],
)
def test_up_favourite_gate_boundaries(inventory: str, up: bool, down: bool) -> None:
    gate = evaluate_endgame(sh(inventory), Outcome.UP, TILT, BAND)
    assert gate.target == sh("30")
    assert gate.up_allowed is up
    assert gate.down_allowed is down


@pytest.mark.parametrize(
    ("inventory", "up", "down"),
    [
        ("-24.999999", False, True),
        ("-25", False, True),
        ("-25.000001", True, True),
        ("-30", True, True),
        ("-34.999999", True, True),
        ("-35", True, False),
        ("-35.000001", True, False),
        ("0", False, True),
        ("-100", True, False),
    ],
)
def test_down_favourite_gate_boundaries(inventory: str, up: bool, down: bool) -> None:
    """The mirror image, asserted independently rather than assumed by symmetry."""
    gate = evaluate_endgame(sh(inventory), Outcome.DOWN, TILT, BAND)
    assert gate.target == sh("-30")
    assert gate.up_allowed is up
    assert gate.down_allowed is down


def test_the_gate_inequalities_are_strict() -> None:
    """Exactly at a boundary the side is blocked, one unit inside it is live."""
    unit = ShareUnits(1)
    at_upper = evaluate_endgame(sh("35"), Outcome.UP, TILT, BAND)
    assert at_upper.up_allowed is False
    just_inside = evaluate_endgame(ShareUnits(sh("35") - unit), Outcome.UP, TILT, BAND)
    assert just_inside.up_allowed is True

    at_lower = evaluate_endgame(sh("25"), Outcome.UP, TILT, BAND)
    assert at_lower.down_allowed is False
    just_above = evaluate_endgame(ShareUnits(sh("25") + unit), Outcome.UP, TILT, BAND)
    assert just_above.down_allowed is True


def test_distance_is_signed_and_exact() -> None:
    gate = evaluate_endgame(sh("12"), Outcome.UP, TILT, BAND)
    assert gate.distance == sh("-18")
    assert gate.favourite is Outcome.UP


def test_at_the_target_both_sides_are_live() -> None:
    """Detailed §40: the gate constrains excursion, it does not stop quoting at the target."""
    for favourite in (Outcome.UP, Outcome.DOWN):
        target = endgame_target(favourite, TILT)
        gate = evaluate_endgame(target, favourite, TILT, BAND)
        assert gate.distance == 0
        assert gate.up_allowed and gate.down_allowed
