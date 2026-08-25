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

    Two different things used to share the name "lost", and their totals disagreed (887 against
    1,049) while being presented side by side as though they measured the same quantity. They
    are now named apart and counted apart:

    * ``execution_queue_loss_actions`` — reconciler decisions that give up a slot: REPLACE and
      CANCEL. A property of the *plan*.
    * ``shadow_slot_losses`` — shadow slot identities that ceased to exist, counted by the
      shadow tracker and including closures the plan does not name, such as a complete fill.

    Each reconciles exactly to its own typed reason counts, which is asserted by test.
    """

    actions: dict[str, int] = field(default_factory=dict)
    quality: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, int] = field(default_factory=dict)
    cycles_with_live_order: int = 0
    keeps_with_live_order: int = 0
    execution_queue_loss_actions: int = 0
    execution_queue_loss_reasons: dict[str, int] = field(default_factory=dict)

    def count_action(self, action: str) -> None:
        self.actions[action] = self.actions.get(action, 0) + 1

    def count_quality(self, quality: str, reason: str) -> None:
        self.quality[quality] = self.quality.get(quality, 0) + 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def count_execution_queue_loss(self, reason: str) -> None:
        """One reconciler action that gives up a live order's slot. Never called for KEEP."""
        self.execution_queue_loss_actions += 1
        self.execution_queue_loss_reasons[reason] = (
            self.execution_queue_loss_reasons.get(reason, 0) + 1
        )

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
            "execution_queue_loss_actions": self.execution_queue_loss_actions,
            "execution_queue_loss_reasons": dict(sorted(self.execution_queue_loss_reasons.items())),
        }
