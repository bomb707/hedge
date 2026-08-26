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
import lzma
import shutil
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from maker5m.persistence import ARCHIVE_SUFFIX, open_for_read


def _open(path: Path) -> tuple[sqlite3.Connection, tempfile.TemporaryDirectory[str] | None]:
    if path.name.endswith(ARCHIVE_SUFFIX):
        directory = tempfile.TemporaryDirectory()
        restored = Path(directory.name) / "restored.sqlite3"
        with lzma.open(path, "rb") as source, restored.open("wb") as target:
            shutil.copyfileobj(source, target, 1 << 20)
        return open_for_read(restored), directory
    return open_for_read(path), None


def summarise(path: Path) -> dict[str, Any]:
    connection, directory = _open(path)
    try:
        places: Counter[str] = Counter()
        actions: Counter[str] = Counter()
        risk_states: Counter[str] = Counter()
        withdrawn = 0
        missing_event_id = 0
        total = 0
        for (payload,) in connection.execute("SELECT payload FROM decisions"):
            record = json.loads(payload)
            total += 1
            state = str(record.get("risk_state"))
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
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = summarise(args.store)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
