"""Normalized, immutable event contracts for the deterministic core.

These are **internal** contracts, not venue payloads. Adapters (P6/P7) translate whatever a
feed sends into these; nothing here knows that a websocket or a REST endpoint exists.

Ordering contract
-----------------
``EventMeta.ingress_ordinal`` is the **total order**, and it is the only thing that defines
it. Timestamps are data, not order: two feeds do not share a timestamp domain, an exchange
timestamp can tie, and a slow feed can deliver an older timestamp later. Relying on
timestamps -- or worse, on Python's arrival order -- would make replay reproduce a different
sequence than production.

The ingress adapter assigns the ordinal, monotonically, as it merges feeds into one stream.
It is also responsible for handing over a **non-decreasing** timestamp: the reducer enforces
both, and fails closed if either is violated (``EventOrderError``). Together they mean a
recorded stream has exactly one legal interpretation.

Identity
--------
``EventMeta.event_id`` is a stable identity supplied by the adapter. The reducer uses it to
refuse to apply the same fill twice, because double-accounting a fill silently corrupts
every downstream figure (invariant I01). Venue-specific de-duplication is P6/P7 work; this
is only the deterministic mechanism that makes it expressible.

All events use ``frozen=True, slots=True``: immutable, cheap, and hashable-by-value where
their fields allow.
"""

from dataclasses import dataclass
from enum import Enum

from maker5m.accounting.ledger import Fill
from maker5m.domain import Outcome
from maker5m.market.btc_price import BtcPrice
from maker5m.market.errors import MarketDefinitionError
from maker5m.market.phase import Phase
from maker5m.market.timebase import TimestampNs
from maker5m.numeric.errors import DomainError
from maker5m.numeric.scales import PRICE_SCALE
from maker5m.numeric.units import PriceUnits, ShareUnits

__all__ = [
    "BookLevel",
    "BookUpdate",
    "Event",
    "EventMeta",
    "HealthComponent",
    "HealthEvent",
    "HealthStatus",
    "Liquidity",
    "OrderStateEvent",
    "OrderStatus",
    "OwnFill",
    "PhaseEvent",
    "SpotTick",
]


@dataclass(frozen=True, slots=True)
class EventMeta:
    """Metadata every event in the deterministic stream carries."""

    market_id: str
    event_id: str
    ingress_ordinal: int
    timestamp: TimestampNs

    def __post_init__(self) -> None:
        if not self.market_id:
            raise MarketDefinitionError("event market_id must not be empty")
        if not self.event_id:
            raise MarketDefinitionError("event_id must not be empty")
        if self.ingress_ordinal < 0:
            raise MarketDefinitionError(
                f"ingress_ordinal must not be negative, got {self.ingress_ordinal}"
            )
        if self.timestamp < 0:
            raise DomainError(f"event timestamp must not be negative, got {self.timestamp}")


# -- market data --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpotTick:
    """External BTC spot.

    Carried so the decision path can be woken by spot alone, independently of CLOB updates
    (invariant I11). P2 stores it; nothing computes a fair value from it here.
    """

    meta: EventMeta
    price: BtcPrice
    source_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class BookLevel:
    """One side's touch: a price and the size resting at it."""

    price: PriceUnits
    size: ShareUnits

    def __post_init__(self) -> None:
        if not 0 <= self.price <= PRICE_SCALE:
            raise DomainError(f"book price must lie in [0, 1], got {self.price}")
        if self.size < 0:
            raise DomainError(f"book size must not be negative, got {self.size}")


@dataclass(frozen=True, slots=True)
class BookUpdate:
    """Normalized top of book for both outcome tokens.

    Top of book only. Depth and queue reconstruction are P8 work and are deliberately absent.

    Both sides are carried rather than deriving DOWN from UP. Canonical section 5.2 states
    the DOWN book carries no independent information *if all levels map exactly* -- a
    conditional, empirical claim. Post-only safety is a hard invariant (I06) and P7 must
    check the real DOWN ask before submitting a BUY DOWN, so the observed value is recorded
    rather than inferred.

    ``sequence`` is the venue's own book version, used later for gap detection. It is not the
    ordering key; ``meta.ingress_ordinal`` is.
    """

    meta: EventMeta
    up_bid: BookLevel | None
    up_ask: BookLevel | None
    down_bid: BookLevel | None
    down_ask: BookLevel | None
    sequence: int | None = None

    def best(self, outcome: Outcome, side: str) -> BookLevel | None:
        """Look up one touch. ``side`` is ``"bid"`` or ``"ask"``."""
        if side not in ("bid", "ask"):
            raise DomainError(f"side must be 'bid' or 'ask', got {side!r}")
        if outcome is Outcome.UP:
            return self.up_bid if side == "bid" else self.up_ask
        return self.down_bid if side == "bid" else self.down_ask


# -- own execution ------------------------------------------------------------------------


class Liquidity(Enum):
    """Whether our side of a fill was the resting one.

    ``TAKER`` is recorded because an intentional taker fill is an execution bug, not an
    acceptable outcome (invariant I07). P2 records it; P9 acts on it.
    """

    MAKER = "MAKER"
    TAKER = "TAKER"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class OwnFill:
    """One of our own executions.

    Composes the accepted P1 :class:`~maker5m.accounting.ledger.Fill` rather than restating
    it, so there is exactly one authoritative fill representation in the project and the
    ledger transition takes it unchanged.
    """

    meta: EventMeta
    fill: Fill
    client_order_id: str | None = None
    venue_order_id: str | None = None
    liquidity: Liquidity = Liquidity.UNKNOWN


class OrderStatus(Enum):
    """Smallest stable internal order vocabulary.

    Venue-specific statuses are mapped onto these by the P6/P7 adapters; that mapping is
    deliberately not attempted here, because it cannot be written correctly without the real
    message set. ``UNKNOWN`` exists so an unmappable status is representable rather than
    guessed -- an unknown order state is a risk condition (Canonical section 28.1), not a
    value to invent.
    """

    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class OrderStateEvent:
    """A normalized order lifecycle change.

    State information only. No cancel/replace API, no submission, no execution behaviour --
    all of that is P7.
    """

    meta: EventMeta
    client_order_id: str
    status: OrderStatus
    outcome: Outcome | None = None
    price: PriceUnits | None = None
    remaining: ShareUnits | None = None
    venue_order_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.client_order_id:
            raise MarketDefinitionError("client_order_id must not be empty")
        if self.price is not None and not 0 <= self.price <= PRICE_SCALE:
            raise DomainError(f"order price must lie in [0, 1], got {self.price}")
        if self.remaining is not None and self.remaining < 0:
            raise DomainError(f"remaining must not be negative, got {self.remaining}")


# -- lifecycle and health -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PhaseEvent:
    """An explicit record of a phase boundary being crossed.

    The phase itself is always derived from the timestamp (see :mod:`maker5m.market.phase`).
    This event journals the transition and lets a quiet market still observe a boundary; the
    reducer validates it against the derived phase and rejects any disagreement.
    """

    meta: EventMeta
    phase: Phase


class HealthComponent(Enum):
    """Which stream a health report is about."""

    CLOB_BOOK = "CLOB_BOOK"
    SPOT_FEED = "SPOT_FEED"
    ORDER_STREAM = "ORDER_STREAM"


class HealthStatus(Enum):
    """Normalized stream health."""

    HEALTHY = "HEALTHY"
    STALE = "STALE"
    DISCONNECTED = "DISCONNECTED"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class HealthEvent:
    """A normalized health report from a feed or transport layer.

    P2 defines and reduces these deterministically. Detecting staleness, measuring gaps, and
    reacting to them are P6 and P9 work -- there are no timers here.
    """

    meta: EventMeta
    component: HealthComponent
    status: HealthStatus
    detail: str | None = None


Event = SpotTick | BookUpdate | OwnFill | OrderStateEvent | PhaseEvent | HealthEvent
"""The closed set of events the deterministic core accepts."""
