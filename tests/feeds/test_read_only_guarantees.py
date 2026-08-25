"""The market-data plane stays strictly public and read-only.

P6 wrote these guards when the repository contained no write path at all. P7 adds one, so
they are rescoped rather than relaxed: the *market-data* modules must still contain no
credential or signing material, and `maker5m.execution.credentials` is now the single
sanctioned place where such material is even named.

What has not changed, and is asserted harder in `tests/execution/test_safety.py`: no real
write adapter can be armed while `LIVE_TRADING_ENABLED` is `False`, and the test suite makes
no authenticated request of any kind.
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


EXECUTION = SRC / "execution"


def python_sources() -> list[Path]:
    """Every module *outside* the execution package, which is the credential boundary."""
    return [
        p
        for p in sorted(SRC.rglob("*.py")) + sorted((REPO / "tools").rglob("*.py"))
        if EXECUTION not in p.parents
    ]


def test_live_trading_remains_disabled() -> None:
    assert maker5m.LIVE_TRADING_ENABLED is False


@pytest.mark.parametrize("path", python_sources(), ids=lambda p: p.name)
def test_no_module_outside_execution_contains_credential_material(path: Path) -> None:
    """Credentials are confined to one module, so there is one place to audit."""
    text = path.read_text(encoding="utf-8").lower()
    for marker in WRITE_PATH_MARKERS:
        assert marker not in text, f"{path.name} mentions {marker!r}"


def test_the_credential_boundary_is_a_single_module() -> None:
    named = sorted(
        p.name
        for p in EXECUTION.rglob("*.py")
        if any(m in p.read_text(encoding="utf-8").lower() for m in ("private_key", "api_secret"))
    )
    assert named == ["credentials.py"], f"credential material leaked into: {named}"


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


def test_runtime_dependencies_are_exactly_the_two_justified_ones() -> None:
    """A WebSocket client and the official SDK. No ORM, broker, or dataframe library."""
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    block = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    declared = re.findall(r'"([A-Za-z0-9_.-]+)', block)
    assert declared == ["websockets", "polymarket-client"], (
        f"unexpected runtime dependencies: {declared}"
    )
    # The official SDK is pinned exactly, never floated.
    assert '"polymarket-client==0.6.0"' in pyproject
    # The legacy archived client is not a dependency. It is named in a comment explaining
    # why it is not used, so this checks the parsed requirement list rather than the text.
    assert "py-clob-client" not in declared
    for banned in ("pandas", "sqlalchemy", "redis", "kafka"):
        assert banned not in pyproject.lower()
