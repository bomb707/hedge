"""The market-data pipeline: normalized payload in, decision out.

This is the transport-free heart of P6, so it can be driven by tests with no network at all.
It owns the book trackers, the venue rules, the health state, and the ingress merger, and it
turns each normalized payload into exactly one Plane 2 event.

Every path here ends in ``merger.submit(...)``, which assigns the single global ingress
ordinal, reduces, and decides. That is what guarantees one legal event order for P5 to replay.

Nothing on this path serializes, writes a file, logs verbosely, or calls a REST endpoint.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from maker5m.feeds.diagnostics import FeedCounters
from maker5m.feeds.errors import FeedConformanceError
from maker5m.feeds.exactness import PrecisionObserver
from maker5m.feeds.health import (
    DEFAULT_CLOB_STALE_AFTER,
    DEFAULT_SPOT_STALE_AFTER,
    StalenessMonitor,
    StreamHealth,
)
from maker5m.feeds.merger import IngressMerger
from maker5m.feeds.polymarket import BookTracker, MarketEventKind, ParsedMessage
from maker5m.feeds.venue import VenueMarketRules, VenueRulesTracker
from maker5m.market.btc_price import BtcPrice
from maker5m.market.events import (
    BookUpdate,
    HealthComponent,
    HealthEvent,
    HealthStatus,
    PhaseEvent,
    SpotTick,
)
from maker5m.market.phase import Phase, phase_at
from maker5m.market.timebase import TimestampNs
from maker5m.strategy.decision import DecisionResult

__all__ = ["MarketDataPipeline"]


@dataclass(slots=True)
class MarketDataPipeline:
    """Normalized payloads -> Plane 2 events -> decisions."""

    merger: IngressMerger
    books: BookTracker
    venue_rules: VenueRulesTracker = field(default_factory=VenueRulesTracker)
    counters: FeedCounters = field(default_factory=FeedCounters)
    price_precision: PrecisionObserver = field(
        default_factory=lambda: PrecisionObserver("polymarket_price")
    )
    size_precision: PrecisionObserver = field(
        default_factory=lambda: PrecisionObserver("polymarket_size")
    )
    spot_precision: PrecisionObserver = field(
        default_factory=lambda: PrecisionObserver("binance_price")
    )
    stage_selector: Callable[[int, str], bool] | None = None
    """Optional ``(ingress_ordinal, event_kind) -> bool`` deciding per-event stage timing.

    The sampling decision has to be made *before* reduce and decide, or those stages cannot be
    timed at all. Off by default, so P5 replay and ordinary P6 capture pay one ``is None``
    check and nothing else.
    """

    clob_health: StreamHealth = field(
        default_factory=lambda: StreamHealth(HealthComponent.CLOB_BOOK)
    )
    spot_health: StreamHealth = field(
        default_factory=lambda: StreamHealth(HealthComponent.SPOT_FEED)
    )
    clob_staleness: StalenessMonitor = field(
        default_factory=lambda: StalenessMonitor(DEFAULT_CLOB_STALE_AFTER)
    )
    spot_staleness: StalenessMonitor = field(
        default_factory=lambda: StalenessMonitor(DEFAULT_SPOT_STALE_AFTER)
    )
    emitted_phases: set[Phase] = field(default_factory=set)

    # -- market data -----------------------------------------------------------------------

    def on_clob_message(self, parsed: ParsedMessage) -> DecisionResult | None:
        """Apply one normalized Polymarket message and emit a BookUpdate if the book moved."""
        self.counters.clob_messages += 1

        if parsed.kind is None:
            self.counters.note_unhandled(parsed.unhandled_kind or "<unknown>")
            return None

        if parsed.kind is MarketEventKind.BOOK:
            self.counters.clob_books += 1
        elif parsed.kind is MarketEventKind.PRICE_CHANGE:
            self.counters.clob_price_changes += 1
        elif parsed.kind is MarketEventKind.BEST_BID_ASK:
            self.counters.clob_best_bid_ask += 1
        elif parsed.kind is MarketEventKind.LAST_TRADE_PRICE:
            self.counters.clob_last_trade_price += 1
        elif parsed.kind is MarketEventKind.TICK_SIZE_CHANGE:
            self.counters.clob_tick_size_changes += 1
            if parsed.tick_size is not None:
                # Recorded as venue metadata. It never touches MarketDefinition.tick.
                self.venue_rules.observe_tick_change(
                    self.merger.clock(),
                    parsed.tick_size.asset_id,
                    parsed.tick_size.old_tick_size,
                    parsed.tick_size.new_tick_size,
                )
            return None

        if parsed.book is not None and parsed.book.tick_size is not None:
            self.venue_rules.observe_rules(
                VenueMarketRules(
                    min_tick_size=parsed.book.tick_size,
                    min_order_size=self.venue_rules.rules.min_order_size,
                    observed_at=self.merger.clock(),
                    source="ws/book",
                )
            )

        touched = self.books.apply(parsed)
        if not touched:
            return None

        now = self.merger.clock()
        was_stale = self.clob_health.status is HealthStatus.STALE
        recovered = self.clob_health.mark_message(now)
        if was_stale and not self.clob_health.awaiting_snapshot:
            self.emit_health(HealthComponent.CLOB_BOOK, recovered, "resumed")
        if parsed.kind is MarketEventKind.BOOK and self.books.ready:
            self.emit_health(HealthComponent.CLOB_BOOK, self.clob_health.mark_snapshot(now))
        if self.clob_health.awaiting_snapshot:
            # Until both sides have a fresh snapshot the book is not trustworthy.
            return None
        return self.emit_book()

    def _measure(self, ingress_ordinal: int, event_kind: str) -> bool:
        selector = self.stage_selector
        return selector is not None and selector(ingress_ordinal, event_kind)

    def emit_book(self) -> DecisionResult:
        """Publish the currently observed top of book for BOTH tokens, as observed.

        ``sequence`` stays ``None``: the venue publishes no documented monotonic sequence, and
        mapping its ``timestamp`` or ``hash`` into that field would fabricate a guarantee.
        """
        meta = self.merger.next_meta("book")
        return self.merger.submit(
            BookUpdate(
                meta=meta,
                up_bid=self.books.up.best_bid(),
                up_ask=self.books.up.best_ask(),
                down_bid=self.books.down.best_bid(),
                down_ask=self.books.down.best_ask(),
                sequence=None,
            ),
            measure_stages=self._measure(meta.ingress_ordinal, "BookUpdate"),
        )

    def on_spot(self, price: BtcPrice) -> DecisionResult:
        """A spot tick alone drives a full decision. Invariant I11, no CLOB message needed."""
        self.counters.spot_messages += 1
        now = self.merger.clock()
        was_stale = self.spot_health.status is HealthStatus.STALE
        recovered = self.spot_health.mark_message(now)
        if self.spot_health.awaiting_snapshot:
            self.emit_health(HealthComponent.SPOT_FEED, self.spot_health.mark_snapshot(now))
        elif was_stale:
            # Publish the recovery, so the halt that the STALE event caused can be lifted by
            # the same ordered health stream that caused it.
            self.emit_health(HealthComponent.SPOT_FEED, recovered, "resumed")
        meta = self.merger.next_meta("spot")
        return self.merger.submit(
            SpotTick(meta=meta, price=price, source_sequence=None),
            measure_stages=self._measure(meta.ingress_ordinal, "SpotTick"),
        )

    # -- lifecycle and health ----------------------------------------------------------------

    def emit_health(
        self, component: HealthComponent, status: HealthStatus, detail: str | None = None
    ) -> DecisionResult:
        meta = self.merger.next_meta("health")
        return self.merger.submit(
            HealthEvent(meta=meta, component=component, status=status, detail=detail),
            measure_stages=self._measure(meta.ingress_ordinal, "HealthEvent"),
        )

    def emit_phase(self, phase: Phase) -> DecisionResult:
        """Emit a boundary event through the same merger, with the next ordinal.

        A quiet market must still cross phases exactly, so this is scheduled rather than
        waiting for the next book or spot message to happen along.
        """
        meta = self.merger.next_meta("phase")
        expected = phase_at(
            self.merger.state.definition.t0,
            meta.timestamp,
            self.merger.state.definition.phase_config,
        )
        if expected is not phase:
            raise FeedConformanceError(
                f"phase scheduler fired {phase.name} at {meta.timestamp}, but that ingress "
                f"time is {expected.name}"
            )
        self.emitted_phases.add(phase)
        return self.merger.submit(
            PhaseEvent(meta=meta, phase=phase),
            measure_stages=self._measure(meta.ingress_ordinal, "PhaseEvent"),
        )

    def on_disconnect(self, component: HealthComponent) -> DecisionResult:
        """Any disconnect invalidates continuity and forces a resnapshot."""
        self.counters.reconnects += 1
        if component is HealthComponent.CLOB_BOOK:
            self.books.clear()
            status = self.clob_health.mark_disconnected()
        else:
            status = self.spot_health.mark_disconnected()
        return self.emit_health(component, status, "transport disconnected")

    def on_uncertain(self, component: HealthComponent, detail: str) -> DecisionResult:
        """Continuity cannot be established: malformed data, unknown token, resubscription."""
        self.counters.malformed += 1
        if component is HealthComponent.CLOB_BOOK:
            self.books.clear()
            status = self.clob_health.mark_uncertain()
        else:
            status = self.spot_health.mark_uncertain()
        return self.emit_health(component, status, detail)

    def check_staleness(self, now: TimestampNs) -> list[DecisionResult]:
        """Emit STALE for any stream that has gone quiet past its OPERATIONAL threshold."""
        out: list[DecisionResult] = []
        for health, monitor in (
            (self.clob_health, self.clob_staleness),
            (self.spot_health, self.spot_staleness),
        ):
            if health.status is HealthStatus.HEALTHY and monitor.is_stale(health, now):
                out.append(self.emit_health(health.component, health.mark_stale(), "stale"))
        return out
