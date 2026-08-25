"""What the trading path records, and nothing more.

P8's first two rounds ran the whole analytical pipeline synchronously: shadow slot mutation,
action counting, classification, distribution updates. Correct, and too expensive — an ordinary
unsampled book update paid +4.9 µs for work no trading decision depends on.

So the hot path now captures *facts* and returns. Everything analytical — queue estimation,
classification, counting, distributions — is reconstructed downstream from this record by
:mod:`maker5m.telemetry.analyzer`, in ingress order, producing identical results.

Representation
--------------
A plain ``tuple``, measured against the alternatives rather than assumed (300k iterations,
best of 7):

```text
tuple of references (16)          76 ns
tuple of extracted primitives     176 ns
slots dataclass                   305 ns
NamedTuple                        362 ns
frozen slots dataclass          1,791 ns
```

The frozen dataclass — the obvious "clean" choice — is 24x the cost of a tuple, which is the
same lesson P8 already learned from ``QueueEstimate``. So: a tuple, with named index constants
so the field layout is still legible.

It carries *references* to objects the cycle has already built (the reconcile plan, the
eligibility record) rather than copying their contents out. Copying 26 primitives costs 176 ns
and buys nothing: those objects are immutable, already allocated by work production performs
anyway, and the analyzer needs exactly what they hold. What is **not** referenced is anything
that would retain the world: no ``MarketState``, no ``DecisionResult``, no book, no
``LiveOrderTable``.

Depth is the one value that *must* be read on the hot path. The book is mutable and moves under
us, so the displayed size at our own price has to be sampled at the moment the cycle sees it;
there is no later time at which it can be recovered.
"""

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "DEFAULT_OBSERVATION_CAPACITY",
    "NOT_CAPTURED",
    "OBS_DECIDE_DONE_NS",
    "OBS_DECIDE_STAGE_NS",
    "OBS_DOWN_DEPTH",
    "OBS_DOWN_PLACED_ID",
    "OBS_ELIGIBILITY",
    "OBS_EVENT_KIND",
    "OBS_FILL",
    "OBS_HEALTHY",
    "OBS_INGRESS_ORDINAL",
    "OBS_PLAN",
    "OBS_PREPARE_DONE_NS",
    "OBS_RAW_RECEIVE_NS",
    "OBS_RECONCILE_DONE_NS",
    "OBS_REDUCE_STAGE_NS",
    "OBS_SEQ",
    "OBS_UP_DEPTH",
    "OBS_UP_PLACED_ID",
    "Observation",
    "ObservationBuffer",
]

Observation = tuple[object, ...]
"""One captured cycle. Index constants below name the fields."""

NOT_CAPTURED: Final[int] = 0
"""A stage timestamp that was deliberately not sampled.

Zero rather than a plausible number: an unsampled cycle has no timing, and inventing one after
the fact would put fabricated values into a latency distribution. Downstream treats zero as
absent, never as "0 ns elapsed".
"""

OBS_SEQ: Final = 0
"""Monotonic capture sequence. A gap means an observation was dropped."""
OBS_INGRESS_ORDINAL: Final = 1
OBS_EVENT_KIND: Final = 2
OBS_HEALTHY: Final = 3
OBS_RAW_RECEIVE_NS: Final = 4
OBS_DECIDE_DONE_NS: Final = 5
OBS_PREPARE_DONE_NS: Final = 6
OBS_RECONCILE_DONE_NS: Final = 7
OBS_REDUCE_STAGE_NS: Final = 8
OBS_DECIDE_STAGE_NS: Final = 9
OBS_PLAN: Final = 10
"""The ``ReconcilePlan``: actions, reasons, prepared orders, resting orders. By reference."""
OBS_UP_DEPTH: Final = 11
OBS_DOWN_DEPTH: Final = 12
"""Displayed same-outcome bid size at the price that matters for this side, read on the hot
path because the book will have moved by the time anything downstream looks."""
OBS_UP_PLACED_ID: Final = 13
OBS_DOWN_PLACED_ID: Final = 14
OBS_ELIGIBILITY: Final = 15
"""The decision's eligibility record, so a NOT_QUOTING side can still name its strategy gate."""
OBS_FILL: Final = 16
"""``(outcome, client_order_id, fully_filled)`` for a shadow fill, else ``None``."""

DEFAULT_OBSERVATION_CAPACITY: Final[int] = 220_000
"""Sized from evidence, not from a round number.

The busiest market measured produced 153,762 cycles, and one observation retains 638 bytes —
the tuple plus the reconcile plan it references. So this bound costs about 134 MiB and leaves
43% headroom. An earlier 160,000 was set from a 117,772-cycle market and a later one filled it
to 96%; a bound with 4% of room left is a bound about to be discovered the hard way.

A larger buffer is not free in a way that matters: retained object graphs are what the garbage
collector walks, and disabling GC in the steady-state benchmark cut the instrumented p99 tail
roughly in half. Extracting the plan's fields into the tuple instead of referencing it was
measured at 315 bytes per observation for about +100 ns of capture — a trade worth revisiting
in P11, which owns continuous draining, and unnecessary for P8's one bounded market.
"""


@dataclass(slots=True)
class ObservationBuffer:
    """Bounded, non-blocking capture. The trading path never waits on it.

    ``deque(maxlen=...)`` drops the oldest on overflow, which is the right failure: recent
    observations describe the market we are still in. A drop is visible two ways — the buffer's
    own count, and a gap in the capture sequence that the analyzer detects independently.

    ``append`` was measured at 46 ns against 95 ns for a hand-rolled preallocated ring, so the
    obvious implementation is also the fast one.
    """

    capacity: int = DEFAULT_OBSERVATION_CAPACITY
    records: deque[Observation] = field(default_factory=deque, repr=False)
    accepted: int = 0
    drained: int = 0

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {self.capacity}")
        if self.records.maxlen != self.capacity:
            self.records = deque(self.records, maxlen=self.capacity)

    def capture(self, observation: Observation) -> None:
        """Record one observation. Never blocks, never raises, never allocates a container."""
        self.accepted += 1
        self.records.append(observation)

    @property
    def dropped(self) -> int:
        """Observations the buffer could not keep.

        Derived rather than counted at append time: a length check on every capture would put
        work back on the hot path to learn something arithmetic already knows.
        """
        return self.accepted - self.drained - len(self.records)

    def drain(self) -> list[Observation]:
        """Take everything currently buffered, in capture order."""
        taken = list(self.records)
        self.records.clear()
        self.drained += len(taken)
        return taken

    def __iter__(self) -> Iterator[Observation]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)
