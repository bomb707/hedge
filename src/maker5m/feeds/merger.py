"""The single ingress merger — the one place production event order is created.

Every adapter hands normalized payloads to *this* object, and only this object assigns
``ingress_ordinal``. No feed numbers its own events, because two independent counters could
not be interleaved into one legal order, and P5 replay depends on there being exactly one.

```text
normalized payload -> assign ingress metadata -> reduce_event -> decide -> forward
```

The forward step is a plain append to an in-memory sink. No serialization, no file write, no
logging, no REST, no UI on this path (I19). Journal encoding happens after the run.

Telemetry sinks are bounded and **drop rather than block** when full: a dropped telemetry
record is an observability incident, a blocked ingress loop is a trading incident. Drops are
counted and reported. Authoritative market events are never dropped before decision
processing — only the downstream copy can be.
"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from maker5m.market.events import Event, EventMeta
from maker5m.market.reducer import reduce_event
from maker5m.market.state import MarketState
from maker5m.market.timebase import TimestampNs
from maker5m.replay.journal import ReplayStep
from maker5m.strategy.decision import DecisionResult
from maker5m.strategy.engine import StrategyEngine

__all__ = ["BoundedSink", "IngressMerger"]


@dataclass(slots=True)
class BoundedSink:
    """A drop-oldest sink for non-critical downstream copies, with drop accounting."""

    capacity: int
    items: deque[ReplayStep] = field(init=False)
    dropped: int = 0

    def __post_init__(self) -> None:
        self.items = deque(maxlen=self.capacity)

    def put(self, step: ReplayStep) -> None:
        if len(self.items) == self.capacity:
            self.dropped += 1
        self.items.append(step)


@dataclass(slots=True)
class IngressMerger:
    """Owns the ordinal counter, the authoritative state, and the decision call."""

    engine: StrategyEngine
    state: MarketState
    clock: Callable[[], TimestampNs]
    market_id: str
    steps: list[ReplayStep] = field(default_factory=list)
    _ordinal: int = 0
    _event_seq: int = 0

    def next_meta(self, prefix: str) -> EventMeta:
        """Assign the next ingress ordinal and a synchronized ingress timestamp."""
        meta = EventMeta(
            market_id=self.market_id,
            event_id=f"{prefix}-{self._event_seq:06d}",
            ingress_ordinal=self._ordinal,
            timestamp=self.clock(),
        )
        self._ordinal += 1
        self._event_seq += 1
        return meta

    def submit(self, event: Event) -> DecisionResult:
        """Reduce, decide, and record. The whole hot path in three lines."""
        self.state = reduce_event(self.state, event)
        decision = self.engine.decide(self.state)
        self.steps.append(ReplayStep(event=event, decision=decision))
        return decision

    @property
    def ordinal(self) -> int:
        return self._ordinal

    @property
    def step_count(self) -> int:
        return len(self.steps)
