"""Feed-side diagnostics kept outside deterministic Plane 2 state.

Source/venue timestamps, clock drift, message counters, and drop counts all live here. None
of it enters ``MarketState``: adding fields there merely because they are convenient would be
an unjustified P2 contract change, and would put data into the replay journal that plays no
part in any decision.

Formal latency attribution — separate send, receive, and decode stamps per stage — is P8.
"""

from dataclasses import dataclass, field

from maker5m.market.timebase import TimestampNs

__all__ = ["ClockHealth", "FeedCounters"]


@dataclass(slots=True)
class ClockHealth:
    """Measures drift between the ingress clock and other time sources.

    Measurement only. Nothing here adjusts the OS clock, corrects the ingress clock, or blocks
    the hot path on a time request — correcting mid-run would reintroduce the backwards jump
    the ingress clock exists to prevent. P9 turns excessive drift into a kill-switch condition.
    """

    samples: int = 0
    last_offset_ns: int = 0
    max_abs_offset_ns: int = 0

    def observe(self, offset_ns: int) -> None:
        self.samples += 1
        self.last_offset_ns = offset_ns
        self.max_abs_offset_ns = max(self.max_abs_offset_ns, abs(offset_ns))

    def summary(self) -> dict[str, int]:
        return {
            "samples": self.samples,
            "last_offset_ns": self.last_offset_ns,
            "max_abs_offset_ns": self.max_abs_offset_ns,
        }


@dataclass(slots=True)
class FeedCounters:
    """What arrived, what was handled, and what was not."""

    clob_messages: int = 0
    clob_books: int = 0
    clob_price_changes: int = 0
    clob_tick_size_changes: int = 0
    clob_best_bid_ask: int = 0
    clob_last_trade_price: int = 0
    clob_unhandled: int = 0
    unhandled_kinds: dict[str, int] = field(default_factory=dict)
    spot_messages: int = 0
    pongs: int = 0
    reconnects: int = 0
    malformed: int = 0
    telemetry_dropped: int = 0
    source_timestamps: list[tuple[TimestampNs, int]] = field(default_factory=list)

    def note_unhandled(self, kind: str) -> None:
        self.clob_unhandled += 1
        self.unhandled_kinds[kind] = self.unhandled_kinds.get(kind, 0) + 1

    def summary(self) -> dict[str, object]:
        return {
            "clob_messages": self.clob_messages,
            "clob_books": self.clob_books,
            "clob_price_changes": self.clob_price_changes,
            "clob_tick_size_changes": self.clob_tick_size_changes,
            "clob_best_bid_ask": self.clob_best_bid_ask,
            "clob_last_trade_price": self.clob_last_trade_price,
            "clob_unhandled": self.clob_unhandled,
            "unhandled_kinds": dict(self.unhandled_kinds),
            "spot_messages": self.spot_messages,
            "pongs": self.pongs,
            "reconnects": self.reconnects,
            "malformed": self.malformed,
            "telemetry_dropped": self.telemetry_dropped,
        }
