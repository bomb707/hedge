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
    "OBS_BOOK",
    "OBS_DECIDE_DONE_NS",
    "OBS_DECIDE_STAGE_NS",
    "OBS_DOWN_DEPTH",
    "OBS_DOWN_PLACED_ID",
    "OBS_ELIGIBILITY",
    "OBS_EVENT_ID",
    "OBS_EVENT_KIND",
    "OBS_EVENT_TS",
    "OBS_FILL",
    "OBS_HEALTHY",
    "OBS_INGRESS_ORDINAL",
    "OBS_PLAN",
    "OBS_PREPARE_DONE_NS",
    "OBS_RAW_RECEIVE_NS",
    "OBS_RECONCILE_DONE_NS",
    "OBS_REDUCE_STAGE_NS",
    "OBS_RISK",
    "OBS_SEQ",
    "OBS_SOURCE_TS",
    "OBS_SPOT",
    "OBS_STRATEGY_INTENT",
    "OBS_TELEMETRY",
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

# -- P11 additions ---------------------------------------------------------------------------
#
# Appended, never inserted: every index above keeps its meaning, so the P8 analyzer reads the
# same stream it always did and its golden equivalence is untouched.
#
# All four are references to values the cycle has *already built* and which are immutable —
# `DecisionTelemetry`, `BookUpdate` and `SpotTick` are frozen dataclasses. Retaining them costs
# a pointer store each and buys the whole of Canonical §25 without recomputing any economics
# downstream, which §25 needs and which a later reconstruction could get wrong. Nothing mutable
# is retained: no MarketState, no book dict, no LiveOrderTable.

OBS_TELEMETRY: Final = 17
"""The decision's own ``DecisionTelemetry``: phase, centre, grid, endgame, exact economics."""
OBS_BOOK: Final = 18
"""The ``BookUpdate`` in force at decision time, or ``None`` before the first book."""
OBS_SPOT: Final = 19
"""The ``SpotTick`` in force at decision time, or ``None`` before the first tick."""
OBS_EVENT_TS: Final = 20
"""``state.last_event_timestamp`` — the ingress clock reading this decision was made at.

Ages are computed downstream as this minus the source's own timestamp. Never a wall-clock read,
and never a fabricated zero when the source timestamp is missing."""
OBS_SOURCE_TS: Final = 21
"""The venue's own timestamp for the triggering message, in nanoseconds, when the feed genuinely
supplied one. ``None`` otherwise — the ingress clock is never passed off as an exchange clock."""
OBS_RISK: Final = 22
"""``(risk_sequence, state, allows_place, allows_cancel)`` when a risk controller is attached."""
OBS_EVENT_ID: Final = 23
"""P2's real ``EventMeta.event_id`` for the triggering event.

The genuine identity, read from the merger, not a string built from the slug and a counter. A
manufactured id looks authoritative and joins to nothing; this one is what the event stream,
the replay journal and the risk trace all already agree on."""
OBS_STRATEGY_INTENT: Final = 24
"""``(up_price, up_size, down_price, down_size)`` the *strategy* wanted, before risk withdrew it.

Captured separately because `risk_adjust` empties the intent a halt refuses, and a record that
only kept the post-risk version could not tell "the strategy declined to quote" from "the
strategy wanted to quote and safety refused" — which is the question telemetry exists to answer."""

DEFAULT_OBSERVATION_CAPACITY: Final[int] = 320_000
"""Sized from evidence, and resized twice as the evidence arrived.

Measured markets have run 117,772, 153,762, and 204,440 cycles. Each bound set from the busiest
market *so far* was nearly filled by the next one — 160,000 reached 96%, 220,000 reached 93% —
so this one is set from the busiest observed plus a little over half again.

One observation retains 638 bytes, the tuple plus the reconcile plan it references, so the bound
costs about 195 MiB. That is a real amount of memory for a five-minute measurement harness, and
the way to reduce it is known and measured: extracting the plan's fields into the tuple instead
of referencing it costs 315 bytes per observation for about +100 ns of capture. P11 owns
continuous draining and should take that trade; P8 analyses one bounded market and does not
need to.

Overflow is not silent. The oldest observations are dropped, the count is visible, and the
analyzer independently sees the sequence gap and marks queue confidence STALE.

Retained graphs are also what the garbage collector walks: disabling GC in the steady-state
benchmark cut the instrumented p99 tail roughly in half, which is the other reason the bound
matters and is not merely a memory ceiling.
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
