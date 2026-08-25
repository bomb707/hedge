"""Deterministic integer time.

Plane 2 never reads a clock (invariant I20). Time is input data: it arrives on an event and
drives a pure transition. That is what makes replay bit-exact, and it is why nothing in
``market/``, ``strategy/``, ``accounting/``, or ``numeric/`` may import ``time`` or
``datetime`` -- a rule enforced statically by ``tests/integration/test_plane2_purity.py``.

Resolution is **integer nanoseconds**. Floats are excluded for the same reason they are
excluded from the ledger: ``0.1`` is not representable, comparison becomes approximate, and
phase boundaries would stop being exact. Nanoseconds are chosen because both candidate
sources speak them natively (``time.monotonic_ns``, ``time.time_ns``) and because exchange
timestamps in milliseconds or microseconds convert up exactly, never down.

Epoch and timezone are deliberately **not** modelled here. A ``TimestampNs`` is an opaque
ordered integer to Plane 2; interpreting it as Unix time, or rendering it in a timezone,
belongs to adapters (P6) and the UI (P12).

P8 will need to distinguish source, local-receive, and monotonic-receive timestamps for
latency attribution. That is deliberately not built now: P2 defines only the single ordering
timestamp the deterministic core requires.
"""

from typing import Final, NewType

from maker5m.numeric.errors import DomainError

__all__ = [
    "NANOS_PER_MICRO",
    "NANOS_PER_MILLI",
    "NANOS_PER_SECOND",
    "DurationNs",
    "TimestampNs",
    "millis",
    "require_non_negative_timestamp",
    "seconds",
]

TimestampNs = NewType("TimestampNs", int)
"""A point in time, in integer nanoseconds, on an epoch Plane 2 does not interpret."""

DurationNs = NewType("DurationNs", int)
"""A span of time, in integer nanoseconds. Distinct from a point in time."""

NANOS_PER_MICRO: Final[int] = 1_000
NANOS_PER_MILLI: Final[int] = 1_000_000
NANOS_PER_SECOND: Final[int] = 1_000_000_000


def seconds(count: int) -> DurationNs:
    """Exact duration from whole seconds."""
    return DurationNs(count * NANOS_PER_SECOND)


def millis(count: int) -> DurationNs:
    """Exact duration from whole milliseconds."""
    return DurationNs(count * NANOS_PER_MILLI)


def require_non_negative_timestamp(value: TimestampNs, *, field: str) -> None:
    """Reject a negative timestamp where the domain forbids one."""
    if value < 0:
        raise DomainError(f"{field}: timestamp must not be negative, got {value}")
