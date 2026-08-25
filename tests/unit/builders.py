"""Shared builders for the deterministic market-state tests."""

from __future__ import annotations

from maker5m.accounting import Fill
from maker5m.domain import Outcome
from maker5m.market import (
    CANONICAL_PHASE_CONFIG,
    BookLevel,
    BookUpdate,
    EventMeta,
    HealthComponent,
    HealthEvent,
    HealthStatus,
    MarketDefinition,
    MarketState,
    OrderStateEvent,
    OrderStatus,
    OwnFill,
    SpotTick,
    TimestampNs,
)
from maker5m.market.btc_price import BtcPrice
from maker5m.market.timebase import NANOS_PER_SECOND, seconds
from maker5m.numeric import PriceUnits, parse_money, parse_price, parse_share

MARKET_ID = "btc-updown-5m-0001"
T0 = TimestampNs(1_700_000_000 * NANOS_PER_SECOND)

# BTC scale is OPEN (O12); tests pick one explicitly rather than relying on a default.
BTC_DECIMALS = 2


def at(offset_s: int) -> TimestampNs:
    return TimestampNs(T0 + seconds(offset_s))


def definition(**overrides: object) -> MarketDefinition:
    fields: dict[str, object] = {
        "market_id": MARKET_ID,
        "slug": "btc-updown-5m-0001",
        "up_token_id": "token-up",
        "down_token_id": "token-down",
        "t0": T0,
        "phase_config": CANONICAL_PHASE_CONFIG,
        "tick": PriceUnits(10_000),
        "strike": BtcPrice.parse("64000.00", scale_decimals=BTC_DECIMALS),
    }
    fields.update(overrides)
    return MarketDefinition(**fields)  # type: ignore[arg-type]


def initial_state() -> MarketState:
    return MarketState.initial(definition())


def meta(
    ordinal: int,
    offset_s: int = 10,
    *,
    event_id: str | None = None,
    market_id: str = MARKET_ID,
) -> EventMeta:
    return EventMeta(
        market_id=market_id,
        event_id=event_id if event_id is not None else f"e{ordinal}",
        ingress_ordinal=ordinal,
        timestamp=at(offset_s),
    )


def book(ordinal: int, offset_s: int = 10, *, bid: str = "0.62", ask: str = "0.63") -> BookUpdate:
    return BookUpdate(
        meta=meta(ordinal, offset_s),
        up_bid=BookLevel(parse_price(bid), parse_share("100")),
        up_ask=BookLevel(parse_price(ask), parse_share("120")),
        down_bid=BookLevel(parse_price("0.37"), parse_share("120")),
        down_ask=BookLevel(parse_price("0.38"), parse_share("100")),
        sequence=ordinal,
    )


def spot(ordinal: int, offset_s: int = 10, *, price: str = "64123.45") -> SpotTick:
    return SpotTick(
        meta=meta(ordinal, offset_s),
        price=BtcPrice.parse(price, scale_decimals=BTC_DECIMALS),
    )


def own_fill(
    ordinal: int,
    offset_s: int = 10,
    *,
    outcome: Outcome = Outcome.UP,
    shares: str = "13.63",
    cost: str = "8.5869",
    fee: str = "0",
    event_id: str | None = None,
) -> OwnFill:
    return OwnFill(
        meta=meta(ordinal, offset_s, event_id=event_id),
        fill=Fill(
            outcome=outcome,
            shares=parse_share(shares),
            cost=parse_money(cost),
            fee=parse_money(fee),
        ),
        client_order_id=f"coid-{ordinal}",
    )


def order_state(
    ordinal: int,
    offset_s: int = 10,
    *,
    client_order_id: str = "coid-1",
    status: OrderStatus = OrderStatus.ACKNOWLEDGED,
) -> OrderStateEvent:
    return OrderStateEvent(
        meta=meta(ordinal, offset_s),
        client_order_id=client_order_id,
        status=status,
        outcome=Outcome.UP,
        price=parse_price("0.63"),
        remaining=parse_share("13.63"),
    )


def health(
    ordinal: int,
    offset_s: int = 10,
    *,
    component: HealthComponent = HealthComponent.CLOB_BOOK,
    status: HealthStatus = HealthStatus.HEALTHY,
) -> HealthEvent:
    return HealthEvent(meta=meta(ordinal, offset_s), component=component, status=status)
