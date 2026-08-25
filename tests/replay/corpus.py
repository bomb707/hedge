"""A non-trivial **SYNTHETIC** journal corpus.

Constructed by this project to exercise the replay machinery. It is **not** evidence about
the target wallet, and every journal built here declares
``JournalProvenance.SYNTHETIC`` so it can never be mistaken for one.

The stream spans all five phases and all six event types, and is shaped so that:

* inventory reaches states where the two O04 grid policies produce different DOWN sizes;
* the centre crosses ``0.5`` in both directions, so ENDGAME sees both favourites;
* the mandatory accounting example (120 UP at $72, 100 DOWN at $50, giving -$2 / -$22) is
  reached exactly through real fills part-way through ENDGAME. A later fractional endgame
  fill then moves the ledger on, so the example is asserted at the step where it holds rather
  than at the end;
* equal timestamps appear with distinct ingress ordinals;
* partial fills with fractional sizes are present.
"""

from __future__ import annotations

from maker5m.accounting import Fill
from maker5m.domain import Outcome
from maker5m.market import (
    CANONICAL_PHASE_CONFIG,
    BookLevel,
    BookUpdate,
    Event,
    EventMeta,
    HealthComponent,
    HealthEvent,
    HealthStatus,
    Liquidity,
    MarketDefinition,
    OrderStateEvent,
    OrderStatus,
    OwnFill,
    Phase,
    PhaseEvent,
    SpotTick,
    TimestampNs,
)
from maker5m.market.btc_price import BtcPrice
from maker5m.market.timebase import NANOS_PER_SECOND, millis, seconds
from maker5m.numeric import PriceUnits, parse_money, parse_price, parse_share
from maker5m.replay import JournalProvenance, RecordedRun, record
from maker5m.strategy import BaseLot, StrategyConfig, default_config

MARKET_ID = "btc-updown-5m-synthetic-0001"
T0 = TimestampNs(1_700_000_000 * NANOS_PER_SECOND)
BTC_DECIMALS = 2  # explicit: the BTC scale is OPEN (O12), never defaulted


def at(offset_s: int, extra_ms: int = 0) -> TimestampNs:
    return TimestampNs(T0 + seconds(offset_s) + millis(extra_ms))


def market() -> MarketDefinition:
    return MarketDefinition(
        market_id=MARKET_ID,
        slug="btc-updown-5m-synthetic-0001",
        up_token_id="synthetic-token-up",
        down_token_id="synthetic-token-down",
        t0=T0,
        phase_config=CANONICAL_PHASE_CONFIG,
        tick=PriceUnits(10_000),
        strike=BtcPrice.parse("64000.00", scale_decimals=BTC_DECIMALS),
    )


class _Stream:
    """Assigns ingress ordinals in order, so the corpus cannot drift out of sequence."""

    def __init__(self) -> None:
        self.ordinal = 0
        self.events: list[Event] = []

    def meta(self, offset_s: int, extra_ms: int = 0, *, event_id: str | None = None) -> EventMeta:
        meta = EventMeta(
            market_id=MARKET_ID,
            event_id=event_id or f"syn-{self.ordinal:04d}",
            ingress_ordinal=self.ordinal,
            timestamp=at(offset_s, extra_ms),
        )
        self.ordinal += 1
        return meta

    def add(self, event: Event) -> None:
        self.events.append(event)


def _book(stream: _Stream, offset_s: int, bid: str, ask: str, extra_ms: int = 0) -> None:
    stream.add(
        BookUpdate(
            meta=stream.meta(offset_s, extra_ms),
            up_bid=BookLevel(parse_price(bid), parse_share("100")),
            up_ask=BookLevel(parse_price(ask), parse_share("100")),
            down_bid=BookLevel(parse_price("0.30"), parse_share("100")),
            down_ask=BookLevel(parse_price("0.32"), parse_share("100")),
            sequence=stream.ordinal,
        )
    )


def _spot(stream: _Stream, offset_s: int, price: str) -> None:
    stream.add(
        SpotTick(
            meta=stream.meta(offset_s),
            price=BtcPrice.parse(price, scale_decimals=BTC_DECIMALS),
            source_sequence=stream.ordinal,
        )
    )


def _fill(
    stream: _Stream,
    offset_s: int,
    outcome: Outcome,
    shares: str,
    cost: str,
    *,
    liquidity: Liquidity = Liquidity.MAKER,
) -> None:
    stream.add(
        OwnFill(
            meta=stream.meta(offset_s),
            fill=Fill(
                outcome=outcome,
                shares=parse_share(shares),
                cost=parse_money(cost),
                fee=parse_money("0"),
                price=None,
            ),
            client_order_id=f"coid-{stream.ordinal}",
            venue_order_id=f"venue-{stream.ordinal}",
            liquidity=liquidity,
        )
    )


def synthetic_events() -> tuple[Event, ...]:
    """The full lifecycle stream. Deterministic and order-stable."""
    s = _Stream()

    # PREARM
    s.add(PhaseEvent(s.meta(0), Phase.PREARM))
    _spot(s, 1, "63990.10")
    s.add(HealthEvent(s.meta(2), HealthComponent.CLOB_BOOK, HealthStatus.UNKNOWN, "warming up"))

    # QUOTE - centre above 0.5, then below, then back above.
    _book(s, 3, "0.61", "0.63")
    s.add(HealthEvent(s.meta(3, 100), HealthComponent.CLOB_BOOK, HealthStatus.HEALTHY, None))
    s.add(HealthEvent(s.meta(3, 200), HealthComponent.SPOT_FEED, HealthStatus.HEALTHY, None))
    s.add(
        OrderStateEvent(
            s.meta(4),
            "coid-a",
            OrderStatus.ACKNOWLEDGED,
            Outcome.UP,
            parse_price("0.62"),
            parse_share("15"),
        )
    )

    # Two book updates sharing a timestamp, distinguished only by ingress ordinal.
    _book(s, 10, "0.61", "0.63")
    _book(s, 10, "0.62", "0.64")

    # Reach the mandatory accounting example through real fills, with fractional partials.
    _fill(s, 12, Outcome.UP, "40", "24")
    _spot(s, 13, "64010.55")
    _fill(s, 20, Outcome.DOWN, "30", "15")
    s.add(
        OrderStateEvent(
            s.meta(21),
            "coid-a",
            OrderStatus.PARTIALLY_FILLED,
            Outcome.UP,
            parse_price("0.62"),
            parse_share("5"),
        )
    )
    _book(s, 30, "0.40", "0.42")  # centre below 0.5
    _fill(s, 35, Outcome.UP, "28.63", "17.178")
    _fill(s, 40, Outcome.DOWN, "27.26", "13.63")
    _book(s, 60, "0.44", "0.46")
    _spot(s, 61, "63880.00")
    _fill(s, 90, Outcome.UP, "31.37", "18.822")
    _fill(s, 120, Outcome.DOWN, "42.74", "21.37")
    s.add(
        OrderStateEvent(
            s.meta(121),
            "coid-b",
            OrderStatus.FILLED,
            Outcome.DOWN,
            parse_price("0.38"),
            parse_share("0"),
        )
    )
    _book(s, 150, "0.55", "0.57")
    _fill(s, 180, Outcome.UP, "20", "12")
    _book(s, 200, "0.61", "0.63")
    s.add(HealthEvent(s.meta(210), HealthComponent.ORDER_STREAM, HealthStatus.HEALTHY, None))

    # ENDGAME - UP favourite, then a DOWN-favourite excursion, then back.
    s.add(PhaseEvent(s.meta(240), Phase.ENDGAME))
    _book(s, 241, "0.69", "0.71")
    _spot(s, 242, "64220.75")
    _book(s, 250, "0.28", "0.30")  # DOWN favourite
    _book(s, 255, "0.49", "0.51")  # raw centre exactly 0.50 -> DOWN by A1
    _book(s, 260, "0.503", "0.505")  # raw 0.504 quantizes to 0.50 but favours UP
    _fill(s, 265, Outcome.DOWN, "0.60", "0.30")
    _book(s, 270, "0.86", "0.88")
    s.add(HealthEvent(s.meta(275), HealthComponent.CLOB_BOOK, HealthStatus.STALE, "no update"))

    # SETTLING and DONE
    s.add(PhaseEvent(s.meta(280), Phase.SETTLING))
    _book(s, 285, "0.90", "0.92")
    _spot(s, 290, "64310.00")
    s.add(PhaseEvent(s.meta(300), Phase.DONE))
    _book(s, 305, "0.95", "0.97")

    return tuple(s.events)


SYNTHETIC_EVENTS: tuple[Event, ...] = synthetic_events()


def synthetic_run(config: StrategyConfig | None = None) -> RecordedRun:
    """Record the synthetic corpus. Always labelled SYNTHETIC."""
    return record(
        market(),
        config or default_config(BaseLot.of(15)),
        SYNTHETIC_EVENTS,
        provenance=JournalProvenance.SYNTHETIC,
        description="Synthetic full-lifecycle corpus. Not target-wallet evidence.",
    )
