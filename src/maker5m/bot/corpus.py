"""The corpus index: one line per attempted market, appended and never rewritten.

Why a JSONL file and not a table
--------------------------------
The index has to survive the process being killed mid-market, which is a thing that will happen
across two hundred markets. An append of one line, opened `a`, written whole and fsynced, is
atomic enough for that on POSIX: a crash leaves either the line or not the line, and a reader
that hits a truncated tail can drop it and keep every entry before it. A database would need a
connection, a schema and a migration to hold rows that are never updated.

**Prior entries are never rewritten.** Not when a later market fails, not on resume, not to
"correct" an earlier one. A market that turned out badly is recorded as having turned out badly;
a second attempt at the same slug appends a second entry with its own attempt number, and the
reader can see both. Losing the inconvenient half of the evidence is exactly what an audit trail
exists to prevent.

Every write here is Plane 3.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["CorpusIndex", "CorpusStats"]


@dataclass(frozen=True, slots=True)
class CorpusStats:
    """What the index holds, counted rather than believed."""

    attempted: int
    complete: int
    incomplete: int
    unsupported: int
    corrupt: int
    eligible: int
    truncated_lines: int

    def summary(self) -> dict[str, int]:
        return {
            "attempted": self.attempted,
            "complete": self.complete,
            "incomplete": self.incomplete,
            "unsupported": self.unsupported,
            "corrupt": self.corrupt,
            "eligible": self.eligible,
            "truncated_lines": self.truncated_lines,
        }


@dataclass(slots=True)
class CorpusIndex:
    """Append-only record of every market this collector has attempted."""

    path: Path
    appended: int = 0
    append_errors: int = 0
    torn_lines_closed: int = 0
    error_samples: list[str] = field(default_factory=list)

    def append(self, entry: dict[str, Any]) -> bool:
        """Add one market. Returns whether it reached the disk. Never raises.

        The line is built completely before the file is opened, so a serialization failure
        cannot leave half an entry behind, and the flush plus `fsync` happen before the handle
        closes so a kill immediately afterwards still finds the line.
        """
        try:
            line = json.dumps(entry, sort_keys=True, default=str, separators=(",", ":")) + "\n"
        except (TypeError, ValueError) as error:
            self._note(error)
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._close_a_torn_line()
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            self._note(error)
            return False
        self.appended += 1
        return True

    def qualifying(
        self,
        *,
        epoch: str,
        config_sha256: str,
        source_revision: str,
        source_tree_sha: str | None = None,
    ) -> int:
        """Durable rows that count toward this epoch's target.

        Every identity is required. Another epoch is another collection, another config hash
        another experiment, another revision another build — and **a dirty tree is not a build at
        all**. `git rev-parse HEAD` does not move when tracked files are edited, so an
        exploratory run started with `--allow-dirty` produces rows carrying the same revision as
        a clean acceptance run. Without `working_tree_clean` in this filter, a later clean
        restart would have counted them and called modified source final empirical evidence.

        The tree hash is compared when the caller supplies one, so a row from a different commit
        with a coincidentally equal revision string cannot slip through either.
        """
        return sum(
            1
            for entry in self.entries()
            if entry.get("verification_status") == "COMPLETE"
            and entry.get("evidence_eligible") is True
            and entry.get("working_tree_clean") is True
            and entry.get("run_mode") == "ACCEPTANCE_CLEAN"
            and entry.get("epoch") == epoch
            and entry.get("config_sha256") == config_sha256
            and entry.get("source_revision") == source_revision
            and (source_tree_sha is None or entry.get("source_tree_sha") == source_tree_sha)
        )

    def _close_a_torn_line(self) -> None:
        """Terminate a half-written final line before appending after it.

        A kill mid-append leaves a fragment with no newline. Appending straight onto it would
        weld the next entry to the wreck of the last one and lose both — one unreadable line
        instead of one unreadable fragment and one good row. So the fragment is closed off,
        fsynced, and left exactly where it is: it is evidence that a process died there, and
        deleting it would be tidying away the only sign of that.
        """
        try:
            if not self.path.exists() or self.path.stat().st_size == 0:
                return
            with self.path.open("rb") as handle:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) == b"\n":
                    return
        except OSError as error:
            self._note(error)
            return
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.torn_lines_closed += 1

    def entries(self) -> list[dict[str, Any]]:
        """Every readable entry, oldest first. A truncated line is dropped, not guessed at."""
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
                # A partial final line from a kill mid-append. Everything before it stands.
                continue
            if isinstance(parsed, dict):
                found.append(parsed)
        return found

    def completed_slugs(self) -> set[str]:
        """Slugs already collected as COMPLETE. A resume must not recollect one.

        Only COMPLETE counts as collected: an INCOMPLETE market is evidence of an incident and
        stays in the index, but the slug is gone — a five-minute market cannot be re-run — so
        nothing will retry it either way. This is what stops a restart from appending a second
        entry for a market it already has.
        """
        return {
            str(entry.get("slug"))
            for entry in self.entries()
            if entry.get("verification_status") == "COMPLETE" and entry.get("slug")
        }

    def attempts(self, slug: str) -> int:
        return sum(1 for entry in self.entries() if entry.get("slug") == slug)

    def stats(self) -> CorpusStats:
        entries = self.entries()
        truncated = 0
        try:
            text = self.path.read_text("utf-8")
            truncated = sum(1 for line in text.splitlines() if line.strip()) - len(entries)
        except (OSError, UnicodeDecodeError):
            truncated = 0
        status = [str(entry.get("verification_status")) for entry in entries]
        return CorpusStats(
            attempted=len(entries),
            complete=status.count("COMPLETE"),
            incomplete=status.count("INCOMPLETE"),
            unsupported=status.count("UNSUPPORTED"),
            corrupt=status.count("CORRUPT"),
            eligible=sum(1 for entry in entries if entry.get("evidence_eligible") is True),
            truncated_lines=max(truncated, 0),
        )

    def _note(self, error: BaseException) -> None:
        self.append_errors += 1
        description = f"{type(error).__name__}: {error}"
        if description not in self.error_samples and len(self.error_samples) < 8:
            self.error_samples.append(description)
