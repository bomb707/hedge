"""Token-bucket rate limiting, with cancel capacity that cannot be starved.

Canonical §20.1 is explicit: **no fixed requote delay.** A ``min_requote_ms`` would add
latency to precisely the requotes that matter most. So the bucket is free under normal
activity and only constrains genuine excess.

Two design points worth stating:

* **Cancels get reserved capacity.** If placements could exhaust the whole bucket, a cancel
  could be starved indefinitely — and a cancel is how the strategy stops quoting at SETTLING
  and how it retires an unsafe order. Correctness depends on being able to withdraw.
* **Time is an argument, never read here.** The bucket is pure so it can be tested
  deterministically; the impure executor supplies ``now_ns`` (I20).

The rate itself is ``OPERATIONAL`` engineering configuration. Canonical §20 offers ~8
requotes/s as an example operational budget and explicitly labels it operational, not a
documented venue limit and not target-wallet evidence.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Final

from maker5m.domain import ParameterStatus
from maker5m.market.timebase import NANOS_PER_SECOND, TimestampNs

__all__ = [
    "DEFAULT_CANCEL_RESERVE",
    "DEFAULT_RATE_PER_SECOND",
    "RATE_LIMIT_STATUS",
    "RateDecision",
    "RequestClass",
    "TokenBucket",
]

DEFAULT_RATE_PER_SECOND: Final[int] = 8
"""Canonical §20's example operational budget. Not a venue-documented limit."""

DEFAULT_BURST: Final[int] = 8
DEFAULT_CANCEL_RESERVE: Final[int] = 2
"""Capacity placements may never consume, so a cancel is always issuable."""

RATE_LIMIT_STATUS: Final = ParameterStatus.OPERATIONAL


class RequestClass(Enum):
    """What a request is for. Cancels are privileged."""

    PLACE = "PLACE"
    CANCEL = "CANCEL"


class RateDecision(Enum):
    ALLOWED = "ALLOWED"
    DEFERRED = "DEFERRED"
    """No capacity right now. The action is suppressed, never queued behind a sleep."""


@dataclass(slots=True)
class TokenBucket:
    """Counts actual network requests, not market-data events.

    Capacity refills continuously rather than in discrete windows, so a normal request never
    waits for a window boundary.
    """

    rate_per_second: int = DEFAULT_RATE_PER_SECOND
    burst: int = DEFAULT_BURST
    cancel_reserve: int = DEFAULT_CANCEL_RESERVE
    _tokens: float = -1.0
    _last_ns: int = -1

    def __post_init__(self) -> None:
        if self.rate_per_second <= 0:
            raise ValueError(f"rate_per_second must be positive, got {self.rate_per_second}")
        if self.burst <= 0:
            raise ValueError(f"burst must be positive, got {self.burst}")
        if not 0 <= self.cancel_reserve < self.burst:
            raise ValueError(f"cancel_reserve must lie in [0, burst), got {self.cancel_reserve}")
        if self._tokens < 0:
            self._tokens = float(self.burst)

    def _refill(self, now_ns: TimestampNs) -> None:
        if self._last_ns < 0:
            self._last_ns = int(now_ns)
            return
        elapsed_ns = int(now_ns) - self._last_ns
        if elapsed_ns <= 0:
            return
        self._last_ns = int(now_ns)
        self._tokens = min(
            float(self.burst),
            self._tokens + (elapsed_ns / NANOS_PER_SECOND) * self.rate_per_second,
        )

    def available(self, now_ns: TimestampNs) -> float:
        self._refill(now_ns)
        return self._tokens

    def acquire(self, request: RequestClass, now_ns: TimestampNs) -> RateDecision:
        """Take one token if the request's own floor permits it.

        A placement may only draw the bucket down to ``cancel_reserve``; a cancel may draw it
        to zero. That is what keeps cancels issuable under sustained placement pressure.
        """
        self._refill(now_ns)
        floor = 0.0 if request is RequestClass.CANCEL else float(self.cancel_reserve)
        if self._tokens - 1.0 < floor - 1e-9:
            return RateDecision.DEFERRED
        self._tokens -= 1.0
        return RateDecision.ALLOWED
