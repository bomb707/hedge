"""Risk is an overlay. The strategy must not learn that it exists.

**SUPPORTING UNIT TEST ONLY.**

Canonical §28: "These are safety controls, not changes to the economic strategy." If a stale
feed or a clock-drift branch reached `StrategyEngine.decide`, the recorded decision would depend
on operational conditions, a replayed journal would no longer reproduce it, and the boundary
between what the strategy wants and what safety permits would stop being inspectable.
"""

from __future__ import annotations

import ast
from pathlib import Path

import maker5m
from maker5m.market.events import HealthStatus
from maker5m.market.timebase import TimestampNs
from maker5m.replay import decode_journal, encode_journal, verify_replay
from maker5m.risk import RiskConfig, RiskEngine, RiskInputs, RiskState, risk_adjust
from maker5m.strategy import StrategyEngine, default_config
from maker5m.strategy.decision import DecisionResult
from tests.replay.corpus import synthetic_run

SRC = Path(maker5m.__file__).parent
NOW = TimestampNs(1_787_647_500_000_000_000)

RISK_WORDS = (
    "risk",
    "halt",
    "kill_switch",
    "stale",
    "drift",
    "api_error",
    "rate_limit_uncertain",
    "reconcil",
)


def test_the_strategy_package_does_not_import_risk() -> None:
    """Plane 2 economics must not depend on the safety overlay."""
    for package in ("strategy", "market", "accounting", "numeric"):
        for path in (SRC / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert not node.module.startswith("maker5m.risk"), f"{path}"
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert not alias.name.startswith("maker5m.risk"), f"{path}"


def test_the_decision_engine_has_no_operational_branch() -> None:
    """No stale-feed, API-error, clock-drift, or order-state branch inside `decide`."""
    tree = ast.parse((SRC / "strategy" / "engine.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "decide":
            body = ast.unparse(node).lower()
            for word in RISK_WORDS:
                assert word not in body, f"StrategyEngine.decide mentions {word!r}"
            return
    raise AssertionError("StrategyEngine.decide was not found")


def test_the_same_decision_is_produced_whatever_the_risk_verdict() -> None:
    """The strategy is asked the same question and gives the same answer, every time."""
    from tests.execution.builders import state_at

    engine = StrategyEngine(default_config())
    state = state_at()
    baseline = engine.decide(state)

    for status in HealthStatus:
        risk = RiskEngine(config=RiskConfig())
        risk.evaluate(
            RiskInputs(
                now_ns=NOW,
                clob_status=status,
                spot_status=status,
                clob_awaiting_snapshot=status is not HealthStatus.HEALTHY,
            )
        )
        again: DecisionResult = engine.decide(state)
        assert again == baseline, f"decide() varied under {status.value}"


def test_a_healthy_verdict_is_a_no_op_on_intent() -> None:
    from tests.execution.builders import decision, desired, state_at

    risk = RiskEngine()
    inputs = RiskInputs(
        now_ns=NOW,
        clob_status=HealthStatus.HEALTHY,
        clob_awaiting_snapshot=False,
        clob_last_message_at=NOW,
        spot_status=HealthStatus.HEALTHY,
        spot_last_message_at=NOW,
    )
    for _ in range(RiskConfig().recovery_confirmations + 1):
        verdict = risk.evaluate(inputs)
    assert verdict.state is RiskState.SAFE

    original = decision(desired(), state_at())
    assert risk_adjust(original, verdict) is original


def test_the_p5_journal_still_verifies_byte_for_byte() -> None:
    """P9 adds no field to any recorded contract, so historical journals are unaffected."""
    run = synthetic_run()
    blob = encode_journal(run.journal)
    outcome = verify_replay(run.journal)
    assert outcome.verified
    assert outcome.final_state == run.final_state
    assert encode_journal(decode_journal(blob)) == blob


def test_no_risk_type_reaches_a_recorded_contract() -> None:
    """Nothing from the risk package may enter MarketState, DecisionResult, or the journal."""
    import dataclasses

    from maker5m.accounting import LedgerState
    from maker5m.market import MarketState
    from maker5m.strategy import DecisionResult as Result
    from maker5m.strategy import DecisionTelemetry, StrategyConfig

    for cls in (MarketState, LedgerState, Result, DecisionTelemetry, StrategyConfig):
        for field in dataclasses.fields(cls):
            annotation = str(field.type)
            assert "Risk" not in annotation, f"{cls.__name__}.{field.name}: {annotation}"
