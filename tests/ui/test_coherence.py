"""Snapshot coherence, hot-path silence, and audit semantics.

**SUPPORTING UNIT TEST ONLY.** Each of these pins a defect that the previous round's tests could
not have caught, because they tested the pieces rather than the shape the runner actually used.
"""

from __future__ import annotations

import ast
import builtins
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from maker5m.market.events import HealthStatus
from maker5m.market.timebase import TimestampNs
from maker5m.risk import RiskConfig, RiskEngine, RiskProvenance, RiskReason, RiskState
from maker5m.risk.trace import HealthFrame, RiskController
from maker5m.strategy import BaseLot, default_config
from maker5m.ui import (
    COMMAND_SCHEMA_VERSION,
    CommandKind,
    ControlIngress,
    HotCommandChannel,
    OperatorCommand,
    SnapshotPublisher,
    drain_operator_commands,
)
from tests.persistence.builders import identity, observation
from tests.ui.test_snapshot_truth import FakeVerdict

HEALTHY = HealthFrame(
    clob_status=HealthStatus.HEALTHY,
    clob_awaiting_snapshot=False,
    spot_status=HealthStatus.HEALTHY,
)


def command(kind: CommandKind, command_id: str = "c1") -> OperatorCommand:
    return OperatorCommand(
        schema_version=COMMAND_SCHEMA_VERSION,
        command_id=command_id,
        kind=kind.value,
        issued_at_ns=1,
    )


def controller() -> RiskController:
    control = RiskController(
        engine=RiskEngine(config=RiskConfig()), provenance=RiskProvenance.SUPPORTING_UNIT_TEST
    )
    for ordinal in range(6):
        control.evaluate(HEALTHY, as_of_ingress_ordinal=ordinal, now_ns=TimestampNs(ordinal))
    return control


# -- §4: the actual hot function, with every synchronous I/O route removed ----------------------


def test_the_production_hot_control_function_performs_no_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rule is no synchronous I/O, not merely no filesystem call.

    A `print` to a pipe nobody is draining blocks as thoroughly as a stalled `stat`, and the
    previous round left prints in `on_tick` while its test asserted it reproduced that body
    "verbatim". This drives the extracted production function instead.
    """
    touched: list[str] = []

    def boom(name: str) -> Any:
        def raiser(*args: Any, **kwargs: Any) -> Any:
            touched.append(name)
            raise OSError(f"synchronous I/O attempted: {name}")

        return raiser

    monkeypatch.setattr(builtins, "print", boom("print"))
    monkeypatch.setattr(logging.Logger, "info", boom("log.info"))
    monkeypatch.setattr(logging.Logger, "warning", boom("log.warning"))
    monkeypatch.setattr(logging, "log", boom("logging.log"))
    for name in ("glob", "read_text", "write_text", "unlink", "mkdir", "stat", "replace"):
        monkeypatch.setattr(Path, name, boom(f"Path.{name}"))
    monkeypatch.setattr(os, "listdir", boom("os.listdir"))
    monkeypatch.setattr(tempfile, "mkstemp", boom("tempfile.mkstemp"))

    channel = HotCommandChannel()
    channel.push(command(CommandKind.OPERATOR_HALT, "halt-1"))
    control = controller()
    ingress = ControlIngress(controller=control)
    events: list[Any] = []

    applied = drain_operator_commands(
        channel,
        ingress,
        ingress_ordinal=10,
        now_ns=TimestampNs(10),
        report=events.append,
    )

    assert touched == [], f"the hot path performed I/O: {touched}"
    assert applied == 1
    assert control.trace.records[-1].state is RiskState.HALTED
    assert events and events[0][0] == "command"


def test_a_failing_reporter_cannot_interrupt_the_market() -> None:
    """A debug channel is not worth a market."""

    def explode(_event: Any) -> None:
        raise RuntimeError("the evidence channel is broken")

    channel = HotCommandChannel()
    channel.push(command(CommandKind.OPERATOR_HALT, "halt-1"))
    control = controller()
    applied = drain_operator_commands(
        channel,
        ControlIngress(controller=control),
        ingress_ordinal=10,
        now_ns=TimestampNs(10),
        report=explode,
    )
    assert applied == 1
    assert control.trace.records[-1].state is RiskState.HALTED


# -- §11-12: the snapshot joins the verdict the decision names ----------------------------------


def publisher() -> SnapshotPublisher:
    return SnapshotPublisher(identity=identity(), config=default_config(BaseLot.of(15)))


def verdict(sequence: int, state: RiskState, reasons: set[RiskReason]) -> FakeVerdict:
    return FakeVerdict(
        state=state,
        active=reasons,
        latched=set(),
        health=HealthFrame(
            clob_status=HealthStatus.HEALTHY,
            clob_awaiting_snapshot=False,
            spot_status=HealthStatus.HEALTHY,
        ),
        sequence=sequence,
        ordinal=sequence,
    )


def decision(risk_sequence: int) -> Any:
    from maker5m.persistence import build_decision_record

    return build_decision_record(
        observation(0, ordinal=risk_sequence, risk=(risk_sequence, "SAFE", True, True)),
        identity(),
        persistence_sequence=1,
    )


def test_the_snapshot_uses_the_named_verdict_not_the_newest(tmp_path: Path) -> None:
    """§11: 50 is SAFE, 51 is HALTED, the decision names 50. It must read SAFE."""
    unit = publisher()
    unit.observe_risk(verdict(50, RiskState.SAFE, set()))
    unit.observe_risk(verdict(51, RiskState.HALTED, {RiskReason.OPERATOR_HALT}))
    unit.observe(decision(50))

    snapshot = unit.build(now=1.0)
    assert snapshot.risk_sequence == 50
    assert snapshot.risk_state == "SAFE"
    assert snapshot.risk_active == ()
    assert snapshot.observation_points["risk_verdict"] == 50


def test_a_worker_lagging_many_records_behind_still_joins_correctly() -> None:
    """§12: risk runs ahead while persistence handles an older decision."""
    unit = publisher()
    for sequence in range(40, 60):
        state = RiskState.HALTED if sequence >= 45 else RiskState.SAFE
        reasons = {RiskReason.OPERATOR_HALT} if sequence >= 45 else set()
        unit.observe_risk(verdict(sequence, state, reasons))

    unit.observe(decision(42))
    snapshot = unit.build(now=1.0)
    assert snapshot.risk_sequence == 42
    assert snapshot.risk_state == "SAFE", "the decision predates the halt"
    assert snapshot.risk_active == ()


def test_an_unarrived_verdict_reads_unavailable_rather_than_borrowing_one() -> None:
    unit = publisher()
    unit.observe_risk(verdict(51, RiskState.HALTED, {RiskReason.OPERATOR_HALT}))
    unit.observe(decision(50))

    snapshot = unit.build(now=1.0)
    assert snapshot.risk_active == ()
    assert snapshot.clob_status == "UNKNOWN", "no verdict means no health, not healthy"


def test_the_verdict_history_is_bounded() -> None:
    from maker5m.ui.snapshot import VERDICT_HISTORY

    unit = publisher()
    for sequence in range(VERDICT_HISTORY * 2):
        unit.observe_risk(verdict(sequence, RiskState.SAFE, set()))
    assert len(unit.verdicts) <= VERDICT_HISTORY
    assert max(unit.verdicts) == VERDICT_HISTORY * 2 - 1


# -- §13: latency comes from the captured observation -------------------------------------------


def test_latency_comes_from_the_observation_and_never_moves_again() -> None:
    """§13: exact values from the captured facts, immune to a later mutable read."""
    from maker5m.persistence import build_decision_record

    captured = list(observation(0, ordinal=4242, risk=(7, "SAFE", True, True)))
    captured[4] = 1_000_000  # raw receive
    captured[5] = 1_021_555  # decide done
    captured[6] = 1_024_675  # prepare done
    captured[7] = 1_033_577  # reconcile done
    frozen = tuple(captured)

    record = build_decision_record(frozen, identity(), persistence_sequence=1)
    unit = publisher()
    unit.observe_risk(verdict(7, RiskState.SAFE, set()))
    unit.observe_decision(record, frozen)

    snapshot = unit.build(now=1.0)
    assert snapshot.decide_ns == 21_555
    assert snapshot.prepare_ns == 3_120
    assert snapshot.reconcile_ns == 8_902
    assert snapshot.receive_to_reconcile_ns == 33_577
    assert snapshot.latency_sample_ordinal == record.ingress_ordinal

    # A foreign merger-like object changing its timings must not move the snapshot.
    class Merger:
        last_reduce_ns = 999_999
        last_decide_ns = 999_999
        stages_measured = True

    Merger.last_decide_ns = 123_456
    again = unit.build(now=2.0)
    assert again.decide_ns == 21_555
    assert again.receive_to_reconcile_ns == 33_577


def test_an_unsampled_observation_yields_no_latency() -> None:
    from maker5m.persistence import build_decision_record
    from maker5m.persistence.records import latency_sample

    unsampled = observation(0, ordinal=4242, risk=(7, "SAFE", True, True))
    captured = list(unsampled)
    captured[5] = 0  # NOT_CAPTURED
    frozen = tuple(captured)
    assert latency_sample(frozen) is None

    unit = publisher()
    unit.observe_decision(build_decision_record(frozen, identity(), persistence_sequence=1), frozen)
    assert unit.build(now=1.0).decide_ns is None


def test_the_publisher_never_reaches_into_trading_objects() -> None:
    """§5/§7: no controller, merger, pipeline or run reference anywhere in the read model.

    Scans the code with docstrings stripped, using the helper the execution guards already use:
    this module *documents* the cross-thread read it removed, and a plain text scan would trip
    over the explanation.
    """
    import ast

    import maker5m.ui.snapshot as snapshot_module

    tree = ast.parse(Path(str(snapshot_module.__file__)).read_text("utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list):
            # Every standalone string expression is documentation — a module or function
            # docstring, or an attribute docstring after a field. None of them is code, and the
            # shared helper only strips the leading one.
            node.body = [  # type: ignore[attr-defined]
                item
                for item in body
                if not (
                    isinstance(item, ast.Expr)
                    and isinstance(item.value, ast.Constant)
                    and isinstance(item.value.value, str)
                )
            ]
    code = ast.unparse(tree)
    for banned in ("controller.trace", ".merger", "pipeline.", "InstrumentedRun"):
        assert banned not in code, f"the read model reaches {banned}"


# -- §14-15: the closed market's own truth wins -------------------------------------------------


def test_the_closed_manifest_overrides_live_counters() -> None:
    """The first P12B final snapshot disagreed with its own manifest by one of everything."""
    unit = publisher()
    unit.observe_risk(verdict(7, RiskState.SAFE, set()))
    unit.observe(decision(7))
    unit.deliver("counters", {"decisions": 82_335, "risk": 82_337, "dropped": 1})
    stale = unit.build(now=1.0)
    assert stale.decisions_persisted == 82_335

    unit.deliver(
        "closed",
        {
            "decision_count": 82_336,
            "risk_count": 82_338,
            "dropped_records": 0,
            "sink_errors": 0,
            "telemetry_complete": True,
            "verification_status": "COMPLETE",
        },
    )
    final = unit.build(now=2.0)
    assert final.decisions_persisted == 82_336
    assert final.risk_records_persisted == 82_338
    assert final.dropped_records == 0
    assert final.sink_errors == 0
    assert final.telemetry_complete is True
    assert final.verification_status == "COMPLETE"


def test_audit_completeness_is_not_merely_an_absence_of_errors() -> None:
    """§18: `BoundedChannel.publish` does not raise when it drops."""
    unit = publisher()
    unit.observe_risk(verdict(7, RiskState.SAFE, set()))
    unit.observe(decision(7))
    unit.deliver("audit_counts", {"accepted": 2, "persisted": 1, "dropped": 1})
    assert unit.build(now=1.0).control_audit_complete is False

    unit.deliver("audit_counts", {"accepted": 2, "persisted": 2, "dropped": 0})
    assert unit.build(now=2.0).control_audit_complete is True


def test_command_history_comes_from_persisted_audit_rows() -> None:
    """§17: the operator's history describes durable evidence."""
    unit = publisher()
    unit.observe_risk(verdict(7, RiskState.SAFE, set()))
    unit.observe(decision(7))
    unit.deliver(
        "control_persisted",
        {"command_id": "halt-1", "kind": "OPERATOR_HALT", "risk_sequence": 964},
    )
    history = unit.build(now=1.0).accepted_commands
    assert history[-1]["command_id"] == "halt-1"
    assert history[-1]["risk_sequence"] == 964


def test_a_bridge_with_a_recorded_failure_is_not_available() -> None:
    """§23: a thread that is running but cannot read the inbox is not a healthy channel."""
    from maker5m.ui import CommandBridge, CommandInbox

    bridge = CommandBridge(inbox=CommandInbox(Path("/nonexistent")), channel=HotCommandChannel())
    bridge.stats.alive = True
    assert bridge.stats.summary()["available"] is True

    bridge.stats.note_error(OSError("inbox unreadable"))
    unit = SnapshotPublisher(
        identity=identity(), config=default_config(BaseLot.of(15)), bridge=bridge
    )
    unit.observe_risk(verdict(7, RiskState.SAFE, set()))
    unit.observe(decision(7))
    assert unit.build(now=1.0).control_channel_available is False


# -- §2: the runner's own tick body, not a paraphrase of it ------------------------------------


FORBIDDEN_IN_TICK = frozenset(
    {
        "print",
        "open",
        "write",
        "flush",
        "dump",
        "dumps",
        "listdir",
        "iterdir",
        "scandir",
        "stat",
        "rename",
        "replace",
        "read_text",
        "write_text",
        "mkdir",
        "unlink",
        "sleep",
        "publish_now",
        "maybe_publish",
        "note_command",
    }
)


def _runner_function(name: str) -> ast.FunctionDef:
    """The production runner's own source, located by name."""
    source = Path("tools/p12_market.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"tools/p12_market.py no longer defines {name}")


def test_the_runners_tick_body_calls_nothing_that_can_block() -> None:
    """P12B's tick printed with flush=True, and the test that claimed to reproduce it did not.

    This reads the shipped file rather than a copy, so a print reintroduced there fails here.
    It covers the lexical body only: what `evaluate_now` does is P8's and P9's to prove.
    """
    tick = _runner_function("on_tick")
    called = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tick)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)
    }
    assert not called & FORBIDDEN_IN_TICK, sorted(called & FORBIDDEN_IN_TICK)
    assert "drain_operator_commands" in called


def test_the_worker_callback_reads_no_trading_object() -> None:
    """`controller.trace.records[-1]` and `run.pipeline.merger` were cross-thread reads."""
    body = ast.dump(_runner_function("on_persisted"))
    for reached in ("trace", "merger", "controller", "pipeline"):
        assert f"attr='{reached}'" not in body, reached


def test_a_straggling_counter_cannot_overwrite_the_closed_manifest() -> None:
    """The manifest is what was written; a live counter arriving later is behind, not ahead."""
    unit = publisher()
    unit.observe_risk(verdict(7, RiskState.SAFE, set()))
    unit.observe(decision(7))
    unit.deliver(
        "closed",
        {
            "decision_count": 82_336,
            "risk_count": 82_338,
            "dropped_records": 0,
            "sink_errors": 0,
            "telemetry_complete": True,
            "verification_status": "COMPLETE",
        },
    )
    unit.deliver("counters", {"decisions": 82_335, "risk": 82_337, "dropped": 1})
    final = unit.build(now=3.0)
    assert final.decisions_persisted == 82_336
    assert final.dropped_records == 0
