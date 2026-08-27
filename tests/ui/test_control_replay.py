"""Operator control is ordered, and therefore replayable.

**SUPPORTING UNIT TEST ONLY.** The point of routing a command through the risk stream rather than
into a mutable field is that the stream can be replayed: the same ordered signals must produce
the same permission transitions, or the audit is a description of something that cannot be
checked.
"""

from __future__ import annotations

from dataclasses import dataclass

from maker5m.market.events import HealthStatus
from maker5m.market.timebase import TimestampNs
from maker5m.risk import (
    RiskConfig,
    RiskEngine,
    RiskProvenance,
    RiskReason,
    RiskSignal,
    RiskSignalKind,
    RiskState,
)
from maker5m.risk.trace import HealthFrame, RiskController
from maker5m.ui import COMMAND_SCHEMA_VERSION, CommandKind, ControlIngress, OperatorCommand

HEALTHY = HealthFrame(
    clob_status=HealthStatus.HEALTHY,
    clob_awaiting_snapshot=False,
    spot_status=HealthStatus.HEALTHY,
    order_stream_status=HealthStatus.HEALTHY,
)


@dataclass(frozen=True, slots=True)
class Step:
    """One thing that happens, in order. A command, a health evaluation, or another signal."""

    kind: str
    ordinal: int
    payload: object = None


def command(kind: CommandKind, command_id: str) -> OperatorCommand:
    return OperatorCommand(
        schema_version=COMMAND_SCHEMA_VERSION,
        command_id=command_id,
        kind=kind.value,
        issued_at_ns=1,
    )


def run(steps: list[Step]) -> list[tuple[int, str, bool, tuple[str, ...], tuple[str, ...]]]:
    """Drive one ordered stream and return the permission trajectory it produced."""
    control = RiskController(
        engine=RiskEngine(config=RiskConfig()), provenance=RiskProvenance.SUPPORTING_UNIT_TEST
    )
    ingress = ControlIngress(controller=control)
    for step in steps:
        if step.kind == "evaluate":
            control.evaluate(
                HEALTHY, as_of_ingress_ordinal=step.ordinal, now_ns=TimestampNs(step.ordinal)
            )
        elif step.kind == "command":
            assert isinstance(step.payload, OperatorCommand)
            ingress.apply(
                step.payload, ingress_ordinal=step.ordinal, now_ns=TimestampNs(step.ordinal)
            )
        elif step.kind == "position_mismatch":
            control.apply(
                RiskSignal(
                    kind=RiskSignalKind.POSITION_RECONCILIATION_RESULT,
                    as_of_ingress_ordinal=step.ordinal,
                    timestamp=TimestampNs(step.ordinal),
                    provenance=control.provenance,
                    reason=RiskReason.POSITION_MISMATCH,
                    flag=bool(step.payload),
                )
            )
    return [
        (
            record.risk_sequence,
            record.state.value,
            record.allows_place,
            tuple(sorted(reason.value for reason in record.active)),
            tuple(sorted(reason.value for reason in record.latched)),
        )
        for record in control.trace.records
    ]


SETTLED = [Step("evaluate", n) for n in range(6)]


def test_halt_survives_a_clean_market_and_releases_to_safe() -> None:
    """§27, first sequence: a healthy market does not talk an operator out of a halt."""
    steps = [
        *SETTLED,
        Step("command", 10, command(CommandKind.OPERATOR_HALT, "halt-1")),
        *[Step("evaluate", n) for n in range(11, 16)],
        Step("command", 20, command(CommandKind.RELEASE_OPERATOR_HALT, "release-1")),
        *[Step("evaluate", n) for n in range(21, 30)],
    ]
    trajectory = run(steps)
    states = [entry[1] for entry in trajectory]

    halted = trajectory[6]
    assert halted[1] == RiskState.HALTED.value
    assert halted[2] is False
    assert "OPERATOR_HALT" in halted[3]

    # Five clean evaluations, and it is still halted. Health is not a release.
    for entry in trajectory[7:12]:
        assert entry[1] == RiskState.HALTED.value, "a clean market does not clear an operator halt"
        assert "OPERATOR_HALT" in entry[3]

    assert states[-1] == RiskState.SAFE.value
    assert trajectory[-1][2] is True


def test_release_does_not_return_to_safe_while_another_reason_stands() -> None:
    """§27, second sequence: an independent reconciliation-required reason outlives the release."""
    steps = [
        *SETTLED,
        Step("command", 10, command(CommandKind.OPERATOR_HALT, "halt-1")),
        Step("position_mismatch", 11, True),
        Step("position_mismatch", 12, False),
        Step("command", 20, command(CommandKind.RELEASE_OPERATOR_HALT, "release-1")),
        *[Step("evaluate", n) for n in range(21, 32)],
    ]
    trajectory = run(steps)
    final = trajectory[-1]

    assert "OPERATOR_HALT" not in final[3], "the operator's own condition did clear"
    assert "POSITION_MISMATCH" in final[4], "and the latch it could not see did not"
    assert final[1] != RiskState.SAFE.value
    assert final[2] is False


def test_the_same_ordered_stream_replays_identically() -> None:
    """Two independent runs of the same ordered control stream, compared record for record."""
    steps = [
        *SETTLED,
        Step("command", 10, command(CommandKind.OPERATOR_HALT, "halt-1")),
        Step("evaluate", 11),
        Step("position_mismatch", 12, True),
        Step("command", 13, command(CommandKind.RELEASE_OPERATOR_HALT, "release-1")),
        Step("position_mismatch", 14, False),
        *[Step("evaluate", n) for n in range(15, 25)],
    ]
    first = run(steps)
    second = run(steps)
    assert first == second
    assert [entry[0] for entry in first] == list(range(len(first))), "sequence is 0..N"


def test_a_command_out_of_order_is_refused_rather_than_reordered() -> None:
    """P9 will not silently reorder its own stream, and an operator cannot make it."""
    control = RiskController(
        engine=RiskEngine(config=RiskConfig()), provenance=RiskProvenance.SUPPORTING_UNIT_TEST
    )
    ingress = ControlIngress(controller=control)
    control.evaluate(HEALTHY, as_of_ingress_ordinal=100, now_ns=TimestampNs(100))

    outcome = ingress.apply(
        command(CommandKind.OPERATOR_HALT, "late"), ingress_ordinal=5, now_ns=TimestampNs(5)
    )
    assert not outcome.accepted
    assert "out of order" in outcome.detail
    assert ingress.refused == 1
    assert control.trace.records[-1].signal.kind is RiskSignalKind.RISK_EVALUATION


def test_a_refused_command_leaves_no_trace_in_the_risk_stream() -> None:
    control = RiskController(
        engine=RiskEngine(config=RiskConfig()), provenance=RiskProvenance.SUPPORTING_UNIT_TEST
    )
    ingress = ControlIngress(controller=control)
    control.evaluate(HEALTHY, as_of_ingress_ordinal=0, now_ns=TimestampNs(0))
    before = control.sequence

    ingress.apply(
        OperatorCommand(
            schema_version=COMMAND_SCHEMA_VERSION,
            command_id="x",
            kind=CommandKind.OPERATOR_HALT.value,
            issued_at_ns=1,
        ),
        ingress_ordinal=-5,
        now_ns=TimestampNs(0),
    )
    assert control.sequence == before, "nothing was recorded for a command that was not accepted"


def test_an_accepted_command_is_published_for_persistence() -> None:
    """The first real market found this missing.

    Without it the two operator commands were applied, appeared in the in-memory trace, and were
    the only two records of 107,252 absent from the durable risk stream — so the market verified
    INCOMPLETE, correctly. A control action that changed what the bot was allowed to do and left
    no durable record is exactly what the audit exists to prevent.
    """
    published: list[object] = []
    control = RiskController(
        engine=RiskEngine(config=RiskConfig()), provenance=RiskProvenance.SUPPORTING_UNIT_TEST
    )
    control.evaluate(HEALTHY, as_of_ingress_ordinal=0, now_ns=TimestampNs(0))
    ingress = ControlIngress(controller=control, publish=published.append)

    ingress.apply(command(CommandKind.OPERATOR_HALT, "h"), ingress_ordinal=1, now_ns=TimestampNs(1))
    ingress.apply(
        command(CommandKind.RELEASE_OPERATOR_HALT, "r"), ingress_ordinal=2, now_ns=TimestampNs(2)
    )

    assert len(published) == 2
    assert [getattr(record, "risk_sequence", None) for record in published] == [1, 2]


def test_a_refused_command_publishes_nothing() -> None:
    published: list[object] = []
    control = RiskController(
        engine=RiskEngine(config=RiskConfig()), provenance=RiskProvenance.SUPPORTING_UNIT_TEST
    )
    control.evaluate(HEALTHY, as_of_ingress_ordinal=100, now_ns=TimestampNs(100))
    ingress = ControlIngress(controller=control, publish=published.append)

    ingress.apply(
        command(CommandKind.OPERATOR_HALT, "late"), ingress_ordinal=1, now_ns=TimestampNs(1)
    )
    assert published == []
