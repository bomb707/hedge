"""Execution-owned order state, distinct from strategy intent.

P4's ``DesiredOrders`` says what the strategy wants. This says what actually exists at the
venue as far as we know — which is a different and less certain thing, because
acknowledgements arrive late, cancels race with fills, and a request can time out leaving the
outcome genuinely unknown.

The table therefore holds **many** orders, not one per side. A model with a single optional
order per outcome cannot represent a cancel racing an acknowledgement, and would either lose
an in-flight order or invent a duplicate. A deterministic per-outcome *view* is derived for
the reconciler on top of that.

Uncertainty is represented rather than resolved: ``PENDING_PLACE``, ``PENDING_CANCEL``, and
``UNKNOWN`` are first-class, and the reconciler waits on them instead of guessing (Canonical
§28.1 makes unknown order state a halt condition, not something to assume away).
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType

from maker5m.domain import Outcome
from maker5m.execution.errors import OrderIdentityError
from maker5m.numeric.units import PriceUnits, ShareUnits

__all__ = ["LiveOrder", "LiveOrderTable", "OrderLifecycle"]


class OrderLifecycle(Enum):
    """Where one order stands, including the states where we genuinely do not know."""

    PENDING_PLACE = "PENDING_PLACE"
    """Submitted; no acknowledgement yet. May or may not exist at the venue."""

    LIVE = "LIVE"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"

    PENDING_CANCEL = "PENDING_CANCEL"
    """Cancel requested; not yet acknowledged. May still fill."""

    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

    UNKNOWN = "UNKNOWN"
    """State could not be established. Never assumed either way."""

    @property
    def resting(self) -> bool:
        """Believed to be working at the venue and able to fill."""
        return self in (OrderLifecycle.LIVE, OrderLifecycle.PARTIALLY_FILLED)

    @property
    def in_flight(self) -> bool:
        """A request is outstanding, so acting again risks a duplicate."""
        return self in (
            OrderLifecycle.PENDING_PLACE,
            OrderLifecycle.PENDING_CANCEL,
            OrderLifecycle.UNKNOWN,
        )

    @property
    def terminal(self) -> bool:
        return self in (
            OrderLifecycle.FILLED,
            OrderLifecycle.CANCELLED,
            OrderLifecycle.REJECTED,
        )


@dataclass(frozen=True, slots=True)
class LiveOrder:
    """One order we have submitted, as far as we currently know."""

    client_order_id: str
    outcome: Outcome
    price: PriceUnits
    original_size: ShareUnits
    remaining_size: ShareUnits
    status: OrderLifecycle
    venue_order_id: str | None = None
    last_ingress_ordinal: int = -1
    supersedes: str | None = None
    """The client order id this one replaces, when it came from a REPLACE."""

    reason: str | None = None

    @property
    def occupies_side(self) -> bool:
        """Whether this order stops us placing another on the same side.

        True for resting orders *and* for in-flight ones: acting while a request is
        outstanding is how duplicates get created.
        """
        return self.status.resting or self.status.in_flight


@dataclass(slots=True)
class LiveOrderTable:
    """All orders this session has submitted, keyed by client order id.

    Terminal orders are retained so a late, duplicated acknowledgement can be recognised as a
    repeat rather than treated as a new order.
    """

    orders: dict[str, LiveOrder] = field(default_factory=dict)
    applied_updates: set[str] = field(default_factory=set)

    # -- lifecycle ----------------------------------------------------------------------

    def register_pending_place(
        self,
        *,
        client_order_id: str,
        outcome: Outcome,
        price: PriceUnits,
        size: ShareUnits,
        ingress_ordinal: int,
        supersedes: str | None = None,
    ) -> LiveOrder:
        """Record a placement attempt. The id must never have been used before."""
        if client_order_id in self.orders:
            raise OrderIdentityError(
                f"client order id {client_order_id!r} has already been used; "
                "every placement attempt needs a fresh identity"
            )
        order = LiveOrder(
            client_order_id=client_order_id,
            outcome=outcome,
            price=price,
            original_size=size,
            remaining_size=size,
            status=OrderLifecycle.PENDING_PLACE,
            last_ingress_ordinal=ingress_ordinal,
            supersedes=supersedes,
        )
        self.orders[client_order_id] = order
        return order

    def get(self, client_order_id: str) -> LiveOrder | None:
        return self.orders.get(client_order_id)

    def require(self, client_order_id: str) -> LiveOrder:
        order = self.orders.get(client_order_id)
        if order is None:
            raise OrderIdentityError(f"unknown client order id {client_order_id!r}")
        return order

    def update(
        self,
        client_order_id: str,
        *,
        status: OrderLifecycle | None = None,
        remaining_size: ShareUnits | None = None,
        venue_order_id: str | None = None,
        ingress_ordinal: int | None = None,
        reason: str | None = None,
        update_id: str | None = None,
    ) -> LiveOrder:
        """Apply one lifecycle update, idempotently when ``update_id`` is supplied.

        A duplicated network response must not advance state twice, and must never cause
        another placement.
        """
        if update_id is not None:
            if update_id in self.applied_updates:
                return self.require(client_order_id)
            self.applied_updates.add(update_id)
        order = self.require(client_order_id)
        updated = replace(
            order,
            status=order.status if status is None else status,
            remaining_size=(order.remaining_size if remaining_size is None else remaining_size),
            venue_order_id=(order.venue_order_id if venue_order_id is None else venue_order_id),
            last_ingress_ordinal=(
                order.last_ingress_ordinal if ingress_ordinal is None else ingress_ordinal
            ),
            reason=order.reason if reason is None else reason,
        )
        self.orders[client_order_id] = updated
        return updated

    # -- views ---------------------------------------------------------------------------

    def __iter__(self) -> Iterator[LiveOrder]:
        return iter(self.orders.values())

    def snapshot(self) -> Mapping[str, LiveOrder]:
        """An immutable view for the pure reconciler."""
        return MappingProxyType(dict(self.orders))

    def occupying(self, outcome: Outcome) -> tuple[LiveOrder, ...]:
        """Every order on ``outcome`` that blocks a new placement, oldest first.

        Sorted by client order id so the view is deterministic regardless of dict history.
        """
        return tuple(
            sorted(
                (o for o in self.orders.values() if o.outcome is outcome and o.occupies_side),
                key=lambda o: o.client_order_id,
            )
        )

    def current(self, outcome: Outcome) -> LiveOrder | None:
        """The single order the reconciler should reason about for this side.

        ``None`` when the side is free. When more than one order occupies a side — which only
        happens transiently during a replacement race — the *earliest* is returned, because
        that is the one that must be resolved before anything else may be submitted.
        """
        occupying = self.occupying(outcome)
        return occupying[0] if occupying else None

    @property
    def open_count(self) -> int:
        return sum(1 for o in self.orders.values() if o.occupies_side)
