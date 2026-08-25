"""A stream that goes quiet must be able to come back.

**SUPPORTING UNIT TEST ONLY** — but the defect it pins was found empirically, by pausing the
real Binance adapter during a live market. The halt fired correctly and then never lifted:
``StreamHealth`` had no path out of ``STALE``, so the bot would have stayed halted for the rest
of the market after one quiet BTC feed.

The distinction these tests protect is the whole of the recovery rule. A stream that was merely
*quiet* is healthy again as soon as it speaks. A stream whose *continuity* broke is not, however
many messages arrive, until a fresh authoritative snapshot lands.
"""

from __future__ import annotations

from maker5m.feeds.health import StreamHealth
from maker5m.market.events import HealthComponent, HealthStatus
from maker5m.market.timebase import TimestampNs, seconds

START = TimestampNs(1_787_647_500_000_000_000)


def at(offset_s: int) -> TimestampNs:
    return TimestampNs(START + seconds(offset_s))


def live_stream(component: HealthComponent = HealthComponent.SPOT_FEED) -> StreamHealth:
    health = StreamHealth(component)
    health.mark_snapshot(at(0))
    assert health.healthy
    return health


def test_a_quiet_stream_that_speaks_again_is_healthy() -> None:
    health = live_stream()
    health.mark_stale()
    assert health.status is HealthStatus.STALE

    assert health.mark_message(at(10)) is HealthStatus.HEALTHY
    assert health.healthy


def test_a_disconnected_stream_is_not_healed_by_a_message() -> None:
    """One message after a continuity break says nothing about the messages that were missed."""
    health = live_stream()
    health.mark_disconnected()
    assert health.mark_message(at(10)) is HealthStatus.DISCONNECTED
    assert health.awaiting_snapshot
    assert not health.healthy

    health.mark_snapshot(at(11))
    assert health.healthy


def test_a_sequence_gap_is_not_healed_by_a_message() -> None:
    health = live_stream()
    health.mark_uncertain()
    assert health.mark_message(at(10)) is HealthStatus.SEQUENCE_GAP
    assert not health.healthy


def test_a_stale_stream_awaiting_a_snapshot_still_needs_one() -> None:
    """Both faults at once: the stricter requirement wins."""
    health = live_stream()
    health.mark_disconnected()
    health.mark_stale()
    assert health.mark_message(at(10)) is HealthStatus.STALE
    assert health.awaiting_snapshot
    assert not health.healthy


def test_an_already_healthy_stream_is_unchanged() -> None:
    health = live_stream()
    assert health.mark_message(at(1)) is HealthStatus.HEALTHY
    assert health.last_message_at == at(1)


def test_the_risk_engine_lifts_a_spot_halt_once_data_resumes() -> None:
    """End to end: the condition that halted us is the condition that clears."""
    from maker5m.risk import RiskConfig, RiskEngine, RiskInputs, RiskReason, RiskState

    config = RiskConfig()
    engine = RiskEngine(config=config)

    def inputs(spot_status: HealthStatus, spot_at: TimestampNs, now: TimestampNs) -> RiskInputs:
        return RiskInputs(
            now_ns=now,
            clob_status=HealthStatus.HEALTHY,
            clob_awaiting_snapshot=False,
            clob_last_message_at=now,
            spot_status=spot_status,
            spot_last_message_at=spot_at,
        )

    for offset in range(3):
        engine.evaluate(inputs(HealthStatus.HEALTHY, at(offset), at(offset)))
    assert engine.state is RiskState.SAFE

    halted = engine.evaluate(inputs(HealthStatus.STALE, at(2), at(30)))
    assert halted.state is RiskState.HALTED
    assert RiskReason.SPOT_STALE in halted.active

    for offset in range(31, 31 + config.recovery_confirmations):
        decision = engine.evaluate(inputs(HealthStatus.HEALTHY, at(offset), at(offset)))
    assert decision.state is RiskState.SAFE, "resumed data must be able to lift the halt"
