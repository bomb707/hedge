"""Normalizing authenticated user events into the existing P2 contracts.

The authenticated stream reports order lifecycle changes and our own trades. Those become
``OrderStateEvent`` and ``OwnFill`` — the contracts P2 already defines and P4 already consumes.
P2 semantics are **not** bent to match SDK object shapes: the deterministic core's contracts
are the fixed point, and the adapter translates onto them.

Credentials stay at the adapter boundary. Nothing here puts a key into ``MarketState``, a
``DecisionResult``, a replay journal, telemetry, or a log line.

A ``TAKER`` liquidity flag is preserved, never discarded. Canonical §11 calls an intentional
taker fill an execution bug, so it must stay visible all the way to P9's kill switch — which
means the normalizer's job is to surface it, not to filter or "handle" it.
"""

from dataclasses import dataclass
from typing import Any, Final

from maker5m.accounting.ledger import Fill
from maker5m.domain import Outcome
from maker5m.execution.errors import ExecutionError
from maker5m.market.events import EventMeta, Liquidity, OrderStateEvent, OrderStatus, OwnFill
from maker5m.numeric.units import MoneyUnits, PriceUnits, ShareUnits, parse_money
from maker5m.numeric.units import parse_price as _parse_price
from maker5m.numeric.units import parse_share as _parse_share

__all__ = [
    "VENUE_STATUS_MAP",
    "TakerFillViolation",
    "normalize_order_update",
    "normalize_trade",
]

VENUE_STATUS_MAP: Final[dict[str, OrderStatus]] = {
    "LIVE": OrderStatus.ACKNOWLEDGED,
    "PLACEMENT": OrderStatus.ACKNOWLEDGED,
    "MATCHED": OrderStatus.PARTIALLY_FILLED,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.CANCELLED,
}
"""Venue vocabulary onto the smallest stable internal set.

An unmapped status becomes ``UNKNOWN`` rather than a guess: an unknown order state is a risk
condition (Canonical §28.1), not a value to invent.
"""


@dataclass(frozen=True, slots=True)
class TakerFillViolation:
    """An execution-invariant violation, surfaced rather than swallowed (I07)."""

    client_order_id: str | None
    venue_order_id: str | None
    outcome: Outcome
    detail: str = "fill reported as TAKER; the strategy is post-only"


def _outcome_for(token_id: str, up_token_id: str, down_token_id: str) -> Outcome:
    if token_id == up_token_id:
        return Outcome.UP
    if token_id == down_token_id:
        return Outcome.DOWN
    raise ExecutionError(
        f"user event names token {token_id!r}, which belongs to neither side of this market"
    )


def normalize_order_update(
    payload: dict[str, Any], meta: EventMeta, *, up_token_id: str, down_token_id: str
) -> OrderStateEvent:
    """Turn one authenticated order update into a P2 ``OrderStateEvent``."""
    client_order_id = payload.get("client_order_id") or payload.get("id")
    if not isinstance(client_order_id, str) or not client_order_id:
        raise ExecutionError("order update carries no usable client order id")
    token_id = payload.get("asset_id")
    if not isinstance(token_id, str):
        raise ExecutionError("order update carries no asset_id")

    raw_status = str(payload.get("status", "")).upper()
    price = payload.get("price")
    remaining = payload.get("size_remaining", payload.get("original_size"))
    venue_order_id = payload.get("order_id")
    reason = payload.get("reason")
    return OrderStateEvent(
        meta=meta,
        client_order_id=client_order_id,
        status=VENUE_STATUS_MAP.get(raw_status, OrderStatus.UNKNOWN),
        outcome=_outcome_for(token_id, up_token_id, down_token_id),
        price=None if price is None else _price(price),
        remaining=None if remaining is None else _size(remaining),
        venue_order_id=venue_order_id if isinstance(venue_order_id, str) else None,
        reason=reason if isinstance(reason, str) else None,
    )


def normalize_trade(
    payload: dict[str, Any], meta: EventMeta, *, up_token_id: str, down_token_id: str
) -> tuple[OwnFill, TakerFillViolation | None]:
    """Turn one authenticated trade into a P2 ``OwnFill``, plus any invariant violation.

    ``cost`` is taken from the venue's own collateral figure when present, never reconstructed
    as ``size * price`` — order construction and atomic rounding mean the two can differ, and
    the ledger follows the money (P1 ``Fill``).
    """
    token_id = payload.get("asset_id")
    if not isinstance(token_id, str):
        raise ExecutionError("trade carries no asset_id")
    outcome = _outcome_for(token_id, up_token_id, down_token_id)

    size = payload.get("size")
    price = payload.get("price")
    if size is None or price is None:
        raise ExecutionError("trade carries no size or price")

    shares = _size(size)
    cost_raw = payload.get("maker_amount_filled", payload.get("cost"))
    cost = _money(cost_raw) if cost_raw is not None else _notional(shares, _price(price))

    liquidity_raw = str(payload.get("liquidity", payload.get("maker_taker", ""))).upper()
    liquidity = {
        "MAKER": Liquidity.MAKER,
        "TAKER": Liquidity.TAKER,
    }.get(liquidity_raw, Liquidity.UNKNOWN)

    fee_raw = payload.get("fee_rate_bps_amount", payload.get("fee"))
    fill = Fill(
        outcome=outcome,
        shares=shares,
        cost=cost,
        fee=_money(fee_raw) if fee_raw is not None else MoneyUnits(0),
        price=_price(price),
    )
    own_fill = OwnFill(
        meta=meta,
        fill=fill,
        client_order_id=(
            payload.get("client_order_id")
            if isinstance(payload.get("client_order_id"), str)
            else None
        ),
        venue_order_id=(
            payload.get("order_id") if isinstance(payload.get("order_id"), str) else None
        ),
        liquidity=liquidity,
    )
    violation = (
        TakerFillViolation(
            client_order_id=own_fill.client_order_id,
            venue_order_id=own_fill.venue_order_id,
            outcome=outcome,
        )
        if liquidity is Liquidity.TAKER
        else None
    )
    return own_fill, violation


def _price(value: Any) -> PriceUnits:
    if not isinstance(value, str):
        raise ExecutionError(
            f"price must arrive as a decimal string, got {type(value).__name__}; "
            "a float has already lost exactness"
        )
    return _parse_price(value)


def _size(value: Any) -> ShareUnits:
    if not isinstance(value, str):
        raise ExecutionError(f"size must arrive as a decimal string, got {type(value).__name__}")
    return _parse_share(value, allow_negative=False)


def _money(value: Any) -> MoneyUnits:
    if not isinstance(value, str):
        raise ExecutionError(f"amount must arrive as a decimal string, got {type(value).__name__}")
    return parse_money(value, allow_negative=False)


def _notional(shares: ShareUnits, price: PriceUnits) -> MoneyUnits:
    from maker5m.numeric.units import Rounding, notional_cost

    return notional_cost(shares, price, rounding=Rounding.FLOOR)
