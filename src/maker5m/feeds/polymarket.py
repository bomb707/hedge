"""Polymarket CLOB market-data adapter (public WebSocket, read-only).

Endpoint: ``wss://ws-subscriptions-clob.polymarket.com/ws/market``, subscribing to both
outcome token ids. Application heartbeat: send the text ``PING`` every 10 s; the server
replies ``PONG``.

Documented market events handled: ``book``, ``price_change``, ``tick_size_change``, and
``best_bid_ask`` where the feature is enabled. Unknown event types are surfaced as an
explicit "unhandled" result and counted — never silently dropped, because a venue adding an
event we ignore would degrade the book without any signal.

Two rules this module exists to enforce
---------------------------------------
**The DOWN book is observed, never derived.** Canonical §5.2's mirror identity is conditional
on all levels mapping exactly, and post-only safety is a hard invariant (I06). The adapter
records what the venue actually sent for each token separately; Up-space reasoning belongs to
the strategy, not to the transport.

**No sequence number is invented.** The current payloads carry ``timestamp`` and ``hash``, and
neither is a documented monotonic sequence with defined continuity semantics. Mapping either
into ``BookUpdate.sequence`` would fabricate a guarantee that does not exist, so ``sequence``
stays ``None`` and continuity is handled conservatively by health signalling instead
(:mod:`maker5m.feeds.health`).

**Venue tick is not strategy tick.** ``book`` messages carry ``tick_size`` and the venue can
emit ``tick_size_change``. That is the venue's currently legal order-price increment, recorded
in :class:`~maker5m.feeds.venue.VenueMarketRules` for P7's submission-legality checks. It does
**not** mutate ``MarketDefinition.tick``: the replica quotes on its documented ``0.01`` grid
unless an empirical strategy revision says otherwise.
"""

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

from maker5m.feeds.errors import FeedConformanceError
from maker5m.feeds.exactness import PrecisionObserver, parse_venue_price, parse_venue_size
from maker5m.market.events import BookLevel
from maker5m.numeric.units import PriceUnits, ShareUnits

__all__ = [
    "POLYMARKET_CLOB_REST",
    "POLYMARKET_MARKET_WS",
    "POLYMARKET_PING",
    "POLYMARKET_PONG",
    "BookTracker",
    "MarketEventKind",
    "ParsedBook",
    "ParsedMessage",
    "ParsedPriceChange",
    "ParsedTickSize",
    "TokenBook",
    "parse_market_message",
    "subscribe_payload",
]

POLYMARKET_MARKET_WS: Final = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
POLYMARKET_CLOB_REST: Final = "https://clob.polymarket.com"
POLYMARKET_PING: Final = "PING"
POLYMARKET_PONG: Final = "PONG"


def subscribe_payload(token_ids: tuple[str, ...]) -> str:
    """The documented market-channel subscription message. Read-only: no auth is sent."""
    return json.dumps({"assets_ids": list(token_ids), "type": "market"})


class MarketEventKind(Enum):
    """Documented market-channel event types this adapter handles."""

    BOOK = "book"
    PRICE_CHANGE = "price_change"
    TICK_SIZE_CHANGE = "tick_size_change"
    BEST_BID_ASK = "best_bid_ask"
    LAST_TRADE_PRICE = "last_trade_price"


@dataclass(frozen=True, slots=True)
class ParsedBook:
    """A full book snapshot for one token."""

    asset_id: str
    market: str
    bids: tuple[tuple[PriceUnits, ShareUnits], ...]
    asks: tuple[tuple[PriceUnits, ShareUnits], ...]
    tick_size: PriceUnits | None
    source_timestamp_ms: int | None
    venue_hash: str | None
    """Recorded for diagnostics. Deliberately **not** used as a sequence number."""


@dataclass(frozen=True, slots=True)
class ParsedPriceChange:
    """One level change within a ``price_change`` message."""

    asset_id: str
    price: PriceUnits
    size: ShareUnits
    side: str
    best_bid: PriceUnits | None
    best_ask: PriceUnits | None


@dataclass(frozen=True, slots=True)
class ParsedTickSize:
    """An observed venue tick change. Venue metadata, not strategy state."""

    asset_id: str
    old_tick_size: PriceUnits | None
    new_tick_size: PriceUnits


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    """The normalized result of one venue message.

    ``unhandled_kind`` is populated for a documented-but-unsupported or entirely unknown
    event type, so it can be counted and reported rather than vanishing.
    """

    kind: MarketEventKind | None
    book: ParsedBook | None = None
    price_changes: tuple[ParsedPriceChange, ...] = ()
    tick_size: ParsedTickSize | None = None
    source_timestamp_ms: int | None = None
    unhandled_kind: str | None = None


def _levels(
    raw: Any, side: str, observer: PrecisionObserver | None, size_observer: PrecisionObserver | None
) -> tuple[tuple[PriceUnits, ShareUnits], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise FeedConformanceError(f"book {side} is not a list: {type(raw).__name__}")
    out: list[tuple[PriceUnits, ShareUnits]] = []
    for entry in raw:
        if not isinstance(entry, dict) or "price" not in entry or "size" not in entry:
            raise FeedConformanceError(f"book {side} level is malformed: {entry!r}")
        out.append(
            (
                parse_venue_price(
                    entry["price"], field_name=f"book.{side}.price", observer=observer
                ),
                parse_venue_size(
                    entry["size"], field_name=f"book.{side}.size", observer=size_observer
                ),
            )
        )
    return tuple(out)


def _opt_price(raw: Any, field_name: str, observer: PrecisionObserver | None) -> PriceUnits | None:
    if raw is None:
        return None
    return parse_venue_price(raw, field_name=field_name, observer=observer)


def _stamp(raw: Any) -> int | None:
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return raw if isinstance(raw, int) else None


def parse_market_message(
    message: Any,
    *,
    price_observer: PrecisionObserver | None = None,
    size_observer: PrecisionObserver | None = None,
) -> ParsedMessage:
    """Normalize one market-channel message object.

    Accepts an already-decoded object; the transport handles framing and the ``PONG`` reply,
    which is not JSON.
    """
    if not isinstance(message, dict):
        raise FeedConformanceError(f"market message is not an object: {type(message).__name__}")
    raw_kind = message.get("event_type")
    if not isinstance(raw_kind, str):
        raise FeedConformanceError(f"market message has no event_type: {sorted(message)}")

    try:
        kind = MarketEventKind(raw_kind)
    except ValueError:
        return ParsedMessage(
            kind=None, unhandled_kind=raw_kind, source_timestamp_ms=_stamp(message.get("timestamp"))
        )

    stamp = _stamp(message.get("timestamp"))

    if kind is MarketEventKind.BOOK:
        asset_id = message.get("asset_id")
        if not isinstance(asset_id, str):
            raise FeedConformanceError("book message has no asset_id")
        return ParsedMessage(
            kind=kind,
            source_timestamp_ms=stamp,
            book=ParsedBook(
                asset_id=asset_id,
                market=str(message.get("market", "")),
                bids=_levels(message.get("bids"), "bids", price_observer, size_observer),
                asks=_levels(message.get("asks"), "asks", price_observer, size_observer),
                tick_size=_opt_price(message.get("tick_size"), "book.tick_size", price_observer),
                source_timestamp_ms=stamp,
                venue_hash=message.get("hash") if isinstance(message.get("hash"), str) else None,
            ),
        )

    if kind is MarketEventKind.PRICE_CHANGE:
        raw_changes = message.get("price_changes")
        if not isinstance(raw_changes, list):
            raise FeedConformanceError("price_change message has no price_changes list")
        changes: list[ParsedPriceChange] = []
        for entry in raw_changes:
            if not isinstance(entry, dict):
                raise FeedConformanceError(f"price_change entry is malformed: {entry!r}")
            for required in ("asset_id", "price", "size", "side"):
                if required not in entry:
                    raise FeedConformanceError(
                        f"price_change entry missing {required!r}: {sorted(entry)}"
                    )
            changes.append(
                ParsedPriceChange(
                    asset_id=str(entry["asset_id"]),
                    price=parse_venue_price(
                        entry["price"], field_name="price_change.price", observer=price_observer
                    ),
                    size=parse_venue_size(
                        entry["size"], field_name="price_change.size", observer=size_observer
                    ),
                    side=str(entry["side"]),
                    best_bid=_opt_price(
                        entry.get("best_bid"), "price_change.best_bid", price_observer
                    ),
                    best_ask=_opt_price(
                        entry.get("best_ask"), "price_change.best_ask", price_observer
                    ),
                )
            )
        return ParsedMessage(kind=kind, price_changes=tuple(changes), source_timestamp_ms=stamp)

    if kind is MarketEventKind.TICK_SIZE_CHANGE:
        new_tick = message.get("new_tick_size")
        if new_tick is None:
            raise FeedConformanceError("tick_size_change has no new_tick_size")
        return ParsedMessage(
            kind=kind,
            source_timestamp_ms=stamp,
            tick_size=ParsedTickSize(
                asset_id=str(message.get("asset_id", "")),
                old_tick_size=_opt_price(
                    message.get("old_tick_size"), "tick_size_change.old", price_observer
                ),
                new_tick_size=parse_venue_price(
                    new_tick, field_name="tick_size_change.new", observer=price_observer
                ),
            ),
        )

    if kind is MarketEventKind.BEST_BID_ASK:
        asset_id = message.get("asset_id")
        if not isinstance(asset_id, str):
            raise FeedConformanceError("best_bid_ask message has no asset_id")
        return ParsedMessage(
            kind=kind,
            source_timestamp_ms=stamp,
            price_changes=(
                ParsedPriceChange(
                    asset_id=asset_id,
                    price=PriceUnits(0),
                    size=ShareUnits(0),
                    side="BEST",
                    best_bid=_opt_price(
                        message.get("best_bid"), "best_bid_ask.best_bid", price_observer
                    ),
                    best_ask=_opt_price(
                        message.get("best_ask"), "best_bid_ask.best_ask", price_observer
                    ),
                ),
            ),
        )

    # last_trade_price carries no book information this phase needs.
    return ParsedMessage(kind=kind, source_timestamp_ms=stamp)


@dataclass(slots=True)
class TokenBook:
    """The observed ladder for one token. Price -> resting size."""

    bids: dict[int, int] = field(default_factory=dict)
    asks: dict[int, int] = field(default_factory=dict)
    snapshot_seen: bool = False

    def apply_snapshot(self, book: ParsedBook) -> None:
        self.bids = {int(p): int(s) for p, s in book.bids if s > 0}
        self.asks = {int(p): int(s) for p, s in book.asks if s > 0}
        self.snapshot_seen = True

    def apply_change(self, change: ParsedPriceChange) -> None:
        side = self.bids if change.side.upper() == "BUY" else self.asks
        if change.size == 0:
            side.pop(int(change.price), None)
        else:
            side[int(change.price)] = int(change.size)

    def best_bid(self) -> BookLevel | None:
        if not self.bids:
            return None
        price = max(self.bids)
        return BookLevel(PriceUnits(price), ShareUnits(self.bids[price]))

    def best_ask(self) -> BookLevel | None:
        if not self.asks:
            return None
        price = min(self.asks)
        return BookLevel(PriceUnits(price), ShareUnits(self.asks[price]))

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.snapshot_seen = False


@dataclass(slots=True)
class BookTracker:
    """Holds the UP and DOWN ladders **separately**, exactly as observed.

    Nothing here derives one side from the other, and nothing applies the Up-space
    complement. That translation is the strategy's, and only after the venue's real state has
    been recorded.
    """

    up_token_id: str
    down_token_id: str
    up: TokenBook = field(default_factory=TokenBook)
    down: TokenBook = field(default_factory=TokenBook)
    unknown_token_messages: int = 0

    def book_for(self, asset_id: str) -> TokenBook | None:
        if asset_id == self.up_token_id:
            return self.up
        if asset_id == self.down_token_id:
            return self.down
        self.unknown_token_messages += 1
        return None

    def apply(self, parsed: ParsedMessage) -> bool:
        """Apply a normalized message. Returns whether observed top-of-book may have moved."""
        if parsed.book is not None:
            book = self.book_for(parsed.book.asset_id)
            if book is None:
                return False
            book.apply_snapshot(parsed.book)
            return True
        touched = False
        for change in parsed.price_changes:
            if change.side == "BEST":
                continue
            book = self.book_for(change.asset_id)
            if book is None:
                continue
            book.apply_change(change)
            touched = True
        return touched

    @property
    def ready(self) -> bool:
        """Both sides have an authoritative snapshot. Required before trusting the book."""
        return self.up.snapshot_seen and self.down.snapshot_seen

    def clear(self) -> None:
        """Drop all state. Used when continuity is uncertain and a resnapshot is required."""
        self.up.clear()
        self.down.clear()
