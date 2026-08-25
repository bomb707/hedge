"""Deterministic trace sampling.

If tracing every book update costs measurably more than it reveals, sampling is allowed — but
it must be **deterministic**, so a replayed run samples exactly the same events. Selection is
``ingress_ordinal % sample_every == 0``; there is no randomness anywhere.

Some events are always traced regardless of the sampling rate, because they are rare and
individually load-bearing: anything that issues a network request, fills, order
acknowledgements and rejections, health failures, and phase boundaries. Sampling exists to
thin out high-frequency book churn, not to hide the interesting cases.

The policy is ``OPERATIONAL`` engineering configuration and is recorded in the run manifest.
It must never be used to make a latency distribution look better than it is.
"""

from dataclasses import dataclass
from typing import Final

from maker5m.domain import ParameterStatus

__all__ = ["ALWAYS_TRACED_KINDS", "SAMPLING_STATUS", "SamplingPolicy"]

SAMPLING_STATUS: Final = ParameterStatus.OPERATIONAL

ALWAYS_TRACED_KINDS: Final[frozenset[str]] = frozenset(
    {"OwnFill", "OrderStateEvent", "PhaseEvent", "HealthEvent"}
)
"""Rare and individually significant. Never thinned out."""


@dataclass(frozen=True, slots=True)
class SamplingPolicy:
    """Deterministic selection. ``sample_every = 1`` traces everything."""

    sample_every: int = 1

    def __post_init__(self) -> None:
        if self.sample_every < 1:
            raise ValueError(f"sample_every must be >= 1, got {self.sample_every}")

    def should_trace(self, *, ingress_ordinal: int, event_kind: str, forced: bool) -> bool:
        """``forced`` covers anything that issued a network request."""
        if forced or event_kind in ALWAYS_TRACED_KINDS:
            return True
        if self.sample_every == 1:
            return True
        return ingress_ordinal % self.sample_every == 0
