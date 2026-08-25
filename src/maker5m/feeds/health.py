"""Feed health and conservative continuity handling.

Polymarket's market payloads expose no documented monotonic sequence, so gap detection cannot
be sequence-based. It is therefore **conservative**: anything that could have interrupted
continuity — a disconnect, a heartbeat failure, a malformed message, an unknown token, a
resubscription — marks the CLOB stream unhealthy and requires a fresh authoritative snapshot
before it may be trusted again.

Staleness thresholds are ``OPERATIONAL`` configuration, not strategy parameters. Nothing in
the frozen sources establishes a stale-feed timeout, so these numbers are engineering choices
and are labelled as such. P6 only *emits* health events; turning them into a trading halt is
P9's job (Canonical §28.1).
"""

from dataclasses import dataclass
from typing import Final

from maker5m.domain import ParameterStatus
from maker5m.market.events import HealthComponent, HealthStatus
from maker5m.market.timebase import DurationNs, TimestampNs, seconds

__all__ = [
    "DEFAULT_CLOB_STALE_AFTER",
    "DEFAULT_SPOT_STALE_AFTER",
    "STALENESS_STATUS",
    "StalenessMonitor",
    "StreamHealth",
]

DEFAULT_SPOT_STALE_AFTER: Final[DurationNs] = seconds(5)
DEFAULT_CLOB_STALE_AFTER: Final[DurationNs] = seconds(10)
STALENESS_STATUS: Final = ParameterStatus.OPERATIONAL
"""Engineering thresholds. The frozen sources establish no stale-feed timeout."""


@dataclass(slots=True)
class StreamHealth:
    """Health of one stream, with the resnapshot requirement made explicit."""

    component: HealthComponent
    status: HealthStatus = HealthStatus.UNKNOWN
    awaiting_snapshot: bool = True
    last_message_at: TimestampNs | None = None

    def mark_message(self, at: TimestampNs) -> None:
        self.last_message_at = at

    def mark_disconnected(self) -> HealthStatus:
        """A disconnect always invalidates continuity."""
        self.status = HealthStatus.DISCONNECTED
        self.awaiting_snapshot = True
        return self.status

    def mark_uncertain(self) -> HealthStatus:
        """Continuity cannot be established: malformed data, unknown token, resubscription."""
        self.status = HealthStatus.SEQUENCE_GAP
        self.awaiting_snapshot = True
        return self.status

    def mark_snapshot(self, at: TimestampNs) -> HealthStatus:
        """A fresh authoritative snapshot is the only way back to HEALTHY."""
        self.awaiting_snapshot = False
        self.status = HealthStatus.HEALTHY
        self.last_message_at = at
        return self.status

    def mark_stale(self) -> HealthStatus:
        self.status = HealthStatus.STALE
        return self.status

    @property
    def healthy(self) -> bool:
        return self.status is HealthStatus.HEALTHY and not self.awaiting_snapshot


@dataclass(slots=True)
class StalenessMonitor:
    """Decides when a stream has gone quiet for too long."""

    threshold: DurationNs

    def is_stale(self, health: StreamHealth, now: TimestampNs) -> bool:
        if health.last_message_at is None:
            return False
        return now - health.last_message_at > self.threshold
