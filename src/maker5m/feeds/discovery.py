"""Read-only market discovery and pre-arm.

Sources, both public and documented:

* ``https://gamma-api.polymarket.com/events?slug=<slug>`` — event and market metadata,
  including ``clobTokenIds``, ``outcomes``, ``eventStartTime``, and ``cryptoMarketConfig``;
* ``https://clob.polymarket.com/markets/<condition_id>`` — venue order rules
  (``minimum_tick_size``, ``minimum_order_size``) and the authoritative token/outcome pairing.

The ``btc-updown-5m-*`` slug encodes ``T0`` as a Unix timestamp, so the current and next
markets are addressable directly and the next one exists well before it starts — which is what
makes pre-arm possible at all (Canonical §21).

Nothing here scrapes HTML, uses an undocumented endpoint, or sends credentials. Discovery
**fails closed**: zero matches, several ambiguous matches, or missing required metadata all
raise :class:`~maker5m.feeds.errors.DiscoveryError`. A market is never invented to keep the
pipeline moving.

Strike is **not** fabricated. See :func:`discover_market` for what the public metadata does and
does not provide.
"""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Final

from maker5m.feeds.errors import DiscoveryError
from maker5m.feeds.exactness import parse_venue_price, parse_venue_size
from maker5m.feeds.venue import VenueMarketRules
from maker5m.market.phase import CANONICAL_PHASE_CONFIG, PhaseConfig
from maker5m.market.state import MarketDefinition
from maker5m.market.timebase import NANOS_PER_SECOND, TimestampNs
from maker5m.numeric.units import PriceUnits

__all__ = [
    "GAMMA_BASE",
    "MARKET_PERIOD_SECONDS",
    "SLUG_PREFIX",
    "USER_AGENT",
    "DiscoveredMarket",
    "current_slug",
    "discover_market",
    "next_slug",
    "slug_for",
    "t0_of_slug",
]

GAMMA_BASE: Final = "https://gamma-api.polymarket.com"
CLOB_BASE: Final = "https://clob.polymarket.com"
SLUG_PREFIX: Final = "btc-updown-5m-"
MARKET_PERIOD_SECONDS: Final = 300
STRATEGY_TICK: Final = PriceUnits(10_000)
"""The replica's own quote grid (0.01). Never overwritten by venue metadata."""


def slug_for(t0_epoch_seconds: int) -> str:
    return f"{SLUG_PREFIX}{t0_epoch_seconds}"


def t0_of_slug(slug: str) -> int:
    if not slug.startswith(SLUG_PREFIX):
        raise DiscoveryError(f"slug {slug!r} is not in the {SLUG_PREFIX}* universe")
    tail = slug[len(SLUG_PREFIX) :]
    if not tail.isdigit():
        raise DiscoveryError(f"slug {slug!r} has no epoch suffix")
    return int(tail)


def current_slug(now_epoch_seconds: int) -> str:
    """The slug of the market currently in progress."""
    return slug_for(now_epoch_seconds - (now_epoch_seconds % MARKET_PERIOD_SECONDS))


def next_slug(now_epoch_seconds: int) -> str:
    """The slug of the market after the current one."""
    base = now_epoch_seconds - (now_epoch_seconds % MARKET_PERIOD_SECONDS)
    return slug_for(base + MARKET_PERIOD_SECONDS)


@dataclass(frozen=True, slots=True)
class DiscoveredMarket:
    """Everything discovery could establish about one market."""

    definition: MarketDefinition
    venue_rules: VenueMarketRules
    condition_id: str
    crypto_config: dict[str, Any]
    strike_available: bool
    """Whether authoritative strike metadata was published. Currently always ``False``."""

    raw_gamma: dict[str, Any]
    raw_clob: dict[str, Any]


USER_AGENT: Final = "maker5m-research/0.0 (read-only market-data client)"
"""An explicit User-Agent. The public API rejects the default ``Python-urllib`` one with 403,
and identifying the client honestly is better practice than impersonating a browser."""


def _get_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise DiscoveryError(f"discovery request failed for {url}: {exc}") from exc


def discover_market(
    slug: str, *, timeout: float = 15.0, phase_config: PhaseConfig = CANONICAL_PHASE_CONFIG
) -> DiscoveredMarket:
    """Resolve one ``btc-updown-5m-*`` market from public metadata.

    **Strike.** The frozen strategy assumes a chained strike
    (``coinPriceStart[N] == coinPriceEnd[N-1]``, Canonical §6.2). Neither the Gamma event
    payload nor the CLOB market payload publishes ``coinPriceStart`` or ``coinPriceEnd`` —
    resolution is a Chainlink BTC/USD TWAP over the window, and the reference price is not
    exposed as a field. So ``definition.strike`` is left ``None`` and ``strike_available`` is
    ``False``. Fabricating a strike from the previous market's outcome would be inventing data
    the venue never published.
    """
    t0_seconds = t0_of_slug(slug)
    events = _get_json(f"{GAMMA_BASE}/events?slug={slug}", timeout)
    if not isinstance(events, list) or len(events) != 1:
        raise DiscoveryError(
            f"gamma returned {len(events) if isinstance(events, list) else '?'} events for "
            f"slug {slug!r}; expected exactly 1"
        )
    markets = events[0].get("markets")
    if not isinstance(markets, list) or len(markets) != 1:
        raise DiscoveryError(
            f"event {slug!r} has {len(markets) if isinstance(markets, list) else '?'} markets; "
            "expected exactly 1"
        )
    market = markets[0]

    condition_id = market.get("conditionId")
    if not isinstance(condition_id, str) or not condition_id:
        raise DiscoveryError(f"market {slug!r} has no conditionId")

    clob = _get_json(f"{CLOB_BASE}/markets/{condition_id}", timeout)
    if not isinstance(clob, dict) or clob.get("condition_id") != condition_id:
        raise DiscoveryError(f"CLOB metadata for {condition_id!r} did not match")

    tokens = clob.get("tokens")
    if not isinstance(tokens, list) or len(tokens) != 2:
        raise DiscoveryError(f"market {slug!r} does not have exactly two tokens")
    by_outcome = {str(t.get("outcome", "")).upper(): str(t.get("token_id", "")) for t in tokens}
    up_token, down_token = by_outcome.get("UP"), by_outcome.get("DOWN")
    if not up_token or not down_token or up_token == down_token:
        raise DiscoveryError(
            f"market {slug!r} token/outcome pairing is ambiguous: {sorted(by_outcome)}"
        )

    tick_raw = clob.get("minimum_tick_size")
    size_raw = clob.get("minimum_order_size")
    venue_rules = VenueMarketRules(
        min_tick_size=(
            parse_venue_price(str(tick_raw), field_name="minimum_tick_size")
            if tick_raw is not None
            else None
        ),
        min_order_size=(
            parse_venue_size(str(size_raw), field_name="minimum_order_size")
            if size_raw is not None
            else None
        ),
        source="clob/markets",
    )

    definition = MarketDefinition(
        market_id=condition_id,
        slug=slug,
        up_token_id=up_token,
        down_token_id=down_token,
        t0=TimestampNs(t0_seconds * NANOS_PER_SECOND),
        phase_config=phase_config,
        # The replica's own grid, from the frozen strategy - not the venue's announced tick.
        tick=STRATEGY_TICK,
        strike=None,
    )

    crypto = market.get("cryptoMarketConfig")
    return DiscoveredMarket(
        definition=definition,
        venue_rules=venue_rules,
        condition_id=condition_id,
        crypto_config=crypto if isinstance(crypto, dict) else {},
        strike_available=False,
        raw_gamma=market,
        raw_clob=clob,
    )
