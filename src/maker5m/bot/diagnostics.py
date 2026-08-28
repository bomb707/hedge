"""Where the resident bytes actually are. Read, never guessed.

P13's corpus ran one process for seventeen hours and ended at 4,262 MB of resident memory having
started at 36 MB, while the Python heap's tracked-object count did not trend at all. Those two
facts together rule out the obvious explanation — a retained session graph — and leave the
question open: *something* is resident, and `statm` cannot say what.

So this module reads the places that can. `/proc/self/status` separates anonymous memory from
file-backed mappings; `/proc/self/smaps_rollup` separates private dirty pages from shared clean
ones; glibc's `mallinfo2` separates the C heap that is *in use* from the C heap that is merely
*retained*, which is the difference between a leak and an allocator that has not given memory
back. A run where `uordblks` is small and `fordblks` is large is not leaking objects.

Everything here is optional and Linux/glibc-specific, and every reader returns ``None`` rather
than a zero when the platform will not say. Nothing in this module may be called from Plane 1:
reading `smaps_rollup` walks the process's mappings in the kernel and can take milliseconds, and
`malloc_trim` walks the heap. This is Plane 3 diagnostics, taken between markets or off the loop.
"""

from __future__ import annotations

import ctypes
import gc
import os
import sys
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Final

__all__ = [
    "MALLINFO2_FIELDS",
    "ROLLUP_FIELDS",
    "STATUS_FIELDS",
    "MemorySnapshot",
    "QuiescentProbe",
    "child_processes",
    "mallinfo2",
    "malloc_trim",
    "memory_status",
    "python_heap",
    "quiescent_probe",
    "smaps_rollup",
    "snapshot",
    "thread_names",
]


STATUS_FIELDS: Final[tuple[str, ...]] = (
    "VmRSS",
    "RssAnon",
    "RssFile",
    "RssShmem",
    "VmSize",
    "VmData",
    "VmSwap",
    "Threads",
)
"""What `/proc/self/status` is asked for. `Threads` is a count; the rest are sizes."""

ROLLUP_FIELDS: Final[tuple[str, ...]] = (
    "Rss",
    "Pss",
    "Private_Clean",
    "Private_Dirty",
    "Shared_Clean",
    "Shared_Dirty",
    "Anonymous",
    "AnonHugePages",
    "Swap",
)
"""What `/proc/self/smaps_rollup` is asked for. All sizes."""

MALLINFO2_FIELDS: Final[tuple[str, ...]] = (
    "arena",
    "ordblks",
    "smblks",
    "hblks",
    "hblkhd",
    "usmblks",
    "fsmblks",
    "uordblks",
    "fordblks",
    "keepcost",
)
"""glibc's `struct mallinfo2`, in declaration order. `size_t` throughout, so no 32-bit overflow.

The two that decide the question are `uordblks` — bytes the program is using — and `fordblks` —
bytes glibc holds but nobody is using. `hblkhd` is what came from `mmap` directly and is returned
to the kernel on free; `arena` is what came from `sbrk` and is not.
"""


def _parse_kb_table(text: str, wanted: tuple[str, ...]) -> dict[str, int | None]:
    """`Name:  1234 kB` lines to bytes. A field the kernel did not print stays ``None``."""
    found: dict[str, int | None] = dict.fromkeys(wanted)
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name not in found:
            continue
        parts = rest.split()
        if not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        found[name] = value * 1024 if len(parts) > 1 and parts[1] == "kB" else value
    return found


def memory_status() -> dict[str, int | None]:
    """`/proc/self/status`, in bytes. Every field ``None`` on a platform without it."""
    try:
        text = Path("/proc/self/status").read_text("utf-8")
    except OSError:
        return dict.fromkeys(STATUS_FIELDS)
    return _parse_kb_table(text, STATUS_FIELDS)


def smaps_rollup() -> dict[str, int | None]:
    """`/proc/self/smaps_rollup`, in bytes. Absent before Linux 4.14 and inside some sandboxes."""
    try:
        text = Path("/proc/self/smaps_rollup").read_text("utf-8")
    except OSError:
        return dict.fromkeys(ROLLUP_FIELDS)
    return _parse_kb_table(text, ROLLUP_FIELDS)


class _MallInfo2(ctypes.Structure):
    _fields_ = [(name, ctypes.c_size_t) for name in MALLINFO2_FIELDS]


def _libc() -> ctypes.CDLL | None:
    try:
        return ctypes.CDLL(None)
    except (OSError, TypeError):  # pragma: no cover - only on platforms without a global handle
        return None


def mallinfo2() -> dict[str, int] | None:
    """glibc's heap accounting, or ``None`` where there is no glibc.

    `mallinfo2` arrived in glibc 2.33; the older `mallinfo` returns `int` fields that wrap at 2 GB
    and would report this process's heap as a negative number, so it is deliberately not used.
    """
    lib = _libc()
    if lib is None:
        return None
    try:
        entry = lib.mallinfo2
    except AttributeError:
        return None
    try:
        entry.restype = _MallInfo2
        entry.argtypes = []
        info = entry()
    except Exception:  # pragma: no cover - a non-glibc libc exporting the name
        return None
    return {name: int(getattr(info, name)) for name in MALLINFO2_FIELDS}


def malloc_trim(pad: int = 0) -> bool | None:
    """Ask glibc to return free heap to the kernel. ``None`` where the call does not exist.

    ``True`` means memory was released, ``False`` means there was nothing releasable. This is a
    *diagnostic*: the difference it makes to RSS is the measurement, and calling it while a market
    is trading would walk the heap under the ingress owner. See `quiescent_probe`.
    """
    lib = _libc()
    if lib is None:
        return None
    try:
        entry = lib.malloc_trim
    except AttributeError:
        return None
    try:
        entry.restype = ctypes.c_int
        entry.argtypes = [ctypes.c_size_t]
        return bool(entry(pad))
    except Exception:  # pragma: no cover
        return None


def child_processes() -> dict[str, int | None]:
    """Resident memory of this process's direct children, by pid. Never added to the parent's.

    P13's cold path — replay, store verification, LZMA — runs in spawned interpreters, and a
    parent's 4.26 GB is not explained by a child's anything. They are read separately and
    reported separately so the two can never be confused for one number.
    """
    pids: set[int] = set()
    try:
        for task in Path("/proc/self/task").iterdir():
            try:
                pids.update(int(pid) for pid in (task / "children").read_text("utf-8").split())
            except (OSError, ValueError):
                continue
    except OSError:
        return {}
    found: dict[str, int | None] = {}
    for pid in sorted(pids):
        try:
            text = Path(f"/proc/{pid}/status").read_text("utf-8")
        except OSError:
            continue  # it exited between the listing and the read; that is not a reading of zero
        found[str(pid)] = _parse_kb_table(text, ("VmRSS",))["VmRSS"]
    return found


def thread_names() -> dict[str, int]:
    """How many threads carry each name. Names, because a count alone cannot say what grew."""
    return dict(sorted(Counter(thread.name for thread in threading.enumerate()).items()))


def python_heap(*, tracked_objects: bool = False) -> dict[str, Any]:
    """What CPython itself accounts for.

    `tracked_objects` is off by default and expensive when on: `gc.get_objects` materialises a
    list of every tracked object, which on this process is millions of pointers. Worth it at a
    market boundary, not at every checkpoint.
    """
    counts = gc.get_count()
    heap: dict[str, Any] = {
        "gc_count": list(counts),
        "gc_threshold": list(gc.get_threshold()),
        "gc_stats": [dict(entry) for entry in gc.get_stats()],
        "allocated_blocks": sys.getallocatedblocks(),
        "tracked_objects": None,
    }
    if tracked_objects:
        heap["tracked_objects"] = len(gc.get_objects())
    return heap


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    """One labelled reading of everything this process's memory can be asked about."""

    label: str
    at_ns: int
    monotonic_ns: int
    status: dict[str, int | None]
    rollup: dict[str, int | None]
    malloc: dict[str, int] | None
    heap: dict[str, Any]
    threads: dict[str, int]
    children: dict[str, int | None]

    @property
    def rss_bytes(self) -> int | None:
        """Resident set from `status`, which is the same number `statm` reports."""
        return self.status.get("VmRSS")

    def summary(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "at_ns": self.at_ns,
            "monotonic_ns": self.monotonic_ns,
            "status": dict(self.status),
            "rollup": dict(self.rollup),
            "malloc": None if self.malloc is None else dict(self.malloc),
            "heap": dict(self.heap),
            "threads": dict(self.threads),
            "children": dict(self.children),
        }

    def compact(self) -> dict[str, Any]:
        """The dozen numbers a per-market checkpoint needs, flat. Eleven of these per market.

        `summary` is the whole reading and belongs in a diagnostic artifact; a corpus row that
        carried it eleven times would be mostly memory readings. Every field kept here is one
        that distinguishes a hypothesis from another: anonymous versus file-backed, in-use versus
        free C heap, mapped-direct versus heap-grown.
        """
        malloc = self.malloc or {}
        return {
            "label": self.label,
            "monotonic_ns": self.monotonic_ns,
            "rss": self.status.get("VmRSS"),
            "rss_anon": self.status.get("RssAnon"),
            "rss_file": self.status.get("RssFile"),
            "vm_data": self.status.get("VmData"),
            "swap": self.status.get("VmSwap"),
            "pss": self.rollup.get("Pss"),
            "private_dirty": self.rollup.get("Private_Dirty"),
            "arena": malloc.get("arena"),
            "hblkhd": malloc.get("hblkhd"),
            "uordblks": malloc.get("uordblks"),
            "fordblks": malloc.get("fordblks"),
            "allocated_blocks": self.heap.get("allocated_blocks"),
            "tracked_objects": self.heap.get("tracked_objects"),
            "threads": sum(self.threads.values()),
            "thread_names": dict(self.threads),
            # Reported beside the parent's figures, never folded into them.
            "children": dict(self.children),
            "child_rss_total": sum(value for value in self.children.values() if value is not None),
        }


def snapshot(label: str, *, tracked_objects: bool = False, at_ns: int = 0) -> MemorySnapshot:
    """Take one reading. Plane 3 only: this reads `/proc` and may take milliseconds."""
    return MemorySnapshot(
        label=label,
        at_ns=at_ns if at_ns else 0,
        monotonic_ns=perf_counter_ns(),
        status=memory_status(),
        rollup=smaps_rollup(),
        malloc=mallinfo2(),
        heap=python_heap(tracked_objects=tracked_objects),
        threads=thread_names(),
        children=child_processes(),
    )


@dataclass(frozen=True, slots=True)
class QuiescentProbe:
    """Three readings around a full collection and a heap trim, and what they mean.

    The interpretation is fixed before the numbers arrive, so the result cannot be read to suit:

    * a large drop after `gc.collect(2)` — live cyclic Python objects were being retained;
    * little from the collection and a large drop after `malloc_trim` — the C heap was holding
      memory nobody was using;
    * little from either — the memory is still live, or mapped somewhere neither call owns, and
      the attribution is not finished.
    """

    label: str
    refused: str | None
    before: MemorySnapshot | None
    after_gc: MemorySnapshot | None
    after_trim: MemorySnapshot | None
    collected: int | None
    trimmed: bool | None
    gc_seconds: float | None
    trim_seconds: float | None

    @staticmethod
    def _delta(first: MemorySnapshot | None, second: MemorySnapshot | None) -> int | None:
        if first is None or second is None:
            return None
        one, two = first.rss_bytes, second.rss_bytes
        return None if one is None or two is None else two - one

    @property
    def gc_release_bytes(self) -> int | None:
        """Negative means RSS fell. ``None`` means the platform did not report."""
        return self._delta(self.before, self.after_gc)

    @property
    def trim_release_bytes(self) -> int | None:
        return self._delta(self.after_gc, self.after_trim)

    def verdict(self) -> str:
        """Which of the three readings this is, by the rule stated above. Never a guess."""
        if self.refused is not None:
            return "NOT_QUIESCENT"
        by_gc, by_trim = self.gc_release_bytes, self.trim_release_bytes
        if by_gc is None or by_trim is None:
            return "NOT_MEASURED"
        released = -(by_gc + by_trim)
        if released <= 0:
            return "NOTHING_RELEASED"
        if -by_gc >= 2 * max(-by_trim, 0):
            return "PYTHON_CYCLIC_RETENTION"
        if -by_trim >= 2 * max(-by_gc, 0):
            return "NATIVE_FREE_HEAP_RETAINED"
        return "MIXED"

    def summary(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "refused": self.refused,
            "verdict": self.verdict(),
            "collected": self.collected,
            "trimmed": self.trimmed,
            "gc_seconds": self.gc_seconds,
            "trim_seconds": self.trim_seconds,
            "gc_release_bytes": self.gc_release_bytes,
            "trim_release_bytes": self.trim_release_bytes,
            "before": None if self.before is None else self.before.summary(),
            "after_gc": None if self.after_gc is None else self.after_gc.summary(),
            "after_trim": None if self.after_trim is None else self.after_trim.summary(),
        }


def quiescent_probe(
    label: str,
    *,
    live_sessions: int,
    pending_tasks: int | None = None,
    trim: bool = True,
    tracked_objects: bool = True,
) -> QuiescentProbe:
    """Measure what a full collection and a heap trim would release, at a quiet boundary.

    **Refuses unless the process is quiescent.** `gc.collect(2)` traverses every tracked object
    and `malloc_trim` walks the heap; either one taken while a market is trading is a
    deliberately induced pause on the ingress owner, which is the thing this whole phase exists
    to stop doing by accident. The caller passes what it knows — how many sessions are still
    alive, how many tasks are still pending — and a refusal is recorded as a refusal, not as a
    zero that would read as "nothing to release".
    """
    if live_sessions:
        return QuiescentProbe(
            label=label,
            refused=f"{live_sessions} live session(s) still resident",
            before=None,
            after_gc=None,
            after_trim=None,
            collected=None,
            trimmed=None,
            gc_seconds=None,
            trim_seconds=None,
        )
    before = snapshot(f"{label}:before", tracked_objects=tracked_objects)
    started = perf_counter_ns()
    collected = gc.collect(2)
    gc_seconds = (perf_counter_ns() - started) / 1e9
    after_gc = snapshot(f"{label}:after_gc", tracked_objects=tracked_objects)
    trimmed: bool | None = None
    trim_seconds: float | None = None
    after_trim = after_gc
    if trim:
        started = perf_counter_ns()
        trimmed = malloc_trim(0)
        trim_seconds = (perf_counter_ns() - started) / 1e9
        after_trim = snapshot(f"{label}:after_trim", tracked_objects=tracked_objects)
    return QuiescentProbe(
        label=label,
        refused=None if pending_tasks is None or pending_tasks >= 0 else "negative pending tasks",
        before=before,
        after_gc=after_gc,
        after_trim=after_trim,
        collected=collected,
        trimmed=trimmed,
        gc_seconds=gc_seconds,
        trim_seconds=trim_seconds,
    )


def available() -> dict[str, bool]:
    """Which diagnostics this platform actually supports. Recorded, so a run says what it had."""
    return {
        "proc_status": Path("/proc/self/status").exists(),
        "smaps_rollup": Path("/proc/self/smaps_rollup").exists(),
        "mallinfo2": mallinfo2() is not None,
        "malloc_trim": malloc_trim(0) is not None,
        "linux": os.name == "posix" and sys.platform.startswith("linux"),
    }
