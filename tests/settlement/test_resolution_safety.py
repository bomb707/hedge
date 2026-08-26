"""Settlement ambiguity reaching execution permission — §15.

SUPPORTING UNIT TEST ONLY for the mechanism. The evidence that it fires on a real market is
the controlled injection recorded in `docs/evidence/P10-SETTLEMENT-REAL-MARKET.md`; these tests
prove the bridge behaves, not that the venue ever produced an ambiguous settlement.
"""

from __future__ import annotations

import pytest

from maker5m.market.events import HealthStatus
from maker5m.market.timebase import TimestampNs
from maker5m.risk import (
    REQUIRES_RECONCILIATION,
    RiskProvenance,
    RiskReason,
    RiskSignal,
    RiskSignalKind,
    RiskState,
)
from maker5m.risk.trace import HealthFrame, RiskController
from maker5m.settlement import (
    AmbiguityReason,
    ResolutionDecision,
    ResolutionState,
    report_resolution,
    resolution_safety_signal,
)

HEALTHY = HealthFrame(
    clob_status=HealthStatus.HEALTHY,
    clob_awaiting_snapshot=False,
    spot_status=HealthStatus.HEALTHY,
    order_stream_status=HealthStatus.HEALTHY,
)


def decision(state: ResolutionState) -> ResolutionDecision:
    reasons = (AmbiguityReason.PROVIDER_DISAGREEMENT,) if state is ResolutionState.AMBIGUOUS else ()
    return ResolutionDecision(state=state, reasons=reasons)


def controller() -> RiskController:
    ctrl = RiskController()
    ctrl.evaluate(HEALTHY, as_of_ingress_ordinal=0, now_ns=TimestampNs(0))
    return ctrl


def test_a_resolved_market_emits_no_signal_at_all() -> None:
    assert (
        resolution_safety_signal(
            decision(ResolutionState.RESOLVED), as_of_ingress_ordinal=1, now_ns=TimestampNs(1)
        )
        is None
    )


def test_an_unresolved_market_emits_no_signal() -> None:
    """Otherwise every market would halt for the whole of its own lifetime."""
    assert (
        resolution_safety_signal(
            decision(ResolutionState.UNRESOLVED), as_of_ingress_ordinal=1, now_ns=TimestampNs(1)
        )
        is None
    )


@pytest.mark.parametrize(
    "state", [ResolutionState.AMBIGUOUS, ResolutionState.INSUFFICIENT_EVIDENCE]
)
def test_not_knowing_the_payout_halts_placement(state: ResolutionState) -> None:
    ctrl = controller()
    assert ctrl.state.allows_place if hasattr(ctrl.state, "allows_place") else True

    record = report_resolution(
        ctrl, decision(state), as_of_ingress_ordinal=1, now_ns=TimestampNs(1)
    )
    assert record is not None
    assert record.signal.kind is RiskSignalKind.RESOLUTION_SAFETY_UPDATE
    assert record.signal.reason is RiskReason.RESOLUTION_AMBIGUOUS
    assert record.signal.flag is True
    assert RiskReason.RESOLUTION_AMBIGUOUS in record.active
    assert not record.allows_place


def test_cancelling_stays_allowed_while_resolution_is_ambiguous() -> None:
    """Being unable to price a market is a reason to stop adding to it, not to be stuck in it."""
    ctrl = controller()
    record = report_resolution(
        ctrl,
        decision(ResolutionState.AMBIGUOUS),
        as_of_ingress_ordinal=1,
        now_ns=TimestampNs(1),
    )
    assert record is not None
    assert record.allows_cancel


def test_the_signal_enters_the_ordered_audit_trace() -> None:
    ctrl = controller()
    before = ctrl.sequence
    record = report_resolution(
        ctrl,
        decision(ResolutionState.AMBIGUOUS),
        as_of_ingress_ordinal=2,
        now_ns=TimestampNs(2),
    )
    assert record is not None
    assert record.risk_sequence == before + 1
    assert ctrl.trace.records[-1] is record


def test_a_later_clean_reading_does_not_clear_the_halt_by_itself() -> None:
    """A second look that happens to agree is not evidence the disagreement was imaginary."""
    ctrl = controller()
    report_resolution(
        ctrl,
        decision(ResolutionState.AMBIGUOUS),
        as_of_ingress_ordinal=1,
        now_ns=TimestampNs(1),
    )
    assert (
        report_resolution(
            ctrl,
            decision(ResolutionState.RESOLVED),
            as_of_ingress_ordinal=2,
            now_ns=TimestampNs(2),
        )
        is None
    )
    after = ctrl.evaluate(HEALTHY, as_of_ingress_ordinal=3, now_ns=TimestampNs(3))
    assert RiskReason.RESOLUTION_AMBIGUOUS in after.active
    assert not after.allows_place


def test_p9_latches_resolution_ambiguity() -> None:
    """O16, closed as an OPERATIONAL safety policy.

    Through P10's first round this reason was an ordinary flag, and the stickiness of a
    settlement halt rested on this package choosing never to emit ``flag=False``. A safety
    property that depends on one caller's restraint is not a contract, so it is now stated in
    the risk engine's own.
    """
    assert RiskReason.RESOLUTION_AMBIGUOUS in REQUIRES_RECONCILIATION


def test_clearing_the_condition_does_not_by_itself_lift_the_halt() -> None:
    """The whole point of the latch: the condition going quiet is not evidence."""
    ctrl = controller()
    report_resolution(
        ctrl,
        decision(ResolutionState.AMBIGUOUS),
        as_of_ingress_ordinal=1,
        now_ns=TimestampNs(1),
    )
    cleared = ctrl.apply(
        RiskSignal(
            kind=RiskSignalKind.RESOLUTION_SAFETY_UPDATE,
            as_of_ingress_ordinal=4,
            timestamp=TimestampNs(4),
            provenance=RiskProvenance.REAL_PUBLIC_MARKET_DATA,
            reason=RiskReason.RESOLUTION_AMBIGUOUS,
            flag=False,
        )
    )
    assert RiskReason.RESOLUTION_AMBIGUOUS not in cleared.active
    assert RiskReason.RESOLUTION_AMBIGUOUS in cleared.latched
    assert cleared.state is RiskState.RECOVERING
    assert not cleared.allows_place


def test_only_explicit_reconciliation_clears_the_latch() -> None:
    ctrl = controller()
    report_resolution(
        ctrl,
        decision(ResolutionState.AMBIGUOUS),
        as_of_ingress_ordinal=1,
        now_ns=TimestampNs(1),
    )
    ctrl.apply(
        RiskSignal(
            kind=RiskSignalKind.RESOLUTION_SAFETY_UPDATE,
            as_of_ingress_ordinal=4,
            timestamp=TimestampNs(4),
            provenance=RiskProvenance.REAL_PUBLIC_MARKET_DATA,
            reason=RiskReason.RESOLUTION_AMBIGUOUS,
            flag=False,
        )
    )
    confirmed = ctrl.apply(
        RiskSignal(
            kind=RiskSignalKind.RECONCILIATION_CONFIRMED,
            as_of_ingress_ordinal=5,
            timestamp=TimestampNs(5),
            provenance=RiskProvenance.REAL_PUBLIC_MARKET_DATA,
            reason=RiskReason.RESOLUTION_AMBIGUOUS,
        )
    )
    assert RiskReason.RESOLUTION_AMBIGUOUS not in confirmed.latched
    assert confirmed.risk_sequence > 0, "lifting a halt is itself an ordered, recorded signal"

    # P9 does not hand permission straight back; the recovery hold does that.
    state = confirmed
    for ordinal in range(6, 16):
        state = ctrl.evaluate(HEALTHY, as_of_ingress_ordinal=ordinal, now_ns=TimestampNs(ordinal))
        if state.state is RiskState.SAFE:
            break
    assert state.state is RiskState.SAFE
    assert state.allows_place


def test_reconciling_while_the_contradiction_is_still_active_does_not_clear_it() -> None:
    """Confirming a problem you are still looking at is not a resolution of it."""
    ctrl = controller()
    report_resolution(
        ctrl,
        decision(ResolutionState.AMBIGUOUS),
        as_of_ingress_ordinal=1,
        now_ns=TimestampNs(1),
    )
    confirmed = ctrl.apply(
        RiskSignal(
            kind=RiskSignalKind.RECONCILIATION_CONFIRMED,
            as_of_ingress_ordinal=2,
            timestamp=TimestampNs(2),
            provenance=RiskProvenance.REAL_PUBLIC_MARKET_DATA,
            reason=RiskReason.RESOLUTION_AMBIGUOUS,
        )
    )
    assert RiskReason.RESOLUTION_AMBIGUOUS in confirmed.latched
    assert confirmed.state is RiskState.HALTED


def test_a_generic_clearing_signal_cannot_stand_in_for_reconciliation() -> None:
    ctrl = controller()
    report_resolution(
        ctrl,
        decision(ResolutionState.AMBIGUOUS),
        as_of_ingress_ordinal=1,
        now_ns=TimestampNs(1),
    )
    cleared = ctrl.apply(
        RiskSignal(
            kind=RiskSignalKind.RESOLUTION_SAFETY_UPDATE,
            as_of_ingress_ordinal=2,
            timestamp=TimestampNs(2),
            provenance=RiskProvenance.REAL_PUBLIC_MARKET_DATA,
            reason=RiskReason.RESOLUTION_AMBIGUOUS,
            flag=False,
        )
    )
    assert RiskReason.RESOLUTION_AMBIGUOUS not in cleared.active
    assert cleared.risk_sequence > 0, "even lifting a halt is an ordered, recorded signal"

    # P9 does not hand permission straight back: clearing the reason enters RECOVERING, and the
    # recovery hold is what actually restores placement. Asserted here so a future change to
    # that hold cannot pass silently through the settlement path.
    assert cleared.state is RiskState.RECOVERING
    assert not cleared.allows_place
