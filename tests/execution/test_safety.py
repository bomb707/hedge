"""The hard live-trading gate, the credential boundary, and the venue order contract.

These are the tests that must never be relaxed. Everything else in P7 can be refactored; if
one of these stops holding, the repository has become capable of doing something it is not
authorised to do.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import maker5m
from maker5m.domain import Outcome
from maker5m.execution import (
    POST_ONLY,
    ExecutionCredentials,
    ExecutionError,
    LiveTradingDisabledError,
    OrderSide,
    RecordingTransport,
    Secret,
    VenueAdapter,
    VenueOrderType,
    build_placement,
    live_trading_enabled,
    require_live_trading_enabled,
)
from maker5m.execution.prepare import PreparationOutcome, PreparedOrder
from tests.execution.builders import UP_TOKEN, code_without_docstrings, px, sh

EXECUTION = Path(maker5m.__file__).parent / "execution"


# -- the gate -------------------------------------------------------------


def test_live_trading_is_disabled() -> None:
    assert maker5m.LIVE_TRADING_ENABLED is False
    assert live_trading_enabled() is False


def test_arming_a_real_write_adapter_is_refused() -> None:
    """Refused before any credential is read or any socket is opened."""
    touched: list[str] = []

    def build_transport(_: ExecutionCredentials) -> RecordingTransport:
        touched.append("constructed")
        return RecordingTransport()

    credentials = ExecutionCredentials(
        private_key=Secret("x"),
        api_key=Secret("y"),
        api_secret=Secret("z"),
        api_passphrase=Secret("w"),
    )
    with pytest.raises(LiveTradingDisabledError, match="live trading is disabled"):
        VenueAdapter.arm_live(credentials, build_transport)
    assert touched == [], "the transport must not be constructed when the gate is closed"


def test_the_gate_names_what_it_refused() -> None:
    with pytest.raises(LiveTradingDisabledError, match="the order router"):
        require_live_trading_enabled("the order router")


def test_no_flag_or_environment_variable_can_bypass_the_gate() -> None:
    """Unlocking must require a source edit and code review, not a runtime switch."""
    for path in EXECUTION.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"getenv", "environ"}:
                raise AssertionError(f"{path.name} reads the environment")
        code = code_without_docstrings(path).lower()
        for marker in ("--live", "force_live", "allow_live", "enable_live", "argparse"):
            assert marker not in code, f"{path.name} implements a bypass switch: {marker!r}"


def test_the_gate_reads_only_the_safety_constant() -> None:
    """One source of truth, imported directly, with nothing between it and the decision."""
    code = code_without_docstrings(EXECUTION / "gate.py")
    assert "LIVE_TRADING_ENABLED" in code
    assert "maker5m.safety" in ast.unparse(ast.parse((EXECUTION / "gate.py").read_text()))


def test_a_mock_transport_remains_freely_usable() -> None:
    """The whole path must be testable while unarmed."""
    adapter = VenueAdapter(transport=RecordingTransport())
    adapter.prewarm(("a", "b"))
    assert isinstance(adapter.transport, RecordingTransport)


# -- the credential boundary -------------------------------------------------


def test_a_secret_never_prints_itself() -> None:
    secret = Secret("super-secret-key")
    assert "super-secret-key" not in repr(secret)
    assert "super-secret-key" not in str(secret)
    assert "super-secret-key" not in f"{secret}"
    assert secret.reveal() == "super-secret-key"


def test_a_secret_is_not_comparable_or_hashable() -> None:
    """Equality invites logging a diff; hashing invites it becoming a dict key."""
    assert (Secret("a") == Secret("a")) is False
    with pytest.raises(TypeError):
        hash(Secret("a"))


def test_credentials_never_print_their_secrets() -> None:
    credentials = ExecutionCredentials(
        private_key=Secret("0xdeadbeef"),
        api_key=Secret("api-key-value"),
        api_secret=Secret("api-secret-value"),
        api_passphrase=Secret("passphrase-value"),
        funder_address="0xpublic",
    )
    rendered = f"{credentials!r} {credentials}"
    for leaked in ("0xdeadbeef", "api-key-value", "api-secret-value", "passphrase-value"):
        assert leaked not in rendered
    assert "0xpublic" in rendered, "the public address is not secret"


def test_an_empty_secret_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        Secret("")


def test_no_credential_loader_reads_a_dotenv_file() -> None:
    """Credentials are supplied explicitly by outer wiring, never picked up from a file."""
    for path in EXECUTION.rglob("*.py"):
        source = "\n".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "dotenv" not in imported, f"{path.name} imports a dotenv loader"
        assert 'open(".env' not in source


def test_no_source_or_fixture_assigns_a_key_shaped_literal() -> None:
    """A 0x+64-hex string is also a public condition id, so shape alone proves nothing.

    What would be alarming is such a literal *assigned to a credential-looking name*, so that
    is what is checked.
    """
    repo = Path(maker5m.__file__).resolve().parents[2]
    pattern = re.compile(
        r"(private_key|api_secret|api_key|passphrase|secret)\s*[=:]\s*[\"\']0x[0-9a-fA-F]{40,}",
        re.IGNORECASE,
    )
    for path in list((repo / "src").rglob("*.py")) + list((repo / "tests").rglob("*")):
        if path.is_dir() or path.suffix not in {".py", ".json"}:
            continue
        assert not pattern.search(path.read_text(encoding="utf-8")), (
            f"{path.name} assigns a key-shaped literal to a credential name"
        )


# -- the venue order contract ------------------------------------------------


def safe_order() -> PreparedOrder:
    return PreparedOrder(
        outcome=Outcome.UP,
        token_id=UP_TOKEN,
        strategy_price=px("0.63"),
        submission_price=px("0.63"),
        strategy_size=sh("15"),
        submission_size=sh("15"),
        venue_tick=px("0.01"),
        min_order_size=sh("5"),
        outcome_status=PreparationOutcome.SAFE,
        observed_ask=px("0.64"),
    )


def test_only_buy_is_representable() -> None:
    assert [member.value for member in OrderSide] == ["BUY"]
    assert not hasattr(OrderSide, "SELL")


def test_only_gtc_is_representable() -> None:
    assert [member.value for member in VenueOrderType] == ["GTC"]
    for forbidden in ("FOK", "FAK", "GTD", "MARKET"):
        assert not hasattr(VenueOrderType, forbidden)


def test_post_only_is_a_constant_not_a_setting() -> None:
    assert POST_ONLY is True


def test_a_serialized_order_is_always_buy_gtc_post_only() -> None:
    """Asserted at the actual adapter boundary, not on an internal boolean."""
    placement = build_placement(safe_order())
    assert placement.side == "BUY"
    assert placement.order_type == "GTC"
    assert placement.post_only is True
    kwargs = placement.as_sdk_kwargs()
    assert kwargs["side"] == "BUY"
    assert kwargs["post_only"] is True
    # No expiration is passed, which is what makes the SDK choose GTC over GTD.
    assert "expiration" not in kwargs


def test_a_non_submittable_order_cannot_be_serialized() -> None:
    import dataclasses

    blocked = dataclasses.replace(safe_order(), outcome_status=PreparationOutcome.WOULD_CROSS)
    with pytest.raises(ExecutionError, match="non-submittable"):
        build_placement(blocked)


def test_no_execution_module_can_produce_post_only_false() -> None:
    """Checked as code, not text: the SDK's unsafe default is quoted in a docstring."""
    for path in EXECUTION.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "post_only":
                assert not (isinstance(node.value, ast.Constant) and node.value.value is False), (
                    f"{path.name} passes post_only=False"
                )


def test_no_execution_module_names_a_taker_order_type() -> None:
    for path in EXECUTION.rglob("*.py"):
        if path.name in ("venue_order.py", "adapter.py"):
            continue  # these name the forbidden types only to document their absence
        text = path.read_text(encoding="utf-8")
        for forbidden in ('"FOK"', '"FAK"', '"SELL"'):
            assert forbidden not in text, f"{path.name} names {forbidden}"


ORDER_PATH_MODULES = (
    "prepare.py",
    "venue_order.py",
    "adapter.py",
    "reconciler.py",
    "live_orders.py",
    "user_stream.py",
    "executor.py",
    "telemetry.py",
)


def test_no_float_conversion_exists_on_the_authoritative_order_path() -> None:
    """A float at the final step would undo a pipeline built to avoid binary error.

    The rate limiter is excluded deliberately: it counts fractional *tokens*, which are not
    an order quantity and never reach the venue.
    """
    for name in ORDER_PATH_MODULES:
        path = EXECUTION / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "float", f"{name} calls float()"


def test_the_rate_limiter_holds_no_order_quantity() -> None:
    """Its floats are token counts, so it must not touch prices or sizes."""
    text = (EXECUTION / "rate_limit.py").read_text(encoding="utf-8")
    for forbidden in ("PriceUnits", "ShareUnits", "MoneyUnits"):
        assert forbidden not in text


def test_the_test_suite_makes_no_authenticated_request() -> None:
    """Nothing in the suite constructs a real SDK client."""
    repo = Path(maker5m.__file__).resolve().parents[2]
    marker = "Secure" + "Client("
    for path in (repo / "tests").rglob("*.py"):
        if path.name == Path(__file__).name:
            continue  # this file names the constructor only in order to forbid it
        assert marker not in path.read_text(encoding="utf-8"), f"{path.name} builds a client"
