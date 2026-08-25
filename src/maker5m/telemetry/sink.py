"""A bounded, non-blocking telemetry sink.

Invariant I19: telemetry must never delay trading. When the buffer is full the **oldest**
record is dropped and a counter increments — a lost observation is an observability incident,
a stalled hot loop is a trading incident.

What is never dropped: authoritative market events and execution actions. Those are not
telemetry; they go through the ingress merger and the live order table respectively.

No file write, no database, no JSON, no logging handler, no metrics exporter. This is an
in-memory ring. P11 owns durable persistence and can drain it.
"""

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final

__all__ = ["DEFAULT_CAPACITY", "TelemetrySink"]

DEFAULT_CAPACITY: Final[int] = 65_536


@dataclass(slots=True)
class TelemetrySink:
    """Drop-oldest ring with drop accounting."""

    capacity: int = DEFAULT_CAPACITY
    records: deque[tuple[object, ...]] = field(init=False)
    dropped: int = 0
    accepted: int = 0

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError(f"capacity must be positive, got {self.capacity}")
        self.records = deque(maxlen=self.capacity)

    def put(self, record: tuple[object, ...]) -> None:
        """Never blocks, never raises, never waits."""
        if len(self.records) == self.capacity:
            self.dropped += 1
        self.records.append(record)
        self.accepted += 1

    def drain(self) -> list[tuple[object, ...]]:
        drained = list(self.records)
        self.records.clear()
        return drained

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterator[tuple[object, ...]]:
        return iter(self.records)
