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
from tests.execution.builders import code_without_docstrings

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
    """Credentials are confined to one module, so there is one place to audit.

    Reads the *code* rather than the prose. Several modules explain which write-path tools they
    deliberately do not use — the settlement reader says it avoids Web3 for four ``eth_call``s —
    and a plain text scan would convict them of their own documentation.
    """
    code = code_without_docstrings(path).lower()
    for marker in WRITE_PATH_MARKERS:
        assert marker not in code, f"{path.name} uses {marker!r}"


def test_the_credential_boundary_is_a_single_module() -> None:
    named = sorted(
        p.name
        for p in EXECUTION.rglob("*.py")
        if any(m in p.read_text(encoding="utf-8").lower() for m in ("private_key", "api_secret"))
    )
    assert named == ["credentials.py"], f"credential material leaked into: {named}"


READ_ONLY_RPC_METHODS = frozenset(
    {"eth_chainId", "eth_getCode", "eth_call", "eth_getBlockByNumber", "eth_getLogs"}
)
"""JSON-RPC methods that only read. Stated as an allow-list, because a deny-list passes
anything nobody thought to forbid."""


def rpc_method_literals(path: Path) -> set[str]:
    """Every literal JSON-RPC method name a file can send."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    methods: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Either a helper called with the method first, or a JSON body naming it.
        constants = list(node.args) + [keyword.value for keyword in node.keywords]
        for item in constants:
            if (
                isinstance(item, ast.Constant)
                and isinstance(item.value, str)
                and item.value.startswith(("eth_", "personal_", "net_", "web3_"))
            ):
                methods.add(item.value)
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "method"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    methods.add(value.value)
    return methods


def posts(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    return 'method="POST"' in text or "method='POST'" in text


def test_no_module_posts_to_a_venue() -> None:
    """Nothing POSTs to a venue, and anything that POSTs at all may only read.

    JSON-RPC is a POST by protocol however read-only its content, so a flat ban would forbid
    reading the Conditional Tokens contract at all. Rather than exempt files by name, the rule
    is behavioural: a file that POSTs must issue only read JSON-RPC methods. On a JSON-RPC
    endpoint the method name is the *only* thing separating a read from a write.
    """
    posting = []
    for path in python_sources():
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\.post\s*\(", text), f"{path.name} performs an HTTP POST"
        assert "/order" not in text, f"{path.name} references an order endpoint"
        if posts(path):
            posting.append(path)

    assert posting, "expected the settlement reader to be found"
    for path in posting:
        methods = rpc_method_literals(path)
        assert methods, f"{path.name} POSTs without a recognisable JSON-RPC method"
        offending = methods - READ_ONLY_RPC_METHODS
        assert not offending, f"{path.name} can send non-read RPC methods: {sorted(offending)}"


def test_no_module_can_reach_a_transaction_sending_method() -> None:
    """Scans code rather than prose: several modules *document* what they refuse to send."""
    forbidden = (
        "eth_sendrawtransaction",
        "eth_sendtransaction",
        "eth_sign",
        "personal_",
        "eth_accounts",
    )
    for path in python_sources():
        code = code_without_docstrings(path).lower()
        for marker in forbidden:
            assert marker not in code, f"{path.name} can reach {marker}"


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
