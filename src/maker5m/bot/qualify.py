"""What it takes for a market to count. One definition, used by everything that counts.

The collector's target, the resume arithmetic and the final report have to mean the same thing by
"two hundred markets". Three implementations of nearly the same rule is how a run ends with the
collector saying 200 and the report saying 198, and nobody able to say which is right — so the
rule lives here and the three of them call it.

A market counts only when **all** of these are durable and agree:

* a corpus row that verified COMPLETE, replayed EXACT, was collected from clean source in this
  epoch under this configuration and this build, and carries no operational fault;
* exactly one `ATTEMPT_STARTED` naming the same attempt;
* exactly one terminal attempt event, and that event is `ATTEMPT_FINISHED` recording that the
  corpus row reached the disk;
* a latency artifact that hashes to what the row recorded **and** identifies itself as this
  market's, from this build.

Anything else is retained and does not count. A market that verified perfectly and whose terminal
attempt record failed to persist is not an accounting rounding error: it is a market this
collector cannot prove it finished.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from maker5m.bot.attempts import ABORTED, FAILED, FINISHED, STARTED
from maker5m.bot.latency import read_latency

__all__ = ["AttemptIndex", "Qualification", "qualification_of", "qualifying_rows"]

JOINED_IDENTITY: tuple[str, ...] = (
    "slug",
    "epoch",
    "config_sha256",
    "source_revision",
    "source_tree_sha",
    "run_mode",
)
"""Fields that must agree between the start, the row and the terminal record.

A terminal event carrying the right attempt id and the wrong slug does not close that attempt;
it describes a different market, and letting it satisfy the gate would make the join decorative.
"""


@dataclass(frozen=True, slots=True)
class AttemptIndex:
    """The attempt ledger, indexed once, with its inconsistencies already visible."""

    starts: dict[str, dict[str, Any]]
    terminals: dict[str, list[dict[str, Any]]]

    @classmethod
    def build(cls, events: Iterable[dict[str, Any]]) -> AttemptIndex:
        starts: dict[str, dict[str, Any]] = {}
        terminals: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            attempt = str(event.get("attempt_id"))
            name = str(event.get("event"))
            if name == STARTED:
                starts[attempt] = event
            elif name in {FINISHED, FAILED, ABORTED}:
                # A list, not an assignment. Two terminal events for one attempt is a ledger
                # inconsistency, and last-one-wins would hide exactly the case worth seeing.
                terminals.setdefault(attempt, []).append(event)
        return cls(starts=starts, terminals=terminals)

    def duplicates(self) -> dict[str, list[str]]:
        return {
            attempt: [str(event.get("event")) for event in events]
            for attempt, events in self.terminals.items()
            if len(events) > 1
        }

    def open_ids(self) -> set[str]:
        return set(self.starts) - set(self.terminals)

    def counts(self) -> dict[str, int]:
        names = [str(event.get("event")) for events in self.terminals.values() for event in events]
        return {
            "attempts_started": len(self.starts),
            "attempts_finished": names.count(FINISHED),
            "attempts_failed": names.count(FAILED),
            "attempts_aborted": names.count(ABORTED),
            "attempts_terminal": len(names),
            "open_attempts": len(self.open_ids()),
            "duplicate_terminal_attempts": len(self.duplicates()),
            "terminal_without_start": len(set(self.terminals) - set(self.starts)),
        }


@dataclass(frozen=True, slots=True)
class Qualification:
    """Whether one corpus row counts, and every reason it does not."""

    slug: str
    qualifies: bool
    reasons: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        return {"slug": self.slug, "qualifies": self.qualifies, "reasons": list(self.reasons)}


def qualification_of(
    entry: dict[str, Any],
    attempts: AttemptIndex,
    *,
    epoch: str,
    config_sha256: str,
    source_revision: str,
    source_tree_sha: str | None = None,
    run_mode: str = "ACCEPTANCE_CLEAN",
    verify_latency: bool = True,
) -> Qualification:
    """Judge one row against everything that has to be true of it."""
    slug = str(entry.get("slug"))
    reasons: list[str] = []

    if entry.get("verification_status") != "COMPLETE":
        reasons.append(f"store verified {entry.get('verification_status')!r}")
    if entry.get("evidence_eligible") is not True:
        reasons.append("the row is not marked evidence-eligible")
    if entry.get("working_tree_clean") is not True:
        reasons.append("collected from modified tracked source")
    if entry.get("run_mode") != run_mode:
        reasons.append(f"run_mode {entry.get('run_mode')!r}, expected {run_mode!r}")
    if entry.get("epoch") != epoch:
        reasons.append(f"epoch {entry.get('epoch')!r}")
    if entry.get("config_sha256") != config_sha256:
        reasons.append("a different configuration")
    if entry.get("source_revision") != source_revision:
        reasons.append("a different source revision")
    if source_tree_sha is not None and entry.get("source_tree_sha") != source_tree_sha:
        reasons.append("a different source tree")

    reasons.extend(_attempt_reasons(entry, attempts))
    if verify_latency:
        reasons.extend(_latency_reasons(entry))
    return Qualification(slug=slug, qualifies=not reasons, reasons=tuple(reasons))


def _attempt_reasons(entry: dict[str, Any], attempts: AttemptIndex) -> list[str]:
    attempt_id = entry.get("attempt_id")
    if not attempt_id:
        return ["the row names no attempt"]
    attempt = str(attempt_id)
    start = attempts.starts.get(attempt)
    if start is None:
        return [f"no ATTEMPT_STARTED for {attempt}"]

    terminals = attempts.terminals.get(attempt, [])
    if not terminals:
        return ["the attempt has no terminal record; this collector cannot prove it finished"]
    if len(terminals) > 1:
        return [
            "the attempt has "
            + str(len(terminals))
            + " terminal records: "
            + ", ".join(str(event.get("event")) for event in terminals)
        ]

    terminal = terminals[0]
    reasons: list[str] = []
    if terminal.get("event") != FINISHED:
        reasons.append(f"the attempt ended {terminal.get('event')!r}")
    if terminal.get("corpus_appended") is not True:
        reasons.append("the terminal record does not say the corpus row was written")

    for name in JOINED_IDENTITY:
        wanted = entry.get(name)
        if name in start and start.get(name) != wanted:
            reasons.append(f"ATTEMPT_STARTED {name} {start.get(name)!r}, row {wanted!r}")
        if name in terminal and terminal.get(name) != wanted:
            reasons.append(f"terminal {name} {terminal.get(name)!r}, row {wanted!r}")
    return reasons


def _latency_reasons(entry: dict[str, Any]) -> list[str]:
    artifact = entry.get("latency_artifact") or {}
    path = artifact.get("path")
    if not path:
        return ["no live latency artifact"]
    expected = {
        "slug": entry.get("slug"),
        "market_id": entry.get("market_id"),
        "condition_id": entry.get("condition_id"),
        "t0_ns": entry.get("t0_ns"),
        "source_revision": entry.get("source_revision"),
        "source_tree_sha": entry.get("source_tree_sha"),
        "config_sha256": entry.get("config_sha256"),
        "epoch": entry.get("epoch"),
        "run_mode": entry.get("run_mode"),
        "sample_every": entry.get("sample_every"),
    }
    try:
        payload = read_latency(
            Path(str(path)),
            expected_sha256=artifact.get("sha256"),
            expected_identity=expected,
        )
    except Exception as error:
        return [f"latency artifact: {type(error).__name__}: {error}"]

    series = payload.get("series_ns") or {}
    reasons: list[str] = []
    for name in ("clob_receive_to_decide", "spot_receive_to_decide"):
        if not series.get(name):
            reasons.append(f"the latency artifact has no {name} samples")
    return reasons


def qualifying_rows(
    entries: Iterable[dict[str, Any]],
    events: Iterable[dict[str, Any]],
    *,
    epoch: str,
    config_sha256: str,
    source_revision: str,
    source_tree_sha: str | None = None,
    run_mode: str = "ACCEPTANCE_CLEAN",
    verify_latency: bool = True,
) -> list[Qualification]:
    """Judge every row. The count is `sum(q.qualifies for q in ...)` and nothing else."""
    attempts = AttemptIndex.build(events)
    return [
        qualification_of(
            entry,
            attempts,
            epoch=epoch,
            config_sha256=config_sha256,
            source_revision=source_revision,
            source_tree_sha=source_tree_sha,
            run_mode=run_mode,
            verify_latency=verify_latency,
        )
        for entry in entries
    ]
