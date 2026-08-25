"""The venue submission contract: BUY, GTC, post-only. Nothing else is representable.

Canonical §11 and §23 make these hard invariants, so they are encoded as types rather than as
runtime checks that could be bypassed:

* ``OrderSide`` has one member. There is no SELL, so no code path can construct one.
* ``VenueOrderType`` has one member. There is no FOK, FAK, or market order.
* ``post_only`` is a property that returns ``True`` and cannot be set.

The official SDK's ``create_limit_order`` defaults ``post_only=False`` and accepts
``Literal["BUY","SELL"]`` and float prices. This module is the narrow gate that makes those
permissive defaults unreachable from this project (I06, I07).
"""

from decimal import Decimal
from enum import Enum
from typing import Final

from maker5m.numeric.scales import PRICE_SCALE, SCALE_DECIMALS, SHARE_SCALE
from maker5m.numeric.units import PriceUnits, ShareUnits

__all__ = ["POST_ONLY", "OrderSide", "VenueOrderType", "price_to_decimal", "size_to_decimal"]


class OrderSide(Enum):
    """The only side this strategy ever submits.

    The strategy is buy-only: it acquires both outcome tokens and exits by redemption, never
    by selling (I15, I16, Canonical §18). A SELL member would make the forbidden action
    representable, so there isn't one.
    """

    BUY = "BUY"


class VenueOrderType(Enum):
    """The only order type this strategy ever submits.

    GTC resting limit orders. GTD is unnecessary because the strategy cancels explicitly at
    SETTLING; FOK and FAK are marketable and would guarantee the taker fill that Canonical §11
    calls an execution bug.
    """

    GTC = "GTC"


POST_ONLY: Final[bool] = True
"""Always true. Never configurable, never overridden on retry (Canonical §11, I06)."""


def price_to_decimal(price: PriceUnits) -> Decimal:
    """Exact ``Decimal`` for the SDK boundary. Never ``float``.

    The SDK accepts ``Decimal | int | float | str``; passing a float would reintroduce binary
    error at the last step of a pipeline built to avoid it.
    """
    return Decimal(price).scaleb(-SCALE_DECIMALS)


def size_to_decimal(size: ShareUnits) -> Decimal:
    """Exact ``Decimal`` for the SDK boundary. Never ``float``."""
    return Decimal(size).scaleb(-SCALE_DECIMALS)


# Compile-time guards: the conversions above assume both scales share SCALE_DECIMALS.
if PRICE_SCALE != 10**SCALE_DECIMALS:  # pragma: no cover - frozen scales make this dead code
    raise AssertionError("price scale no longer matches SCALE_DECIMALS")
if SHARE_SCALE != 10**SCALE_DECIMALS:  # pragma: no cover
    raise AssertionError("share scale no longer matches SCALE_DECIMALS")
