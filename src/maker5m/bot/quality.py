"""L3 quote classification, aggregated by the dimensions the corpus needs.

Canonical §34-L3 asks for every quote opportunity to be classified `AT_FRONT`,
`PRICE_OK_BUT_DEEP`, `OFF_PRICE`, `NOT_QUOTING` or `STALE`. P8 already does that, in
`maker5m.telemetry.classifier`, and there is exactly one classifier in this project — this module
does not decide anything, it counts what P8 decided, split by side, phase and time.

**Every queue figure here is a SHADOW_ESTIMATE.** P8's shadow tracker models where our order
would sit given what the public book showed; the venue never told us our queue position, and it
never will until a real order rests. The bias is documented in
`maker5m.telemetry.queue_estimate` and is not corrected here — relabelled data would be worse
than biased data honestly labelled.

`STALE` comes from P6, through P8. Nothing in this module looks at a price age, a timestamp or a
gap and decides for itself that a feed was stale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from maker5m.market.timebase import NANOS_PER_SECOND
from maker5m.telemetry.analyzer import QuoteEvent
from maker5m.telemetry.metrics import quantile

__all__ = ["QUALITY_LABELS", "QUEUE_PROVENANCE", "QualityAggregate"]

QUALITY_LABELS: Final[tuple[str, ...]] = (
    "AT_FRONT",
    "PRICE_OK_BUT_DEEP",
    "OFF_PRICE",
    "NOT_QUOTING",
    "STALE",
)

QUEUE_PROVENANCE: Final[str] = "SHADOW_ESTIMATE"
"""What every queue number in this module is. Never a venue queue position."""

BUCKET_SECONDS: Final[int] = 30
"""Time resolution of the within-market breakdown. Ten buckets over a five-minute market."""


def _bump(table: dict[str, dict[str, int]], key: str, quality: str) -> None:
    row = table.setdefault(key, {})
    row[quality] = row.get(quality, 0) + 1


@dataclass(slots=True)
class QualityAggregate:
    """One market's L3 distribution, by side, by phase, by time, and by event source.

    Fed from `TelemetryAnalyzer.on_quote`, on the persistence worker's thread. Counting only:
    no classification is made here and none is second-guessed.
    """

    t0_ns: int
    total: dict[str, int] = field(default_factory=dict)
    by_reason: dict[str, int] = field(default_factory=dict)
    by_outcome: dict[str, dict[str, int]] = field(default_factory=dict)
    by_phase: dict[str, dict[str, int]] = field(default_factory=dict)
    by_bucket: dict[str, dict[str, int]] = field(default_factory=dict)
    by_event_kind: dict[str, dict[str, int]] = field(default_factory=dict)
    queue_ahead: list[int] = field(default_factory=list, repr=False)
    queue_confidence: dict[str, int] = field(default_factory=dict)
    resting_cycles: int = 0
    """Cycles where an order actually rested — the denominator `AT_FRONT` belongs to."""

    def observe(self, event: QuoteEvent) -> None:
        classification = event.classification
        quality = classification.quality.value
        self.total[quality] = self.total.get(quality, 0) + 1
        reason = classification.reason.value
        self.by_reason[reason] = self.by_reason.get(reason, 0) + 1
        _bump(self.by_outcome, event.outcome, quality)
        _bump(self.by_phase, event.phase or "UNKNOWN", quality)
        _bump(self.by_bucket, self._bucket(event.event_timestamp_ns), quality)
        _bump(self.by_event_kind, str(event.event_kind), quality)

        ahead = classification.queue_ahead
        if ahead is not None:
            self.resting_cycles += 1
            self.queue_ahead.append(int(ahead))
            confidence = getattr(classification.confidence, "value", classification.confidence)
            name = "UNKNOWN" if confidence is None else str(confidence)
            self.queue_confidence[name] = self.queue_confidence.get(name, 0) + 1

    def _bucket(self, timestamp_ns: Any) -> str:
        """Seconds from T0, in fixed buckets. Out-of-window events keep their own label."""
        if not isinstance(timestamp_ns, int) or not self.t0_ns:
            return "UNKNOWN"
        offset = (timestamp_ns - self.t0_ns) // NANOS_PER_SECOND
        if offset < 0:
            return "PRE_T0"
        return f"{(offset // BUCKET_SECONDS) * BUCKET_SECONDS:03d}s"

    @property
    def classified(self) -> int:
        return sum(self.total.values())

    def fraction(self, quality: str) -> float | None:
        """Share of classified opportunities. ``None`` when nothing was classified.

        Not ``0.0``: a market that classified nothing has no rate, and reporting zero would
        make a broken collector look like a market that simply never quoted.
        """
        total = self.classified
        return None if total == 0 else self.total.get(quality, 0) / total

    def summary(self) -> dict[str, Any]:
        ordered = sorted(self.queue_ahead)
        return {
            "provenance": QUEUE_PROVENANCE,
            "note": (
                "Queue figures are P8's shadow model of where our order would sit, not a venue "
                "queue position. No own order was ever sent. STALE is P6's verdict, carried "
                "through P8, never re-derived here."
            ),
            "classified": self.classified,
            "total": {label: self.total.get(label, 0) for label in QUALITY_LABELS},
            "fractions": {label: self.fraction(label) for label in QUALITY_LABELS},
            "by_reason": dict(sorted(self.by_reason.items())),
            "by_outcome": {k: dict(sorted(v.items())) for k, v in sorted(self.by_outcome.items())},
            "by_phase": {k: dict(sorted(v.items())) for k, v in sorted(self.by_phase.items())},
            "by_time_bucket": {
                k: dict(sorted(v.items())) for k, v in sorted(self.by_bucket.items())
            },
            "by_event_kind": {
                k: dict(sorted(v.items())) for k, v in sorted(self.by_event_kind.items())
            },
            "resting_cycles": self.resting_cycles,
            "queue_ahead_shadow_estimate": {
                "n": len(ordered),
                "p50": quantile(ordered, 0.50) if ordered else None,
                "p75": quantile(ordered, 0.75) if ordered else None,
                "p90": quantile(ordered, 0.90) if ordered else None,
                "p95": quantile(ordered, 0.95) if ordered else None,
                "max": ordered[-1] if ordered else None,
            },
            "queue_confidence": dict(sorted(self.queue_confidence.items())),
        }
