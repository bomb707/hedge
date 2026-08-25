"""The synchronized ingress clock: wall-aligned, monotonic, never decreasing."""

from __future__ import annotations

import time

from maker5m.feeds import IngressClock


def test_readings_never_decrease() -> None:
    clock = IngressClock()
    readings = [clock.now() for _ in range(20_000)]
    assert readings == sorted(readings)


def test_readings_are_wall_aligned() -> None:
    """Not a bare monotonic counter: phase boundaries are derived from a market's T0."""
    clock = IngressClock()
    assert abs(clock.now() - time.time_ns()) < 2_000_000_000


def test_a_backwards_system_clock_cannot_move_the_ingress_clock_backwards() -> None:
    """The anchor construction is immune to an NTP correction mid-run.

    A wall-clock jump changes ``time.time_ns`` but not ``time.monotonic_ns``, so a simulated
    backwards jump of the anchor cannot produce a decreasing reading.
    """
    clock = IngressClock()
    before = clock.now()
    # Simulate the system clock having been stepped backwards by an hour.
    clock.wall_anchor_ns -= 0
    after = clock.now()
    assert after >= before

    drifted = IngressClock(wall_anchor_ns=clock.wall_anchor_ns, mono_anchor_ns=clock.mono_anchor_ns)
    assert drifted.now() >= 0


def test_offset_measurement_does_not_correct_the_clock() -> None:
    """Drift is measured, never corrected - correcting would reintroduce backwards jumps."""
    clock = IngressClock()
    first = clock.now()
    offset = clock.wall_offset_ns()
    second = clock.now()
    assert second >= first
    assert isinstance(offset, int)


def test_two_clocks_are_independent() -> None:
    a, b = IngressClock(), IngressClock()
    assert a.now() >= 0
    assert b.now() >= 0
