"""What the process is holding. Sampled per market, so a leak is visible while it is happening.

P13 is the first composition meant to run for hours. Two hundred markets is two hundred pipelines,
persistence workers, SQLite connections, UI publishers and asyncio tasks, and the acceptance
question is not "did it crash" but "was anything accumulating". A number recorded at the start and
the end of every market answers that; a number recorded once at the end does not.

Everything here is read from `/proc` and the standard library, with no third-party dependency and
no failure that matters: a platform that will not report resident memory returns `None` rather
than a zero that would read as "nothing held".
"""

from __future__ import annotations

import gc
import os
import threading
import weakref
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Final

__all__ = [
    "GC_EVENT_LIMIT",
    "GC_RECORD_FROM_GENERATION",
    "GEN2_EVERY",
    "LIVE_SESSIONS",
    "GcEventLog",
    "GcObserver",
    "GcWindow",
    "ResourceSample",
    "pace_full_collections",
    "sample_resources",
    "tiers",
]


LIVE_SESSIONS: weakref.WeakSet[Any] = weakref.WeakSet()
"""Every market session that still exists, held weakly so this cannot itself be the leak.

RSS is the operating system's view and glibc does not hand freed arenas back promptly, so a
process that has released everything can still read high. This counts the objects: if a market's
session is gone from here, its pipeline, its recorded stream and its analyzer are gone with it,
whatever the resident-set number says.
"""


@dataclass(frozen=True, slots=True)
class ResourceSample:
    """One reading. ``None`` means "this platform did not say", never "zero"."""

    rss_bytes: int | None
    threads: int
    open_fds: int | None
    pending_tasks: int | None
    live_sessions: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "rss_bytes": self.rss_bytes,
            "threads": self.threads,
            "open_fds": self.open_fds,
            "pending_tasks": self.pending_tasks,
            "live_sessions": self.live_sessions,
        }


def tiers(samples: list[int]) -> dict[str, int | None]:
    """Quantiles of a measured series, or an explicit nothing. Never zeros standing in."""
    from maker5m.telemetry.metrics import quantile

    ordered = sorted(samples)
    if not ordered:
        return {"n": 0, "p50": None, "p95": None, "p99": None, "max": None}
    return {
        "n": len(ordered),
        "p50": quantile(ordered, 0.50),
        "p95": quantile(ordered, 0.95),
        "p99": quantile(ordered, 0.99),
        "max": ordered[-1],
    }


GEN2_EVERY: Final[int] = 400
"""How many generation-1 collections pass before a full one. CPython's default is 10.

**OPERATIONAL, and chosen from a measurement rather than a preference.** A full collection is
proportional to the number of *tracked* objects, and this process holds a market's recorded event
stream: about 750,000 tracked objects per market, 2.3 million with a closed market's cold work
still in flight. The corrected pilot measured 37 full collections costing 17.1 seconds in twenty
minutes, with a **worst case of 1.46 seconds**, and that traversal runs inside whichever
allocation triggers it — including the ingress owner's own cycle, where single `observe` calls of
541 and 762 milliseconds appeared against a 25 microsecond median.

Full collections over that graph find almost nothing: `ReplayStep` holds an event and a decision,
which hold tuples, integers and strings. It is acyclic, and reference counting frees all of it.
The collector is therefore made *rarer*, not disabled — cyclic garbage does exist elsewhere in an
asyncio process and something has to collect it eventually.

Forty times fewer full collections, not zero. The effect is recorded per market by `GcObserver`,
so the next run's numbers say whether this was the right value rather than whether it sounded
like one.
"""


def pace_full_collections(every: int = GEN2_EVERY) -> tuple[int, int, int]:
    """Make full collections rare. Returns the thresholds in force. Process-wide, Plane 3."""
    allocations, gen1, _ = gc.get_threshold()
    gc.set_threshold(allocations, gen1, every)
    return gc.get_threshold()


GC_EVENT_LIMIT: Final[int] = 400_000
"""How many individual collections are kept. Three arrays of machine integers, so ~10 MB full.

The corpus recorded 86,417 generation-1 and 215 generation-2 collections in seventeen hours,
which is nothing; generation 0 ran 950,584 times and is deliberately counted rather than
recorded. Overflow is counted too — a truncated log that silently stopped recording would answer
"how many collections did this market see" with a smaller number and no way to tell.
"""

GC_RECORD_FROM_GENERATION: Final[int] = 1
"""The lowest generation kept as an individual event. Generation 0 is a counter only."""


@dataclass(slots=True)
class GcEventLog:
    """Every collection that mattered, as three parallel arrays of integers.

    P13's `GcObserver` kept a running maximum, and a running maximum cannot be attributed: the
    corpus reported "this market's max gen-2 pause" for forty markets when what it had was the
    largest pause *so far in the run*. Only one of those forty had actually raised it. The fix is
    not a better summary, it is keeping the events — a generation, a start and an end each — so a
    market's window can be intersected with them afterwards and the answer is exact.

    Arrays rather than tuples because this is memory-diagnostic code and 24 bytes per event that
    does not move is better than 160 that does.
    """

    limit: int = GC_EVENT_LIMIT
    min_generation: int = GC_RECORD_FROM_GENERATION
    generations: array[int] = field(default_factory=lambda: array("b"))
    starts_ns: array[int] = field(default_factory=lambda: array("q"))
    ends_ns: array[int] = field(default_factory=lambda: array("q"))
    dropped: int = 0

    def record(self, generation: int, start_ns: int, end_ns: int) -> None:
        """Keep one collection. Called from a gc callback, so it allocates almost nothing."""
        if generation < self.min_generation:
            return
        if len(self.generations) >= self.limit:
            self.dropped += 1
            return
        self.generations.append(generation)
        self.starts_ns.append(start_ns)
        self.ends_ns.append(end_ns)

    def __len__(self) -> int:
        return len(self.generations)

    def window(self, from_ns: int, to_ns: int, *, exact_from: int = 2) -> GcWindow:
        """Every recorded collection overlapping ``[from_ns, to_ns)``, by generation.

        A collection that straddles the boundary counts, and both figures are kept: the whole
        pause, because the process paid all of it, and the part that fell inside the window,
        because that is what this market waited for. `exact_from` names the generation whose
        individual pause durations are listed rather than only summed.
        """
        collections: dict[int, int] = {}
        total_ns: dict[int, int] = {}
        overlap_ns: dict[int, int] = {}
        longest_ns: dict[int, int] = {}
        exact: list[dict[str, int]] = []
        for index in range(len(self.generations)):
            start, end = self.starts_ns[index], self.ends_ns[index]
            if end <= from_ns or start >= to_ns:
                continue
            generation = self.generations[index]
            elapsed = end - start
            inside = min(end, to_ns) - max(start, from_ns)
            collections[generation] = collections.get(generation, 0) + 1
            total_ns[generation] = total_ns.get(generation, 0) + elapsed
            overlap_ns[generation] = overlap_ns.get(generation, 0) + inside
            longest_ns[generation] = max(longest_ns.get(generation, 0), elapsed)
            if generation >= exact_from:
                exact.append(
                    {
                        "generation": generation,
                        "start_ns": start,
                        "end_ns": end,
                        "duration_ns": elapsed,
                        "overlap_ns": inside,
                    }
                )
        return GcWindow(
            from_ns=from_ns,
            to_ns=to_ns,
            collections=collections,
            total_pause_ns=total_ns,
            overlap_pause_ns=overlap_ns,
            longest_pause_ns=longest_ns,
            events=tuple(exact),
            dropped=self.dropped,
        )


@dataclass(frozen=True, slots=True)
class GcWindow:
    """What the collector did during one market. Derived from events, never from a maximum."""

    from_ns: int
    to_ns: int
    collections: dict[int, int]
    total_pause_ns: dict[int, int]
    overlap_pause_ns: dict[int, int]
    longest_pause_ns: dict[int, int]
    events: tuple[dict[str, int], ...]
    dropped: int

    def summary(self) -> dict[str, Any]:
        return {
            "from_ns": self.from_ns,
            "to_ns": self.to_ns,
            "window_ns": self.to_ns - self.from_ns,
            "collections": {str(k): v for k, v in sorted(self.collections.items())},
            "total_pause_ns": {str(k): v for k, v in sorted(self.total_pause_ns.items())},
            "overlap_pause_ns": {str(k): v for k, v in sorted(self.overlap_pause_ns.items())},
            "longest_pause_ns": {str(k): v for k, v in sorted(self.longest_pause_ns.items())},
            "events": [dict(event) for event in self.events],
            "dropped_events": self.dropped,
        }


@dataclass(slots=True)
class GcObserver:
    """What the garbage collector did, and for how long. Measured, never assumed.

    A full collection is proportional to the number of *tracked* objects, live or not, and this
    process holds a market's recorded event stream — around 750,000 tracked objects per market,
    which costs about 74 ms to traverse. That traversal happens inside whatever allocation
    happens to trigger it, and on this process that can be the ingress owner's own cycle: the
    corrected pilot measured single `observe` calls of 277 to 674 ms against a 25 microsecond
    median while several markets' graphs were resident.

    This records it rather than inferring it. No threshold is tuned here on a hunch; the numbers
    go into the corpus and the decision is taken on them.

    The running totals below are the whole process's, and are only ever reported as the whole
    process's. Per-market attribution comes from `events`, which keeps each collection's start
    and end so a market's window can be intersected with it — see `GcEventLog`.
    """

    collections: dict[int, int] = field(default_factory=dict)
    pause_ns: dict[int, int] = field(default_factory=dict)
    max_pause_ns: dict[int, int] = field(default_factory=dict)
    events: GcEventLog = field(default_factory=GcEventLog)
    _started: int = 0
    _installed: bool = False

    def install(self) -> None:
        if self._installed:
            return
        gc.callbacks.append(self._on_gc)
        self._installed = True

    def remove(self) -> None:
        if self._installed and self._on_gc in gc.callbacks:
            gc.callbacks.remove(self._on_gc)
        self._installed = False

    def _on_gc(self, phase: str, info: dict[str, int]) -> None:
        if phase == "start":
            self._started = perf_counter_ns()
            return
        generation = int(info.get("generation", -1))
        finished = perf_counter_ns()
        elapsed = finished - self._started
        self.collections[generation] = self.collections.get(generation, 0) + 1
        self.pause_ns[generation] = self.pause_ns.get(generation, 0) + elapsed
        self.max_pause_ns[generation] = max(self.max_pause_ns.get(generation, 0), elapsed)
        self.events.record(generation, self._started, finished)

    def counters(self) -> dict[int, int]:
        """A copy of the cumulative collection counts. Differenced to attribute generation 0."""
        return dict(self.collections)

    def window(self, from_ns: int, to_ns: int) -> GcWindow:
        """Exactly what happened between two `perf_counter_ns` readings."""
        return self.events.window(from_ns, to_ns)

    def summary(self, *, tracked_objects: bool = True) -> dict[str, Any]:
        return {
            "thresholds": list(gc.get_threshold()),
            "collections": {str(k): v for k, v in sorted(self.collections.items())},
            "total_pause_ns": {str(k): v for k, v in sorted(self.pause_ns.items())},
            "max_pause_ns": {str(k): v for k, v in sorted(self.max_pause_ns.items())},
            "recorded_events": len(self.events),
            "dropped_events": self.events.dropped,
            # A running maximum over the whole process. It is not any one market's maximum, and
            # the corpus report that read it as one was wrong; `window` is the per-market answer.
            "max_pause_is_process_wide": True,
            "tracked_objects": len(gc.get_objects()) if tracked_objects else None,
        }


def _rss_bytes() -> int | None:
    try:
        fields = Path("/proc/self/statm").read_text("utf-8").split()
    except OSError:
        return None
    if len(fields) < 2:
        return None
    try:
        return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError):
        return None


def _open_fds() -> int | None:
    try:
        return sum(1 for _ in Path("/proc/self/fd").iterdir())
    except OSError:
        return None


def _pending_tasks() -> int | None:
    """Asyncio tasks that have not finished, when called from inside a running loop."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return None
    return sum(1 for task in asyncio.all_tasks() if not task.done())


def sample_resources() -> ResourceSample:
    """One reading of everything this process is holding. Plane 3; never on an ingress path."""
    return ResourceSample(
        rss_bytes=_rss_bytes(),
        threads=threading.active_count(),
        open_fds=_open_fds(),
        pending_tasks=_pending_tasks(),
        live_sessions=len(LIVE_SESSIONS),
    )
