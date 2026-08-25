"""The venue adapter: the only place the official SDK is touched.

Verified against ``polymarket-client==0.6.0`` (import name ``polymarket``, repository
``Polymarket/py-sdk``). The legacy archived ``py-clob-client`` is not used.

What the SDK owns
-----------------
EIP-712 signing, L1/L2 authentication, wire serialization, authenticated HTTP, and the
authenticated user WebSocket. Reinventing any of that to avoid a dependency would be worse
than depending on it.

What it must not own
--------------------
Strategy decisions, inventory, sizing, reconciliation, queue-preservation policy, post-only
policy, or rate policy. Those live in ``maker5m.execution`` and no SDK type crosses back into
``StrategyEngine``.

Three facts about the SDK that shape this module
------------------------------------------------
1. ``create_limit_order(..., post_only: bool = False, ...)`` — the default is the **unsafe**
   one for us, so ``POST_ONLY`` is passed explicitly on every call and there is no code path
   that can omit it.
2. It sets ``order_type = "GTC" if expiration is None else "GTD"``. We never pass an
   expiration, so every order is GTC. The strategy cancels explicitly at SETTLING, so GTD is
   unnecessary.
3. ``price``/``size`` accept ``Decimal | int | float | str``. We pass ``Decimal``. Passing a
   float would reintroduce binary error at the final step of a pipeline built to avoid it.

Latency shape
-------------
``create_limit_order`` resolves market metadata through the SDK's own cache. On a cache miss
that is a REST round trip **before signing**, which is exactly the hazard Canonical §22 warns
about — so :meth:`VenueAdapter.prewarm` populates that cache during pre-arm, and the hot path
is then ``sign -> POST``.

``post_order`` returns ``AcceptedOrder | RejectedOrder`` immediately; it does not wait for a
transaction hash or on-chain settlement. The SDK's ``wait_for_order_fill_settlement`` exists
and is deliberately never called — subsequent execution state comes from the user stream.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from maker5m.execution.credentials import ExecutionCredentials
from maker5m.execution.errors import ExecutionError
from maker5m.execution.gate import require_live_trading_enabled
from maker5m.execution.prepare import PreparedOrder
from maker5m.execution.venue_order import (
    POST_ONLY,
    OrderSide,
    VenueOrderType,
    price_to_decimal,
    size_to_decimal,
)

__all__ = [
    "SDK_DISTRIBUTION",
    "SDK_IMPORT_NAME",
    "SDK_PINNED_VERSION",
    "OrderPlacement",
    "RecordingTransport",
    "VenueAdapter",
    "VenueTransport",
]

SDK_DISTRIBUTION = "polymarket-client"
SDK_PINNED_VERSION = "0.6.0"
SDK_IMPORT_NAME = "polymarket"


@dataclass(frozen=True, slots=True)
class OrderPlacement:
    """Exactly what will be sent, in SDK vocabulary, with no float anywhere."""

    token_id: str
    price: Any
    """``decimal.Decimal``. Typed loosely only to keep the SDK out of this signature."""

    size: Any
    """``decimal.Decimal``."""

    side: str
    order_type: str
    post_only: bool

    def as_sdk_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``SecureClient.create_limit_order``.

        ``expiration`` is deliberately absent, which is what makes the order GTC.
        """
        return {
            "token_id": self.token_id,
            "price": self.price,
            "size": self.size,
            "side": self.side,
            "post_only": self.post_only,
        }


def build_placement(prepared: PreparedOrder) -> OrderPlacement:
    """Serialize a prepared order for the venue. Pure, and the only construction path.

    Refuses a non-submittable order outright: the guard's verdict is not advisory.
    """
    if not prepared.submittable:
        raise ExecutionError(
            f"refusing to serialize a non-submittable order: {prepared.outcome_status.value}"
        )
    return OrderPlacement(
        token_id=prepared.token_id,
        price=price_to_decimal(prepared.submission_price),
        size=size_to_decimal(prepared.submission_size),
        side=OrderSide.BUY.value,
        order_type=VenueOrderType.GTC.value,
        post_only=POST_ONLY,
    )


@runtime_checkable
class VenueTransport(Protocol):
    """The narrow surface the executor needs. Implemented by the SDK and by test doubles."""

    def prewarm(self, token_ids: tuple[str, ...]) -> None: ...

    def place(self, placement: OrderPlacement) -> Any: ...

    def cancel(self, venue_order_id: str) -> Any: ...


@dataclass(slots=True)
class RecordingTransport:
    """A test double that records requests and performs no network I/O whatsoever.

    Used everywhere in the suite, so no test can reach a venue even by mistake.
    """

    placements: list[OrderPlacement] = field(default_factory=list)
    cancels: list[str] = field(default_factory=list)
    prewarmed: list[tuple[str, ...]] = field(default_factory=list)
    metadata_requests: int = 0

    def prewarm(self, token_ids: tuple[str, ...]) -> None:
        self.prewarmed.append(token_ids)
        self.metadata_requests += 1

    def place(self, placement: OrderPlacement) -> OrderPlacement:
        self.placements.append(placement)
        return placement

    def cancel(self, venue_order_id: str) -> str:
        self.cancels.append(venue_order_id)
        return venue_order_id


@dataclass(slots=True)
class VenueAdapter:
    """Wraps a transport. Arming a **real** one requires live trading to be enabled."""

    transport: VenueTransport

    @classmethod
    def arm_live(cls, credentials: ExecutionCredentials, build_transport: Any) -> "VenueAdapter":
        """Construct a real authenticated adapter, or refuse.

        The gate is checked **first**: before the credential is read and before any client is
        constructed, so a disabled build cannot open a connection or touch a key.
        """
        require_live_trading_enabled("the authenticated Polymarket write adapter")
        return cls(transport=build_transport(credentials))  # pragma: no cover - P14 only

    def prewarm(self, token_ids: tuple[str, ...]) -> None:
        """Populate the SDK's metadata cache during pre-arm, off the hot path."""
        self.transport.prewarm(token_ids)

    def place(self, prepared: PreparedOrder) -> Any:
        """Sign and POST one order. No metadata lookup, no settlement polling."""
        return self.transport.place(build_placement(prepared))

    def cancel(self, venue_order_id: str) -> Any:
        return self.transport.cancel(venue_order_id)
