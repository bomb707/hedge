"""A controllable :class:`InstrumentedRun`, so shadow queue lifecycle can be driven directly.

The first P8 implementation had no end-to-end test of the harness itself, which is precisely
where the desired-price/executable-order confusion lived. These builders exist so the lifecycle
can be exercised event by event with an exactly known book.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from maker5m.domain import Outcome
from maker5m.execution import Executor, RecordingTransport, VenueAdapter
from maker5m.feeds import BookTracker, IngressMerger, MarketDataPipeline
from maker5m.market import MarketState
from maker5m.numeric import PriceUnits, ShareUnits, parse_price, parse_share
from maker5m.strategy import BaseLot, StrategyEngine, default_config
from maker5m.strategy.decision import DecisionResult, DesiredOrder, DesiredOrders
from maker5m.telemetry import InstrumentedRun, SamplingPolicy, TelemetryAnalyzer
from tests.execution.builders import market, rules, state_at


def build(sample_every: int = 1) -> InstrumentedRun:
    definition = market()
    engine = StrategyEngine(default_config(BaseLot.of(15)))
    merger = IngressMerger(
        engine=engine,
        state=MarketState.initial(definition),
        clock=lambda: definition.t0,
        market_id=definition.market_id,
    )
    pipeline = MarketDataPipeline(
        merger=merger,
        books=BookTracker(definition.up_token_id, definition.down_token_id),
    )
    # A live run reaches HEALTHY after its first good snapshot on both tokens; these tests
    # start from that state so classification is not swamped by CONTINUITY_LOST.
    pipeline.clob_health.mark_snapshot(definition.t0)
    pipeline.spot_health.mark_snapshot(definition.t0)
    return InstrumentedRun(
        pipeline=pipeline,
        engine=engine,
        rules=rules(),
        executor=Executor(adapter=VenueAdapter(RecordingTransport())),
        sampling=SamplingPolicy(sample_every),
    )


def set_book(
    harness: InstrumentedRun,
    outcome: Outcome,
    *,
    bid: str,
    bid_size: str,
    ask: str,
) -> None:
    """Overwrite one outcome's ladder directly. No feed decoding, no ambiguity."""
    book = harness.pipeline.books.up if outcome is Outcome.UP else harness.pipeline.books.down
    book.bids.clear()
    book.asks.clear()
    book.bids[int(parse_price(bid))] = int(parse_share(bid_size))
    book.asks[int(parse_price(ask))] = int(parse_share("500"))
    book.snapshot_seen = True


@dataclass(frozen=True, slots=True)
class Want:
    """What the strategy wants on one side this cycle."""

    price: PriceUnits
    size: ShareUnits


def wants(price: str, size: str = "15") -> Want:
    return Want(parse_price(price), parse_share(size))


def step(
    harness: InstrumentedRun,
    *,
    up: Want | None = None,
    down: Want | None = None,
    up_bid: str = "0.62",
    up_bid_size: str = "0",
    up_ask: str = "0.64",
    event_kind: str = "BookUpdate",
    healthy: bool = True,
) -> None:
    """Drive exactly one observed cycle with a fully specified UP book."""
    state = state_at(up_bid=up_bid, up_ask=up_ask, down_bid="0.35", down_ask="0.38")
    harness.pipeline.merger.state = state
    # A live run assigns a fresh ingress ordinal per event. Leaving it pinned at zero would
    # make `ordinal % sample_every == 0` always true and silently disable sampling.
    harness.pipeline.merger.advance_ordinal()
    # Stage sampling is decided before reduce/decide in a live run; mirror that here.
    harness.pipeline.merger.stages_measured = harness.sampling.selects(
        harness.pipeline.merger.ordinal, event_kind
    )
    if healthy:
        harness.pipeline.clob_health.mark_snapshot(harness.pipeline.merger.state.definition.t0)
    else:
        harness.pipeline.clob_health.mark_disconnected()
    set_book(harness, Outcome.UP, bid=up_bid, bid_size=up_bid_size, ask=up_ask)
    set_book(harness, Outcome.DOWN, bid="0.35", bid_size="0", ask="0.38")

    orders = DesiredOrders(
        up=None if up is None else DesiredOrder(Outcome.UP, up.price, up.size),
        down=None if down is None else DesiredOrder(Outcome.DOWN, down.price, down.size),
    )
    base: DecisionResult = harness.engine.decide(state)
    harness.observe(event_kind, 0, replace(base, orders=orders))


def analyzed(harness: InstrumentedRun) -> TelemetryAnalyzer:
    """Fold the captured observations, the way a real run does after DONE.

    Every measurement now comes from downstream analysis, so a test that wants to know what was
    measured has to ask the analyzer rather than the harness. Re-folding the whole buffer per
    call is wasteful and irrelevant at test sizes, and it keeps each assertion honest about
    where the numbers come from.
    """
    return harness.analyze()
