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
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from time import perf_counter_ns
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:  # pragma: no cover - typing only
    from _typeshed import DataclassInstance

from maker5m.persistence.schema import (
    MANIFEST_SCHEMA_VERSION,
    STORE_SCHEMA_VERSION,
    SUPPORTED_STORE_SCHEMA_VERSIONS,
    ControlAuditRow,
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
-- APPEND-ONLY: decisions, fills, risk_records, settlements, control_audit, persistence_log.
--   Once a row exists at an identity it is evidence, and evidence is not rewritten. These are
--   written with a plain INSERT so a second write at the same identity raises IntegrityError,
--   is counted as a sink error, and leaves the original row exactly as it was.
--
-- FINAL/METADATA: markets, market_metrics.
--   Genuinely mutable by contract. `markets` is registered when the run starts and updated once
--   at close with the manifest; `market_metrics` is a single derived summary per market. Both
--   describe the market rather than recording an event in it, and both are written exactly once
--   more than they are created, so an upsert is the honest shape.
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
CREATE TABLE IF NOT EXISTS persistence_log (
    persistence_sequence INTEGER PRIMARY KEY,
    market_id TEXT NOT NULL,
    record_type TEXT NOT NULL,
    record_key TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS persistence_log_market ON persistence_log(market_id);
CREATE TABLE IF NOT EXISTS control_audit (
    persistence_sequence INTEGER PRIMARY KEY,
    market_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    risk_sequence INTEGER,
    schema_version INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS control_audit_command
    ON control_audit(market_id, command_id);
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


_FIELD_CACHE: dict[type, tuple[tuple[str, ...], ...]] = {}


def _payload(record: DataclassInstance) -> str:
    """A record's full field set, as JSON text. Columns carry what is queried; this carries
    the rest, so a field added later is still readable from an older file without a migration.

    Written by hand rather than with `dataclasses.asdict` because this runs once per record on
    the writer thread, and the writer thread holds the GIL while it runs. `asdict` recurses and
    deep-copies every value; combined with a recursive JSON pre-pass and `sort_keys`, one
    decision record cost **92 microseconds** to encode, which showed up as a measurable
    regression in the trading path's own p50. The flat walk below is one level deep — the only
    nested values in these schemas are `SideRecord` and `ExactRatio` — and costs **26**.

    `sort_keys` is gone with it. Field order comes from the dataclass definition and is stable
    for a given build, so the output was already deterministic; sorting it again cost 10
    microseconds per record to re-establish a property it already had.
    """
    cls = type(record)
    names = _FIELD_CACHE.get(cls)
    if names is None:
        names = tuple((f.name,) for f in fields(record))
        _FIELD_CACHE[cls] = names
    out: dict[str, Any] = {}
    for (name,) in names:
        value = getattr(record, name)
        kind = type(value)
        if value is None or kind is int or kind is str or kind is bool:
            out[name] = value
        elif is_dataclass(value) and not isinstance(value, type):
            out[name] = {f.name: _scalar(getattr(value, f.name)) for f in fields(value)}
        elif kind is tuple or kind is list:
            out[name] = [_scalar(item) for item in value]
        else:
            out[name] = str(value)
    return json.dumps(out, separators=(",", ":"))


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, tuple | list):
        return [_scalar(item) for item in value]
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

    writes_after_close: int = 0
    duplicate_writes: int = 0
    """Writes refused because an event-like row already existed at that identity."""
    error_samples: list[str] = field(default_factory=list)
    """A few distinct database failures, kept because a bare count names nothing.

    Bounded so a persistently broken store cannot turn its own failure into a memory leak."""

    def open(self) -> None:
        """Open for writing, refusing a store this build does not define.

        The version is read *before* anything is created or stamped. An earlier version ran the
        schema script and then set `user_version` unconditionally, so opening a future store for
        writing would quietly downgrade its metadata and half-apply this build's tables to it —
        the write side has to fail closed for the same reason the read side does.
        """
        existing = self.path.exists() and self.path.stat().st_size > 0
        connection = sqlite3.connect(str(self.path), isolation_level=None)
        if existing:
            # Reading accepts older versions; *writing* does not. Opening a V2 store for writing
            # would run this build's schema script against it and add a table it never had,
            # which is a schema change without a version change.
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != STORE_SCHEMA_VERSION:
                connection.close()
                raise SchemaVersionError(
                    f"store at {self.path} declares schema {version}, this build writes "
                    f"{STORE_SCHEMA_VERSION}; refusing to open it for writing rather than "
                    "changing a file it does not understand"
                )
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.executescript(SCHEMA)
        if not existing:
            connection.execute(f"PRAGMA user_version={STORE_SCHEMA_VERSION}")
        connection.execute("BEGIN")
        self._connection = connection

    def close(self) -> None:
        if self._connection is None:
            return
        try:
            self._flush()
            self._connection.close()
        except sqlite3.Error as error:
            self._record_error(error)
        finally:
            self._connection = None

    # -- writing ---------------------------------------------------------------------------

    def _execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
        connection = self._connection
        if connection is None:
            self._record_closed()
            return
        try:
            connection.execute(statement, parameters)
        except sqlite3.IntegrityError as error:
            # A second write at an identity that already holds a record. The first one stands:
            # audit evidence is append-only, and an upsert here would let a later write silently
            # replace what actually happened. Counted, so the market cannot verify complete.
            self._record_error(error)
            self.duplicate_writes += 1
            return
        except sqlite3.Error as error:
            self._record_error(error)
            return
        self.rows_written += 1
        self._pending += 1
        if self._pending >= self.batch_size:
            self._flush()

    def _record_closed(self) -> None:
        """A write that arrived when there was no connection to take it.

        Its own case rather than a bare counter: "the store was closed" and "the disk refused
        the row" are different faults, and a count that cannot tell them apart sends whoever
        reads it looking in the wrong place.
        """
        self.sink_errors += 1
        self.writes_after_close += 1
        description = "StoreClosed: a record arrived with no open connection"
        if description not in self.error_samples and len(self.error_samples) < 8:
            self.error_samples.append(description)

    def _record_error(self, error: sqlite3.Error) -> None:
        self.sink_errors += 1
        description = f"{type(error).__name__}: {error}"
        if description not in self.error_samples and len(self.error_samples) < 8:
            self.error_samples.append(description)

    def _flush(self) -> None:
        connection = self._connection
        if connection is None or self._pending == 0:
            return
        started = perf_counter_ns()
        try:
            connection.execute("COMMIT")
            connection.execute("BEGIN")
        except sqlite3.Error as error:
            self._record_error(error)
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

    def _log(self, sequence: int, market_id: str, record_type: str, key: str) -> None:
        """One row per stored event-like record, in a single total storage order.

        The typed tables each have their own primary key, so nothing in them can show that the
        *combined* storage order has no hole. This envelope can: a missing prefix, a duplicate
        across two different tables, or a gap all show up as a break in one contiguous run.

        Storage order, not causality. `ingress_ordinal`, `risk_sequence` and the settlement
        block remain the orders that mean something.
        """
        self._execute(
            "INSERT INTO persistence_log"
            " (persistence_sequence, market_id, record_type, record_key) VALUES (?, ?, ?, ?)",
            (sequence, market_id, record_type, key),
        )

    def write_decision(self, record: DecisionRecord) -> None:
        self._log(
            record.persistence_sequence, record.market_id, "decision", str(record.ingress_ordinal)
        )
        self._execute(
            "INSERT INTO decisions (persistence_sequence, market_id,"
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
        self._log(record.persistence_sequence, record.market_id, "fill", record.event_id)
        self._execute(
            "INSERT INTO fills (persistence_sequence, market_id, ingress_ordinal,"
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
        self._log(record.persistence_sequence, record.market_id, "risk", str(record.risk_sequence))
        self._execute(
            "INSERT INTO risk_records (persistence_sequence, market_id,"
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
        self._log(record.persistence_sequence, record.market_id, "settlement", record.condition_id)
        self._execute(
            "INSERT INTO settlements (persistence_sequence, market_id, condition_id,"
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

    def write_control_audit(self, record: ControlAuditRow) -> None:
        """Append-only, like every other event-like row, and unique on the command id."""
        self._log(record.persistence_sequence, record.market_id, "control", record.command_id)
        self._execute(
            "INSERT INTO control_audit (persistence_sequence, market_id, command_id, kind,"
            " accepted, risk_sequence, schema_version, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.persistence_sequence,
                record.market_id,
                record.command_id,
                record.kind,
                int(record.accepted),
                record.risk_sequence,
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
    if version not in SUPPORTED_STORE_SCHEMA_VERSIONS:
        connection.close()
        raise SchemaVersionError(
            f"store schema {version} is not one this build reads "
            f"({sorted(SUPPORTED_STORE_SCHEMA_VERSIONS)}); refusing to guess at a layout it "
            "does not define"
        )
    return connection


MANIFEST_VERSION_IN_USE: Final[int] = MANIFEST_SCHEMA_VERSION
