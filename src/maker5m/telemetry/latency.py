"""High-resolution latency measurement. Plane 1 observation, never strategy state.

Two clock domains, kept strictly apart
--------------------------------------
``EventMeta.timestamp`` is the synchronized wall-aligned **ingress** clock. It drives the
market lifecycle — phase boundaries are derived from a market's ``T0`` — and it is part of the
deterministic core.

This module uses ``time.perf_counter_ns()``, a monotonic high-resolution clock with **no
defined epoch**. It is only ever used to subtract two readings taken from itself.

Mixing them is the trap worth naming: subtracting a venue timestamp from a ``perf_counter``
reading produces a number that looks like a latency and is meaningless. Venue timestamps stay
in feed diagnostics; nothing here compares across domains.

Nothing measured here enters ``MarketState``, ``DecisionResult``, ``LedgerState``, or a P5
journal. A latency value is an observation about *this run on this machine*; putting one into a
replayed decision would make replay depend on the machine that recorded it (I20).

Allocation
----------
P4 and P7 profiling showed frozen dataclass construction costing ~99 ns per field. A trace is
therefore a **mutable slotted builder** filled in place during the cycle, not a frozen object
rebuilt at every stage. Immutable snapshots are produced downstream, if and when something
actually needs one.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

__all__ = ["LatencyClock", "Stage", "TraceBuilder", "perf_now_ns"]


def perf_now_ns() -> int:
    """The latency clock. Monotonic, high-resolution, no epoch."""
    return time.perf_counter_ns()


LatencyClock = Callable[[], int]
"""Injectable so tests can drive stage timing deterministically."""


class Stage(Enum):
    """Points on the critical path, in the order they occur.

    Not every event populates every stage: a KEEP cycle legitimately has no dispatch stages,
    and that absence is itself a measurement worth having.
    """

    RAW_RECEIVE = "RAW_RECEIVE"
    DECODE_DONE = "DECODE_DONE"
    NORMALIZE_DONE = "NORMALIZE_DONE"
    INGRESS_ASSIGNED = "INGRESS_ASSIGNED"
    REDUCE_DONE = "REDUCE_DONE"
    DECIDE_DONE = "DECIDE_DONE"
    PREPARE_DONE = "PREPARE_DONE"
    RECONCILE_DONE = "RECONCILE_DONE"
    RATE_DECISION_DONE = "RATE_DECISION_DONE"
    DISPATCH_START = "DISPATCH_START"
    DISPATCH_RETURN = "DISPATCH_RETURN"
    USER_ACK_RECEIVE = "USER_ACK_RECEIVE"
    USER_FILL_RECEIVE = "USER_FILL_RECEIVE"


STAGE_COUNT: Final[int] = len(Stage)
_STAGE_INDEX: Final[dict[Stage, int]] = {stage: i for i, stage in enumerate(Stage)}


@dataclass(slots=True)
class TraceBuilder:
    """One cycle's stage timings, filled in place.

    Mutable and slotted on purpose: this is measurement scaffolding on the hot path, and a
    frozen rebuild per stage would cost more than the thing being measured.
    """

    market_id: str = ""
    event_id: str = ""
    ingress_ordinal: int = -1
    event_kind: str = ""
    outcome: str | None = None
    action: str | None = None
    client_order_id: str | None = None
    stamps: list[int] = field(default_factory=lambda: [0] * STAGE_COUNT)

    def mark(self, stage: Stage, clock: LatencyClock = perf_now_ns) -> int:
        """Record one stage. Returns the reading so a caller can reuse it."""
        value = clock()
        self.stamps[_STAGE_INDEX[stage]] = value
        return value

    def set(self, stage: Stage, value: int) -> None:
        """Record a stage from a reading already taken, without calling the clock again."""
        self.stamps[_STAGE_INDEX[stage]] = value

    def at(self, stage: Stage) -> int | None:
        value = self.stamps[_STAGE_INDEX[stage]]
        return value if value else None

    def duration(self, start: Stage, end: Stage) -> int | None:
        """Nanoseconds between two stages, or ``None`` if either was not reached.

        Missing stages are explicit rather than defaulted to zero: a KEEP cycle has no
        dispatch, and reporting that as "0 ns of dispatch" would corrupt the distribution.
        """
        first = self.at(start)
        second = self.at(end)
        if first is None or second is None:
            return None
        return second - first

    def reset(self) -> None:
        """Reuse the builder for the next cycle without allocating a new one."""
        self.market_id = ""
        self.event_id = ""
        self.ingress_ordinal = -1
        self.event_kind = ""
        self.outcome = None
        self.action = None
        self.client_order_id = None
        for index in range(STAGE_COUNT):
            self.stamps[index] = 0

    def snapshot(self) -> tuple[object, ...]:
        """A compact immutable copy for downstream publication."""
        return (
            self.market_id,
            self.event_id,
            self.ingress_ordinal,
            self.event_kind,
            self.outcome,
            self.action,
            self.client_order_id,
            tuple(self.stamps),
        )
