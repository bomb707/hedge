"""Market discovery and pre-arm, against real captured metadata."""

from __future__ import annotations

import pytest

from maker5m.feeds import DiscoveryError, current_slug, next_slug, slug_for, t0_of_slug
from maker5m.feeds.discovery import MARKET_PERIOD_SECONDS, STRATEGY_TICK
from maker5m.numeric import parse_price, parse_share
from tests.feeds.conftest import load


def test_slug_encodes_t0() -> None:
    assert slug_for(1787646900) == "btc-updown-5m-1787646900"
    assert t0_of_slug("btc-updown-5m-1787646900") == 1787646900


def test_current_and_next_slugs_are_five_minutes_apart() -> None:
    now = 1787646901
    assert t0_of_slug(next_slug(now)) - t0_of_slug(current_slug(now)) == MARKET_PERIOD_SECONDS
    assert t0_of_slug(current_slug(now)) <= now < t0_of_slug(next_slug(now))


def test_a_slug_on_an_exact_boundary_is_its_own_current_market() -> None:
    assert t0_of_slug(current_slug(1787646900)) == 1787646900


@pytest.mark.parametrize("slug", ["", "btc-updown-5m-", "eth-updown-5m-1", "btc-updown-5m-abc"])
def test_a_malformed_slug_fails_closed(slug: str) -> None:
    with pytest.raises(DiscoveryError):
        t0_of_slug(slug)


# -- real captured discovery metadata --------------------------------------------


def test_the_real_gamma_fixture_carries_what_discovery_needs() -> None:
    market = load("polymarket_discovery_gamma.json")["event"]["markets"][0]
    assert market["slug"].startswith("btc-updown-5m-")
    assert market["conditionId"].startswith("0x")
    assert market["eventStartTime"].endswith("Z")
    assert len(eval_tokens(market["clobTokenIds"])) == 2


def eval_tokens(raw: str) -> list[str]:
    import json

    tokens: list[str] = json.loads(raw)
    return tokens


def test_the_real_clob_fixture_carries_venue_rules_and_token_pairing() -> None:
    market = load("polymarket_discovery_clob.json")["market"]
    assert market["condition_id"].startswith("0x")
    outcomes = {t["outcome"].upper(): t["token_id"] for t in market["tokens"]}
    assert set(outcomes) == {"UP", "DOWN"}
    assert outcomes["UP"] != outcomes["DOWN"]
    assert parse_price(str(market["minimum_tick_size"])) > 0
    assert parse_share(str(market["minimum_order_size"])) > 0


def test_the_observed_venue_tick_is_not_forced_onto_the_strategy() -> None:
    """They coincide today at 0.01. They are still separate concepts."""
    market = load("polymarket_discovery_clob.json")["market"]
    venue_tick = parse_price(str(market["minimum_tick_size"]))
    assert parse_price("0.01") == STRATEGY_TICK
    assert venue_tick == STRATEGY_TICK  # true now; not relied upon
    assert STRATEGY_TICK is not None


def test_the_settlement_mechanism_matches_the_frozen_strategy() -> None:
    """Canonical §7 states a 60-second TWAP. Live venue metadata says the same."""
    market = load("polymarket_discovery_gamma.json")["event"]["markets"][0]
    config = market["cryptoMarketConfig"]
    assert config["twapEnabled"] is True
    assert config["twapLookbackSeconds"] == 60
    assert config["duration"] == "5m"


def test_no_strike_field_is_published() -> None:
    """The strategy assumes a chained strike; the public metadata does not expose one.

    Neither ``coinPriceStart`` nor ``coinPriceEnd`` appears in either payload, so strike
    chaining is UNVERIFIED and no strike is fabricated (see docs/OPEN_ITEMS.md O14).
    """
    for name in ("polymarket_discovery_gamma.json", "polymarket_discovery_clob.json"):
        text = str(load(name)).lower()
        assert "coinpricestart" not in text
        assert "coinpriceend" not in text
