"""Allocator maintenance, and the window in which it is allowed to happen.

`p13-resource-1` established what the residual growth is: free glibc heap. `uordblks` stayed
between 13 and 21 MB across 57 markets while resident memory rose, `fordblks` finished at 169.5 MB
against an `arena` of 188.0 MB, and a `malloc_trim` after the last market returned 63 MB that a
full collection would not. The memory is not referenced by anything. glibc is simply holding it.

`malloc_trim` gives it back, and the reason it was not simply called during the diagnostic work is
that it takes the allocator's locks for the whole process. A thread is not a shield: a coroutine
that stays responsive proves the *event loop* was not blocked and proves nothing about the thread
that was trying to allocate a book update at the time. So this module does not try to make the
call cheap. It makes it happen **only where no market is quoting**, and it records enough around
each one that the real pilot can say whether anything noticed.

The window is the market clock's, not a heuristic. Canonical timing stops quoting at ``T0+280``
and the next market does not begin quoting until its own ``T0+3``, which is ``T0+303`` on the
previous market's clock. That is a 23-second gap in which the closing market is SETTLING and the
opening one is PREARM or warming its feeds, and in which **no market is legitimately in QUOTE or
ENDGAME**. Maintenance happens there or it does not happen.

Nothing here is strategy. The margin and the one-per-rollover policy are OPERATIONAL, recorded in
the configuration identity so a run says which policy produced it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from time import perf_counter_ns
from typing import Any, Final

from maker5m.bot.diagnostics import mallinfo2, malloc_trim, memory_status
from maker5m.market.phase import CANONICAL_PHASE_CONFIG, Phase, PhaseConfig, phase_at
from maker5m.market.timebase import NANOS_PER_SECOND, TimestampNs

__all__ = [
    "MAINTENANCE_MARGIN_S",
    "MARKET_SECONDS",
    "QUOTING_PHASES",
    "AllocatorMaintenance",
    "MaintenanceWindow",
    "maintenance_window",
]

MARKET_SECONDS: Final[int] = 300
"""The market cadence. Every T0 sits on a 300-second boundary."""

MAINTENANCE_MARGIN_S: Final[float] = 10.0
"""How much of the gap is left untouched before the next market may quote. OPERATIONAL.

The gap runs from ``T0+280`` to ``T0+303``. Ten seconds of it are given back, so maintenance may
begin no later than ``T0+293`` however long the call itself takes. A window that has already been
missed is **skipped**, never run late — a trim that overruns into `QUOTE` is the one outcome this
whole contract exists to make impossible.
"""

QUOTING_PHASES: Final[frozenset[Phase]] = frozenset({Phase.QUOTE, Phase.ENDGAME})
"""The phases in which a market may have executable quoting intent. Maintenance never runs here."""


@dataclass(frozen=True, slots=True)
class MaintenanceWindow:
    """Whether maintenance may run at an instant, and — when it may not — precisely why not.

    A refusal carries its reason because "did not run" and "was not allowed to run" are different
    facts, and a run that reports only the count of trims cannot tell them apart afterwards.
    """

    allowed: bool
    reason: str
    rollover: int | None
    now_ns: int
    seconds_since_stop_quoting: float | None
    seconds_to_quote_start: float | None
    phases: dict[str, str]

    def summary(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "rollover": self.rollover,
            "now_ns": self.now_ns,
            "seconds_since_stop_quoting": self.seconds_since_stop_quoting,
            "seconds_to_quote_start": self.seconds_to_quote_start,
            "phases": dict(self.phases),
        }


def _refused(
    reason: str, now_ns: int, rollover: int | None, phases: dict[str, str]
) -> MaintenanceWindow:
    return MaintenanceWindow(
        allowed=False,
        reason=reason,
        rollover=rollover,
        now_ns=now_ns,
        seconds_since_stop_quoting=None,
        seconds_to_quote_start=None,
        phases=phases,
    )


def maintenance_window(
    now_ns: int,
    sessions: Iterable[Any],
    *,
    config: PhaseConfig = CANONICAL_PHASE_CONFIG,
    margin_s: float = MAINTENANCE_MARGIN_S,
    completed: Iterable[int] = (),
    shutting_down: bool = False,
) -> MaintenanceWindow:
    """The six conditions, evaluated together. Pure: a clock is passed in, never read.

    1. no live session is in `QUOTE`;
    2. no live session is in `ENDGAME`;
    3. the closing market is at or past its stop-quoting boundary;
    4. the opening market's quote start is still at least `margin_s` away;
    5. this rollover has not already been maintained;
    6. the collector is not shutting down.

    Phases are derived from each session's own ``t0_ns`` through P2's phase machine rather than
    read from whatever a session last observed, so the answer is exact rather than a report of
    how recently a market ticked.
    """
    phases: dict[str, str] = {}
    for session in sessions:
        t0_ns = getattr(session, "t0_ns", None)
        if not isinstance(t0_ns, int):
            continue
        slug = str(getattr(session, "slug", "?"))
        phases[slug] = phase_at(TimestampNs(t0_ns), TimestampNs(now_ns), config).value

    now_s = now_ns / NANOS_PER_SECOND
    next_t0 = (int(now_s) // MARKET_SECONDS + 1) * MARKET_SECONDS
    previous_t0 = next_t0 - MARKET_SECONDS
    rollover = next_t0

    if shutting_down:
        return _refused("the collector is shutting down", now_ns, rollover, phases)
    if rollover in set(completed):
        return _refused("this rollover has already been maintained", now_ns, rollover, phases)

    quoting = sorted(
        slug for slug, phase in phases.items() if phase in {p.value for p in QUOTING_PHASES}
    )
    if quoting:
        return _refused(f"live session(s) quoting: {', '.join(quoting)}", now_ns, rollover, phases)

    stop_quoting_s = previous_t0 + config.stop_quoting_offset / NANOS_PER_SECOND
    quote_start_s = next_t0 + config.quote_start_offset / NANOS_PER_SECOND
    since_stop = now_s - stop_quoting_s
    to_quote = quote_start_s - now_s

    window = MaintenanceWindow(
        allowed=True,
        reason="",
        rollover=rollover,
        now_ns=now_ns,
        seconds_since_stop_quoting=since_stop,
        seconds_to_quote_start=to_quote,
        phases=phases,
    )
    if since_stop < 0:
        return _refused(
            f"the closing market is still quoting for another {-since_stop:.1f}s",
            now_ns,
            rollover,
            phases,
        )
    if to_quote < margin_s:
        return _refused(
            f"only {to_quote:.1f}s until the next market quotes; the margin is {margin_s:.1f}s",
            now_ns,
            rollover,
            phases,
        )
    return window


def _reading() -> dict[str, int | None]:
    """The allocator's own account, plus resident memory. Read either side of a trim."""
    status = memory_status()
    info = mallinfo2()
    return {
        "rss": status.get("VmRSS"),
        "rss_anon": status.get("RssAnon"),
        "arena": None if info is None else info["arena"],
        "uordblks": None if info is None else info["uordblks"],
        "fordblks": None if info is None else info["fordblks"],
        "hblkhd": None if info is None else info["hblkhd"],
    }


@dataclass(slots=True)
class AllocatorMaintenance:
    """One `malloc_trim(0)` per rollover, in the window, measured.

    Deliberately not adaptive. It does not look at resident memory, at `fordblks`, at how busy
    the market was or at how large the journal is before deciding — a policy that responded to
    any of those would turn one experiment into a search over policies, and the result would not
    say whether allocator maintenance works.
    """

    enabled: bool = True
    margin_s: float = MAINTENANCE_MARGIN_S
    shutting_down: bool = False
    completed: set[int] = field(default_factory=set)
    events: list[dict[str, Any]] = field(default_factory=list)
    refusals: dict[str, int] = field(default_factory=dict)

    def consider(self, now_ns: int, sessions: Iterable[Any]) -> MaintenanceWindow:
        """Evaluate the contract. Records why a refusal happened, without acting."""
        window = maintenance_window(
            now_ns,
            sessions,
            margin_s=self.margin_s,
            completed=self.completed,
            shutting_down=self.shutting_down,
        )
        if not window.allowed:
            key = window.reason.split(";")[0]
            self.refusals[key] = self.refusals.get(key, 0) + 1
        return window

    def claim(self, window: MaintenanceWindow) -> bool:
        """Take this rollover, once. Called before the trim so a second pass cannot repeat it."""
        if not window.allowed or window.rollover is None:
            return False
        if window.rollover in self.completed:
            return False
        self.completed.add(window.rollover)
        return True

    def trim(self, window: MaintenanceWindow) -> dict[str, Any]:
        """Read, trim, read. **Call this from a worker thread, never from the event loop.**

        It returns a record rather than raising: a diagnostic that could take a market down would
        be a worse problem than the one it is measuring. A platform without `malloc_trim` reports
        ``returned: None`` and releases nothing, which is a fact about the platform and not a
        measurement of zero.
        """
        before = _reading()
        started = perf_counter_ns()
        returned: bool | None = None
        error: str | None = None
        try:
            returned = malloc_trim(0)
        except Exception as failure:  # pragma: no cover - malloc_trim does not raise on glibc
            error = f"{type(failure).__name__}: {failure}"
        finished = perf_counter_ns()
        after = _reading()
        released = (
            None
            if before["rss"] is None or after["rss"] is None
            else int(before["rss"]) - int(after["rss"])
        )
        record: dict[str, Any] = {
            "rollover": window.rollover,
            "started_ns": started,
            "finished_ns": finished,
            "duration_ns": finished - started,
            "returned": returned,
            "error": error,
            "released_rss_bytes": released,
            "seconds_since_stop_quoting": window.seconds_since_stop_quoting,
            "seconds_to_quote_start": window.seconds_to_quote_start,
            "phases": dict(window.phases),
            "before": before,
            "after": after,
        }
        self.events.append(record)
        return record

    def summary(self) -> dict[str, Any]:
        from maker5m.telemetry.metrics import quantile

        durations = sorted(int(event["duration_ns"]) for event in self.events)
        released = sorted(
            int(event["released_rss_bytes"])
            for event in self.events
            if event["released_rss_bytes"] is not None
        )
        return {
            "policy": {
                "enabled": self.enabled,
                "action": "malloc_trim(0)",
                "per_rollover": 1,
                "margin_s": self.margin_s,
                "adaptive": False,
            },
            "trims": len(self.events),
            "successful": sum(1 for event in self.events if event["returned"] is True),
            "nothing_to_release": sum(1 for event in self.events if event["returned"] is False),
            "unsupported": sum(1 for event in self.events if event["returned"] is None),
            "errors": [event["error"] for event in self.events if event["error"]],
            "duration_ns": {
                "n": len(durations),
                "p50": quantile(durations, 0.50) if durations else None,
                "p95": quantile(durations, 0.95) if durations else None,
                "p99": quantile(durations, 0.99) if durations else None,
                "max": durations[-1] if durations else None,
            },
            "released_rss_bytes": {
                "n": len(released),
                "total": sum(released),
                "p50": quantile(released, 0.50) if released else None,
                "p95": quantile(released, 0.95) if released else None,
                "max": released[-1] if released else None,
            },
            "refusals": dict(sorted(self.refusals.items())),
        }
