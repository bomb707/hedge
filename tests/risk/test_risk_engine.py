"""Every Canonical §28.1 condition trips, and none of them trades.

**SUPPORTING UNIT TEST ONLY.** These are constructed inputs to a pure function. They prove the
state machine is correct; they prove nothing about how the bot behaves against a real venue.
The real-market integration gate is separate and lives in `docs/evidence/P9-REAL-MARKET-*.md`.
"""

from __future__ import annotations

import dataclasses

import pytest

from maker5m.market.events import HealthStatus
from maker5m.market.timebase import TimestampNs, millis, seconds
from maker5m.risk import (
    REQUIRES_RECONCILIATION,
    RiskConfig,
    RiskEngine,
    RiskInputs,
    RiskReason,
    RiskSnapshot,
    RiskState,
    active_reasons,
    evaluate,
)

NOW = TimestampNs(1_787_647_500_000_000_000)
CONFIG = RiskConfig()


def healthy(**overrides: object) -> RiskInputs:
    """A fully trusted world: both feeds healthy, snapshot in hand, nothing outstanding."""
    base = RiskInputs(
        now_ns=NOW,
        clob_status=HealthStatus.HEALTHY,
        clob_awaiting_snapshot=False,
        clob_last_message_at=NOW,
        spot_status=HealthStatus.HEALTHY,
        spot_last_message_at=NOW,
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def settled(inputs: RiskInputs, config: RiskConfig = CONFIG) -> RiskEngine:
    """An engine driven to SAFE, so a following condition is a transition rather than a start."""
    engine = RiskEngine(config=config)
    for _ in range(config.recovery_confirmations + 1):
        engine.evaluate(inputs)
    assert engine.state is RiskState.SAFE
    return engine


# -- the healthy baseline ------------------------------------------------------------------


def test_a_fully_healthy_world_reaches_safe() -> None:
    engine = RiskEngine()
    decision = engine.evaluate(healthy())
    assert decision.state is RiskState.RECOVERING, "one clear evaluation is not enough"
    decision = engine.evaluate(healthy())
    assert decision.state is RiskState.SAFE
    assert decision.active == frozenset()
    assert decision.allows_place


def test_a_fresh_engine_starts_unsafe() -> None:
    """Before anything has been observed, nothing is known. Permission must not be the default."""
    engine = RiskEngine()
    assert engine.state is RiskState.RECOVERING
    assert not evaluate(RiskSnapshot(), RiskInputs(now_ns=NOW), CONFIG).allows_place


# -- every §28.1 condition halts ------------------------------------------------------------


CONDITIONS: list[tuple[str, dict[str, object], RiskReason]] = [
    ("clob stale status", {"clob_status": HealthStatus.STALE}, RiskReason.CLOB_STALE),
    (
        "clob quiet past threshold",
        {"clob_last_message_at": TimestampNs(NOW - seconds(30))},
        RiskReason.CLOB_STALE,
    ),
    (
        "clob disconnected",
        {"clob_status": HealthStatus.DISCONNECTED},
        RiskReason.CLOB_CONTINUITY_UNCERTAIN,
    ),
    (
        "clob sequence gap",
        {"clob_status": HealthStatus.SEQUENCE_GAP},
        RiskReason.CLOB_CONTINUITY_UNCERTAIN,
    ),
    (
        "clob awaiting snapshot",
        {"clob_awaiting_snapshot": True},
        RiskReason.CLOB_CONTINUITY_UNCERTAIN,
    ),
    ("spot stale status", {"spot_status": HealthStatus.STALE}, RiskReason.SPOT_STALE),
    (
        "spot quiet past threshold",
        {"spot_last_message_at": TimestampNs(NOW - seconds(30))},
        RiskReason.SPOT_STALE,
    ),
    (
        "spot disconnected",
        {"spot_status": HealthStatus.DISCONNECTED},
        RiskReason.SPOT_STALE,
    ),
    ("clock drift", {"clock_drift_ns": int(seconds(1))}, RiskReason.CLOCK_DRIFT),
    (
        "negative clock drift",
        {"clock_drift_ns": -int(seconds(1))},
        RiskReason.CLOCK_DRIFT,
    ),
    (
        "order state uncertain",
        {"order_state_uncertain": True},
        RiskReason.ORDER_STATE_UNCERTAIN,
    ),
    (
        "order stream unknown while required",
        {"order_stream_required": True, "order_stream_status": HealthStatus.UNKNOWN},
        RiskReason.ORDER_STATE_UNCERTAIN,
    ),
    (
        "maker-only uncertain",
        {"maker_only_uncertain": True},
        RiskReason.MAKER_ONLY_UNCERTAIN,
    ),
    ("position mismatch", {"position_mismatch": True}, RiskReason.POSITION_MISMATCH),
    (
        "cost ledger mismatch",
        {"cost_ledger_mismatch": True},
        RiskReason.COST_LEDGER_MISMATCH,
    ),
    ("api error rate", {"api_errors_exceeded": True}, RiskReason.API_ERROR_RATE),
    (
        "rate-limit uncertainty",
        {"rate_limit_uncertain": True},
        RiskReason.RATE_LIMIT_UNCERTAIN,
    ),
    (
        "resolution ambiguous",
        {"resolution_ambiguous": True},
        RiskReason.RESOLUTION_AMBIGUOUS,
    ),
    ("taker fill", {"taker_fill_seen": True}, RiskReason.TAKER_FILL),
]


@pytest.mark.parametrize(
    ("label", "override", "reason"), CONDITIONS, ids=[case[0] for case in CONDITIONS]
)
def test_each_condition_halts_and_names_itself(
    label: str, override: dict[str, object], reason: RiskReason
) -> None:
    engine = settled(healthy())
    decision = engine.evaluate(healthy(**override))
    assert decision.state is RiskState.HALTED, label
    assert reason in decision.active, label
    assert not decision.allows_place
    assert decision.allows_cancel, "withdrawing a quote must never be blocked"


def test_every_canonical_condition_is_represented() -> None:
    """Guard against a reason existing in the enum but never being reachable."""
    covered = {reason for _, _, reason in CONDITIONS}
    assert covered == set(RiskReason), sorted(reason.value for reason in set(RiskReason) - covered)


def test_an_absent_order_stream_is_not_a_fault() -> None:
    """No credential exists before P14, so an unopened stream is not evidence of trouble."""
    decision = evaluate(RiskSnapshot(), healthy(order_stream_status=HealthStatus.UNKNOWN), CONFIG)
    assert RiskReason.ORDER_STATE_UNCERTAIN not in decision.active


def test_a_stream_that_has_never_spoken_is_not_yet_stale() -> None:
    reasons = active_reasons(healthy(clob_last_message_at=None, spot_last_message_at=None), CONFIG)
    assert RiskReason.CLOB_STALE not in reasons
    assert RiskReason.SPOT_STALE not in reasons


# -- multiple faults --------------------------------------------------------------------------


def test_simultaneous_conditions_are_all_reported() -> None:
    engine = settled(healthy())
    decision = engine.evaluate(
        healthy(spot_status=HealthStatus.STALE, clock_drift_ns=int(seconds(1)))
    )
    assert decision.active == {RiskReason.SPOT_STALE, RiskReason.CLOCK_DRIFT}


def test_one_condition_clearing_does_not_resume_while_another_remains() -> None:
    engine = settled(healthy())
    engine.evaluate(healthy(spot_status=HealthStatus.STALE, clock_drift_ns=int(seconds(1))))

    decision = engine.evaluate(healthy(clock_drift_ns=int(seconds(1))))
    assert decision.state is RiskState.HALTED
    assert decision.active == {RiskReason.CLOCK_DRIFT}
    assert not decision.allows_place

    for _ in range(CONFIG.recovery_confirmations):
        decision = engine.evaluate(healthy())
    assert decision.state is RiskState.SAFE


# -- recovery ---------------------------------------------------------------------------------


def test_halted_passes_through_recovering_and_never_jumps_to_safe() -> None:
    engine = settled(healthy())
    assert engine.evaluate(healthy(spot_status=HealthStatus.STALE)).state is RiskState.HALTED

    states = [engine.evaluate(healthy()).state for _ in range(CONFIG.recovery_confirmations)]
    assert states[0] is RiskState.RECOVERING, "the first clear evaluation must not be SAFE"
    assert states[-1] is RiskState.SAFE


def test_a_clob_reconnect_alone_does_not_restore_safe() -> None:
    """A socket being up is not a book being trustworthy. The snapshot is the evidence."""
    engine = settled(healthy())
    engine.evaluate(healthy(clob_status=HealthStatus.DISCONNECTED, clob_awaiting_snapshot=True))

    for _ in range(6):
        decision = engine.evaluate(
            healthy(clob_status=HealthStatus.HEALTHY, clob_awaiting_snapshot=True)
        )
        assert decision.state is RiskState.HALTED, "still awaiting a snapshot"
        assert RiskReason.CLOB_CONTINUITY_UNCERTAIN in decision.active

    for _ in range(CONFIG.recovery_confirmations):
        decision = engine.evaluate(healthy())
    assert decision.state is RiskState.SAFE


@pytest.mark.parametrize("reason", sorted(REQUIRES_RECONCILIATION, key=RiskReason.__str__))
def test_reasons_needing_reconciliation_latch_past_their_condition(reason: RiskReason) -> None:
    """The condition going quiet is not the same as the problem being understood."""
    field = {
        RiskReason.ORDER_STATE_UNCERTAIN: "order_state_uncertain",
        RiskReason.POSITION_MISMATCH: "position_mismatch",
        RiskReason.COST_LEDGER_MISMATCH: "cost_ledger_mismatch",
        RiskReason.TAKER_FILL: "taker_fill_seen",
    }[reason]
    engine = settled(healthy())
    engine.evaluate(healthy(**{field: True}))

    for _ in range(10):
        decision = engine.evaluate(healthy())
        assert decision.state is RiskState.RECOVERING
        assert reason in decision.latched
        assert not decision.allows_place

    engine.reconciled(reason)
    for _ in range(CONFIG.recovery_confirmations):
        decision = engine.evaluate(healthy())
    assert decision.state is RiskState.SAFE


def test_reconciliation_cannot_clear_a_still_active_condition() -> None:
    engine = settled(healthy())
    engine.evaluate(healthy(position_mismatch=True))
    engine.reconciled(RiskReason.POSITION_MISMATCH)
    decision = engine.evaluate(healthy(position_mismatch=True))
    assert decision.state is RiskState.HALTED
    assert RiskReason.POSITION_MISMATCH in decision.active


def test_reconciling_a_reason_that_needs_none_is_refused() -> None:
    """A guard against "reconciling" a stale feed instead of waiting for real data."""
    engine = RiskEngine()
    with pytest.raises(ValueError, match="do not require reconciliation"):
        engine.reconciled(RiskReason.CLOB_STALE)


# -- determinism and configuration --------------------------------------------------------------


def test_evaluation_is_pure_and_repeatable() -> None:
    inputs = healthy(spot_status=HealthStatus.STALE, position_mismatch=True)
    snapshot = RiskSnapshot()
    first = evaluate(snapshot, inputs, CONFIG)
    for _ in range(20):
        again = evaluate(snapshot, inputs, CONFIG)
        assert again.state is first.state
        assert again.active == first.active
        assert again.latched == first.latched
        assert again.snapshot == first.snapshot


def test_thresholds_are_configuration_not_constants() -> None:
    tight = RiskConfig(clock_drift_limit_ns=millis(1), recovery_confirmations=1)
    drifting = healthy(clock_drift_ns=int(millis(5)))
    assert RiskReason.CLOCK_DRIFT in active_reasons(drifting, tight)
    assert RiskReason.CLOCK_DRIFT not in active_reasons(drifting, CONFIG)


def test_configuration_is_labelled_operational() -> None:
    """Invariant I18: nothing here is reconstructed from the frozen sources."""
    from maker5m.domain import ParameterStatus

    assert RiskConfig().status is ParameterStatus.OPERATIONAL


def test_invalid_configuration_is_refused() -> None:
    for bad in (
        {"api_error_threshold": 0},
        {"recovery_confirmations": 0},
        {"clock_drift_limit_ns": 0},
    ):
        with pytest.raises(ValueError):
            RiskConfig(**bad)  # type: ignore[arg-type]
