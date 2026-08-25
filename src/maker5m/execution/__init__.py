"""Order execution and reconciliation. Plane 1, with a pure Plane 2 diff. Built in P7.

See ``docs/INVARIANTS.md`` I06-I10 and ``docs/ARCHITECTURE_SSOT.md`` §4.

```text
DecisionResult -> prepare -> post-only guard -> reconcile -> rate limit -> transport
```

The three rules that shape everything here:

* **An unchanged valid order is KEPT.** Every cancellation destroys a queue timestamp, and
  fill rate collapses as depth-ahead grows (Canonical §10.1). There is no age-based,
  timer-based, or event-count-based replacement anywhere (I09, I10).
* **The venue adapter rejects an illegal intent; it never alters one.** Prices are passed
  through untouched — moving one changes queue position, which changes the strategy.
* **Post-only is not a setting.** ``OrderSide`` has one member, ``VenueOrderType`` has one
  member, and ``post_only`` is a constant ``True``. SELL, FOK, FAK, market orders, and a
  ``post_only=False`` retry are not representable (I06, I07).

Live trading stays hard-disabled: a real write adapter cannot be constructed while
``LIVE_TRADING_ENABLED`` is ``False``, and the check runs before any credential or socket is
touched. Mock transports remain freely usable, so the whole path is testable unarmed.
"""

from maker5m.execution.adapter import (
    SDK_DISTRIBUTION,
    SDK_IMPORT_NAME,
    SDK_PINNED_VERSION,
    OrderPlacement,
    RecordingTransport,
    VenueAdapter,
    VenueTransport,
    build_placement,
)
from maker5m.execution.credentials import ExecutionCredentials, Secret
from maker5m.execution.errors import (
    ExecutionError,
    LiveTradingDisabledError,
    OrderIdentityError,
    PreparationError,
)
from maker5m.execution.executor import (
    ExecutionCycle,
    Executor,
    prepare_both_sides,
)
from maker5m.execution.gate import live_trading_enabled, require_live_trading_enabled
from maker5m.execution.live_orders import LiveOrder, LiveOrderTable, OrderLifecycle
from maker5m.execution.prepare import PreparationOutcome, PreparedOrder, prepare_order
from maker5m.execution.rate_limit import (
    DEFAULT_CANCEL_RESERVE,
    DEFAULT_RATE_PER_SECOND,
    RATE_LIMIT_STATUS,
    RateDecision,
    RequestClass,
    TokenBucket,
)
from maker5m.execution.reconciler import (
    ReconcileAction,
    ReconcilePlan,
    SideAction,
    SideReason,
    reconcile,
)
from maker5m.execution.replacement import (
    REPLACEMENT_POLICY_STATUS,
    PendingReplacement,
    ReplacementPolicy,
    ReplacementTracker,
)
from maker5m.execution.telemetry import ExecutionRecord
from maker5m.execution.user_stream import (
    VENUE_STATUS_MAP,
    TakerFillViolation,
    normalize_order_update,
    normalize_trade,
)
from maker5m.execution.venue_order import (
    POST_ONLY,
    OrderSide,
    VenueOrderType,
    price_to_decimal,
    size_to_decimal,
)

__all__ = [
    "DEFAULT_CANCEL_RESERVE",
    "DEFAULT_RATE_PER_SECOND",
    "POST_ONLY",
    "RATE_LIMIT_STATUS",
    "REPLACEMENT_POLICY_STATUS",
    "SDK_DISTRIBUTION",
    "SDK_IMPORT_NAME",
    "SDK_PINNED_VERSION",
    "VENUE_STATUS_MAP",
    "ExecutionCredentials",
    "ExecutionCycle",
    "ExecutionError",
    "ExecutionRecord",
    "Executor",
    "LiveOrder",
    "LiveOrderTable",
    "LiveTradingDisabledError",
    "OrderIdentityError",
    "OrderLifecycle",
    "OrderPlacement",
    "OrderSide",
    "PendingReplacement",
    "PreparationError",
    "PreparationOutcome",
    "PreparedOrder",
    "RateDecision",
    "ReconcileAction",
    "ReconcilePlan",
    "RecordingTransport",
    "ReplacementPolicy",
    "ReplacementTracker",
    "RequestClass",
    "Secret",
    "SideAction",
    "SideReason",
    "TakerFillViolation",
    "TokenBucket",
    "VenueAdapter",
    "VenueOrderType",
    "VenueTransport",
    "build_placement",
    "live_trading_enabled",
    "normalize_order_update",
    "normalize_trade",
    "prepare_both_sides",
    "prepare_order",
    "price_to_decimal",
    "reconcile",
    "require_live_trading_enabled",
    "size_to_decimal",
]
