"""External BTC spot adapter (Binance public market data, read-only).

Purpose in this project is narrow and worth stating plainly: the external feed exists so the
decision path can be woken by spot alone, independently of Polymarket CLOB updates
(invariant I11). Its established value is latency and queue pre-emption, **not** superior
directional prediction (Canonical §9.1). P6 is transport plumbing; the spot-to-CLOB timing
model remains O09 and is not attempted here.

Stream choice
-------------
``<symbol>@aggTrade``. Its ``p`` field is a single exact traded price as a decimal string —
one number, no derivation, no normalisation choice to justify. ``bookTicker`` was not used
precisely because turning a bid and an ask into one ``SpotTick`` requires a midpoint rule,
and silently inventing one would be an unrecorded OPERATIONAL feed-normalisation decision.
If a future phase switches streams, that rule must be written down first.

Precision
---------
Prices are decimal strings and symbol precision is **metadata-driven**
(``PRICE_FILTER.tickSize``, ``quotePrecision`` from ``/api/v3/exchangeInfo``). There is no
single global BTC precision to hard-code, which is the core evidence behind O12's closure:
:class:`~maker5m.market.btc_price.BtcPrice` stays self-describing and derives its scale from
the exact string the feed sent.

``float()`` is never applied to a price. The raw JSON text is parsed directly.
"""

import json
from dataclasses import dataclass
from typing import Any, Final

from maker5m.feeds.errors import FeedConformanceError
from maker5m.feeds.exactness import PrecisionObserver, decimals_in
from maker5m.market.btc_price import MAX_BTC_SCALE_DECIMALS, BtcPrice
from maker5m.numeric.errors import NumericError

__all__ = [
    "BINANCE_REST_BASE",
    "BINANCE_WS_BASE",
    "DEFAULT_SYMBOL",
    "BinanceSymbolRules",
    "SpotMessage",
    "agg_trade_stream",
    "parse_agg_trade",
    "parse_btc_price",
    "parse_symbol_rules",
]

BINANCE_WS_BASE: Final = "wss://stream.binance.com:9443/ws"
BINANCE_REST_BASE: Final = "https://api.binance.com"
DEFAULT_SYMBOL: Final = "BTCUSDT"


def agg_trade_stream(symbol: str = DEFAULT_SYMBOL) -> str:
    """The documented aggregate-trade stream name for a symbol."""
    return f"{symbol.lower()}@aggTrade"


@dataclass(frozen=True, slots=True)
class BinanceSymbolRules:
    """Official symbol metadata. The authority on this symbol's price precision."""

    symbol: str
    tick_size: str
    min_price: str
    quote_precision: int
    base_asset_precision: int

    @property
    def tick_decimals(self) -> int:
        """Decimals implied by ``PRICE_FILTER.tickSize``, as the venue writes it."""
        return decimals_in(self.tick_size)


def parse_symbol_rules(payload: str | bytes, symbol: str) -> BinanceSymbolRules:
    """Extract one symbol's rules from an ``/api/v3/exchangeInfo`` response."""
    try:
        data: Any = json.loads(payload)
        entries = [s for s in data["symbols"] if s["symbol"] == symbol]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise FeedConformanceError(f"exchangeInfo payload is not usable: {exc}") from exc
    if len(entries) != 1:
        raise FeedConformanceError(
            f"exchangeInfo returned {len(entries)} entries for {symbol!r}; expected exactly 1"
        )
    entry = entries[0]
    filters = {f.get("filterType"): f for f in entry.get("filters", [])}
    price_filter = filters.get("PRICE_FILTER")
    if price_filter is None:
        raise FeedConformanceError(f"{symbol}: exchangeInfo has no PRICE_FILTER")
    return BinanceSymbolRules(
        symbol=entry["symbol"],
        tick_size=str(price_filter["tickSize"]),
        min_price=str(price_filter["minPrice"]),
        quote_precision=int(entry["quotePrecision"]),
        base_asset_precision=int(entry["baseAssetPrecision"]),
    )


def parse_btc_price(text: str, observer: PrecisionObserver | None = None) -> BtcPrice:
    """Parse a BTC price decimal string into an exact self-describing value.

    The scale comes from the string itself, so whatever precision the feed sends is
    represented exactly with nothing rounded and nothing assumed.
    """
    if not isinstance(text, str):
        raise FeedConformanceError(
            f"BTC price must arrive as a decimal string, got {type(text).__name__}; "
            "a JSON float has already lost exactness"
        )
    if observer is not None:
        observer.observe(text)
    places = decimals_in(text)
    if places > MAX_BTC_SCALE_DECIMALS:
        raise FeedConformanceError(
            f"BTC price {text!r} carries {places} decimals, beyond the sanity bound "
            f"{MAX_BTC_SCALE_DECIMALS}"
        )
    try:
        return BtcPrice.parse(text, scale_decimals=places)
    except NumericError as exc:
        raise FeedConformanceError(f"BTC price {text!r} is not parseable: {exc}") from exc


@dataclass(frozen=True, slots=True)
class SpotMessage:
    """One normalized external spot observation, before ingress metadata is attached."""

    price: BtcPrice
    source_sequence: int | None
    source_timestamp_ms: int | None
    """The venue's own stamp. Diagnostics only — it never becomes EventMeta.timestamp."""


def parse_agg_trade(payload: str | bytes, observer: PrecisionObserver | None = None) -> SpotMessage:
    """Parse one ``@aggTrade`` message.

    Documented fields used: ``p`` (price, decimal string), ``a`` (aggregate trade id),
    ``T`` (trade time, ms). Anything malformed raises rather than yielding a partial value.
    """
    try:
        data: Any = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise FeedConformanceError(f"aggTrade payload is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise FeedConformanceError(f"aggTrade payload is not an object: {type(data).__name__}")
    if "p" not in data:
        raise FeedConformanceError(f"aggTrade message has no price field 'p': {sorted(data)}")

    sequence = data.get("a")
    stamp = data.get("T")
    return SpotMessage(
        price=parse_btc_price(data["p"], observer),
        source_sequence=int(sequence) if isinstance(sequence, int) else None,
        source_timestamp_ms=int(stamp) if isinstance(stamp, int) else None,
    )
