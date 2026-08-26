"""Durable telemetry persistence and post-market analytics. Plane 3, built in P11.

Nothing in this package runs on the trading path. The hot side captures references into a
bounded, non-blocking buffer and returns; everything here — draining, projection, serialization,
SQL, hashing, metrics — happens on a background thread that Plane 1 never waits for and never
hears from.

The contract in one line: **persistence can fail without stopping trading.** A stalled disk, a
broken database, a bug in this package: all of them cost telemetry and none of them cost a
decision cycle. What they do cost is the right to call the market's telemetry complete, which is
recorded in the manifest and enforced by the verifier.
"""

from maker5m.persistence.analytics import MetricsAccumulator, risk_row, settlement_row
from maker5m.persistence.records import MarketIdentity, build_decision_record
from maker5m.persistence.schema import (
    DECISION_SCHEMA_VERSION,
    FILL_SCHEMA_VERSION,
    MANIFEST_SCHEMA_VERSION,
    METRICS_SCHEMA_VERSION,
    RISK_ROW_SCHEMA_VERSION,
    SETTLEMENT_ROW_SCHEMA_VERSION,
    STORE_SCHEMA_VERSION,
    DecisionRecord,
    ExactRatio,
    FillProvenance,
    FillRecord,
    Manifest,
    MarketMetrics,
    RiskRow,
    SettlementRow,
    SideRecord,
    TelemetryProvenance,
)
from maker5m.persistence.store import (
    DEFAULT_BATCH_SIZE,
    SchemaVersionError,
    TelemetryStore,
    database_digest,
    open_for_read,
)
from maker5m.persistence.verify import (
    VerificationResult,
    VerificationStatus,
    read_manifest,
    verify_store,
)
from maker5m.persistence.worker import (
    DEFAULT_POLL_SECONDS,
    PersistenceWorker,
    WorkerStats,
)

__all__ = [
    "DECISION_SCHEMA_VERSION",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_POLL_SECONDS",
    "FILL_SCHEMA_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "METRICS_SCHEMA_VERSION",
    "RISK_ROW_SCHEMA_VERSION",
    "SETTLEMENT_ROW_SCHEMA_VERSION",
    "STORE_SCHEMA_VERSION",
    "DecisionRecord",
    "ExactRatio",
    "FillProvenance",
    "FillRecord",
    "Manifest",
    "MarketIdentity",
    "MarketMetrics",
    "MetricsAccumulator",
    "PersistenceWorker",
    "RiskRow",
    "SchemaVersionError",
    "SettlementRow",
    "SideRecord",
    "TelemetryProvenance",
    "TelemetryStore",
    "VerificationResult",
    "VerificationStatus",
    "WorkerStats",
    "build_decision_record",
    "database_digest",
    "open_for_read",
    "read_manifest",
    "risk_row",
    "settlement_row",
    "verify_store",
]
