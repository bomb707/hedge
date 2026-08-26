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
from maker5m.persistence.archive import (
    ARCHIVE_SUFFIX,
    ArchiveIdentity,
    ArchiveResult,
    ArchiveVerificationError,
    archive_store,
    open_verified_archive,
    restore_store,
    verify_archive,
)
from maker5m.persistence.capture import (
    DEFAULT_FILL_CAPACITY,
    DEFAULT_RISK_CAPACITY,
    BoundedChannel,
    FillCapture,
)
from maker5m.persistence.records import (
    Liquidity,
    MarketIdentity,
    build_decision_record,
    build_fill_record,
)
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
    "ARCHIVE_SUFFIX",
    "DECISION_SCHEMA_VERSION",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_FILL_CAPACITY",
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_RISK_CAPACITY",
    "FILL_SCHEMA_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "METRICS_SCHEMA_VERSION",
    "RISK_ROW_SCHEMA_VERSION",
    "SETTLEMENT_ROW_SCHEMA_VERSION",
    "STORE_SCHEMA_VERSION",
    "ArchiveIdentity",
    "ArchiveResult",
    "ArchiveVerificationError",
    "BoundedChannel",
    "DecisionRecord",
    "ExactRatio",
    "FillCapture",
    "FillProvenance",
    "FillRecord",
    "Liquidity",
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
    "archive_store",
    "build_decision_record",
    "build_fill_record",
    "database_digest",
    "open_for_read",
    "open_verified_archive",
    "read_manifest",
    "restore_store",
    "risk_row",
    "settlement_row",
    "verify_archive",
    "verify_store",
]
