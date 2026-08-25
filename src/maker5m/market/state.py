"""The authoritative per-market state.

Single ownership: only the hot-execution plane holds a ``MarketState``, and it never shares
a mutable reference. The object is a frozen dataclass and every transition returns a new
one, so "shared mutable state" is not merely discouraged, it is unrepresentable
(``docs/ARCHITECTURE_SSOT.md`` section 5).

The field set is scoped to what P2 owns. Canonical section 24.1 identity and lifecycle,
24.2 *observed* market data, and 24.3 accounting are all here. Canonical 24.2's derived
values (quote centre, sigma, fair-value confidence), all of 24.4 strategy state, and all of
24.5 execution detail are deliberately absent: they belong to P3, P4, and P7, and carrying
unused nullable placeholders for them would invite premature strategy coupling.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from maker5m.accounting.ledger import LedgerState
from maker5m.domain import Outcome
from maker5m.market.btc_price import BtcPrice
from maker5m.market.errors import MarketDefinitionError
from maker5m.market.events import (
    BookUpdate,
    HealthComponent,
    HealthStatus,
    OrderStatus,
    SpotTick,
)
from maker5m.market.phase import Phase, PhaseConfig, phase_at
from maker5m.market.timebase import TimestampNs
from maker5m.numeric.errors import DomainError
from maker5m.numeric.units import PriceUnits, ShareUnits

__all__ = ["EMPTY_ORDERS", "HealthState", "MarketDefinition", "MarketState", "OrderRecord"]


@dataclass(frozen=True, slots=True)
class MarketDefinition:
    """Immutable identity and lifecycle configuration of one market.

    Fixed for the market's whole life. Separating it from the mutable-by-transition state
    means a reducer cannot accidentally rewrite a market's identity.
    """

    market_id: str
    slug: str
    up_token_id: str
    down_token_id: str
    t0: TimestampNs
    phase_config: PhaseConfig
    tick: PriceUnits
    strike: BtcPrice | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("market_id", self.market_id),
            ("slug", self.slug),
            ("up_token_id", self.up_token_id),
            ("down_token_id", self.down_token_id),
        ):
            if not value:
                raise MarketDefinitionError(f"{name} must not be empty")
        if self.up_token_id == self.down_token_id:
            raise MarketDefinitionError(
                f"up and down token ids must differ, both are {self.up_token_id!r}"
            )
        if self.t0 < 0:
            raise DomainError(f"t0 must not be negative, got {self.t0}")
        if self.tick <= 0:
            raise MarketDefinitionError(f"tick must be positive, got {self.tick}")

    @property
    def market_end(self) -> TimestampNs:
        """``T0 + duration``. The window close (Canonical section 6.1)."""
        return TimestampNs(self.t0 + self.phase_config.duration)

    def token_id(self, outcome: Outcome) -> str:
        return self.up_token_id if outcome is Outcome.UP else self.down_token_id

    def outcome_of(self, token_id: str) -> Outcome:
        """Map a token id back to its outcome. Raises on a token from another market."""
        if token_id == self.up_token_id:
            return Outcome.UP
        if token_id == self.down_token_id:
            return Outcome.DOWN
        raise MarketDefinitionError(
            f"token {token_id!r} belongs to neither side of market {self.market_id!r}"
        )


@dataclass(frozen=True, slots=True)
class OrderRecord:
    """Latest known normalized state of one of our orders.

    Enough for P7 to reconcile against; nothing more. No queue position (P8), no submission
    or cancel behaviour (P7).
    """

    client_order_id: str
    status: OrderStatus
    outcome: Outcome | None = None
    price: PriceUnits | None = None
    remaining: ShareUnits | None = None
    venue_order_id: str | None = None
    reason: str | None = None
    last_ingress_ordinal: int = -1


@dataclass(frozen=True, slots=True)
class HealthState:
    """Latest normalized health of each stream.

    A fixed small set of typed fields rather than a mapping: the components are known, and a
    typed field cannot be misspelled at a call site.
    """

    clob_book: HealthStatus = HealthStatus.UNKNOWN
    spot_feed: HealthStatus = HealthStatus.UNKNOWN
    order_stream: HealthStatus = HealthStatus.UNKNOWN

    def with_component(self, component: HealthComponent, status: HealthStatus) -> "HealthState":
        if component is HealthComponent.CLOB_BOOK:
            return HealthState(status, self.spot_feed, self.order_stream)
        if component is HealthComponent.SPOT_FEED:
            return HealthState(self.clob_book, status, self.order_stream)
        return HealthState(self.clob_book, self.spot_feed, status)

    def status_of(self, component: HealthComponent) -> HealthStatus:
        if component is HealthComponent.CLOB_BOOK:
            return self.clob_book
        if component is HealthComponent.SPOT_FEED:
            return self.spot_feed
        return self.order_stream

    @property
    def all_healthy(self) -> bool:
        return (
            self.clob_book is HealthStatus.HEALTHY
            and self.spot_feed is HealthStatus.HEALTHY
            and self.order_stream is HealthStatus.HEALTHY
        )


EMPTY_ORDERS: Mapping[str, OrderRecord] = MappingProxyType({})
"""Shared empty order map. Read-only, so sharing one instance is safe."""


@dataclass(frozen=True, slots=True)
class MarketState:
    """Authoritative state of one market at one point in the event stream."""

    definition: MarketDefinition
    last_event_timestamp: TimestampNs
    last_ingress_ordinal: int = -1
    book: BookUpdate | None = None
    spot: SpotTick | None = None
    ledger: LedgerState = field(default_factory=LedgerState)
    orders: Mapping[str, OrderRecord] = EMPTY_ORDERS
    health: HealthState = field(default_factory=HealthState)
    applied_fill_ids: frozenset[str] = frozenset()
    resolution: Outcome | None = None

    @classmethod
    def initial(cls, definition: MarketDefinition) -> "MarketState":
        """A fresh market: flat inventory, nothing observed, clock parked at ``T0``.

        ``last_ingress_ordinal`` starts at ``-1`` so that ordinal ``0`` is acceptable, and
        ``last_event_timestamp`` starts at ``T0`` so the derived phase is ``PREARM``.
        """
        return cls(definition=definition, last_event_timestamp=definition.t0)

    # -- derived ------------------------------------------------------------------------

    @property
    def market_id(self) -> str:
        return self.definition.market_id

    @property
    def phase(self) -> Phase:
        """The phase implied by the latest event's timestamp.

        Derived, never stored, so it cannot disagree with the event stream. See
        :mod:`maker5m.market.phase`.
        """
        return phase_at(self.definition.t0, self.last_event_timestamp, self.definition.phase_config)

    def phase_at_timestamp(self, at: TimestampNs) -> Phase:
        """The phase that would apply at an arbitrary timestamp for this market."""
        return phase_at(self.definition.t0, at, self.definition.phase_config)

    @property
    def net_inventory(self) -> ShareUnits:
        """``I = n_up - n_down`` (invariant I02), from the authoritative ledger."""
        return self.ledger.net_inventory
