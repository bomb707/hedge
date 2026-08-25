"""Authoritative market state, event contracts, and phase machine. Plane 1/2. Built in P2.

See ``docs/ARCHITECTURE_SSOT.md`` sections 3 and 5.

Holds the normalized event contracts, the single-owner :class:`MarketState`, the deterministic
reducer, the frozen :class:`MarketSnapshot` published to Plane 3, and the phase machine
``PREARM -> QUOTE -> ENDGAME -> SETTLING -> DONE``.

Time is an event field, never an ambient clock read - that is what makes replay exact
(invariant I20). The phase is derived from the event timestamp and never stored, so it cannot
drift out of agreement with the stream.

Nothing here decides anything: no quote centre, no sizing, no order construction. Those are
P3, P4, and P7.
"""

from maker5m.domain import Outcome
from maker5m.market.btc_price import BtcPrice
from maker5m.market.errors import (
    DuplicateEventError,
    EventOrderError,
    InvalidPhaseTransitionError,
    MarketDefinitionError,
    MarketStateError,
    WrongMarketError,
)
from maker5m.market.events import (
    BookLevel,
    BookUpdate,
    Event,
    EventMeta,
    HealthComponent,
    HealthEvent,
    HealthStatus,
    Liquidity,
    OrderStateEvent,
    OrderStatus,
    OwnFill,
    PhaseEvent,
    SpotTick,
)
from maker5m.market.phase import CANONICAL_PHASE_CONFIG, Phase, PhaseConfig, phase_at
from maker5m.market.reducer import reduce_event, reduce_events
from maker5m.market.snapshot import MarketSnapshot, snapshot
from maker5m.market.state import (
    EMPTY_ORDERS,
    HealthState,
    MarketDefinition,
    MarketState,
    OrderRecord,
)
from maker5m.market.timebase import (
    NANOS_PER_MICRO,
    NANOS_PER_MILLI,
    NANOS_PER_SECOND,
    DurationNs,
    TimestampNs,
    millis,
    seconds,
)

__all__ = [
    "CANONICAL_PHASE_CONFIG",
    "EMPTY_ORDERS",
    "NANOS_PER_MICRO",
    "NANOS_PER_MILLI",
    "NANOS_PER_SECOND",
    "BookLevel",
    "BookUpdate",
    "BtcPrice",
    "DuplicateEventError",
    "DurationNs",
    "Event",
    "EventMeta",
    "EventOrderError",
    "HealthComponent",
    "HealthEvent",
    "HealthState",
    "HealthStatus",
    "InvalidPhaseTransitionError",
    "Liquidity",
    "MarketDefinition",
    "MarketDefinitionError",
    "MarketSnapshot",
    "MarketState",
    "MarketStateError",
    "OrderRecord",
    "OrderStateEvent",
    "OrderStatus",
    "Outcome",
    "OwnFill",
    "Phase",
    "PhaseConfig",
    "PhaseEvent",
    "SpotTick",
    "TimestampNs",
    "WrongMarketError",
    "millis",
    "phase_at",
    "reduce_event",
    "reduce_events",
    "seconds",
    "snapshot",
]
