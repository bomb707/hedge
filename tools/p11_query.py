"""Answer the P11 acceptance questions from the durable record itself.

Deliberately not from counters the runner kept while it ran. The point of a durability phase is
that the persisted market is the evidence, so anything that can be asked of the file is asked of
the file — if the store cannot answer "how many PLACEs happened while risk was HALTED", the
store is not the audit it claims to be.

This is also the supported read path P12 and P15 will use, exercised here rather than promised.

Read-only. Opens the database read-only, or restores an archive to a temporary file first.
"""

import argparse
import json
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from maker5m.persistence import (
    ARCHIVE_SUFFIX,
    ArchiveIdentity,
    ArchiveVerificationError,
    open_for_read,
    open_verified_archive,
)


def _open(
    path: Path, sidecar: Path | None
) -> tuple[sqlite3.Connection, tempfile.TemporaryDirectory[str] | None]:
    """Open a store, or an archive that has proved it is the market it claims to be.

    An archive is never queried on the strength of decompressing cleanly. lzma will happily
    restore a file whose contents are not the market the sidecar names, and a query answered
    from it would be indistinguishable from a real answer.
    """
    if not path.name.endswith(ARCHIVE_SUFFIX):
        return open_for_read(path), None

    found = sidecar or _default_sidecar(path)
    if found is None or not found.exists():
        raise ArchiveVerificationError(
            f"{path.name}: no sidecar manifest, so this archive has no identity to check. "
            "Pass --sidecar. An artifact without identity is not evidence."
        )
    directory = tempfile.TemporaryDirectory()
    restored = open_verified_archive(
        path, ArchiveIdentity.from_sidecar(found), Path(directory.name) / "restored.sqlite3"
    )
    return open_for_read(restored), directory


def _default_sidecar(archive: Path) -> Path | None:
    """`<name>.p11.manifest.json` beside `<name>.p11.sqlite3.xz`."""
    stem = archive.name[: -len(ARCHIVE_SUFFIX)]
    candidate = archive.with_name(f"{stem}.manifest.json")
    return candidate if candidate.exists() else None


def summarise(path: Path, sidecar: Path | None = None) -> dict[str, Any]:
    connection, directory = _open(path, sidecar)
    try:
        verdicts = {
            int(row[0]): (str(row[1]), bool(row[2]), bool(row[3]))
            for row in connection.execute(
                "SELECT risk_sequence,"
                " json_extract(payload, '$.state'),"
                " json_extract(payload, '$.allows_place'),"
                " json_extract(payload, '$.allows_cancel')"
                " FROM risk_records"
            )
        }
        places: Counter[str] = Counter()
        copy_mismatches = 0
        dangling_risk = 0
        actions: Counter[str] = Counter()
        risk_states: Counter[str] = Counter()
        withdrawn = 0
        missing_event_id = 0
        total = 0
        for (payload,) in connection.execute("SELECT payload FROM decisions"):
            record = json.loads(payload)
            total += 1
            referenced = record.get("risk_sequence")
            verdict = verdicts.get(int(referenced)) if referenced is not None else None
            if referenced is not None and verdict is None:
                dangling_risk += 1
            # The persisted verdict, not the decision's copy of it. The copy is compared against
            # it and counted, never used in its place.
            state = verdict[0] if verdict is not None else "<no risk row>"
            if verdict is not None and (
                record.get("risk_state") != verdict[0]
                or bool(record.get("risk_allows_place")) != verdict[1]
                or bool(record.get("risk_allows_cancel")) != verdict[2]
            ):
                copy_mismatches += 1
            risk_states[state] += 1
            if not record.get("event_id"):
                missing_event_id += 1
            if record.get("risk_withdrew_intent"):
                withdrawn += 1
            for side in ("up", "down"):
                action = str(record.get(side, {}).get("action"))
                actions[action] += 1
                if action == "PLACE":
                    places[state] += 1

        risk = [
            int(row[0])
            for row in connection.execute(
                "SELECT risk_sequence FROM risk_records ORDER BY risk_sequence"
            )
        ]
        log = [
            int(row[0])
            for row in connection.execute(
                "SELECT persistence_sequence FROM persistence_log ORDER BY persistence_sequence"
            )
        ]
        fills = int(connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0])
        settlements = int(connection.execute("SELECT COUNT(*) FROM settlements").fetchone()[0])
        return {
            "source": str(path),
            "decisions": total,
            "decisions_with_no_event_id": missing_event_id,
            "risk_states": dict(sorted(risk_states.items())),
            "places_by_risk_state": dict(sorted(places.items())),
            "actions": dict(sorted(actions.items())),
            "risk_withdrew_intent": withdrawn,
            "decision_risk_copy_mismatches": copy_mismatches,
            "decisions_naming_an_absent_risk_row": dangling_risk,
            "risk_records": len(risk),
            "risk_first": risk[0] if risk else None,
            "risk_last": risk[-1] if risk else None,
            "risk_exact_from_zero": risk == list(range(len(risk))),
            "storage_first": log[0] if log else None,
            "storage_last": log[-1] if log else None,
            "storage_count": len(log),
            "storage_exact_from_one": log == list(range(1, len(log) + 1)),
            "storage_duplicates": len(log) - len(set(log)),
            "fills": fills,
            "settlements": settlements,
        }
    finally:
        connection.close()
        if directory is not None:
            directory.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("store", type=Path)
    parser.add_argument("--sidecar", type=Path, help="manifest identifying an archive")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = summarise(args.store, args.sidecar)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
