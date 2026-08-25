"""The risk sequence is the audit's index, so the verifier has to prove it.

**SUPPORTING UNIT TEST ONLY.** These are constructed traces exercising the verifier. They do not
substitute for the real-market evidence, which is separate.

The earlier verifier derived its expectation from ``records[0]``, so a trace whose prefix had
been lost — ``3, 4, 5`` — verified happily as "internally contiguous", and its forward-gap scan
also accepted duplicates (``0, 1, 1, 2``) and backwards values (``0, 1, 2, 1``). It never
compared the sequence it produced against the sequence that was recorded either, so the one
number the whole audit is indexed by went unverified.

The contract is now positional and absolute: ``record[i].risk_sequence == i``.
"""

from __future__ import annotations

import pytest

from maker5m.market.events import HealthStatus
from maker5m.market.timebase import TimestampNs
from maker5m.risk import (
    HealthFrame,
    RiskController,
    RiskDivergenceError,
    RiskProvenance,
    RiskReason,
    RiskRecord,
    RiskSignalKind,
    RiskState,
    RiskTrace,
    verify_risk_replay,
)

NOW = TimestampNs(1_787_647_500_000_000_000)
HEALTHY = HealthFrame(
    clob_status=HealthStatus.HEALTHY,
    clob_awaiting_snapshot=False,
    spot_status=HealthStatus.HEALTHY,
)


def stream(length: int = 8) -> list[RiskRecord]:
    """A complete trace: sequences 0 … length-1, with a halt in the middle."""
    controller = RiskController(provenance=RiskProvenance.SUPPORTING_UNIT_TEST)
    unhealthy = HealthFrame(HealthStatus.DISCONNECTED, True, HealthStatus.HEALTHY)
    for ordinal in range(length):
        health = unhealthy if ordinal == 3 else HEALTHY
        controller.evaluate(health, as_of_ingress_ordinal=ordinal, now_ns=NOW)
    records = list(controller.trace)
    assert [record.risk_sequence for record in records] == list(range(length))
    return records


def sequences(records: list[RiskRecord]) -> list[int]:
    return [record.risk_sequence for record in records]


# -- A. the valid case still verifies -----------------------------------------------------


def test_a_complete_contiguous_trace_verifies() -> None:
    records = stream()
    assert sequences(records) == [0, 1, 2, 3, 4, 5, 6, 7]
    outcome = verify_risk_replay(records)
    assert outcome.verified
    assert outcome.record_count == 8
    assert outcome.sequence_gaps == ()


def test_the_fixture_is_not_trivial() -> None:
    """Guard against the trace degenerating into one state that proves nothing."""
    records = stream()
    assert {record.state for record in records} >= {RiskState.SAFE, RiskState.HALTED}


# -- B-F. every malformed shape fails, and says which index -------------------------------


def renumber(records: list[RiskRecord], numbers: list[int]) -> list[RiskRecord]:
    return [
        record._replace(risk_sequence=number)
        for record, number in zip(records, numbers, strict=True)
    ]


MALFORMED: list[tuple[str, list[int], int, int]] = [
    ("lost prefix", [3, 4, 5, 6, 7, 8, 9, 10], 0, 3),
    ("missing middle record", [0, 1, 3, 4, 5, 6, 7, 8], 2, 3),
    ("duplicate", [0, 1, 1, 2, 3, 4, 5, 6], 2, 1),
    ("backwards", [0, 1, 2, 1, 4, 5, 6, 7], 3, 1),
    ("globally shifted", [100, 101, 102, 103, 104, 105, 106, 107], 0, 100),
]


@pytest.mark.parametrize(
    ("label", "numbers", "expected", "actual"), MALFORMED, ids=[case[0] for case in MALFORMED]
)
def test_a_malformed_sequence_fails_closed(
    label: str, numbers: list[int], expected: int, actual: int
) -> None:
    with pytest.raises(RiskDivergenceError) as info:
        verify_risk_replay(renumber(stream(), numbers))
    assert info.value.field_name == "risk_sequence", label
    assert info.value.expected == expected, label
    assert info.value.actual == actual, label


def test_an_actually_sliced_trace_fails_rather_than_looking_contiguous() -> None:
    """Not renumbered — genuinely dropping the first three records of a real trace."""
    records = stream()[3:]
    assert sequences(records) == [3, 4, 5, 6, 7], "the slice really does start at 3"
    with pytest.raises(RiskDivergenceError) as info:
        verify_risk_replay(records)
    assert info.value.field_name == "risk_sequence"
    assert (info.value.expected, info.value.actual) == (0, 3)


def test_a_middle_record_actually_removed_fails() -> None:
    records = stream()
    with pytest.raises(RiskDivergenceError) as info:
        verify_risk_replay(records[:4] + records[5:])
    assert (info.value.expected, info.value.actual) == (4, 5)


# -- G, J. the sequence is proved, not merely used ------------------------------------------


def test_a_tampered_sequence_fails_even_when_every_verdict_is_intact() -> None:
    """The verdict fields all agree; only the index was altered."""
    records = stream()
    records[5] = records[5]._replace(risk_sequence=99)
    with pytest.raises(RiskDivergenceError) as info:
        verify_risk_replay(records)
    assert info.value.field_name == "risk_sequence"
    assert (info.value.expected, info.value.actual) == (5, 99)


def test_the_produced_sequence_is_compared_to_the_recorded_one() -> None:
    """Not just positional: replay derives a sequence and checks it against the record.

    Constructed so the positional check passes — 0…7 in order — while the record at index 4
    claims a sequence the replay will not produce for it. Only the produced-versus-recorded
    comparison can catch that, so this fails if the verifier merely walks positions.
    """
    import maker5m.risk.replay as replay_module

    records = stream()
    original = replay_module._require_complete_sequence
    replay_module._require_complete_sequence = lambda _records: None  # type: ignore[assignment]
    try:
        records[4] = records[4]._replace(risk_sequence=40)
        with pytest.raises(RiskDivergenceError) as info:
            verify_risk_replay(records)
        assert info.value.field_name == "risk_sequence"
        assert (info.value.expected, info.value.actual) == (4, 40)
    finally:
        replay_module._require_complete_sequence = original


# -- H, I. the bounded trace, and what dropping means ---------------------------------------


def overflowing(capacity: int, cycles: int) -> RiskController:
    controller = RiskController(
        trace=RiskTrace(capacity=capacity), provenance=RiskProvenance.SUPPORTING_UNIT_TEST
    )
    for ordinal in range(cycles):
        controller.evaluate(HEALTHY, as_of_ingress_ordinal=ordinal, now_ns=NOW)
    return controller


def test_an_overflowed_trace_cannot_verify_as_complete() -> None:
    """Drop-oldest is right for the hot path and fatal for the audit claim."""
    controller = overflowing(capacity=16, cycles=100)
    assert controller.trace.dropped == 84
    assert len(controller.trace) == 16

    retained = list(controller.trace)
    assert retained[0].risk_sequence == 84, "the tail really does start late"
    with pytest.raises(RiskDivergenceError) as info:
        verify_risk_replay(retained)
    assert info.value.field_name == "risk_sequence"
    assert (info.value.expected, info.value.actual) == (0, 84)


def test_a_trace_that_never_overflowed_verifies() -> None:
    controller = overflowing(capacity=256, cycles=100)
    assert controller.trace.dropped == 0
    outcome = verify_risk_replay(list(controller.trace), config=controller.config)
    assert outcome.verified
    assert outcome.record_count == 100


def test_nothing_renumbers_a_dropped_tail() -> None:
    """The retained records keep their real sequences. No repair, no reconstruction."""
    controller = overflowing(capacity=8, cycles=40)
    retained = list(controller.trace)
    assert sequences(retained) == list(range(32, 40))


# -- the rules this must not weaken ----------------------------------------------------------


def test_one_ingress_ordinal_may_carry_several_risk_sequences() -> None:
    """More than one evaluation or signal may occur between market events. That is legal."""
    controller = RiskController(provenance=RiskProvenance.SUPPORTING_UNIT_TEST)
    for _ in range(4):
        controller.evaluate(HEALTHY, as_of_ingress_ordinal=7, now_ns=NOW)
    records = list(controller.trace)
    assert {record.as_of_ingress_ordinal for record in records} == {7}
    assert sequences(records) == [0, 1, 2, 3]
    assert verify_risk_replay(records).verified


def test_the_out_of_order_ingress_rule_is_unchanged() -> None:
    from maker5m.risk import RiskOrderError

    controller = RiskController(provenance=RiskProvenance.SUPPORTING_UNIT_TEST)
    controller.evaluate(HEALTHY, as_of_ingress_ordinal=100, now_ns=NOW)
    with pytest.raises(RiskOrderError, match="out of order"):
        controller.evaluate(HEALTHY, as_of_ingress_ordinal=99, now_ns=NOW)


def test_the_reconciliation_rules_are_unchanged() -> None:
    from maker5m.risk import RiskOrderError, RiskSignal

    controller = RiskController(provenance=RiskProvenance.SUPPORTING_UNIT_TEST)
    for ordinal in range(4):
        controller.evaluate(HEALTHY, as_of_ingress_ordinal=ordinal, now_ns=NOW)
    with pytest.raises(RiskOrderError, match="not latched"):
        controller.apply(
            RiskSignal(
                kind=RiskSignalKind.RECONCILIATION_CONFIRMED,
                as_of_ingress_ordinal=5,
                timestamp=NOW,
                provenance=RiskProvenance.SUPPORTING_UNIT_TEST,
                reason=RiskReason.POSITION_MISMATCH,
            )
        )
