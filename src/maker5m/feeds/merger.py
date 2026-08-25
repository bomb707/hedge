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
    perf_clock: Callable[[], int] | None = None
    """Optional high-resolution clock. When set, ``submit`` records its own stage timings.

    Opt-in so the hot path costs one ``is None`` check when measurement is off. These readings
    are on the *latency* clock and never enter Plane 2 state.
    """

    last_reduce_ns: int = 0
    last_decide_ns: int = 0
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
        perf = self.perf_clock
        self.state = reduce_event(self.state, event)
        if perf is not None:
            self.last_reduce_ns = perf()
        decision = self.engine.decide(self.state)
        if perf is not None:
            self.last_decide_ns = perf()
        self.steps.append(ReplayStep(event=event, decision=decision))
        return decision

    @property
    def ordinal(self) -> int:
        return self._ordinal

    def advance_ordinal(self) -> int:
        """Advance the counter without building an event. Offline harnesses only.

        The measurement and benchmark harnesses reduce prepared events directly instead of
        decoding feed frames, so nothing would otherwise move the ordinal — and a pinned
        ordinal silently disables deterministic sampling, because ``0 % n == 0`` always holds.
        """
        self._ordinal += 1
        return self._ordinal

    @property
    def step_count(self) -> int:
        return len(self.steps)
