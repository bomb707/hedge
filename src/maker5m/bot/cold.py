"""Cold work, in a child process, so a closed market cannot slow the one that is trading.

Verifying a store, replaying a journal and compressing 650 MB of SQLite are all CPU-bound and
all measured in tens of seconds. A thread would hold the GIL through most of that, and the
thread it would be stealing from is the one consuming a live market's book. So this runs in a
**separate interpreter**, started with `spawn` rather than `fork`: forking a process that is
holding an open SQLite connection and two live websockets copies exactly the state a child must
not inherit.

Everything crosses the boundary as paths and plain values, because everything here has to be
picklable and because the child is not entitled to any of the parent's objects.

Nothing in this module writes to a venue, signs anything, or opens a network connection.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, NamedTuple

__all__ = ["ColdRequest", "cold_finalize"]


class ColdRequest(NamedTuple):
    """One closed market's cold work. Plain values only — this crosses a process boundary."""

    slug: str
    journal_path: str
    database_path: str
    expected_database_sha256: str
    keep_raw_store: bool


def _replay(journal_path: Path) -> dict[str, Any]:
    """Re-derive every decision from the journal and compare, under the journal's own config.

    A deterministic mismatch makes the market ineligible as evidence. The journal is not
    "repaired": a stream that does not reproduce is a fact about the software, and editing it
    would destroy the only record of that fact.
    """
    from maker5m.replay import decode_journal, encode_journal, verify_replay

    try:
        raw = journal_path.read_bytes()
    except OSError as error:
        return {"status": "UNREADABLE", "error": f"{type(error).__name__}: {error}"}

    digest = hashlib.sha256(raw).hexdigest()
    result: dict[str, Any] = {"journal_bytes": len(raw), "journal_sha256": digest}
    try:
        journal = decode_journal(raw)
    except Exception as error:
        return {**result, "status": "UNDECODABLE", "error": f"{type(error).__name__}: {error}"}

    try:
        outcome = verify_replay(journal)
    except Exception as error:
        # The first divergence, reported where it is. Downstream steps are noise.
        return {
            **result,
            "status": "MISMATCH",
            "steps": journal.step_count,
            "error": f"{type(error).__name__}: {error}",
        }

    return {
        **result,
        "status": "EXACT" if outcome.verified else "NOT_COMPARED",
        "steps": outcome.step_count,
        "verified": outcome.verified,
        # The journal is its own record: what it encodes back to must be what it was read as,
        # or its hash describes something other than the bytes that were replayed.
        "byte_roundtrip_identical": encode_journal(journal) == raw,
    }


def _verify_and_archive(request: ColdRequest) -> dict[str, Any]:
    from maker5m.persistence import archive_store, database_digest, verify_store
    from maker5m.persistence.store import SchemaVersionError

    database = Path(request.database_path)
    if not database.exists():
        return {"verification_status": "CORRUPT", "error": "the store file is not there"}

    size, digest = database_digest(database)
    if request.expected_database_sha256 and digest != request.expected_database_sha256:
        return {
            "verification_status": "CORRUPT",
            "database_bytes": size,
            "database_sha256": digest,
            "error": "the store changed between closing and verification",
        }
    try:
        result = verify_store(database, expected_sha256=digest)
    except SchemaVersionError as error:
        return {
            "verification_status": "UNSUPPORTED",
            "database_bytes": size,
            "database_sha256": digest,
            "error": f"{type(error).__name__}: {error}",
        }
    except Exception as error:
        return {
            "verification_status": "CORRUPT",
            "database_bytes": size,
            "database_sha256": digest,
            "error": f"{type(error).__name__}: {error}",
        }

    verified: dict[str, Any] = {
        "verification_status": result.status.value,
        "verification": result.summary(),
        "database_bytes": size,
        "database_sha256": digest,
    }

    # Cold, lossless, and only after the store verified. The archive is proved to restore
    # byte-identically before the raw file is even considered removable, and a store that did
    # not verify keeps its raw file whatever the setting says: the failed market is the evidence.
    if result.status.value != "COMPLETE":
        return {**verified, "archive": None}
    try:
        archive = archive_store(database, remove_raw=not request.keep_raw_store)
    except Exception as error:
        return {**verified, "archive": None, "archive_error": f"{type(error).__name__}: {error}"}
    return {**verified, "archive": archive.summary()}


def cold_finalize(request: ColdRequest) -> dict[str, Any]:
    """Replay-validate the journal, verify the store, and archive it. Child process only."""
    return {
        "slug": request.slug,
        "replay": _replay(Path(request.journal_path)),
        **_verify_and_archive(request),
    }
