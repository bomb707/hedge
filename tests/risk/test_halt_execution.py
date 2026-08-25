"""What a halt does to execution, and — more importantly — what it never does.

**SUPPORTING UNIT TEST ONLY.** Constructed states driven through the real P7 reconciler.

Canonical §28 opens by saying the target strategy does not use a conventional stop-loss, and
invariant I15 forbids flattening before settlement. So the interesting assertions here are
negative: a halt must produce CANCEL and nothing else, and no code path may reach for a SELL, a
hedge, a merge, a split, or a convert.
"""

from __future__ import annotations

import ast
from pathlib import Path

import maker5m
from maker5m.domain import Outcome
from maker5m.execution import prepare_both_sides, reconcile
from maker5m.execution.live_orders import LiveOrderTable, OrderLifecycle
from maker5m.execution.reconciler import ReconcileAction
from maker5m.market.events import HealthStatus
from maker5m.market.timebase import TimestampNs, seconds
from maker5m.numeric import parse_price, parse_share
from maker5m.risk import RiskConfig, RiskEngine, RiskInputs, RiskReason, RiskState, risk_adjust
from maker5m.strategy.decision import DecisionResult, DesiredOrder, DesiredOrders
from tests.execution.builders import decision as build_decision
from tests.execution.builders import desired, rules, state_at

NOW = TimestampNs(1_787_647_500_000_000_000)
SRC = Path(maker5m.__file__).parent


def healthy(**overrides: object) -> RiskInputs:

    base = RiskInputs(
        now_ns=NOW,
        clob_status=HealthStatus.HEALTHY,
        clob_awaiting_snapshot=False,
        spot_status=HealthStatus.HEALTHY,
    )
    return base._replace(**overrides)  # type: ignore[arg-type]


def safe_engine() -> RiskEngine:
    engine = RiskEngine()
    for _ in range(RiskConfig().recovery_confirmations + 1):
        engine.evaluate(healthy())
    assert engine.state is RiskState.SAFE
    return engine


def resting_table() -> LiveOrderTable:
    """One live order per side, matching what `desired()` asks for."""
    table = LiveOrderTable()
    state = state_at()
    for index, order in enumerate((desired().up, desired().down)):
        assert order is not None
        client_order_id = f"live-{index}"
        table.register_pending_place(
            client_order_id=client_order_id,
            outcome=order.outcome,
            price=order.price,
            size=order.size,
            ingress_ordinal=index,
        )
        table.update(client_order_id, status=OrderLifecycle.LIVE, venue_order_id=client_order_id)
    assert state is not None
    return table


def plan_for(intent: DecisionResult, table: LiveOrderTable) -> object:
    state = state_at()
    prepared = prepare_both_sides(intent, state, rules())
    live = {
        Outcome.UP: table.current(Outcome.UP),
        Outcome.DOWN: table.current(Outcome.DOWN),
    }
    return reconcile(prepared, live)


# -- healthy risk changes nothing ------------------------------------------------------------


def test_a_safe_verdict_returns_the_identical_decision_object() -> None:
    """Identity, not equality: risk must not even rebuild a healthy strategy intent."""
    engine = safe_engine()
    original = build_decision(desired(), state_at())
    adjusted = risk_adjust(original, engine.evaluate(healthy()))
    assert adjusted is original


def test_a_safe_verdict_leaves_the_plan_placing() -> None:
    engine = safe_engine()
    intent = risk_adjust(build_decision(desired(), state_at()), engine.evaluate(healthy()))
    plan = plan_for(intent, LiveOrderTable())
    assert [side.action for side in plan.sides] == [  # type: ignore[attr-defined]
        ReconcileAction.PLACE,
        ReconcileAction.PLACE,
    ]


# -- a halt withdraws, and only withdraws ------------------------------------------------------


def test_a_halt_empties_the_intent() -> None:
    engine = safe_engine()
    halted = engine.evaluate(healthy(spot_status=HealthStatus.STALE))
    assert halted.state is RiskState.HALTED
    adjusted = risk_adjust(build_decision(desired(), state_at()), halted)
    assert adjusted.orders.is_empty
    assert adjusted.telemetry is not None, "telemetry survives so the record stays complete"


def test_a_halt_cancels_resting_orders_and_places_nothing() -> None:
    engine = safe_engine()
    halted = engine.evaluate(healthy(spot_status=HealthStatus.STALE))
    intent = risk_adjust(build_decision(desired(), state_at()), halted)
    plan = plan_for(intent, resting_table())
    actions = [side.action for side in plan.sides]  # type: ignore[attr-defined]
    assert actions == [ReconcileAction.CANCEL, ReconcileAction.CANCEL]
    assert ReconcileAction.PLACE not in actions
    assert ReconcileAction.REPLACE not in actions


def test_a_halt_with_nothing_resting_plans_nothing_at_all() -> None:
    engine = safe_engine()
    halted = engine.evaluate(healthy(spot_status=HealthStatus.STALE))
    intent = risk_adjust(build_decision(desired(), state_at()), halted)
    plan = plan_for(intent, LiveOrderTable())
    assert [side.action for side in plan.sides] == [  # type: ignore[attr-defined]
        ReconcileAction.NOTHING,
        ReconcileAction.NOTHING,
    ]


def test_a_price_change_during_a_halt_cancels_rather_than_replaces() -> None:
    """The placement half of CANCEL_THEN_PLACE must not survive a halt."""
    engine = safe_engine()
    halted = engine.evaluate(healthy(clock_drift_ns=int(seconds(1))))
    moved = DesiredOrders(
        up=DesiredOrder(Outcome.UP, parse_price("0.55"), parse_share("15")),
        down=DesiredOrder(Outcome.DOWN, parse_price("0.40"), parse_share("15")),
    )
    intent = risk_adjust(build_decision(moved, state_at()), halted)
    plan = plan_for(intent, resting_table())
    actions = [side.action for side in plan.sides]  # type: ignore[attr-defined]
    assert actions == [ReconcileAction.CANCEL, ReconcileAction.CANCEL]


def test_recovering_is_not_nearly_safe() -> None:
    engine = safe_engine()
    engine.evaluate(healthy(order_state_uncertain=True))
    recovering = engine.evaluate(healthy())
    assert recovering.state is RiskState.RECOVERING
    assert not recovering.allows_place
    intent = risk_adjust(build_decision(desired(), state_at()), recovering)
    plan = plan_for(intent, LiveOrderTable())
    assert ReconcileAction.PLACE not in [side.action for side in plan.sides]  # type: ignore[attr-defined]


def test_an_unknown_order_is_waited_on_not_cancelled_again() -> None:
    """An order that may or may not exist is not a question to answer with another request."""
    table = resting_table()
    table.update("live-0", status=OrderLifecycle.UNKNOWN)
    engine = safe_engine()
    halted = engine.evaluate(healthy(order_state_uncertain=True))
    intent = risk_adjust(build_decision(desired(), state_at()), halted)
    plan = plan_for(intent, table)
    up = plan.action_for(Outcome.UP)  # type: ignore[attr-defined]
    # WAIT, specifically: not a second CANCEL for an order that may already be gone, and not a
    # PLACE that would create a duplicate if it is not.
    assert up.action is ReconcileAction.WAIT


# -- the negative guarantees -------------------------------------------------------------------


def test_the_risk_package_contains_no_trading_verb() -> None:
    """Canonical §28 rejects stop-loss behaviour; I15 forbids pre-settlement flattening."""
    forbidden = ("sell", "hedge", "flatten", "merge", "split", "convert", "liquidate")
    for path in (SRC / "risk").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                lowered = node.name.lower()
                for verb in forbidden:
                    assert verb not in lowered, f"{path.name}:{node.name}"
            if isinstance(node, ast.Attribute):
                lowered = node.attr.lower()
                for verb in forbidden:
                    assert verb not in lowered, f"{path.name}: attribute {node.attr}"


def test_the_risk_package_never_constructs_an_order() -> None:
    """A halt withdraws intent. It never builds one of its own."""
    for path in (SRC / "risk").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "DesiredOrder", f"{path.name} builds an order"


def test_the_only_intent_a_halt_produces_is_empty() -> None:
    from maker5m.risk.overlay import EMPTY_INTENT

    assert EMPTY_INTENT.is_empty
    assert EMPTY_INTENT.count == 0


def test_no_risk_path_touches_the_ledger() -> None:
    """The fill stays in the ledger exactly as executed. Risk changes permission, not history."""
    for path in (SRC / "risk").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                assert "ledger" not in ast.unparse(node).lower(), f"{path.name}: writes a ledger"


def test_the_risk_package_reads_no_clock() -> None:
    """`now_ns` is always an argument, so a replay reproduces the same halts (I20)."""
    banned = {"time", "datetime"}
    for path in (SRC / "risk").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in banned, f"{path.name}"
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in banned, f"{path.name}"


def test_taker_fill_is_a_halt_and_not_a_correction() -> None:
    """I07: an intentional taker fill is an execution bug. There is no recovery trade."""
    engine = safe_engine()
    halted = engine.evaluate(healthy(taker_fill_seen=True))
    assert halted.state is RiskState.HALTED
    assert RiskReason.TAKER_FILL in halted.active
    intent = risk_adjust(build_decision(desired(), state_at()), halted)
    assert intent.orders.is_empty
    # And it does not clear by itself when the next fill happens to be a maker fill.
    for _ in range(10):
        assert engine.evaluate(healthy()).state is RiskState.RECOVERING
