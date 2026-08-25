"""P6 is strictly public and read-only. These tests are the structural proof.

No order endpoint, no credential, no wallet key, no signing, no write path. Execution begins
at P7, and until then the repository must not contain the means to place an order even by
accident.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import maker5m
from maker5m.feeds import POLYMARKET_MARKET_WS, subscribe_payload

SRC = Path(__file__).resolve().parents[2] / "src" / "maker5m"
REPO = Path(__file__).resolve().parents[2]

WRITE_PATH_MARKERS = (
    "private_key",
    "privatekey",
    "api_secret",
    "apisecret",
    "passphrase",
    "signtypeddata",
    "sign_typed_data",
    "eth_account",
    "web3",
    "post_order",
    "place_order",
    "cancel_order",
    "create_order",
    "l1_auth",
    "l2_auth",
    "poly_apikey",
    "poly_passphrase",
    "poly_signature",
)


def python_sources() -> list[Path]:
    return sorted(SRC.rglob("*.py")) + sorted((REPO / "tools").rglob("*.py"))


def test_live_trading_remains_disabled() -> None:
    assert maker5m.LIVE_TRADING_ENABLED is False


@pytest.mark.parametrize("path", python_sources(), ids=lambda p: p.name)
def test_no_module_contains_credential_or_signing_material(path: Path) -> None:
    text = path.read_text(encoding="utf-8").lower()
    for marker in WRITE_PATH_MARKERS:
        assert marker not in text, f"{path.name} mentions {marker!r}"


def test_no_module_posts_to_a_venue() -> None:
    """Every HTTP call in the project is a GET, and there are no order endpoints."""
    for path in python_sources():
        text = path.read_text(encoding="utf-8")
        assert 'method="POST"' not in text
        assert "method='POST'" not in text
        assert not re.search(r"\.post\s*\(", text), f"{path.name} performs an HTTP POST"
        assert "/order" not in text, f"{path.name} references an order endpoint"


def test_the_only_websocket_endpoints_are_public_market_data() -> None:
    urls: set[str] = set()
    for path in python_sources():
        urls.update(re.findall(r"wss://[A-Za-z0-9.:/_-]+", path.read_text(encoding="utf-8")))
    assert urls, "expected at least one market-data endpoint"
    for url in urls:
        assert url in {
            POLYMARKET_MARKET_WS,
            "wss://stream.binance.com:9443/ws",
        }, f"unexpected websocket endpoint: {url}"
        assert "user" not in url, "the authenticated user channel must not be used"


def test_the_polymarket_subscription_sends_no_authentication() -> None:
    import json

    payload = json.loads(subscribe_payload(("a", "b")))
    assert set(payload) == {"assets_ids", "type"}
    assert payload["type"] == "market", "the authenticated 'user' channel is not used"


def test_no_environment_variable_is_read_for_a_secret() -> None:
    for path in python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"getenv", "environ"}:
                raise AssertionError(f"{path.name} reads the environment")


def test_the_feeds_package_has_no_write_capable_dependency() -> None:
    """Only a WebSocket client and the standard library. No SDK, ORM, broker, or dataframe."""
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    block = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    declared = re.findall(r'"([A-Za-z0-9_.-]+)', block)
    assert declared == ["websockets"], f"unexpected runtime dependencies: {declared}"
    for banned in ("pandas", "pydantic", "sqlalchemy", "redis", "kafka", "py-clob-client"):
        assert banned not in pyproject.lower()
