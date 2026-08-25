"""The synchronized ingress clock.

P2's deterministic stream requires a **non-decreasing** ``EventMeta.timestamp``, and the
strategy reacts when data is *received*, not when a venue stamped it. So:

```text
EventMeta.timestamp = synchronized LOCAL INGRESS time
```

It is not an exchange timestamp. Source timestamps are retained separately in feed
diagnostics (:mod:`maker5m.feeds.diagnostics`) and never enter Plane 2 state, because doing
so would need a P2 contract change nothing has justified.

Why not ``time.time_ns()`` directly: the wall clock can jump backwards (NTP correction,
manual adjustment, leap-second handling), and a single backwards step would make the reducer
reject the event stream. Why not ``time.monotonic_ns()`` alone: it has no defined epoch, so
phase boundaries derived from a market's ``T0`` would be meaningless.

The anchor construction gives both properties:

```text
wall_anchor  = time.time_ns()        once, at start
mono_anchor  = time.monotonic_ns()   once, at start
ingress_time = wall_anchor + (time.monotonic_ns() - mono_anchor)
```

Wall-aligned, monotonic, and non-decreasing. The trade-off is deliberate: the clock does not
follow later NTP corrections, so it can drift from true wall time over a long run. That drift
is *measured* rather than corrected (:class:`~maker5m.feeds.diagnostics.ClockHealth`), because
correcting it would reintroduce the backwards jump this exists to prevent. P9 turns excessive
drift into a kill-switch condition.

Formal latency timestamping — separate source, local-receive, and monotonic-receive stamps —
is P8. This is only the single ordering timestamp the deterministic core requires.
"""

import time
from dataclasses import dataclass, field

from maker5m.market.timebase import TimestampNs

__all__ = ["IngressClock"]


@dataclass(slots=True)
class IngressClock:
    """Wall-aligned monotonic clock. Single-owner, used only by the ingress merger."""

    wall_anchor_ns: int = field(default_factory=time.time_ns)
    mono_anchor_ns: int = field(default_factory=time.monotonic_ns)
    _last: int = 0

    def now(self) -> TimestampNs:
        """The current ingress time. Never returns a value below a previous call."""
        value = self.wall_anchor_ns + (time.monotonic_ns() - self.mono_anchor_ns)
        if value < self._last:  # pragma: no cover - monotonic makes this unreachable
            value = self._last
        self._last = value
        return TimestampNs(value)

    def wall_offset_ns(self, wall_now_ns: int | None = None) -> int:
        """How far this clock has drifted from the raw system wall clock.

        Measured, never corrected. A large magnitude means the system clock moved under us.
        """
        wall = time.time_ns() if wall_now_ns is None else wall_now_ns
        return wall - (self.wall_anchor_ns + (time.monotonic_ns() - self.mono_anchor_ns))
