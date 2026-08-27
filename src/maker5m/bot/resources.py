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

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["ResourceSample", "sample_resources", "tiers"]


@dataclass(frozen=True, slots=True)
class ResourceSample:
    """One reading. ``None`` means "this platform did not say", never "zero"."""

    rss_bytes: int | None
    threads: int
    open_fds: int | None
    pending_tasks: int | None

    def summary(self) -> dict[str, Any]:
        return {
            "rss_bytes": self.rss_bytes,
            "threads": self.threads,
            "open_fds": self.open_fds,
            "pending_tasks": self.pending_tasks,
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
    )
