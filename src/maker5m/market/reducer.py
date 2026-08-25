"""Deterministic state transitions.

```text
new_state = reduce_event(old_state, event)
```

Pure by construction: no clock, no I/O, no randomness, no global state, no mutation of the
input state or the input event. The same state and the same event always produce exactly the
same result, which is what makes P5 replay reproduce production decisions bit for bit
(invariant I20).

Every event passes the same three ordering and identity checks before anything is applied:

1. **market association** -- an event for another market is a routing bug, not a no-op;
2. **ingress ordinal strictly increasing** -- the total order is explicit, never Python's
   arrival order;
3. **timestamp non-decreasing** -- the ingress adapter normalises timestamps as it merges
   feeds, so a decrease means the merge is broken.

All three fail closed. Silently normalising a corrupted stream would let authoritative state
drift from reality and would then be faithfully reproduced by replay, which is worse than
stopping.
"""

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from maker5m.accounting.ledger import LedgerState
from maker5m.market.errors import (
    DuplicateEventError,
    EventOrderError,
    InvalidPhaseTransitionError,
    WrongMarketError,
)
from maker5m.market.events import (
    BookUpdate,
    Event,
    EventMeta,
    HealthEvent,
    OrderStateEvent,
    OwnFill,
    PhaseEvent,
    SpotTick,
)
from maker5m.market.state import HealthState, MarketState, OrderRecord

__all__ = ["reduce_event", "reduce_events"]


def _check_common(state: MarketState, meta: EventMeta) -> None:
    if meta.market_id != state.market_id:
        raise WrongMarketError(
            f"event {meta.event_id!r} is for market {meta.market_id!r}, "
            f"state is market {state.market_id!r}"
        )
    if meta.ingress_ordinal <= state.last_ingress_ordinal:
        raise EventOrderError(
            f"ingress_ordinal must strictly increase: got {meta.ingress_ordinal} "
            f"after {state.last_ingress_ordinal} (event {meta.event_id!r})"
        )
    if meta.timestamp < state.last_event_timestamp:
        raise EventOrderError(
            f"timestamp must not decrease: got {meta.timestamp} after "
            f"{state.last_event_timestamp} (event {meta.event_id!r})"
        )


def reduce_event(state: MarketState, event: Event) -> MarketState:
    """Apply one event and return the resulting state.

    Ordered by expected event frequency: book updates dominate the stream, so they are
    tested first. This is dispatch by ``isinstance``, not by a registry or a generic event
    bus -- direct, allocation-free, and trivially followed when reading a replay.
    """
    meta = event.meta
    _check_common(state, meta)

    if isinstance(event, BookUpdate):
        return _advance(state, meta, book=event)
    if isinstance(event, SpotTick):
        return _advance(state, meta, spot=event)
    if isinstance(event, OwnFill):
        return _apply_own_fill(state, event)
    if isinstance(event, OrderStateEvent):
        return _apply_order_state(state, event)
    if isinstance(event, HealthEvent):
        return _advance(
            state, meta, health=state.health.with_component(event.component, event.status)
        )
    return _apply_phase(state, event)


def reduce_events(state: MarketState, events: Iterable[Event]) -> MarketState:
    """Fold a sequence of events. Any rejection propagates; nothing is partially applied."""
    for event in events:
        state = reduce_event(state, event)
    return state


# -- per-event application ----------------------------------------------------------------


def _apply_own_fill(state: MarketState, event: OwnFill) -> MarketState:
    """Apply a fill to the P1 ledger exactly once.

    Re-delivery is rejected rather than ignored. Double-accounting a fill silently corrupts
    every downstream figure (invariant I01), and quietly swallowing a repeat would hide a
    broken ingress path. De-duplicating a venue that legitimately re-sends is P6/P7 work; the
    identity mechanism it needs is ``EventMeta.event_id``, enforced here.
    """
    if event.meta.event_id in state.applied_fill_ids:
        raise DuplicateEventError(
            f"fill {event.meta.event_id!r} has already been applied to market "
            f"{state.market_id!r}; applying it again would double-account"
        )
    return _advance(
        state,
        event.meta,
        ledger=state.ledger.apply_fill(event.fill),
        applied_fill_ids=state.applied_fill_ids | {event.meta.event_id},
    )


def _apply_order_state(state: MarketState, event: OrderStateEvent) -> MarketState:
    record = OrderRecord(
        client_order_id=event.client_order_id,
        status=event.status,
        outcome=event.outcome,
        price=event.price,
        remaining=event.remaining,
        venue_order_id=event.venue_order_id,
        reason=event.reason,
        last_ingress_ordinal=event.meta.ingress_ordinal,
    )
    orders = dict(state.orders)
    orders[event.client_order_id] = record
    return _advance(state, event.meta, orders=MappingProxyType(orders))


def _apply_phase(state: MarketState, event: PhaseEvent) -> MarketState:
    """Validate a phase transition against the phase the timestamp implies.

    The phase is always derived (:mod:`maker5m.market.phase`); this event only journals the
    boundary. A ``PhaseEvent`` claiming a phase its own timestamp does not imply is a bug in
    whatever produced it, and is rejected rather than allowed to define a phase the clock
    disagrees with.
    """
    expected = state.phase_at_timestamp(event.meta.timestamp)
    if event.phase is not expected:
        raise InvalidPhaseTransitionError(
            f"phase event claims {event.phase.name} at {event.meta.timestamp}, "
            f"but that timestamp is {expected.name} for market {state.market_id!r}"
        )
    return _advance(state, event.meta)


# -- state construction -------------------------------------------------------------------


def _advance(
    state: MarketState,
    meta: EventMeta,
    *,
    book: BookUpdate | None = None,
    spot: SpotTick | None = None,
    ledger: LedgerState | None = None,
    orders: Mapping[str, OrderRecord] | None = None,
    health: HealthState | None = None,
    applied_fill_ids: frozenset[str] | None = None,
) -> MarketState:
    """Build the next state, always advancing the ordering fields.

    Every transition goes through here, so no event can update payload state while forgetting
    to move the clock or the ordinal. ``None`` means "unchanged": no transition ever clears a
    field back to ``None``, so the sentinel is unambiguous.
    """
    return MarketState(
        definition=state.definition,
        last_event_timestamp=meta.timestamp,
        last_ingress_ordinal=meta.ingress_ordinal,
        book=state.book if book is None else book,
        spot=state.spot if spot is None else spot,
        ledger=state.ledger if ledger is None else ledger,
        orders=state.orders if orders is None else orders,
        health=state.health if health is None else health,
        applied_fill_ids=(state.applied_fill_ids if applied_fill_ids is None else applied_fill_ids),
        resolution=state.resolution,
    )
