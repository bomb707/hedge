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
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Final

__all__ = [
    "GEN2_EVERY",
    "LIVE_SESSIONS",
    "GcObserver",
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
    """

    collections: dict[int, int] = field(default_factory=dict)
    pause_ns: dict[int, int] = field(default_factory=dict)
    max_pause_ns: dict[int, int] = field(default_factory=dict)
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
        elapsed = perf_counter_ns() - self._started
        self.collections[generation] = self.collections.get(generation, 0) + 1
        self.pause_ns[generation] = self.pause_ns.get(generation, 0) + elapsed
        self.max_pause_ns[generation] = max(self.max_pause_ns.get(generation, 0), elapsed)

    def summary(self) -> dict[str, Any]:
        return {
            "thresholds": list(gc.get_threshold()),
            "collections": {str(k): v for k, v in sorted(self.collections.items())},
            "total_pause_ns": {str(k): v for k, v in sorted(self.pause_ns.items())},
            "max_pause_ns": {str(k): v for k, v in sorted(self.max_pause_ns.items())},
            "tracked_objects": len(gc.get_objects()),
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
