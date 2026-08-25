"""The transport-free pipeline: ordering, spot-wake, health, phases, venue-vs-strategy tick.

No network. Payloads are fed in directly, which is exactly how the live capture drives it —
the transport only moves bytes.
"""

from __future__ import annotations

import pytest

from maker5m.feeds import (
    BookTracker,
    IngressMerger,
    MarketDataPipeline,
    StreamHealth,
    parse_btc_price,
    parse_market_message,
)
from maker5m.feeds.errors import FeedConformanceError
from maker5m.market import (
    CANONICAL_PHASE_CONFIG,
    BookUpdate,
    HealthComponent,
    HealthEvent,
    HealthStatus,
    MarketDefinition,
    MarketState,
    Phase,
    PhaseEvent,
    SpotTick,
    TimestampNs,
)
from maker5m.market.timebase import NANOS_PER_SECOND, seconds
from maker5m.numeric import PriceUnits, parse_price
from maker5m.strategy import BaseLot, StrategyEngine, default_config

UP = "token-up"
DOWN = "token-down"
T0 = TimestampNs(1_787_646_900 * NANOS_PER_SECOND)


class FakeClock:
    """A deterministic ingress clock. Advances only when the test says so."""

    def __init__(self, start: TimestampNs) -> None:
        self.value = start

    def __call__(self) -> TimestampNs:
        return self.value

    def advance(self, offset_ns: int) -> None:
        self.value = TimestampNs(self.value + offset_ns)

    def set_offset(self, seconds_from_t0: float) -> None:
        self.value = TimestampNs(T0 + int(seconds_from_t0 * NANOS_PER_SECOND))


def status_of(health: StreamHealth) -> HealthStatus:
    """Read through a function so mypy does not narrow status across a mutating call."""
    return health.status


def books_ready(pipeline: MarketDataPipeline) -> bool:
    return pipeline.books.ready


def awaiting_snapshot(health: StreamHealth) -> bool:
    return health.awaiting_snapshot


def definition() -> MarketDefinition:
    return MarketDefinition(
        market_id="0xcondition",
        slug="btc-updown-5m-1787646900",
        up_token_id=UP,
        down_token_id=DOWN,
        t0=T0,
        phase_config=CANONICAL_PHASE_CONFIG,
        tick=PriceUnits(10_000),
    )


def build() -> tuple[MarketDataPipeline, FakeClock]:
    clock = FakeClock(TimestampNs(T0 - seconds(10)))
    market = definition()
    merger = IngressMerger(
        engine=StrategyEngine(default_config(BaseLot.of(15))),
        state=MarketState.initial(market),
        clock=clock,
        market_id=market.market_id,
    )
    return MarketDataPipeline(merger=merger, books=BookTracker(UP, DOWN)), clock


def book_message(asset_id: str, bid: str, ask: str) -> dict[str, object]:
    return {
        "event_type": "book",
        "asset_id": asset_id,
        "bids": [{"price": bid, "size": "100"}],
        "asks": [{"price": ask, "size": "100"}],
        "tick_size": "0.01",
    }


def seed_books(pipeline: MarketDataPipeline) -> None:
    pipeline.on_clob_message(parse_market_message(book_message(UP, "0.62", "0.64")))
    pipeline.on_clob_message(parse_market_message(book_message(DOWN, "0.35", "0.37")))


# -- ingress ordering ------------------------------------------------------------


def test_ordinals_form_one_strictly_increasing_total_order() -> None:
    pipeline, clock = build()
    clock.set_offset(60)
    seed_books(pipeline)
    for i in range(5):
        clock.advance(1_000)
        pipeline.on_spot(parse_btc_price(f"8000{i}.00"))
    ordinals = [step.event.meta.ingress_ordinal for step in pipeline.merger.steps]
    assert ordinals == list(range(len(ordinals)))
    assert len(set(ordinals)) == len(ordinals)


def test_ingress_timestamps_are_non_decreasing() -> None:
    pipeline, clock = build()
    clock.set_offset(60)
    seed_books(pipeline)
    for _ in range(10):
        clock.advance(500)
        pipeline.on_spot(parse_btc_price("80000.00"))
    stamps = [step.event.meta.timestamp for step in pipeline.merger.steps]
    assert stamps == sorted(stamps)


def test_identical_source_timestamps_do_not_disturb_ingress_order() -> None:
    """Venue stamps are data. Order comes from the ingress ordinal alone."""
    pipeline, clock = build()
    clock.set_offset(60)
    seed_books(pipeline)
    before = pipeline.merger.ordinal
    for price in ("0.61", "0.63", "0.60"):
        # Same venue timestamp on every message, clock frozen.
        pipeline.on_clob_message(
            parse_market_message(
                {
                    "event_type": "price_change",
                    "timestamp": "1787646900000",
                    "price_changes": [
                        {"asset_id": UP, "price": price, "size": "10", "side": "BUY"}
                    ],
                }
            )
        )
    ordinals = [step.event.meta.ingress_ordinal for step in pipeline.merger.steps[before:]]
    assert ordinals == sorted(ordinals)
    assert len(set(ordinals)) == len(ordinals)


def test_every_event_carries_the_market_id() -> None:
    pipeline, clock = build()
    clock.set_offset(60)
    seed_books(pipeline)
    assert all(s.event.meta.market_id == "0xcondition" for s in pipeline.merger.steps)


# -- invariant I11: spot alone wakes the decision path ----------------------------


def test_a_spot_tick_alone_reduces_and_decides() -> None:
    """No Polymarket message is involved. Invariant I11, demonstrated without a network."""
    pipeline, clock = build()
    clock.set_offset(60)
    decision = pipeline.on_spot(parse_btc_price("80123.45"))
    assert decision is not None
    # The first spot message also establishes spot-feed health, so the SpotTick is last.
    assert isinstance(pipeline.merger.steps[-1].event, SpotTick)
    assert pipeline.merger.state.spot is not None
    assert pipeline.merger.state.book is None, "no Polymarket message was involved"
    assert not any(isinstance(step.event, BookUpdate) for step in pipeline.merger.steps)


def test_spot_decisions_occur_between_clob_events() -> None:
    pipeline, clock = build()
    clock.set_offset(60)
    seed_books(pipeline)
    clock.advance(1_000)
    pipeline.on_spot(parse_btc_price("80001.00"))
    clock.advance(1_000)
    pipeline.on_clob_message(
        parse_market_message(
            {
                "event_type": "price_change",
                "price_changes": [{"asset_id": UP, "price": "0.63", "size": "12", "side": "BUY"}],
            }
        )
    )
    kinds = [type(step.event).__name__ for step in pipeline.merger.steps]
    spot_index = kinds.index("SpotTick")
    assert "BookUpdate" in kinds[:spot_index]
    assert "BookUpdate" in kinds[spot_index + 1 :]


def test_the_spot_price_reaches_state_exactly() -> None:
    pipeline, clock = build()
    clock.set_offset(60)
    pipeline.on_spot(parse_btc_price("80046.12345678"))
    spot = pipeline.merger.state.spot
    assert spot is not None
    assert str(spot.price) == "80046.12345678"


# -- book publication --------------------------------------------------------------


def test_no_book_update_is_published_before_both_snapshots_arrive() -> None:
    pipeline, clock = build()
    clock.set_offset(60)
    pipeline.on_clob_message(parse_market_message(book_message(UP, "0.62", "0.64")))
    assert pipeline.merger.step_count == 0, "a one-sided book is not yet trustworthy"
    pipeline.on_clob_message(parse_market_message(book_message(DOWN, "0.35", "0.37")))
    assert pipeline.merger.step_count >= 1


def test_published_books_carry_both_sides_as_observed() -> None:
    pipeline, clock = build()
    clock.set_offset(60)
    seed_books(pipeline)
    update = pipeline.merger.state.book
    assert isinstance(update, BookUpdate)
    assert update.up_bid is not None and update.up_ask is not None
    assert update.down_bid is not None and update.down_ask is not None
    assert update.down_bid.price == parse_price("0.35")
    # Deliberately not a mirror: 1 - 0.64 would be 0.36, but the venue sent 0.35. The
    # adapter records what arrived rather than applying Canonical section 5.2's conditional
    # mirror identity.
    assert update.down_bid.price != PriceUnits(1_000_000 - update.up_ask.price)


def test_published_books_never_invent_a_sequence() -> None:
    pipeline, clock = build()
    clock.set_offset(60)
    seed_books(pipeline)
    assert all(
        step.event.sequence is None
        for step in pipeline.merger.steps
        if isinstance(step.event, BookUpdate)
    )


# -- venue tick vs strategy tick ------------------------------------------------------


def test_a_venue_tick_change_is_recorded_and_never_touches_the_strategy_tick() -> None:
    pipeline, clock = build()
    clock.set_offset(60)
    seed_books(pipeline)
    strategy_tick_before = pipeline.merger.state.definition.tick

    pipeline.on_clob_message(
        parse_market_message(
            {
                "event_type": "tick_size_change",
                "asset_id": UP,
                "old_tick_size": "0.01",
                "new_tick_size": "0.001",
            }
        )
    )
    assert pipeline.venue_rules.tick_change_count == 1
    assert pipeline.venue_rules.rules.min_tick_size == parse_price("0.001")
    assert pipeline.merger.state.definition.tick == strategy_tick_before
    assert pipeline.merger.state.definition.tick == parse_price("0.01")


def test_a_tick_size_change_is_never_silently_discarded() -> None:
    pipeline, clock = build()
    clock.set_offset(60)
    pipeline.on_clob_message(
        parse_market_message(
            {"event_type": "tick_size_change", "asset_id": UP, "new_tick_size": "0.001"}
        )
    )
    assert pipeline.counters.clob_tick_size_changes == 1
    assert pipeline.venue_rules.tick_changes


def test_the_book_message_tick_size_is_recorded_as_venue_metadata() -> None:
    pipeline, clock = build()
    clock.set_offset(60)
    seed_books(pipeline)
    assert pipeline.venue_rules.rules.min_tick_size == parse_price("0.01")
    assert pipeline.venue_rules.rules.source == "ws/book"


def test_unhandled_event_types_are_counted_not_dropped_silently() -> None:
    pipeline, clock = build()
    clock.set_offset(60)
    pipeline.on_clob_message(parse_market_message({"event_type": "teleport"}))
    assert pipeline.counters.clob_unhandled == 1
    assert pipeline.counters.unhandled_kinds == {"teleport": 1}


# -- health, reconnect, staleness ------------------------------------------------------


def test_a_disconnect_marks_the_stream_unhealthy_and_drops_the_book() -> None:
    pipeline, clock = build()
    clock.set_offset(60)
    seed_books(pipeline)
    assert books_ready(pipeline)

    clock.advance(1_000)
    pipeline.on_disconnect(HealthComponent.CLOB_BOOK)
    assert not books_ready(pipeline), "continuity is uncertain; the book must be dropped"
    assert awaiting_snapshot(pipeline.clob_health)
    last = pipeline.merger.steps[-1].event
    assert isinstance(last, HealthEvent)
    assert last.status is HealthStatus.DISCONNECTED


def test_only_a_fresh_snapshot_restores_healthy() -> None:
    pipeline, clock = build()
    clock.set_offset(60)
    seed_books(pipeline)
    clock.advance(1_000)
    pipeline.on_disconnect(HealthComponent.CLOB_BOOK)

    # A price_change alone must not restore trust.
    clock.advance(1_000)
    pipeline.on_clob_message(
        parse_market_message(
            {
                "event_type": "price_change",
                "price_changes": [{"asset_id": UP, "price": "0.63", "size": "5", "side": "BUY"}],
            }
        )
    )
    assert awaiting_snapshot(pipeline.clob_health)
    assert status_of(pipeline.clob_health) is not HealthStatus.HEALTHY

    clock.advance(1_000)
    seed_books(pipeline)
    assert status_of(pipeline.clob_health) is HealthStatus.HEALTHY
    assert not awaiting_snapshot(pipeline.clob_health)


def test_reconnect_does_not_reorder_events() -> None:
    """Recovered data gets the next ordinal; nothing is spliced backwards."""
    pipeline, clock = build()
    clock.set_offset(60)
    seed_books(pipeline)
    clock.advance(1_000)
    pipeline.on_disconnect(HealthComponent.CLOB_BOOK)
    clock.advance(1_000)
    seed_books(pipeline)
    ordinals = [step.event.meta.ingress_ordinal for step in pipeline.merger.steps]
    assert ordinals == sorted(ordinals) == list(range(len(ordinals)))


def test_malformed_data_marks_continuity_uncertain() -> None:
    pipeline, clock = build()
    clock.set_offset(60)
    seed_books(pipeline)
    clock.advance(1_000)
    pipeline.on_uncertain(HealthComponent.CLOB_BOOK, "malformed message")
    assert status_of(pipeline.clob_health) is HealthStatus.SEQUENCE_GAP
    assert awaiting_snapshot(pipeline.clob_health)
    assert not books_ready(pipeline)


def test_staleness_emits_a_health_event() -> None:
    pipeline, clock = build()
    clock.set_offset(60)
    seed_books(pipeline)
    assert status_of(pipeline.clob_health) is HealthStatus.HEALTHY
    clock.advance(seconds(60))
    results = pipeline.check_staleness(clock())
    assert results
    assert status_of(pipeline.clob_health) is HealthStatus.STALE
    last = pipeline.merger.steps[-1].event
    assert isinstance(last, HealthEvent)
    assert last.status is HealthStatus.STALE


def test_a_fresh_stream_is_not_stale() -> None:
    pipeline, clock = build()
    clock.set_offset(60)
    seed_books(pipeline)
    clock.advance(1_000)
    assert pipeline.check_staleness(clock()) == []


# -- phase scheduling ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset_s", "phase"),
    [(3, Phase.QUOTE), (240, Phase.ENDGAME), (280, Phase.SETTLING), (300, Phase.DONE)],
)
def test_phase_events_fire_at_exact_boundaries(offset_s: int, phase: Phase) -> None:
    pipeline, clock = build()
    clock.set_offset(offset_s)
    pipeline.emit_phase(phase)
    step = pipeline.merger.steps[-1]
    assert isinstance(step.event, PhaseEvent)
    assert step.event.phase is phase
    assert step.decision.telemetry.phase is phase


def test_a_phase_event_at_the_wrong_time_fails_closed() -> None:
    """The scheduler cannot push the market into a phase its ingress time denies."""
    pipeline, clock = build()
    clock.set_offset(100)
    with pytest.raises(FeedConformanceError, match="ENDGAME"):
        pipeline.emit_phase(Phase.ENDGAME)


def test_a_quiet_market_still_reaches_endgame() -> None:
    """No book or spot traffic at all: the scheduled boundary alone moves the phase."""
    pipeline, clock = build()
    clock.set_offset(240)
    pipeline.emit_phase(Phase.ENDGAME)
    assert pipeline.merger.state.phase is Phase.ENDGAME
    assert Phase.ENDGAME in pipeline.emitted_phases


def test_phase_events_share_the_single_ordinal_sequence() -> None:
    pipeline, clock = build()
    clock.set_offset(3)
    pipeline.emit_phase(Phase.QUOTE)
    seed_books(pipeline)
    clock.set_offset(240)
    pipeline.emit_phase(Phase.ENDGAME)
    ordinals = [step.event.meta.ingress_ordinal for step in pipeline.merger.steps]
    assert ordinals == list(range(len(ordinals)))
