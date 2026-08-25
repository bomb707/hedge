"""Market data adapters. Plane 1. Built in P6. First network access in the project.

See ``docs/ARCHITECTURE_SSOT.md`` §4 and §11.

Read-only public market data only: Polymarket's market WebSocket, Binance's public spot
stream, and public discovery endpoints. **No order endpoint, no credential, no wallet key, no
signing, no write path** exists here or anywhere this package imports. Execution begins at P7.

Three contracts this package establishes:

* ``EventMeta.timestamp`` is **synchronized local ingress time**, not a venue timestamp —
  the strategy reacts when data is received (:mod:`~maker5m.feeds.ingress_clock`);
* one :class:`~maker5m.feeds.merger.IngressMerger` assigns every ``ingress_ordinal``, so
  there is exactly one legal production event order for P5 to replay;
* the venue's announced tick is recorded as venue metadata and never mutates the strategy's
  quote grid (:mod:`~maker5m.feeds.venue`).

External BTC spot can wake the decision path on its own, with no Polymarket message involved
(invariant I11).
"""

from maker5m.feeds.binance import (
    BINANCE_REST_BASE,
    BINANCE_WS_BASE,
    DEFAULT_SYMBOL,
    BinanceSymbolRules,
    SpotMessage,
    agg_trade_stream,
    parse_agg_trade,
    parse_btc_price,
    parse_symbol_rules,
)
from maker5m.feeds.diagnostics import ClockHealth, FeedCounters
from maker5m.feeds.discovery import (
    DiscoveredMarket,
    current_slug,
    discover_market,
    next_slug,
    slug_for,
    t0_of_slug,
)
from maker5m.feeds.errors import (
    DiscoveryError,
    ExactnessError,
    FeedConformanceError,
    FeedError,
    TransportError,
)
from maker5m.feeds.exactness import (
    PrecisionObserver,
    decimals_in,
    parse_venue_price,
    parse_venue_size,
)
from maker5m.feeds.health import (
    DEFAULT_CLOB_STALE_AFTER,
    DEFAULT_SPOT_STALE_AFTER,
    STALENESS_STATUS,
    StalenessMonitor,
    StreamHealth,
)
from maker5m.feeds.ingress_clock import IngressClock
from maker5m.feeds.merger import BoundedSink, IngressMerger
from maker5m.feeds.pipeline import MarketDataPipeline
from maker5m.feeds.polymarket import (
    POLYMARKET_CLOB_REST,
    POLYMARKET_MARKET_WS,
    POLYMARKET_PING,
    POLYMARKET_PONG,
    BookTracker,
    MarketEventKind,
    ParsedBook,
    ParsedMessage,
    ParsedPriceChange,
    ParsedTickSize,
    TokenBook,
    parse_market_message,
    subscribe_payload,
)
from maker5m.feeds.venue import VenueMarketRules, VenueRulesTracker

__all__ = [
    "BINANCE_REST_BASE",
    "BINANCE_WS_BASE",
    "DEFAULT_CLOB_STALE_AFTER",
    "DEFAULT_SPOT_STALE_AFTER",
    "DEFAULT_SYMBOL",
    "POLYMARKET_CLOB_REST",
    "POLYMARKET_MARKET_WS",
    "POLYMARKET_PING",
    "POLYMARKET_PONG",
    "STALENESS_STATUS",
    "BinanceSymbolRules",
    "BookTracker",
    "BoundedSink",
    "ClockHealth",
    "DiscoveredMarket",
    "DiscoveryError",
    "ExactnessError",
    "FeedConformanceError",
    "FeedCounters",
    "FeedError",
    "IngressClock",
    "IngressMerger",
    "MarketDataPipeline",
    "MarketEventKind",
    "ParsedBook",
    "ParsedMessage",
    "ParsedPriceChange",
    "ParsedTickSize",
    "PrecisionObserver",
    "SpotMessage",
    "StalenessMonitor",
    "StreamHealth",
    "TokenBook",
    "TransportError",
    "VenueMarketRules",
    "VenueRulesTracker",
    "agg_trade_stream",
    "current_slug",
    "decimals_in",
    "discover_market",
    "next_slug",
    "parse_agg_trade",
    "parse_btc_price",
    "parse_market_message",
    "parse_symbol_rules",
    "parse_venue_price",
    "parse_venue_size",
    "slug_for",
    "subscribe_payload",
    "t0_of_slug",
]
