"""Read-only live capture: real public market data through the real deterministic core.

Composition for one 5-minute market. Two public WebSockets, one ingress merger, one
:class:`~maker5m.feeds.pipeline.MarketDataPipeline`, and a scheduled phase clock.

Strictly read-only. No order endpoint, no API key, no wallet key, no signing, no write path
of any kind exists in this module or anywhere it imports. ``LIVE_TRADING_ENABLED`` stays
``False``.

Concurrency shape: every producer puts a normalized payload on **one** ``asyncio.Queue``, and
a single consumer drains it. The consumer is the only thing that assigns ingress ordinals,
mutates state, or calls ``decide()``, so single ownership holds without a lock and there is
exactly one legal event order (``ARCHITECTURE_SSOT`` §3.4).

Reconnect never reorders: a recovered stream's next message simply gets the next ordinal.
Recovered history is never spliced backwards into an already-consumed stream — instead the
stream is marked unhealthy, the book is dropped, and a fresh snapshot re-establishes trust.
"""

import asyncio
import json
import random
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter_ns
from typing import Any, Final

import websockets

from maker5m.feeds.binance import (
    BINANCE_WS_BASE,
    DEFAULT_SYMBOL,
    agg_trade_stream,
    parse_agg_trade,
)
from maker5m.feeds.diagnostics import ClockHealth, FeedCounters
from maker5m.feeds.discovery import DiscoveredMarket
from maker5m.feeds.errors import FeedError
from maker5m.feeds.ingress_clock import IngressClock
from maker5m.feeds.merger import IngressMerger
from maker5m.feeds.pipeline import MarketDataPipeline
from maker5m.feeds.polymarket import (
    POLYMARKET_MARKET_WS,
    POLYMARKET_PING,
    POLYMARKET_PONG,
    BookTracker,
    parse_market_message,
    subscribe_payload,
)
from maker5m.feeds.venue import VenueMarketRules
from maker5m.market.events import HealthComponent
from maker5m.market.phase import Phase
from maker5m.market.state import MarketState
from maker5m.market.timebase import NANOS_PER_SECOND, TimestampNs
from maker5m.replay.journal import Journal, JournalHeader
from maker5m.replay.schema import JournalProvenance
from maker5m.strategy.config import StrategyConfig
from maker5m.strategy.decision import DecisionResult
from maker5m.strategy.engine import StrategyEngine

__all__ = ["CaptureResult", "capture_market"]

HEARTBEAT_SECONDS: Final = 10.0
"""Polymarket's documented application heartbeat interval."""

RECONNECT_BASE_SECONDS: Final = 0.5
RECONNECT_MAX_SECONDS: Final = 15.0
"""Bounded exponential backoff with jitter. OPERATIONAL infrastructure, not strategy."""


@dataclass(slots=True)
class _Payload:
    """One item queued for the single ingress consumer."""

    kind: str
    data: Any = None


@dataclass(slots=True)
class CaptureResult:
    """Everything one capture produced."""

    journal: Journal
    final_state: MarketState
    counters: FeedCounters
    clock_health: ClockHealth
    precision: dict[str, object]
    venue_tick_changes: int
    prearm_ready_ns: TimestampNs | None
    next_market_t0_ns: TimestampNs | None
    next_market_slug: str | None

    @property
    def prearm_slack_ns(self) -> int | None:
        if self.prearm_ready_ns is None or self.next_market_t0_ns is None:
            return None
        return self.next_market_t0_ns - self.prearm_ready_ns


async def _backoff(attempt: int) -> None:
    delay = min(RECONNECT_MAX_SECONDS, RECONNECT_BASE_SECONDS * (2**attempt))
    await asyncio.sleep(delay * (0.5 + random.random() / 2))


class ForcedDisconnectError(Exception):
    """A deliberately induced socket close, used for P9 fault injection on a real market.

    Raised inside the producer so it travels the *same* path a genuine network failure takes:
    the socket really closes, the reconnect really happens, the subscription is really re-sent,
    and a fresh authoritative snapshot really has to arrive. Nothing about the market data is
    simulated - only the decision to drop the connection.
    """


async def _polymarket_producer(
    queue: asyncio.Queue[_Payload],
    token_ids: tuple[str, ...],
    stop: asyncio.Event,
    force_disconnect: asyncio.Event | None = None,
) -> None:
    """Maintain the CLOB subscription, with the documented PING heartbeat and reconnect."""
    attempt = 0
    while not stop.is_set():
        try:
            async with websockets.connect(POLYMARKET_MARKET_WS, open_timeout=20) as ws:
                await ws.send(subscribe_payload(token_ids))
                attempt = 0

                async def heartbeat(socket: Any = ws) -> None:
                    while True:
                        await asyncio.sleep(HEARTBEAT_SECONDS)
                        await socket.send(POLYMARKET_PING)

                beat = asyncio.create_task(heartbeat())
                try:
                    while not stop.is_set():
                        if force_disconnect is not None and force_disconnect.is_set():
                            force_disconnect.clear()
                            raise ForcedDisconnectError
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        text = raw.decode() if isinstance(raw, bytes) else raw
                        if text.strip() == POLYMARKET_PONG:
                            await queue.put(_Payload("pong"))
                            continue
                        await queue.put(_Payload("clob", text))
                finally:
                    beat.cancel()
        except asyncio.CancelledError:
            raise
        except Exception:
            if stop.is_set():
                return
            await queue.put(_Payload("clob_disconnect"))
            attempt += 1
            await _backoff(attempt)


async def _binance_producer(
    queue: asyncio.Queue[_Payload], symbol: str, stop: asyncio.Event
) -> None:
    """Maintain the external BTC spot subscription."""
    url = f"{BINANCE_WS_BASE}/{agg_trade_stream(symbol)}"
    attempt = 0
    while not stop.is_set():
        try:
            async with websockets.connect(url, open_timeout=20) as ws:
                attempt = 0
                while not stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    text = raw.decode() if isinstance(raw, bytes) else raw
                    await queue.put(_Payload("spot", text))
        except asyncio.CancelledError:
            raise
        except Exception:
            if stop.is_set():
                return
            await queue.put(_Payload("spot_disconnect"))
            attempt += 1
            await _backoff(attempt)


async def _phase_producer(
    queue: asyncio.Queue[_Payload], t0_ns: TimestampNs, clock: IngressClock, stop: asyncio.Event
) -> None:
    """Fire a boundary event at each exact phase transition.

    Scheduled rather than piggy-backed on market data: a quiet market must still enter ENDGAME
    at ``T0+240`` and SETTLING at ``T0+280`` on time.
    """
    config = None  # offsets come from the caller's definition via the pipeline
    del config
    boundaries = (
        (3, Phase.QUOTE),
        (240, Phase.ENDGAME),
        (280, Phase.SETTLING),
        (300, Phase.DONE),
    )
    for offset_s, phase in boundaries:
        target = t0_ns + offset_s * NANOS_PER_SECOND
        while not stop.is_set():
            remaining_ns = target - clock.now()
            if remaining_ns <= 0:
                break
            await asyncio.sleep(min(0.25, remaining_ns / 1e9))
        if stop.is_set():
            return
        await queue.put(_Payload("phase", phase))


def _warm(payload: _Payload, pipeline: MarketDataPipeline) -> None:
    """Consume a pre-T0 message: update the trackers, emit nothing.

    Precision observers still run, so the O10/O12 evidence covers the warm-up window too.
    """
    if payload.kind == "clob":
        try:
            decoded = json.loads(payload.data)
        except json.JSONDecodeError:
            return
        for message in decoded if isinstance(decoded, list) else [decoded]:
            try:
                parsed = parse_market_message(
                    message,
                    price_observer=pipeline.price_precision,
                    size_observer=pipeline.size_precision,
                )
            except FeedError:
                pipeline.counters.malformed += 1
                continue
            pipeline.counters.clob_messages += 1
            if parsed.book is not None and parsed.book.tick_size is not None:
                pipeline.venue_rules.observe_rules(
                    VenueMarketRules(
                        min_tick_size=parsed.book.tick_size,
                        min_order_size=pipeline.venue_rules.rules.min_order_size,
                        source="ws/book",
                    )
                )
            pipeline.books.apply(parsed)
    elif payload.kind == "spot":
        try:
            parse_agg_trade(payload.data, pipeline.spot_precision)
        except FeedError:
            pipeline.counters.malformed += 1
    elif payload.kind == "clob_disconnect":
        pipeline.books.clear()
        pipeline.counters.reconnects += 1
    elif payload.kind == "pong":
        pipeline.counters.pongs += 1


async def capture_market(
    market: DiscoveredMarket,
    config: StrategyConfig,
    *,
    symbol: str = DEFAULT_SYMBOL,
    run_until_offset_s: int = 305,
    start_before_t0_s: int = 30,
    next_market: DiscoveredMarket | None = None,
    prearm_ready_ns: TimestampNs | None = None,
    description: str = "",
    observer: Callable[[str, int, DecisionResult], None] | None = None,
    on_pipeline: Callable[[MarketDataPipeline], None] | None = None,
    gate: Callable[[str, TimestampNs], bool] | None = None,
    on_tick: Callable[[TimestampNs, MarketDataPipeline], None] | None = None,
    force_clob_disconnect: asyncio.Event | None = None,
) -> CaptureResult:
    """Capture one full 5-minute market, read-only, through the production core.

    ``on_pipeline`` is called once with the live pipeline, before any event is consumed, so a
    measurement harness can attach without this function knowing anything about it.
    ``observer`` then receives ``(event_kind, raw_receive_ns, decision)`` after each decision,
    on the high-resolution latency clock. Both are optional, so the identical code path runs
    with instrumentation off for a like-for-like comparison.

    The last three arguments exist for P9 fault injection **on a real market**, and all three
    default to off so P6 and P8 runs are byte-identical to before:

    * ``gate(kind, now)`` may return ``False`` to suppress one arriving payload. This is how a
      local adapter is "paused": the venue socket stays connected and the real data keeps
      arriving, we simply stop delivering it to ourselves, which is exactly the failure a
      wedged consumer produces.
    * ``on_tick(now, pipeline)`` runs once per loop iteration, so a harness can evaluate risk
      and check staleness continuously rather than only when the queue happens to go quiet.
    * ``force_clob_disconnect`` makes the producer really drop its socket, so the reconnect,
      the resubscription, and the fresh snapshot are all genuine.

    None of these fabricates market data. The book, the trades, and the BTC prices are real
    throughout; only the local failure is induced.
    """
    definition = market.definition
    clock = IngressClock()
    merger = IngressMerger(
        engine=StrategyEngine(config),
        state=MarketState.initial(definition),
        clock=clock.now,
        market_id=definition.market_id,
    )
    pipeline = MarketDataPipeline(
        merger=merger,
        books=BookTracker(definition.up_token_id, definition.down_token_id),
    )
    pipeline.venue_rules.observe_rules(market.venue_rules)
    if on_pipeline is not None:
        on_pipeline(pipeline)
    clock_health = ClockHealth()

    queue: asyncio.Queue[_Payload] = asyncio.Queue()
    stop = asyncio.Event()
    tokens = (definition.up_token_id, definition.down_token_id)

    # Wait until the capture window opens, then start producers.
    open_at = definition.t0 - start_before_t0_s * NANOS_PER_SECOND
    while clock.now() < open_at:
        await asyncio.sleep(0.2)

    tasks = [
        asyncio.create_task(_polymarket_producer(queue, tokens, stop, force_clob_disconnect)),
        asyncio.create_task(_binance_producer(queue, symbol, stop)),
        asyncio.create_task(_phase_producer(queue, definition.t0, clock, stop)),
    ]

    end_at = definition.t0 + run_until_offset_s * NANOS_PER_SECOND
    warming = True
    try:
        while clock.now() < end_at:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=1.0)
            except TimeoutError:
                if not warming:
                    now = clock.now()
                    pipeline.check_staleness(now)
                    if on_tick is not None:
                        on_tick(now, pipeline)
                continue

            now = clock.now()
            if gate is not None and not gate(payload.kind, now):
                # Suppressed by fault injection. The message really arrived; we are declining
                # to consume it, which is what a stalled local adapter looks like from here.
                pipeline.check_staleness(now)
                if on_tick is not None:
                    on_tick(now, pipeline)
                continue
            if warming and now >= definition.t0:
                # Pre-arm is over. Whatever the trackers already hold becomes the
                # market's opening state, so the strategy has a warm book from its very
                # first event.
                warming = False
                if pipeline.books.ready:
                    pipeline.emit_health(
                        HealthComponent.CLOB_BOOK,
                        pipeline.clob_health.mark_snapshot(now),
                    )

            if warming:
                # Before T0 the market's deterministic stream has not begun: MarketState
                # starts with its clock parked at T0, so emitting an earlier-stamped
                # event would violate the non-decreasing timestamp contract. Messages are
                # still consumed and applied to the trackers - that warming is the whole
                # point of pre-arm (Canonical section 21) - but no Plane 2 event is
                # produced yet.
                _warm(payload, pipeline)
                continue

            raw_receive_ns = perf_counter_ns() if observer is not None else 0

            if payload.kind == "clob":
                try:
                    decoded = json.loads(payload.data)
                except json.JSONDecodeError:
                    pipeline.on_uncertain(HealthComponent.CLOB_BOOK, "undecodable frame")
                    continue
                messages = decoded if isinstance(decoded, list) else [decoded]
                for message in messages:
                    try:
                        parsed = parse_market_message(
                            message,
                            price_observer=pipeline.price_precision,
                            size_observer=pipeline.size_precision,
                        )
                    except FeedError:
                        pipeline.on_uncertain(HealthComponent.CLOB_BOOK, "malformed message")
                        continue
                    if parsed.source_timestamp_ms is not None:
                        clock_health.observe(parsed.source_timestamp_ms * 1_000_000 - clock.now())
                    decision = pipeline.on_clob_message(parsed)
                    if observer is not None and decision is not None:
                        observer("BookUpdate", raw_receive_ns, decision)

            elif payload.kind == "spot":
                try:
                    spot = parse_agg_trade(payload.data, pipeline.spot_precision)
                except FeedError:
                    pipeline.on_uncertain(HealthComponent.SPOT_FEED, "malformed spot message")
                    continue
                if spot.source_timestamp_ms is not None:
                    clock_health.observe(spot.source_timestamp_ms * 1_000_000 - clock.now())
                spot_decision = pipeline.on_spot(spot.price)
                if observer is not None:
                    observer("SpotTick", raw_receive_ns, spot_decision)

            elif payload.kind == "phase":
                phase_decision = pipeline.emit_phase(payload.data)
                if observer is not None:
                    observer("PhaseEvent", raw_receive_ns, phase_decision)

            elif payload.kind == "clob_disconnect":
                pipeline.on_disconnect(HealthComponent.CLOB_BOOK)

            elif payload.kind == "spot_disconnect":
                pipeline.on_disconnect(HealthComponent.SPOT_FEED)

            elif payload.kind == "pong":
                pipeline.counters.pongs += 1

            if on_tick is not None:
                pipeline.check_staleness(clock.now())
                on_tick(clock.now(), pipeline)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    journal = Journal(
        header=JournalHeader(
            market=definition,
            config=config,
            provenance=JournalProvenance.LIVE_PAPER,
            description=description or f"Read-only public capture of {definition.slug}",
        ),
        steps=tuple(merger.steps),
    )
    return CaptureResult(
        journal=journal,
        final_state=merger.state,
        counters=pipeline.counters,
        clock_health=clock_health,
        precision={
            "polymarket_price": pipeline.price_precision.summary(),
            "polymarket_size": pipeline.size_precision.summary(),
            "binance_price": pipeline.spot_precision.summary(),
        },
        venue_tick_changes=pipeline.venue_rules.tick_change_count,
        prearm_ready_ns=prearm_ready_ns,
        next_market_t0_ns=None if next_market is None else next_market.definition.t0,
        next_market_slug=None if next_market is None else next_market.definition.slug,
    )
