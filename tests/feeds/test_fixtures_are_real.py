"""Every fixture is a labelled real public capture, with its provenance recorded."""

from __future__ import annotations

import pytest

from tests.feeds.conftest import FIXTURES, load

FIXTURE_NAMES = sorted(p.name for p in FIXTURES.glob("*.json"))


def test_fixtures_exist() -> None:
    assert FIXTURE_NAMES, "no real fixtures were captured"


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_every_fixture_records_its_provenance(name: str) -> None:
    """Synthetic and real data must never be confusable, here or in a journal."""
    data = load(name)
    assert data["_fixture_kind"] == "REAL_PUBLIC_FIXTURE"
    assert data["_captured_at_utc"].endswith("Z")
    assert data["_source"]
    assert data["_endpoint"].startswith(("https://", "wss://"))


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_no_fixture_carries_a_credential(name: str) -> None:
    """These are public read-only endpoints; nothing secret should be capturable."""
    text = (FIXTURES / name).read_text(encoding="utf-8").lower()
    for marker in ("api_key", "apikey", "secret", "passphrase", "private_key", "authorization"):
        assert marker not in text, f"{name} contains {marker!r}"


def test_polymarket_fixtures_identify_their_market_and_tokens() -> None:
    for name in ("polymarket_book.json", "polymarket_price_change.json"):
        data = load(name)
        assert data["_market_slug"].startswith("btc-updown-5m-")
        assert data["_up_token_id"] and data["_down_token_id"]
        assert data["_up_token_id"] != data["_down_token_id"]


def test_binance_fixtures_identify_their_symbol_and_stream() -> None:
    trades = load("binance_aggtrade.json")
    assert trades["_symbol"] == "BTCUSDT"
    assert trades["_stream"] == "btcusdt@aggTrade"
    assert load("binance_exchangeinfo.json")["_symbol"] == "BTCUSDT"
