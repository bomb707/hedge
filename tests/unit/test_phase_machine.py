"""Exact phase boundaries. Integer nanoseconds, half-open intervals, no epsilon."""

from __future__ import annotations

import pytest

from maker5m.market import (
    CANONICAL_PHASE_CONFIG,
    Phase,
    PhaseConfig,
    TimestampNs,
    phase_at,
)
from maker5m.market.errors import MarketDefinitionError
from maker5m.market.timebase import NANOS_PER_SECOND, seconds

T0 = TimestampNs(1_700_000_000 * NANOS_PER_SECOND)
CFG = CANONICAL_PHASE_CONFIG


def at(offset_ns: int) -> TimestampNs:
    return TimestampNs(T0 + offset_ns)


def test_canonical_offsets_are_the_confirmed_values() -> None:
    assert CFG.quote_start_offset == seconds(3)
    assert CFG.endgame_offset == seconds(240)
    assert CFG.stop_quoting_offset == seconds(280)
    assert CFG.duration == seconds(300)
    assert CFG.version == "canonical-v1"


@pytest.mark.parametrize(
    ("boundary_seconds", "before", "on_and_after"),
    [
        (3, Phase.PREARM, Phase.QUOTE),
        (240, Phase.QUOTE, Phase.ENDGAME),
        (280, Phase.ENDGAME, Phase.SETTLING),
        (300, Phase.SETTLING, Phase.DONE),
    ],
)
def test_each_boundary_to_the_nanosecond(
    boundary_seconds: int, before: Phase, on_and_after: Phase
) -> None:
    """boundary - 1ns, exactly the boundary, boundary + 1ns.

    Intervals are half-open: an event exactly at T0+240s is already ENDGAME.
    """
    b = boundary_seconds * NANOS_PER_SECOND
    assert phase_at(T0, at(b - 1), CFG) is before
    assert phase_at(T0, at(b), CFG) is on_and_after
    assert phase_at(T0, at(b + 1), CFG) is on_and_after


def test_exactly_t0_is_prearm() -> None:
    assert phase_at(T0, T0, CFG) is Phase.PREARM


def test_before_t0_is_prearm() -> None:
    """The next market is discovered and pre-armed during the previous window."""
    assert phase_at(T0, at(-seconds(120)), CFG) is Phase.PREARM
    assert phase_at(T0, TimestampNs(0), CFG) is Phase.PREARM


def test_long_after_close_stays_done() -> None:
    assert phase_at(T0, at(seconds(10_000)), CFG) is Phase.DONE


@pytest.mark.parametrize(
    ("offset_s", "expected"),
    [
        (0, Phase.PREARM),
        (2, Phase.PREARM),
        (3, Phase.QUOTE),
        (7, Phase.QUOTE),
        (239, Phase.QUOTE),
        (240, Phase.ENDGAME),
        (255, Phase.ENDGAME),
        (279, Phase.ENDGAME),
        (280, Phase.SETTLING),
        (299, Phase.SETTLING),
        (300, Phase.DONE),
    ],
)
def test_representative_offsets(offset_s: int, expected: Phase) -> None:
    assert phase_at(T0, at(seconds(offset_s)), CFG) is expected


def test_phase_is_monotonic_across_the_window() -> None:
    order = [Phase.PREARM, Phase.QUOTE, Phase.ENDGAME, Phase.SETTLING, Phase.DONE]
    seen = -1
    for second in range(-5, 305):
        index = order.index(phase_at(T0, at(seconds(second)), CFG))
        assert index >= seen, f"phase went backwards at T0+{second}s"
        seen = index


def test_phase_is_independent_of_absolute_epoch() -> None:
    """Only the offset from T0 matters, so a different epoch cannot change behaviour."""
    other_t0 = TimestampNs(42)
    for second in (0, 3, 240, 280, 300):
        assert phase_at(T0, at(seconds(second)), CFG) is phase_at(
            other_t0, TimestampNs(other_t0 + seconds(second)), CFG
        )


# -- configuration validation ------------------------------------------------------------


@pytest.mark.parametrize(
    ("quote", "endgame", "stop", "duration"),
    [
        (240, 240, 280, 300),  # quote == endgame
        (3, 280, 240, 300),  # endgame after stop
        (3, 240, 300, 300),  # stop == duration
        (3, 240, 280, 280),  # duration == stop
        (300, 240, 280, 300),  # quote after everything
    ],
)
def test_offsets_must_be_strictly_increasing(
    quote: int, endgame: int, stop: int, duration: int
) -> None:
    with pytest.raises(MarketDefinitionError):
        PhaseConfig(
            quote_start_offset=seconds(quote),
            endgame_offset=seconds(endgame),
            stop_quoting_offset=seconds(stop),
            duration=seconds(duration),
            version="bad",
        )


def test_negative_quote_start_is_rejected() -> None:
    with pytest.raises(MarketDefinitionError):
        PhaseConfig(seconds(-1), seconds(240), seconds(280), seconds(300), "bad")


def test_config_version_is_required_for_replay_identity() -> None:
    with pytest.raises(MarketDefinitionError):
        PhaseConfig(seconds(3), seconds(240), seconds(280), seconds(300), "")


def test_config_is_immutable() -> None:
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        CFG.duration = seconds(1)  # type: ignore[misc]
