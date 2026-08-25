"""Deterministic monitors that turn observations into risk conditions.

None of these reads a clock. ``now_ns`` arrives as an argument everywhere, which is what makes a
replayed run produce the same halts as the live run that recorded it (invariant I20). A monitor
that consulted ``time.time()`` would make the safety verdict depend on when the replay happened.
"""

from bisect import bisect_left
from dataclasses import dataclass, field

from maker5m.market.timebase import DurationNs, TimestampNs
from maker5m.risk.config import RiskConfig

__all__ = ["ApiErrorMonitor", "ApiOutcome", "clock_drift_exceeded"]


class ApiOutcome:
    """Namespace for the two classifications, kept as plain booleans at call sites."""

    SUCCESS = True
    FAILURE = False


@dataclass(slots=True)
class ApiErrorMonitor:
    """Sliding-window failure counter over authenticated/venue API calls.

    Canonical §28.1 says "API errors exceed threshold", which requires distinguishing a single
    transient failure from a sustained rate. A bare counter cannot: it would either latch on the
    first timeout or reset on the first success. So failures are kept with their timestamps and
    aged out of a fixed window.

    A success does **not** erase the window. That matters: a venue that fails four times and
    succeeds once has not become healthy, and letting one success clear the record would make
    the threshold unreachable in exactly the flapping case it exists to catch.

    Queries do not mutate. An earlier draft aged failures out inside ``failures_in_window``,
    which meant asking about a later time silently destroyed the ability to ask about an
    earlier one — the answer depended on the order the questions were asked in. Failures are
    now discarded only when a new one is recorded, and queries count with a binary search over
    the timestamps, which are appended in order.
    """

    window: DurationNs
    threshold: int
    failures: list[TimestampNs] = field(default_factory=list, repr=False)
    total_failures: int = 0
    total_successes: int = 0

    @classmethod
    def from_config(cls, config: RiskConfig) -> "ApiErrorMonitor":
        return cls(window=config.api_error_window, threshold=config.api_error_threshold)

    def record(self, now_ns: TimestampNs, *, success: bool) -> None:
        if success:
            self.total_successes += 1
        else:
            self.total_failures += 1
            self.failures.append(now_ns)
            # Discard only on record, so the list cannot grow without bound while still
            # leaving every query free of side effects.
            cutoff = now_ns - self.window
            first = bisect_left(self.failures, cutoff)
            if first:
                del self.failures[:first]

    def failures_in_window(self, now_ns: TimestampNs) -> int:
        """How many failures fall inside the window ending at ``now_ns``. Read-only."""
        cutoff = now_ns - self.window
        return len(self.failures) - bisect_left(self.failures, cutoff)

    def exceeded(self, now_ns: TimestampNs) -> bool:
        return self.failures_in_window(now_ns) >= self.threshold

    def summary(self) -> dict[str, int]:
        return {
            "window_ns": int(self.window),
            "threshold": self.threshold,
            "failures_retained": len(self.failures),
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
        }


def clock_drift_exceeded(drift_ns: int, limit_ns: DurationNs) -> bool:
    """Absolute drift against a configured limit.

    The drift estimate is supplied, never measured here, and it is a single number in one clock
    domain — P6's ingress clock against an external reference. Nothing in this module subtracts
    a ``perf_counter`` reading from a wall-clock one, which would produce a plausible-looking
    number that means nothing.
    """
    return abs(drift_ns) > limit_ns
