"""Shared builders for the execution tests. No network, no clock, no credentials."""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path as _Path

from maker5m.domain import Outcome
from maker5m.feeds.venue import VenueMarketRules
from maker5m.market import (
    CANONICAL_PHASE_CONFIG,
    BookLevel,
    BookUpdate,
    EventMeta,
    MarketDefinition,
    MarketState,
    TimestampNs,
)
from maker5m.market.timebase import NANOS_PER_SECOND, seconds
from maker5m.numeric import PriceUnits, ShareUnits, parse_price, parse_share
from maker5m.strategy.decision import DecisionResult, DesiredOrder, DesiredOrders

UP_TOKEN = "token-up"
DOWN_TOKEN = "token-down"
T0 = TimestampNs(1_787_647_500 * NANOS_PER_SECOND)
TICK = PriceUnits(10_000)


def market() -> MarketDefinition:
    return MarketDefinition(
        market_id="0xcondition",
        slug="btc-updown-5m-1787647500",
        up_token_id=UP_TOKEN,
        down_token_id=DOWN_TOKEN,
        t0=T0,
        phase_config=CANONICAL_PHASE_CONFIG,
        tick=TICK,
    )


def rules(tick: str = "0.01", min_size: str = "5") -> VenueMarketRules:
    return VenueMarketRules(
        min_tick_size=parse_price(tick),
        min_order_size=parse_share(min_size),
        source="test",
    )


def state_at(
    offset_s: int = 60,
    *,
    up_bid: str | None = "0.62",
    up_ask: str | None = "0.64",
    down_bid: str | None = "0.35",
    down_ask: str | None = "0.38",
) -> MarketState:
    """A quoting market with an observed two-sided book on both tokens."""

    def level(price: str | None) -> BookLevel | None:
        return None if price is None else BookLevel(parse_price(price), parse_share("100"))

    base = MarketState.initial(market())
    update = BookUpdate(
        meta=EventMeta("0xcondition", "e0", 0, TimestampNs(T0 + seconds(offset_s))),
        up_bid=level(up_bid),
        up_ask=level(up_ask),
        down_bid=level(down_bid),
        down_ask=level(down_ask),
    )
    from maker5m.market import reduce_event

    return reduce_event(base, update)


def desired(
    up_price: str | None = "0.63",
    up_size: str | None = "15",
    down_price: str | None = "0.37",
    down_size: str | None = "15",
) -> DesiredOrders:
    up = (
        None
        if up_price is None or up_size is None
        else DesiredOrder(Outcome.UP, parse_price(up_price), parse_share(up_size))
    )
    down = (
        None
        if down_price is None or down_size is None
        else DesiredOrder(Outcome.DOWN, parse_price(down_price), parse_share(down_size))
    )
    return DesiredOrders(up=up, down=down)


def decision(orders: DesiredOrders, state: MarketState) -> DecisionResult:
    """Wrap desired orders in a real DecisionResult using the engine's own telemetry."""
    from maker5m.strategy import StrategyEngine, default_config

    base = StrategyEngine(default_config()).decide(state)
    return dataclasses.replace(base, orders=orders)


def sh(text: str) -> ShareUnits:
    return parse_share(text)


def px(text: str) -> PriceUnits:
    return parse_price(text)


def code_without_docstrings(path: _Path) -> str:
    """Source with docstrings stripped.

    Several execution modules *describe* the things they refuse to implement - a
    ``min_requote_ms`` delay, a ``--live`` flag, ``post_only=False``. A plain text scan would
    trip over that documentation, so structural guards read the code instead of the prose.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body.pop(0)
    return ast.unparse(tree)
