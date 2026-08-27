"""The snapshot reports what P6, P9, P8 and P10 actually recorded — nothing inferred.

**SUPPORTING UNIT TEST ONLY.** The first P12 hardcoded `risk_active = ()` and `risk_latched = ()`,
derived "awaiting snapshot" from "not healthy", and called the spot feed HEALTHY because a spot
timestamp existed. Each of those is a dashboard telling an operator something nobody measured.
"""

from __future__ import annotations

from typing import Any

from maker5m.market.events import HealthStatus
from maker5m.risk import RiskReason, RiskState
from maker5m.risk.trace import HealthFrame
from maker5m.strategy import BaseLot, default_config
from maker5m.ui import SnapshotPublisher
from tests.persistence.builders import identity, observation


class FakeVerdict:
    """The shape of a P9 RiskRecord, carrying exactly what P9 recorded."""

    def __init__(
        self,
        *,
        state: RiskState,
        active: set[RiskReason],
        latched: set[RiskReason],
        health: HealthFrame,
        sequence: int = 77,
        ordinal: int = 4242,
    ) -> None:
        self.state = state
        self.active = frozenset(active)
        self.latched = frozenset(latched)
        self.health = health
        self.risk_sequence = sequence
        self.allows_place = False
        self.allows_cancel = True
        self.signal = type("S", (), {"as_of_ingress_ordinal": ordinal})()


def record(**overrides: Any) -> Any:
    from maker5m.persistence import build_decision_record

    return build_decision_record(
        observation(0, ordinal=4242, **overrides), identity(), persistence_sequence=1
    )


def publisher() -> SnapshotPublisher:
    return SnapshotPublisher(identity=identity(), config=default_config(BaseLot.of(15)))


def test_the_snapshot_reports_the_exact_recorded_reasons() -> None:
    """§25: the reasons P9 wrote down, not a set derived from the state name."""
    verdict = FakeVerdict(
        state=RiskState.HALTED,
        active={RiskReason.OPERATOR_HALT, RiskReason.CLOB_STALE},
        latched={RiskReason.POSITION_MISMATCH},
        health=HealthFrame(
            clob_status=HealthStatus.STALE,
            clob_awaiting_snapshot=False,
            spot_status=HealthStatus.STALE,
            order_stream_status=HealthStatus.UNKNOWN,
        ),
    )
    unit = publisher()
    unit.observe(record(risk=(77, "HALTED", False, True)), verdict)
    snapshot = unit.build(now=1.0)

    assert snapshot.risk_active == ("CLOB_STALE", "OPERATOR_HALT")
    assert snapshot.risk_latched == ("POSITION_MISMATCH",)
    assert snapshot.risk_state == "HALTED"
    assert snapshot.risk_sequence == 77
    assert snapshot.allows_place is False
    assert snapshot.allows_cancel is True


def test_health_comes_from_the_frame_not_from_the_presence_of_data() -> None:
    """A spot price can exist while the feed is STALE. Those are different facts."""
    verdict = FakeVerdict(
        state=RiskState.HALTED,
        active={RiskReason.SPOT_STALE},
        latched=set(),
        health=HealthFrame(
            clob_status=HealthStatus.HEALTHY,
            clob_awaiting_snapshot=False,
            spot_status=HealthStatus.STALE,
        ),
    )
    unit = publisher()
    built = record()
    assert built.spot_age_ns is not None, "there *is* a spot timestamp"

    unit.observe(built, verdict)
    snapshot = unit.build(now=1.0)
    assert snapshot.spot_status == "STALE", "a present price is not a healthy feed"
    assert snapshot.clob_status == "HEALTHY"
    assert snapshot.clob_awaiting_snapshot is False


def test_awaiting_snapshot_is_read_not_derived_from_health() -> None:
    verdict = FakeVerdict(
        state=RiskState.HALTED,
        active=set(),
        latched=set(),
        health=HealthFrame(
            clob_status=HealthStatus.HEALTHY,
            clob_awaiting_snapshot=True,
            spot_status=HealthStatus.HEALTHY,
        ),
    )
    unit = publisher()
    unit.observe(record(), verdict)
    snapshot = unit.build(now=1.0)
    assert snapshot.clob_status == "HEALTHY"
    assert snapshot.clob_awaiting_snapshot is True, "healthy and awaiting a snapshot at once"


def test_no_verdict_reports_unknown_rather_than_healthy() -> None:
    unit = publisher()
    unit.observe(record(), None)
    snapshot = unit.build(now=1.0)
    assert snapshot.clob_status == "UNKNOWN"
    assert snapshot.spot_status == "UNKNOWN"
    assert snapshot.risk_active == ()


def test_the_latency_view_carries_p8_measurements() -> None:
    """§26: exactly the numbers P8 measured, and the ordinal they were measured at."""
    unit = publisher()
    unit.observe(record(), None)
    unit.observe_latency(
        4240,
        {
            "decide_ns": 21_555,
            "prepare_ns": 3_120,
            "reconcile_ns": 8_902,
            "receive_to_reconcile_ns": 13_802,
        },
    )
    snapshot = unit.build(now=1.0)
    assert snapshot.decide_ns == 21_555
    assert snapshot.prepare_ns == 3_120
    assert snapshot.reconcile_ns == 8_902
    assert snapshot.receive_to_reconcile_ns == 13_802
    assert snapshot.latency_sample_ordinal == 4240


def test_an_unsampled_cycle_reports_no_latency_rather_than_zero() -> None:
    unit = publisher()
    unit.observe(record(), None)
    snapshot = unit.build(now=1.0)
    assert snapshot.decide_ns is None
    assert snapshot.latency_sample_ordinal is None


def test_the_snapshot_says_which_ordinal_each_part_describes() -> None:
    """§14: a view that mixes observation points must at least say that it does."""
    verdict = FakeVerdict(
        state=RiskState.SAFE,
        active=set(),
        latched=set(),
        health=HealthFrame(),
        sequence=77,
        ordinal=4242,
    )
    unit = publisher()
    unit.observe(record(), verdict)
    unit.observe_latency(4100, {"decide_ns": 1})
    points = unit.build(now=1.0).observation_points
    assert points["decision"] == 4242
    assert points["risk_verdict"] == 4242
    assert points["latency_sample"] == 4100
    assert points["counters"] is None


def test_settlement_reaches_the_snapshot() -> None:
    """§27."""
    unit = publisher()
    unit.observe(record(), None)
    unit.deliver(
        "settlement",
        {
            "state": "RESOLVED",
            "winning_outcome": "UP",
            "authoritative_block": 92_696_216,
            "payout_numerators": [1, 0],
            "note": "redemption is disabled in this build; nothing was redeemed",
        },
    )
    snapshot = unit.build(now=1.0)
    assert snapshot.resolution_state == "RESOLVED"
    assert snapshot.winning_outcome == "UP"
    assert snapshot.authoritative_block == 92_696_216
    assert snapshot.payout_numerators == (1, 0)
    assert snapshot.redemption_enabled is False
    assert "disabled" in snapshot.settlement_note


def test_verification_status_replaces_unknown_after_close() -> None:
    """§16: an operator should not be left reading 'unknown' after the verifier answered."""
    unit = publisher()
    unit.observe(record(), None)
    assert unit.build(now=1.0).telemetry_complete is None

    unit.deliver("verification", {"status": "COMPLETE", "complete": True})
    snapshot = unit.build(now=2.0)
    assert snapshot.verification_status == "COMPLETE"
    assert snapshot.telemetry_complete is True


def test_messages_arrive_through_one_bounded_inbox() -> None:
    """§9: nothing mutates the read model from another thread by reaching into it."""
    unit = publisher()
    unit.observe(record(), None)
    unit.deliver("counters", {"decisions": 42, "risk": 43})
    unit.deliver("command", {"command_id": "abc", "kind": "OPERATOR_HALT", "accepted": True})
    snapshot = unit.build(now=1.0)
    assert snapshot.decisions_persisted == 42
    assert snapshot.risk_records_persisted == 43
    assert snapshot.accepted_commands[0]["command_id"] == "abc"


def test_the_command_history_is_bounded() -> None:
    unit = publisher()
    unit.observe(record(), None)
    for index in range(40):
        unit.deliver("command", {"command_id": f"c{index}"})
    snapshot = unit.build(now=1.0)
    assert len(snapshot.accepted_commands) == 10
    assert snapshot.accepted_commands[-1]["command_id"] == "c39"
