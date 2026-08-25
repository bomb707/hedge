"""Immutable snapshot published to Plane 3.

Plane 3 -- telemetry, persistence, UI -- reads only snapshots. It never touches the
authoritative ``MarketState``, never holds a lock the trading path can wait on, and may fall
arbitrarily behind or die without affecting trading (invariant I19,
``docs/ARCHITECTURE_SSOT.md`` section 2.1).

Everything here is frozen and by value. The order map becomes a tuple sorted by client order
id, so a snapshot of the same state is always byte-identical regardless of dict insertion
history -- that determinism is what lets a snapshot be compared across a replay.

Producing a snapshot is a handful of field reads and one small sort. There is no
serialization, no copying of the book, and no I/O: encoding is Plane 3's problem, on Plane
3's thread.
"""

from dataclasses import dataclass

from maker5m.accounting.ledger import LedgerState
from maker5m.domain import Outcome
from maker5m.market.btc_price import BtcPrice
from maker5m.market.events import BookLevel
from maker5m.market.phase import Phase
from maker5m.market.state import HealthState, MarketState, OrderRecord
from maker5m.market.timebase import TimestampNs

__all__ = ["MarketSnapshot", "snapshot"]


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """A read-only, deterministically-produced view of one market.

    ``ledger`` is the accepted P1 :class:`~maker5m.accounting.ledger.LedgerState`, embedded
    rather than flattened. It is itself frozen, so this leaks no mutable reference, and it
    preserves the rule that a PnL figure requires an explicit rebate mode -- a snapshot that
    pre-computed one number would have had to pick a mode silently.
    """

    market_id: str
    slug: str
    phase: Phase
    t0: TimestampNs
    market_end: TimestampNs
    last_event_timestamp: TimestampNs
    last_ingress_ordinal: int
    phase_config_version: str
    strike: BtcPrice | None
    spot: BtcPrice | None
    spot_timestamp: TimestampNs | None
    up_bid: BookLevel | None
    up_ask: BookLevel | None
    down_bid: BookLevel | None
    down_ask: BookLevel | None
    book_timestamp: TimestampNs | None
    book_sequence: int | None
    ledger: LedgerState
    orders: tuple[OrderRecord, ...]
    health: HealthState
    fill_count: int
    resolution: Outcome | None


def snapshot(state: MarketState) -> MarketSnapshot:
    """Produce the Plane 3 view of ``state``. Pure, cheap, and deterministic."""
    book = state.book
    spot = state.spot
    return MarketSnapshot(
        market_id=state.definition.market_id,
        slug=state.definition.slug,
        phase=state.phase,
        t0=state.definition.t0,
        market_end=state.definition.market_end,
        last_event_timestamp=state.last_event_timestamp,
        last_ingress_ordinal=state.last_ingress_ordinal,
        phase_config_version=state.definition.phase_config.version,
        strike=state.definition.strike,
        spot=None if spot is None else spot.price,
        spot_timestamp=None if spot is None else spot.meta.timestamp,
        up_bid=None if book is None else book.up_bid,
        up_ask=None if book is None else book.up_ask,
        down_bid=None if book is None else book.down_bid,
        down_ask=None if book is None else book.down_ask,
        book_timestamp=None if book is None else book.meta.timestamp,
        book_sequence=None if book is None else book.sequence,
        ledger=state.ledger,
        orders=tuple(state.orders[key] for key in sorted(state.orders)),
        health=state.health,
        fill_count=len(state.applied_fill_ids),
        resolution=state.resolution,
    )
