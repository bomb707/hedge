"""Reading persisted telemetry back, and deciding whether to believe it.

The verifier's job is to be *unwilling*. It reports COMPLETE only when everything it can check
agrees; anything else — a market that never closed, a hole in a sequence, a count that does not
match its manifest, a file whose hash has moved — is INCOMPLETE or CORRUPT, and it never repairs
either. A telemetry store that quietly fixed itself would be a store whose contents mean nothing.

This matters beyond tidiness. P15 will use persisted markets to close open strategy items, and a
market with a hole in it cannot be allowed to look like one without. "Trading went fine" is not
evidence that the record of it is whole.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from maker5m.persistence.schema import (
    MANIFEST_SCHEMA_VERSION,
    Manifest,
)
from maker5m.persistence.store import SchemaVersionError, database_digest, open_for_read

__all__ = ["VerificationResult", "VerificationStatus", "read_manifest", "verify_store"]


class VerificationStatus(Enum):
    COMPLETE = "COMPLETE"
    """Every check passed. This market may be used as evidence."""

    INCOMPLETE = "INCOMPLETE"
    """Readable, and known to be missing something. Never silently upgraded."""

    CORRUPT = "CORRUPT"
    """Unreadable, or readable and self-contradictory."""

    UNSUPPORTED = "UNSUPPORTED"
    """A schema this build does not define. Refused rather than guessed at."""


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: VerificationStatus
    market_id: str | None
    slug: str | None
    checks: dict[str, bool] = field(default_factory=dict)
    failures: tuple[str, ...] = ()
    decision_rows: int = 0
    fill_rows: int = 0
    risk_rows: int = 0
    settlement_rows: int = 0
    manifest: Manifest | None = None

    @property
    def complete(self) -> bool:
        return self.status is VerificationStatus.COMPLETE

    def summary(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "market_id": self.market_id,
            "slug": self.slug,
            "checks": dict(sorted(self.checks.items())),
            "failures": list(self.failures),
            "decision_rows": self.decision_rows,
            "fill_rows": self.fill_rows,
            "risk_rows": self.risk_rows,
            "settlement_rows": self.settlement_rows,
        }


def read_manifest(connection: sqlite3.Connection, market_id: str) -> Manifest | None:
    row = connection.execute(
        "SELECT manifest_json FROM markets WHERE market_id = ?", (market_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    data: dict[str, Any] = json.loads(row[0])
    if int(data.get("schema_version", -1)) != MANIFEST_SCHEMA_VERSION:
        raise SchemaVersionError(f"manifest schema {data.get('schema_version')} is not supported")
    fields = {
        key: value
        for key, value in data.items()
        if key in {f.name for f in Manifest.__dataclass_fields__.values()}
    }
    fields["notes"] = tuple(fields.get("notes") or ())
    return Manifest(**fields)


def _contiguous(values: list[int]) -> bool:
    """Whether a sorted sequence has no holes. Empty counts as contiguous."""
    if not values:
        return True
    return values == list(range(values[0], values[0] + len(values)))


def verify_store(
    path: Path, *, market_id: str | None = None, expected_sha256: str | None = None
) -> VerificationResult:
    """Check one persisted market end to end.

    ``expected_sha256`` comes from the sidecar manifest, not from inside the database, because a
    file cannot contain its own hash: writing the digest into the store would change the store
    and invalidate the digest in the same act. The sidecar is written once the database is
    closed and nothing is still touching it.

    Passing ``None`` skips the hash check and says so in ``checks``; it does not quietly pass.
    """
    try:
        connection = open_for_read(path)
    except SchemaVersionError as error:
        return VerificationResult(
            status=VerificationStatus.UNSUPPORTED,
            market_id=market_id,
            slug=None,
            failures=(str(error),),
        )
    except (FileNotFoundError, sqlite3.DatabaseError) as error:
        return VerificationResult(
            status=VerificationStatus.CORRUPT,
            market_id=market_id,
            slug=None,
            failures=(f"{type(error).__name__}: {error}",),
        )

    checks: dict[str, bool] = {}
    failures: list[str] = []
    try:
        markets = connection.execute(
            "SELECT market_id, slug, closed FROM markets"
            + ("" if market_id is None else " WHERE market_id = ?"),
            () if market_id is None else (market_id,),
        ).fetchall()
        if len(markets) != 1:
            return VerificationResult(
                status=VerificationStatus.CORRUPT,
                market_id=market_id,
                slug=None,
                failures=(f"expected exactly one market in the store, found {len(markets)}",),
            )
        found_id, slug, closed = str(markets[0][0]), str(markets[0][1]), bool(markets[0][2])

        decisions = connection.execute(
            "SELECT persistence_sequence, capture_sequence, market_id FROM decisions"
            " WHERE market_id = ? ORDER BY persistence_sequence",
            (found_id,),
        ).fetchall()
        fills = connection.execute(
            "SELECT COUNT(*) FROM fills WHERE market_id = ?", (found_id,)
        ).fetchone()[0]
        risks = connection.execute(
            "SELECT risk_sequence FROM risk_records WHERE market_id = ? ORDER BY risk_sequence",
            (found_id,),
        ).fetchall()
        settlements = connection.execute(
            "SELECT COUNT(*) FROM settlements WHERE market_id = ?", (found_id,)
        ).fetchone()[0]

        manifest = read_manifest(connection, found_id)
    except sqlite3.DatabaseError as error:
        return VerificationResult(
            status=VerificationStatus.CORRUPT,
            market_id=market_id,
            slug=None,
            failures=(f"{type(error).__name__}: {error}",),
        )
    except SchemaVersionError as error:
        return VerificationResult(
            status=VerificationStatus.UNSUPPORTED,
            market_id=market_id,
            slug=None,
            failures=(str(error),),
        )
    finally:
        connection.close()

    checks["market_closed"] = closed
    if not closed:
        failures.append("market was never closed; it may be a crash or a run still in progress")

    checks["one_market_identity"] = all(str(row[2]) == found_id for row in decisions)
    if not checks["one_market_identity"]:
        failures.append("decision rows carry a different market id than the market row")

    persistence = [int(row[0]) for row in decisions]
    checks["persistence_sequence_contiguous"] = _contiguous(persistence)
    if not checks["persistence_sequence_contiguous"]:
        failures.append("storage order has a hole; rows were lost after being sequenced")

    capture = [int(row[1]) for row in decisions]
    checks["capture_sequence_ordered"] = capture == sorted(capture)
    if not checks["capture_sequence_ordered"]:
        failures.append("capture order is not monotonic; the stream was reordered")

    risk_sequences = [int(row[0]) for row in risks]
    checks["risk_sequence_contiguous"] = _contiguous(risk_sequences)
    if not checks["risk_sequence_contiguous"]:
        failures.append("risk sequence has a hole; the P9 audit trail is incomplete")

    checks["manifest_present"] = manifest is not None
    if manifest is None:
        failures.append("no manifest; nothing states what this market should contain")
        return VerificationResult(
            status=VerificationStatus.INCOMPLETE,
            market_id=found_id,
            slug=slug,
            checks=checks,
            failures=tuple(failures),
            decision_rows=len(decisions),
            fill_rows=int(fills),
            risk_rows=len(risks),
            settlement_rows=int(settlements),
        )

    checks["decision_count_matches_manifest"] = manifest.decision_count == len(decisions)
    if not checks["decision_count_matches_manifest"]:
        failures.append(
            f"manifest claims {manifest.decision_count} decisions, store holds {len(decisions)}"
        )
    checks["risk_count_matches_manifest"] = manifest.risk_count == len(risks)
    if not checks["risk_count_matches_manifest"]:
        failures.append(
            f"manifest claims {manifest.risk_count} risk records, store holds {len(risks)}"
        )
    checks["fill_count_matches_manifest"] = manifest.fill_count == int(fills)
    if not checks["fill_count_matches_manifest"]:
        failures.append(f"manifest claims {manifest.fill_count} fills, store holds {fills}")
    checks["settlement_count_matches_manifest"] = manifest.settlement_count == int(settlements)
    if not checks["settlement_count_matches_manifest"]:
        failures.append(
            f"manifest claims {manifest.settlement_count} settlements, store holds {settlements}"
        )

    checks["no_drops"] = manifest.dropped_records == 0
    if not checks["no_drops"]:
        failures.append(f"{manifest.dropped_records} records were dropped by the bounded buffer")
    checks["no_sequence_gaps"] = manifest.sequence_gaps == 0
    if not checks["no_sequence_gaps"]:
        failures.append(
            f"{manifest.sequence_gaps} gap(s), {manifest.lost_observations} observations lost"
        )
    checks["no_sink_errors"] = manifest.sink_errors == 0
    if not checks["no_sink_errors"]:
        failures.append(f"{manifest.sink_errors} sink error(s) while writing")

    if expected_sha256 is not None:
        _, digest = database_digest(path)
        checks["database_hash_matches"] = digest == expected_sha256
        if not checks["database_hash_matches"]:
            failures.append("database hash does not match the sidecar; the file has changed")
    else:
        checks["database_hash_checked"] = False

    checks["manifest_reports_complete"] = manifest.telemetry_complete

    status = (
        VerificationStatus.COMPLETE
        if not failures and manifest.telemetry_complete
        else VerificationStatus.INCOMPLETE
    )
    return VerificationResult(
        status=status,
        market_id=found_id,
        slug=slug,
        checks=checks,
        failures=tuple(failures),
        decision_rows=len(decisions),
        fill_rows=int(fills),
        risk_rows=len(risks),
        settlement_rows=int(settlements),
        manifest=manifest,
    )
