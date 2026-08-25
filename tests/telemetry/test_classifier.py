"""Execution-quality classification, and the reasons it must never collapse."""

from __future__ import annotations

import pytest

from maker5m.execution import PreparationOutcome, RateDecision, ReconcileAction, SideReason
from maker5m.numeric import parse_price, parse_share
from maker5m.numeric.units import PriceUnits
from maker5m.telemetry import (
    ExecutionQuality,
    QualityReason,
    QueueConfidence,
    QueueEstimate,
    QuoteClassification,
    classify,
)

PRICE = parse_price("0.63")


def estimate(
    ahead: str = "0",
    confidence: QueueConfidence = QueueConfidence.ESTIMATED,
    price: PriceUnits = PRICE,
) -> QueueEstimate:
    return QueueEstimate(
        client_order_id="shadow-1",
        price=price,
        ahead=parse_share(ahead),
        confidence=confidence,
        displayed_at_submit=parse_share(ahead),
        level_existed_before=parse_share(ahead) > 0,
    )


def at(**overrides: object) -> QuoteClassification:
    kwargs: dict[str, object] = {
        "action": ReconcileAction.KEEP,
        "side_reason": SideReason.UNCHANGED,
        "desired_price": PRICE,
        "estimate": estimate(),
        "preparation": PreparationOutcome.SAFE,
    }
    kwargs.update(overrides)
    return classify(**kwargs)  # type: ignore[arg-type]


# -- the five classifications --------------------------------------------


def test_at_front() -> None:
    result = at(estimate=estimate("0"))
    assert result.quality is ExecutionQuality.AT_FRONT
    assert result.reason is QualityReason.QUOTING
    assert result.queue_ahead == 0


def test_price_ok_but_deep() -> None:
    result = at(estimate=estimate("15"))
    assert result.quality is ExecutionQuality.PRICE_OK_BUT_DEEP
    assert result.queue_ahead == parse_share("15")


def test_any_positive_queue_ahead_is_deep_without_an_invented_threshold() -> None:
    """Choosing a threshold is what O08 exists to answer."""
    for ahead in ("0.000001", "1", "15", "500"):
        assert at(estimate=estimate(ahead)).quality is ExecutionQuality.PRICE_OK_BUT_DEEP
    # The actual quantity stays visible alongside the label.
    assert at(estimate=estimate("500")).queue_ahead == parse_share("500")


def test_off_price() -> None:
    result = at(estimate=estimate(price=parse_price("0.62")))
    assert result.quality is ExecutionQuality.OFF_PRICE
    assert result.desired_price == PRICE
    assert result.resting_price == parse_price("0.62")


def test_not_quoting_when_no_order_is_intended() -> None:
    result = at(
        action=ReconcileAction.NOTHING,
        side_reason=SideReason.NO_DESIRED_NO_LIVE,
        desired_price=None,
        preparation=None,
        estimate=None,
    )
    assert result.quality is ExecutionQuality.NOT_QUOTING
    assert result.reason is QualityReason.PHASE_NOT_QUOTING


def test_not_quoting_when_intended_but_no_live_order() -> None:
    result = at(action=ReconcileAction.PLACE, estimate=None)
    assert result.quality is ExecutionQuality.NOT_QUOTING
    assert result.reason is QualityReason.NO_LIVE_ORDER


def test_stale_when_continuity_is_unhealthy() -> None:
    result = at(continuity_healthy=False)
    assert result.quality is ExecutionQuality.STALE
    assert result.reason is QualityReason.CONTINUITY_LOST


def test_stale_when_the_estimate_cannot_be_trusted() -> None:
    for confidence in (QueueConfidence.STALE, QueueConfidence.UNKNOWN):
        result = at(estimate=estimate("5", confidence))
        assert result.quality is ExecutionQuality.STALE
        assert result.confidence is confidence


# -- typed reasons are preserved -----------------------------------------


@pytest.mark.parametrize(
    ("preparation", "reason"),
    [
        (PreparationOutcome.WOULD_CROSS, QualityReason.POST_ONLY_BLOCK),
        (PreparationOutcome.NO_BOOK, QualityReason.CENTRE_UNAVAILABLE),
        (PreparationOutcome.OFF_VENUE_TICK, QualityReason.OFF_VENUE_TICK),
        (PreparationOutcome.BELOW_MIN_SIZE, QualityReason.BELOW_MIN_SIZE),
        (PreparationOutcome.ZERO_AFTER_QUANTIZATION, QualityReason.BELOW_MIN_SIZE),
        (PreparationOutcome.UNKNOWN_VENUE_RULES, QualityReason.CONTINUITY_LOST),
    ],
)
def test_a_blocked_side_keeps_its_specific_reason(
    preparation: PreparationOutcome, reason: QualityReason
) -> None:
    """Collapsing these into one unexplained NOT_QUOTING would destroy what P11 needs."""
    result = at(action=ReconcileAction.BLOCKED, preparation=preparation, estimate=None)
    assert result.quality is ExecutionQuality.NOT_QUOTING
    assert result.reason is reason


def test_a_rate_deferred_side_says_so() -> None:
    result = at(action=ReconcileAction.PLACE, rate_decision=RateDecision.DEFERRED, estimate=None)
    assert result.quality is ExecutionQuality.NOT_QUOTING
    assert result.reason is QualityReason.RATE_DEFERRED


def test_an_in_flight_side_says_so() -> None:
    result = at(action=ReconcileAction.WAIT, side_reason=SideReason.IN_FLIGHT, estimate=None)
    assert result.quality is ExecutionQuality.NOT_QUOTING
    assert result.reason is QualityReason.IN_FLIGHT


def test_an_unknown_state_is_distinguished_from_ordinary_in_flight() -> None:
    result = at(action=ReconcileAction.WAIT, side_reason=SideReason.UNKNOWN_STATE, estimate=None)
    assert result.reason is QualityReason.CONTINUITY_LOST


@pytest.mark.parametrize(
    "strategy_reason",
    [QualityReason.ENDGAME_GATE, QualityReason.HARD_BAND, QualityReason.PHASE_NOT_QUOTING],
)
def test_a_strategy_gate_reason_survives_to_the_classifier(strategy_reason: QualityReason) -> None:
    """A NOT_QUOTING result must still say which gate suppressed the side."""
    result = at(
        action=ReconcileAction.NOTHING,
        side_reason=SideReason.NO_DESIRED_NO_LIVE,
        desired_price=None,
        preparation=None,
        estimate=None,
        strategy_reason=strategy_reason,
    )
    assert result.quality is ExecutionQuality.NOT_QUOTING
    assert result.reason is strategy_reason


def test_every_quality_and_reason_is_typed() -> None:
    result = at()
    assert isinstance(result.quality, ExecutionQuality)
    assert isinstance(result.reason, QualityReason)


def test_the_classifier_is_pure() -> None:
    assert at() == at()
