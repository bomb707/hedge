"""Operational risk, health, and recovery (Canonical §28, §28.1; Detailed §38).

The bot refuses to create new risk when it cannot trust its own state. Nothing in this package
is a trading decision: there is no SELL, no hedge, no flatten, no merge, no split, no convert,
and no directional rescue. A halt withdraws quotes and holds the balances exactly as they are.
"""

from maker5m.risk.config import (
    DEFAULT_API_ERROR_THRESHOLD,
    DEFAULT_API_ERROR_WINDOW,
    DEFAULT_CLOCK_DRIFT_LIMIT,
    RISK_CONFIG_STATUS,
    RiskConfig,
)
from maker5m.risk.engine import (
    RiskDecision,
    RiskEngine,
    RiskInputs,
    RiskSnapshot,
    active_reasons,
    evaluate,
)
from maker5m.risk.monitors import ApiErrorMonitor, clock_drift_exceeded
from maker5m.risk.overlay import EMPTY_INTENT, risk_adjust
from maker5m.risk.reasons import REQUIRES_RECONCILIATION, RiskReason, RiskState
from maker5m.risk.reconciliation import (
    CostLedgerCheck,
    OrderFinding,
    OrderReconciliation,
    PositionCheck,
    VenueExecution,
    VenueOpenOrder,
    VenuePosition,
    check_cost_ledger,
    check_position,
    reconcile_orders,
)
from maker5m.risk.replay import (
    RiskDivergenceError,
    RiskReplayOutcome,
    verify_risk_replay,
)
from maker5m.risk.trace import (
    DEFAULT_RISK_TRACE_CAPACITY,
    RISK_SCHEMA_VERSION,
    HealthFrame,
    OperationalState,
    RiskController,
    RiskOrderError,
    RiskProvenance,
    RiskRecord,
    RiskSignal,
    RiskSignalKind,
    RiskTrace,
)

__all__ = [
    "DEFAULT_API_ERROR_THRESHOLD",
    "DEFAULT_API_ERROR_WINDOW",
    "DEFAULT_CLOCK_DRIFT_LIMIT",
    "DEFAULT_RISK_TRACE_CAPACITY",
    "EMPTY_INTENT",
    "REQUIRES_RECONCILIATION",
    "RISK_CONFIG_STATUS",
    "RISK_SCHEMA_VERSION",
    "ApiErrorMonitor",
    "CostLedgerCheck",
    "HealthFrame",
    "OperationalState",
    "OrderFinding",
    "OrderReconciliation",
    "PositionCheck",
    "RiskConfig",
    "RiskController",
    "RiskDecision",
    "RiskDivergenceError",
    "RiskEngine",
    "RiskInputs",
    "RiskOrderError",
    "RiskProvenance",
    "RiskReason",
    "RiskRecord",
    "RiskReplayOutcome",
    "RiskSignal",
    "RiskSignalKind",
    "RiskSnapshot",
    "RiskState",
    "RiskTrace",
    "VenueExecution",
    "VenueOpenOrder",
    "VenuePosition",
    "active_reasons",
    "check_cost_ledger",
    "check_position",
    "clock_drift_exceeded",
    "evaluate",
    "reconcile_orders",
    "risk_adjust",
    "verify_risk_replay",
]
