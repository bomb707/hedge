"""The durable sink. SQLite, one owner, Plane 3 only.

Why SQLite and not something else: it is in the standard library, it stores exact integers and
text without a serialization layer, it already solves crash-safety, it is queryable by P12 and
P15 later without inventing a format, and it adds no service to operate. A JSON-lines file would
be simpler to write and worse to read; anything networked would put a socket between telemetry
and disk, which is the direction this package exists to avoid.

**One thread owns the connection.** Plane 1 never touches `sqlite3` in any form — no connection,
no cursor, no commit, no checkpoint, no fsync. That is not a style preference: `commit()` fsyncs,
and an fsync on the trading path is a stall measured in milliseconds against a decision budget
measured in microseconds.

**Errors stop here.** Every database exception is caught, counted, and dropped. A failing disk is
an observability incident; a trading loop that raises because its telemetry could not be written
is a trading incident, and the second is much worse than the first.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Final

from maker5m.persistence.schema import (
    MANIFEST_SCHEMA_VERSION,
    STORE_SCHEMA_VERSION,
    DecisionRecord,
    FillRecord,
    Manifest,
    MarketMetrics,
    RiskRow,
    SettlementRow,
)

__all__ = ["DEFAULT_BATCH_SIZE", "SchemaVersionError", "TelemetryStore"]

DEFAULT_BATCH_SIZE: Final[int] = 500
"""Rows per transaction.

A transaction per row would fsync per row; a transaction per market would risk the whole market
on one crash. Five hundred is roughly a second of a busy real market (measured peak ~1,100
decisions/second) and keeps the worst-case loss to about that."""

SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS markets (
    market_id TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    condition_id TEXT,
    provenance TEXT NOT NULL,
    manifest_json TEXT,
    closed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS decisions (
    persistence_sequence INTEGER PRIMARY KEY,
    market_id TEXT NOT NULL,
    ingress_ordinal INTEGER NOT NULL,
    capture_sequence INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_market ON decisions(market_id, ingress_ordinal);
CREATE TABLE IF NOT EXISTS fills (
    persistence_sequence INTEGER PRIMARY KEY,
    market_id TEXT NOT NULL,
    ingress_ordinal INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    provenance TEXT NOT NULL,
    liquidity TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fills_market ON fills(market_id, ingress_ordinal);
CREATE TABLE IF NOT EXISTS risk_records (
    persistence_sequence INTEGER PRIMARY KEY,
    market_id TEXT NOT NULL,
    risk_sequence INTEGER NOT NULL,
    as_of_ingress_ordinal INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS risk_order ON risk_records(market_id, risk_sequence);
CREATE TABLE IF NOT EXISTS settlements (
    persistence_sequence INTEGER PRIMARY KEY,
    market_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    resolution_state TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS market_metrics (
    market_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    payload TEXT NOT NULL
);
"""


class SchemaVersionError(RuntimeError):
    """A database this build cannot honestly read.

    Raised on the *read* side only. Guessing at an unknown layout is how a silently wrong number
    ends up in an analysis, so an unrecognised version refuses rather than tries.
    """


def _payload(record: object) -> str:
    """A record's full field set, as canonical JSON text.

    Columns exist for what is queried; the payload keeps everything else so a field added later
    does not require a migration to have been readable earlier. `json` is used rather than
    `repr` because it round-trips, and `sort_keys` because a stable byte order makes the file
    hash meaningful.
    """

    if is_dataclass(record) and not isinstance(record, type):
        data = asdict(record)
    else:  # pragma: no cover - every caller passes a dataclass
        data = dict(vars(record))
    return json.dumps(_jsonable(data), sort_keys=True, separators=(",", ":"))


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, bool | int | str) or value is None:
        return value
    return str(value)


@dataclass(slots=True)
class TelemetryStore:
    """Batched, crash-tolerant writer. Owned by exactly one Plane-3 thread."""

    path: Path
    batch_size: int = DEFAULT_BATCH_SIZE
    _connection: sqlite3.Connection | None = field(default=None, repr=False)
    _pending: int = 0
    sink_errors: int = 0
    rows_written: int = 0
    batches: int = 0
    transaction_ns: int = 0
    """Total time spent inside commits. An OPERATIONAL measurement, not a budget."""

    def open(self) -> None:
        connection = sqlite3.connect(str(self.path), isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.executescript(SCHEMA)
        connection.execute(f"PRAGMA user_version={STORE_SCHEMA_VERSION}")
        connection.execute("BEGIN")
        self._connection = connection

    def close(self) -> None:
        if self._connection is None:
            return
        try:
            self._flush()
            self._connection.close()
        except sqlite3.Error:
            self.sink_errors += 1
        finally:
            self._connection = None

    # -- writing ---------------------------------------------------------------------------

    def _execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
        connection = self._connection
        if connection is None:
            self.sink_errors += 1
            return
        try:
            connection.execute(statement, parameters)
        except sqlite3.Error:
            self.sink_errors += 1
            return
        self.rows_written += 1
        self._pending += 1
        if self._pending >= self.batch_size:
            self._flush()

    def _flush(self) -> None:
        connection = self._connection
        if connection is None or self._pending == 0:
            return
        started = perf_counter_ns()
        try:
            connection.execute("COMMIT")
            connection.execute("BEGIN")
        except sqlite3.Error:
            self.sink_errors += 1
        self.transaction_ns += perf_counter_ns() - started
        self.batches += 1
        self._pending = 0

    def flush(self) -> None:
        """Commit whatever is pending. Called by the worker, never by Plane 1."""
        self._flush()

    def register_market(
        self, *, market_id: str, slug: str, condition_id: str | None, provenance: str
    ) -> None:
        self._execute(
            "INSERT OR REPLACE INTO markets"
            " (market_id, slug, condition_id, provenance, manifest_json, closed)"
            " VALUES (?, ?, ?, ?,"
            " (SELECT manifest_json FROM markets WHERE market_id = ?), 0)",
            (market_id, slug, condition_id, provenance, market_id),
        )

    def write_decision(self, record: DecisionRecord) -> None:
        self._execute(
            "INSERT OR REPLACE INTO decisions (persistence_sequence, market_id,"
            " ingress_ordinal, capture_sequence, event_id, event_kind, schema_version, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.persistence_sequence,
                record.market_id,
                record.ingress_ordinal,
                record.capture_sequence,
                record.event_id,
                record.event_kind,
                record.schema_version,
                _payload(record),
            ),
        )

    def write_fill(self, record: FillRecord) -> None:
        self._execute(
            "INSERT OR REPLACE INTO fills (persistence_sequence, market_id, ingress_ordinal,"
            " event_id, provenance, liquidity, schema_version, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.persistence_sequence,
                record.market_id,
                record.ingress_ordinal,
                record.event_id,
                record.provenance,
                record.liquidity,
                record.schema_version,
                _payload(record),
            ),
        )

    def write_risk(self, record: RiskRow) -> None:
        self._execute(
            "INSERT OR REPLACE INTO risk_records (persistence_sequence, market_id,"
            " risk_sequence, as_of_ingress_ordinal, schema_version, payload)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                record.persistence_sequence,
                record.market_id,
                record.risk_sequence,
                record.as_of_ingress_ordinal,
                record.schema_version,
                _payload(record),
            ),
        )

    def write_settlement(self, record: SettlementRow) -> None:
        self._execute(
            "INSERT OR REPLACE INTO settlements (persistence_sequence, market_id, condition_id,"
            " resolution_state, schema_version, payload) VALUES (?, ?, ?, ?, ?, ?)",
            (
                record.persistence_sequence,
                record.market_id,
                record.condition_id,
                record.resolution_state,
                record.schema_version,
                _payload(record),
            ),
        )

    def write_metrics(self, metrics: MarketMetrics) -> None:
        self._execute(
            "INSERT OR REPLACE INTO market_metrics (market_id, schema_version, payload)"
            " VALUES (?, ?, ?)",
            (metrics.market_id, metrics.schema_version, _payload(metrics)),
        )

    def write_manifest(self, manifest: Manifest) -> None:
        """Record the manifest and mark the market closed.

        Closing is the *last* write on purpose. A crash before this leaves `closed = 0`, and the
        verifier reports the market INCOMPLETE — which is the truth, and is what stops a partial
        market from ever being mistaken for a whole one.
        """
        self._execute(
            "UPDATE markets SET manifest_json = ?, closed = ? WHERE market_id = ?",
            (_payload(manifest), int(manifest.closed), manifest.market_id),
        )
        self._flush()


def database_digest(path: Path) -> tuple[int, str]:
    """Size and SHA-256 of a closed database file, for the manifest.

    Taken after the writer has finished, so it describes a file nothing is still changing.
    """
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def open_for_read(path: Path) -> sqlite3.Connection:
    """Open an existing store, refusing a schema this build does not understand."""
    if not path.exists():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != STORE_SCHEMA_VERSION:
        connection.close()
        raise SchemaVersionError(
            f"store schema {version} is not {STORE_SCHEMA_VERSION}; refusing to guess at a "
            "layout this build does not define"
        )
    return connection


MANIFEST_VERSION_IN_USE: Final[int] = MANIFEST_SCHEMA_VERSION
