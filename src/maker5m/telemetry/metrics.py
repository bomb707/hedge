"""Distribution and counter aggregation for the P8 evidence run.

Distributions, not averages. An average latency hides the tail, and the tail is what decides
whether an order reaches a fresh price level before the crowd (Canonical §10.1).

Samples are kept as plain integers in a list and quantiles computed by sorting on demand. At a
few thousand samples per market that costs nothing, and it avoids an approximation whose error
would be indistinguishable from the thing being measured.
"""

from dataclasses import dataclass, field

__all__ = ["ActionCounters", "Distribution", "quantile"]


def quantile(sorted_samples: list[int], fraction: float) -> int:
    """Nearest-rank quantile. Exact for the sample set, with no interpolation invented."""
    if not sorted_samples:
        raise ValueError("no samples")
    if not 0.0 < fraction <= 1.0:
        raise ValueError(f"fraction must lie in (0, 1], got {fraction}")
    rank = max(1, min(len(sorted_samples), int(-(-len(sorted_samples) * fraction // 1))))
    return sorted_samples[rank - 1]


@dataclass(slots=True)
class Distribution:
    """A named sample set. Append is O(1); quantiles are computed on demand."""

    label: str
    samples: list[int] = field(default_factory=list)

    def add(self, value: int) -> None:
        self.samples.append(value)

    def __len__(self) -> int:
        return len(self.samples)

    def summary(self) -> dict[str, object]:
        if not self.samples:
            return {"label": self.label, "count": 0}
        ordered = sorted(self.samples)
        return {
            "label": self.label,
            "count": len(ordered),
            "p50": quantile(ordered, 0.50),
            "p90": quantile(ordered, 0.90),
            "p95": quantile(ordered, 0.95),
            "p99": quantile(ordered, 0.99),
            "max": ordered[-1],
        }


@dataclass(slots=True)
class ActionCounters:
    """Reconcile-action and queue-slot accounting.

    ``KEEP`` is never counted as a queue loss — it is the opposite, and conflating them would
    make the most important behaviour in the system look like churn.
    """

    actions: dict[str, int] = field(default_factory=dict)
    quality: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, int] = field(default_factory=dict)
    cycles_with_live_order: int = 0
    keeps_with_live_order: int = 0
    queue_slots_acquired: int = 0
    queue_slots_kept: int = 0
    queue_slots_lost: int = 0
    queue_loss_reasons: dict[str, int] = field(default_factory=dict)

    def count_action(self, action: str) -> None:
        self.actions[action] = self.actions.get(action, 0) + 1

    def count_quality(self, quality: str, reason: str) -> None:
        self.quality[quality] = self.quality.get(quality, 0) + 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def count_queue_loss(self, reason: str) -> None:
        self.queue_slots_lost += 1
        self.queue_loss_reasons[reason] = self.queue_loss_reasons.get(reason, 0) + 1

    @property
    def keep_ratio(self) -> float | None:
        """KEEP share of cycles where a live order actually existed.

        ``None`` rather than ``1.0`` when no live order ever existed: an undefined ratio is
        not a perfect one.
        """
        if self.cycles_with_live_order == 0:
            return None
        return self.keeps_with_live_order / self.cycles_with_live_order

    def rate(self, quality: str) -> float | None:
        total = sum(self.quality.values())
        return None if total == 0 else self.quality.get(quality, 0) / total

    def summary(self) -> dict[str, object]:
        return {
            "actions": dict(sorted(self.actions.items())),
            "quality": dict(sorted(self.quality.items())),
            "reasons": dict(sorted(self.reasons.items())),
            "cycles_with_live_order": self.cycles_with_live_order,
            "keeps_with_live_order": self.keeps_with_live_order,
            "keep_ratio": self.keep_ratio,
            "queue_slots_acquired": self.queue_slots_acquired,
            "queue_slots_kept": self.queue_slots_kept,
            "queue_slots_lost": self.queue_slots_lost,
            "queue_loss_reasons": dict(sorted(self.queue_loss_reasons.items())),
        }
