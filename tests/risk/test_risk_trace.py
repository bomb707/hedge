"""The ordered risk audit stream, and the questions it exists to answer.

**SUPPORTING UNIT TEST ONLY.**

Before this, execution permission could change with no record of when, why, or relative to which
market events: reconciliation mutated a latched snapshot directly, and operational conditions
flipped ``allows_place`` invisibly. A run could be replayed for its economics and not for its
permissions.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import maker5m
from maker5m.market.events import HealthStatus
from maker5m.market.timebase import TimestampNs
from maker5m.risk import (
    RISK_SCHEMA_VERSION,
    HealthFrame,
    RiskController,
    RiskDivergenceError,
    RiskOrderError,
    RiskProvenance,
    RiskReason,
    RiskSignal,
    RiskSignalKind,
    RiskState,
    verify_risk_replay,
)

NOW = TimestampNs(1_787_647_500_000_000_000)
HEALTHY = HealthFrame(
    clob_status=HealthStatus.HEALTHY,
    clob_awaiting_snapshot=False,
    spot_status=HealthStatus.HEALTHY,
)


def controller() -> RiskController:
    return RiskController(provenance=RiskProvenance.SUPPORTING_UNIT_TEST)


def settle(rc: RiskController, ordinal: int = 0) -> int:
    """Drive to SAFE, returning the next free ingress ordinal."""
    for offset in range(rc.config.recovery_confirmations + 1):
        rc.evaluate(HEALTHY, as_of_ingress_ordinal=ordinal + offset, now_ns=NOW)
    assert rc.state is RiskState.SAFE
    return ordinal + rc.config.recovery_confirmations + 1


def signal(kind: RiskSignalKind, ordinal: int, **kwargs: object) -> RiskSignal:
    return RiskSignal(
        kind=kind,
        as_of_ingress_ordinal=ordinal,
        timestamp=NOW,
        provenance=RiskProvenance.SUPPORTING_UNIT_TEST,
        **kwargs,  # type: ignore[arg-type]
    )


# -- ordering ---------------------------------------------------------------------------------


def test_every_record_carries_a_strictly_increasing_sequence() -> None:
    rc = controller()
    for ordinal in range(10):
        rc.evaluate(HEALTHY, as_of_ingress_ordinal=ordinal, now_ns=NOW)
    sequences = [record.risk_sequence for record in rc.trace]
    assert sequences == list(range(10))


def test_every_record_states_its_market_position_and_schema() -> None:
    rc = controller()
    record = rc.evaluate(HEALTHY, as_of_ingress_ordinal=4242, now_ns=NOW)
    assert record.as_of_ingress_ordinal == 4242
    assert record.schema_version == RISK_SCHEMA_VERSION
    assert record.signal.provenance is RiskProvenance.SUPPORTING_UNIT_TEST
    assert record.signal.timestamp == NOW


def test_an_out_of_order_signal_is_refused() -> None:
    rc = controller()
    rc.evaluate(HEALTHY, as_of_ingress_ordinal=100, now_ns=NOW)
    with pytest.raises(RiskOrderError, match="out of order"):
        rc.evaluate(HEALTHY, as_of_ingress_ordinal=99, now_ns=NOW)


def test_repeating_an_ordinal_is_allowed_because_a_cycle_can_be_evaluated_twice() -> None:
    """The observe path and the idle tick may both evaluate at one ingress position."""
    rc = controller()
    rc.evaluate(HEALTHY, as_of_ingress_ordinal=7, now_ns=NOW)
    rc.evaluate(HEALTHY, as_of_ingress_ordinal=7, now_ns=NOW)
    assert [r.risk_sequence for r in rc.trace] == [0, 1]


# -- reconciliation is an ordered signal, not a hidden mutation --------------------------------


def test_a_latched_reason_needs_an_explicit_reconciliation_signal() -> None:
    rc = controller()
    ordinal = settle(rc)
    rc.apply(signal(RiskSignalKind.POSITION_RECONCILIATION_RESULT, ordinal, flag=True), HEALTHY)
    assert rc.state is RiskState.HALTED

    # The condition clears, but the latch does not.
    ordinal += 1
    record = rc.apply(
        signal(RiskSignalKind.POSITION_RECONCILIATION_RESULT, ordinal, flag=False), HEALTHY
    )
    assert record.state is RiskState.RECOVERING
    assert RiskReason.POSITION_MISMATCH in record.latched
    assert not record.allows_place

    for _ in range(6):
        ordinal += 1
        record = rc.evaluate(HEALTHY, as_of_ingress_ordinal=ordinal, now_ns=NOW)
        assert record.state is RiskState.RECOVERING

    ordinal += 1
    rc.apply(
        signal(
            RiskSignalKind.RECONCILIATION_CONFIRMED,
            ordinal,
            reason=RiskReason.POSITION_MISMATCH,
        )
    )
    for _ in range(rc.config.recovery_confirmations):
        ordinal += 1
        record = rc.evaluate(HEALTHY, as_of_ingress_ordinal=ordinal, now_ns=NOW)
    assert record.state is RiskState.SAFE


def test_a_reconciliation_signal_clears_only_the_reason_it_names() -> None:
    rc = controller()
    ordinal = settle(rc)
    rc.apply(signal(RiskSignalKind.POSITION_RECONCILIATION_RESULT, ordinal, flag=True), HEALTHY)
    ordinal += 1
    rc.apply(signal(RiskSignalKind.COST_RECONCILIATION_RESULT, ordinal, flag=True), HEALTHY)
    ordinal += 1
    rc.apply(signal(RiskSignalKind.POSITION_RECONCILIATION_RESULT, ordinal, flag=False), HEALTHY)
    ordinal += 1
    record = rc.apply(
        signal(RiskSignalKind.COST_RECONCILIATION_RESULT, ordinal, flag=False), HEALTHY
    )
    assert record.latched == {RiskReason.POSITION_MISMATCH, RiskReason.COST_LEDGER_MISMATCH}

    ordinal += 1
    record = rc.apply(
        signal(
            RiskSignalKind.RECONCILIATION_CONFIRMED,
            ordinal,
            reason=RiskReason.POSITION_MISMATCH,
        )
    )
    assert record.latched == {RiskReason.COST_LEDGER_MISMATCH}
    assert not record.allows_place


def test_reconciling_something_not_latched_is_refused() -> None:
    """Either a duplicate, or a claim about something that was never in doubt."""
    rc = controller()
    settle(rc)
    with pytest.raises(RiskOrderError, match="not latched"):
        rc.apply(
            signal(RiskSignalKind.RECONCILIATION_CONFIRMED, 99, reason=RiskReason.POSITION_MISMATCH)
        )


def test_reconciling_a_reason_that_never_latches_is_refused() -> None:
    rc = controller()
    settle(rc)
    with pytest.raises(RiskOrderError, match="does not require reconciliation"):
        rc.apply(signal(RiskSignalKind.RECONCILIATION_CONFIRMED, 99, reason=RiskReason.CLOB_STALE))


def test_a_reconciliation_signal_must_name_a_reason() -> None:
    rc = controller()
    settle(rc)
    with pytest.raises(RiskOrderError, match="must name the reason"):
        rc.apply(signal(RiskSignalKind.RECONCILIATION_CONFIRMED, 99))


def test_nothing_outside_the_controller_may_change_permission() -> None:
    """`engine.reconciled` must be reachable only through the ordered signal path."""
    src = Path(maker5m.__file__).parent
    for path in list((src / "risk").rglob("*.py")) + list(Path("tools").rglob("*.py")):
        if path.name in ("trace.py", "engine.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr != "reconciled", f"{path}: invisible mutation"


# -- every operational condition is an ordered signal ------------------------------------------


OPERATIONAL = [
    (RiskSignalKind.ORDER_RECONCILIATION_RESULT, RiskReason.ORDER_STATE_UNCERTAIN),
    (RiskSignalKind.POSITION_RECONCILIATION_RESULT, RiskReason.POSITION_MISMATCH),
    (RiskSignalKind.COST_RECONCILIATION_RESULT, RiskReason.COST_LEDGER_MISMATCH),
    (RiskSignalKind.API_ERROR_STATE_UPDATE, RiskReason.API_ERROR_RATE),
    (RiskSignalKind.RATE_LIMIT_STATE_UPDATE, RiskReason.RATE_LIMIT_UNCERTAIN),
    (RiskSignalKind.RESOLUTION_SAFETY_UPDATE, RiskReason.RESOLUTION_AMBIGUOUS),
    (RiskSignalKind.MAKER_ONLY_STATE_UPDATE, RiskReason.MAKER_ONLY_UNCERTAIN),
    (RiskSignalKind.TAKER_FILL_OBSERVED, RiskReason.TAKER_FILL),
]


@pytest.mark.parametrize(("kind", "reason"), OPERATIONAL, ids=[k.value for k, _ in OPERATIONAL])
def test_each_operational_signal_halts_and_is_recorded(
    kind: RiskSignalKind, reason: RiskReason
) -> None:
    rc = controller()
    ordinal = settle(rc)
    record = rc.apply(signal(kind, ordinal, flag=True), HEALTHY)
    assert record.state is RiskState.HALTED
    assert reason in record.active
    assert not record.allows_place
    assert record.allows_cancel
    assert rc.trace.records[-1] is record


def test_clock_drift_arrives_as_a_numeric_signal() -> None:
    rc = controller()
    ordinal = settle(rc)
    limit = int(rc.config.clock_drift_limit_ns)
    record = rc.apply(
        signal(RiskSignalKind.CLOCK_HEALTH_UPDATE, ordinal, value_ns=limit * 4), HEALTHY
    )
    assert RiskReason.CLOCK_DRIFT in record.active
    ordinal += 1
    record = rc.apply(signal(RiskSignalKind.CLOCK_HEALTH_UPDATE, ordinal, value_ns=0), HEALTHY)
    assert RiskReason.CLOCK_DRIFT not in record.active


# -- replay -------------------------------------------------------------------------------------


def busy_stream() -> RiskController:
    """A stream with feed faults, operational faults, latches, and reconciliations."""
    rc = controller()
    ordinal = settle(rc)
    unhealthy = HealthFrame(HealthStatus.DISCONNECTED, True, HealthStatus.HEALTHY)
    rc.evaluate(unhealthy, as_of_ingress_ordinal=ordinal, now_ns=NOW)
    ordinal += 1
    for _ in range(3):
        rc.evaluate(HEALTHY, as_of_ingress_ordinal=ordinal, now_ns=NOW)
        ordinal += 1
    rc.apply(signal(RiskSignalKind.ORDER_RECONCILIATION_RESULT, ordinal, flag=True), HEALTHY)
    ordinal += 1
    rc.apply(signal(RiskSignalKind.ORDER_RECONCILIATION_RESULT, ordinal, flag=False), HEALTHY)
    ordinal += 1
    rc.evaluate(HEALTHY, as_of_ingress_ordinal=ordinal, now_ns=NOW)
    ordinal += 1
    rc.apply(
        signal(
            RiskSignalKind.RECONCILIATION_CONFIRMED,
            ordinal,
            reason=RiskReason.ORDER_STATE_UNCERTAIN,
        )
    )
    ordinal += 1
    for _ in range(3):
        rc.evaluate(HEALTHY, as_of_ingress_ordinal=ordinal, now_ns=NOW)
        ordinal += 1
    return rc


def test_the_stream_is_non_trivial() -> None:
    rc = busy_stream()
    states = {record.state for record in rc.trace}
    assert states == {RiskState.SAFE, RiskState.HALTED, RiskState.RECOVERING}
    assert any(record.latched for record in rc.trace)


def test_replay_reproduces_the_whole_trajectory() -> None:
    rc = busy_stream()
    outcome = verify_risk_replay(list(rc.trace), config=rc.config)
    assert outcome.verified
    assert outcome.record_count == len(rc.trace)
    assert outcome.sequence_gaps == ()
    assert [record.state for record in rc.trace] == list(outcome.states)


def test_replay_is_repeatable() -> None:
    records = list(busy_stream().trace)
    first = verify_risk_replay(records)
    for _ in range(5):
        assert verify_risk_replay(records).states == first.states


def test_the_same_signals_produce_the_same_records() -> None:
    one = [record.summary() for record in busy_stream().trace]
    two = [record.summary() for record in busy_stream().trace]
    assert one == two


@pytest.mark.parametrize(
    "field_name", ["state", "active", "latched", "allows_place", "allows_cancel"]
)
def test_a_tampered_record_fails_closed(field_name: str) -> None:
    records = list(busy_stream().trace)
    index = next(i for i, r in enumerate(records) if r.state is RiskState.HALTED)
    tampered = {
        "state": RiskState.SAFE,
        "active": frozenset(),
        "latched": frozenset({RiskReason.TAKER_FILL}),
        "allows_place": True,
        "allows_cancel": False,
    }[field_name]
    records[index] = records[index]._replace(**{field_name: tampered})  # type: ignore[arg-type]

    with pytest.raises(RiskDivergenceError) as info:
        verify_risk_replay(records)
    assert info.value.field_name == field_name
    assert info.value.risk_sequence == records[index].risk_sequence


def test_replay_fails_at_the_first_divergence_not_the_last() -> None:
    records = list(busy_stream().trace)
    halted = [i for i, r in enumerate(records) if r.state is RiskState.HALTED]
    assert len(halted) >= 2
    for index in halted[:2]:
        records[index] = records[index]._replace(allows_place=True)
    with pytest.raises(RiskDivergenceError) as info:
        verify_risk_replay(records)
    assert info.value.risk_sequence == records[halted[0]].risk_sequence


def test_a_wrong_schema_version_fails_closed() -> None:
    records = list(busy_stream().trace)
    records[1] = records[1]._replace(schema_version=RISK_SCHEMA_VERSION + 1)
    with pytest.raises(RiskDivergenceError) as info:
        verify_risk_replay(records)
    assert info.value.field_name == "schema_version"


def test_a_sequence_gap_fails_closed() -> None:
    """A permission audit missing records cannot explain the cycles it lost."""
    records = list(busy_stream().trace)
    kept = records[:2] + records[4:]
    with pytest.raises(RiskDivergenceError) as info:
        verify_risk_replay(kept)
    assert info.value.field_name == "risk_sequence"
    assert info.value.expected == 2


def test_a_complete_stream_reports_no_gaps() -> None:
    outcome = verify_risk_replay(list(busy_stream().trace))
    assert outcome.sequence_gaps == ()


# -- the trace does not become a latency defect ---------------------------------------------------


def test_the_trace_is_bounded_and_never_blocks() -> None:
    rc = RiskController(
        provenance=RiskProvenance.SUPPORTING_UNIT_TEST,
    )
    rc.trace.capacity = 16
    rc.trace.__post_init__()
    for ordinal in range(200):
        rc.evaluate(HEALTHY, as_of_ingress_ordinal=ordinal, now_ns=NOW)
    assert len(rc.trace) == 16
    assert rc.trace.accepted == 200
    assert rc.trace.dropped == 184


def test_the_trace_performs_no_io() -> None:
    """P11 owns durable persistence. Nothing here encodes, writes, or logs on the hot path."""
    source = (Path(maker5m.__file__).parent / "risk" / "trace.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in {"json", "logging", "sqlite3", "pathlib"}
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in {"json", "logging", "sqlite3", "pathlib"}
