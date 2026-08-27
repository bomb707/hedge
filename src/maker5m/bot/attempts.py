"""Every market this collector touched, written down before it touches it.

The corpus index records markets that *finished*. That is the wrong moment for the only durable
record of an attempt to be written: `p13-corpus-1` attempted fourteen markets and left twelve
rows, and the two that were in flight when the process stopped exist nowhere at all. A phase whose
contract is "retain every attempted market" cannot have its ledger start after the risky part.

So the attempt is registered **first**, fsynced, and only then is the session launched. If the
ledger cannot be written, the market is not launched: a market nobody recorded is worse than a
market nobody collected.

Append-only, like everything else here. `ATTEMPT_STARTED` is never updated; a terminal event is
appended beside it with the same `attempt_id`, and a start with no terminal event is a market the
process died in the middle of — which the next start-up finds, records as aborted, and inventories
rather than quietly forgetting.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

__all__ = [
    "ABORTED",
    "ATTEMPT_SCHEMA_VERSION",
    "FAILED",
    "FINISHED",
    "STARTED",
    "AttemptLedger",
]

ATTEMPT_SCHEMA_VERSION: Final[int] = 1
STARTED: Final[str] = "ATTEMPT_STARTED"
FINISHED: Final[str] = "ATTEMPT_FINISHED"
FAILED: Final[str] = "ATTEMPT_FAILED"
ABORTED: Final[str] = "ABORTED_PREVIOUS_PROCESS"

TERMINAL: Final[frozenset[str]] = frozenset({FINISHED, FAILED, ABORTED})


class LedgerWriteError(RuntimeError):
    """The attempt could not be made durable. The market must not be launched."""


@dataclass(slots=True)
class AttemptLedger:
    """Append-only record of every attempt, its outcome, and every one that was abandoned."""

    path: Path
    appended: int = 0
    errors: list[str] = field(default_factory=list)

    def _append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, sort_keys=True, default=str, separators=(",", ":")) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._close_a_torn_line()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self.appended += 1

    def _close_a_torn_line(self) -> None:
        """Terminate a half-written line before appending after it. Same rule as the corpus."""
        try:
            if not self.path.exists() or self.path.stat().st_size == 0:
                return
            with self.path.open("rb") as handle:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) == b"\n":
                    return
        except OSError:
            return
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    def start(self, *, slug: str, t0_ns: int, identity: dict[str, Any], **detail: Any) -> str:
        """Register an attempt and return its id. Raises if it could not be made durable.

        The caller must not launch the market unless this returns. Failing closed here costs one
        five-minute window; failing open costs the ability to say what the collector did.
        """
        attempt_id = uuid.uuid4().hex
        try:
            self._append(
                {
                    "schema_version": ATTEMPT_SCHEMA_VERSION,
                    "event": STARTED,
                    "attempt_id": attempt_id,
                    "slug": slug,
                    "t0_ns": t0_ns,
                    "pid": os.getpid(),
                    "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    **identity,
                    **detail,
                }
            )
        except (OSError, TypeError, ValueError) as error:
            description = f"{type(error).__name__}: {error}"
            self.errors.append(description)
            raise LedgerWriteError(
                f"could not register an attempt for {slug}: {description}"
            ) from error
        return attempt_id

    def finish(self, attempt_id: str, *, event: str = FINISHED, **detail: Any) -> bool:
        """Close an attempt. Never raises: the market has already happened by now."""
        try:
            self._append(
                {
                    "schema_version": ATTEMPT_SCHEMA_VERSION,
                    "event": event,
                    "attempt_id": attempt_id,
                    "pid": os.getpid(),
                    "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    **detail,
                }
            )
        except (OSError, TypeError, ValueError) as error:
            self.errors.append(f"{type(error).__name__}: {error}")
            return False
        return True

    def events(self) -> list[dict[str, Any]]:
        """Every readable event, in order. A torn final line is dropped, not guessed at."""
        try:
            text = self.path.read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        found: list[dict[str, Any]] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                found.append(parsed)
        return found

    def open_attempts(self) -> list[dict[str, Any]]:
        """Starts with no terminal event: markets a process died in the middle of."""
        events = self.events()
        terminal = {
            str(event.get("attempt_id")) for event in events if event.get("event") in TERMINAL
        }
        return [
            event
            for event in events
            if event.get("event") == STARTED and str(event.get("attempt_id")) not in terminal
        ]

    def recover(self, *, inventory: Any = None) -> list[dict[str, Any]]:
        """Close every abandoned attempt with an explicit aborted record.

        Appends rather than edits, and never claims the market completed. `inventory` is given
        the attempt and returns whatever of its artifacts survived, so an orphaned journal or
        store is recorded where somebody will find it instead of sitting on disk unreferenced.
        """
        recovered: list[dict[str, Any]] = []
        for attempt in self.open_attempts():
            found = {} if inventory is None else inventory(attempt)
            self.finish(
                str(attempt.get("attempt_id")),
                event=ABORTED,
                slug=attempt.get("slug"),
                evidence_eligible=False,
                verification_status="ABORTED",
                reason=(
                    "the process that started this attempt exited before finalising it; the "
                    "market was attempted and is not collected"
                ),
                started_by_pid=attempt.get("pid"),
                orphan_artifacts=found,
            )
            recovered.append({**attempt, "orphan_artifacts": found})
        return recovered

    def summary(self) -> dict[str, Any]:
        events = self.events()
        counts: dict[str, int] = {}
        for event in events:
            name = str(event.get("event"))
            counts[name] = counts.get(name, 0) + 1
        return {
            "path": str(self.path),
            "events": len(events),
            "by_event": dict(sorted(counts.items())),
            "attempts_started": counts.get(STARTED, 0),
            "attempts_terminal": sum(counts.get(name, 0) for name in TERMINAL),
            "open_attempts": len(self.open_attempts()),
            "write_errors": list(self.errors),
        }
