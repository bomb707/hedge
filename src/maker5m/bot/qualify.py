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

__all__ = [
    "AttemptIndex",
    "Qualification",
    "QualificationReport",
    "qualification_of",
    "qualify_all",
    "qualifying_rows",
]

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

    starts: dict[str, list[dict[str, Any]]]
    terminals: dict[str, list[dict[str, Any]]]

    @classmethod
    def build(cls, events: Iterable[dict[str, Any]]) -> AttemptIndex:
        starts: dict[str, list[dict[str, Any]]] = {}
        terminals: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            attempt = str(event.get("attempt_id"))
            name = str(event.get("event"))
            if name == STARTED:
                # Lists on both sides. An assignment would collapse two starts for one attempt
                # into one and quietly pick a winner, and "exactly one ATTEMPT_STARTED" is a
                # contract, not a description of the usual case.
                starts.setdefault(attempt, []).append(event)
            elif name in {FINISHED, FAILED, ABORTED}:
                terminals.setdefault(attempt, []).append(event)
        return cls(starts=starts, terminals=terminals)

    def duplicate_starts(self) -> dict[str, int]:
        return {attempt: len(events) for attempt, events in self.starts.items() if len(events) > 1}

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
            "attempts_started": sum(len(events) for events in self.starts.values()),
            "attempt_ids_started": len(self.starts),
            "duplicate_start_attempts": len(self.duplicate_starts()),
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
    """Whether one corpus row counts, and every reason it does not.

    Carries the row's position and attempt as well as its slug, because a slug does not identify
    a row: two rows can name the same market, and rejoining judgements to rows by slug alone
    would let a refused row's numbers enter the aggregates on a qualifying neighbour's ticket.
    """

    row_index: int
    slug: str
    attempt_id: str | None
    qualifies: bool
    reasons: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        return {
            "row_index": self.row_index,
            "slug": self.slug,
            "attempt_id": self.attempt_id,
            "qualifies": self.qualifies,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class QualificationReport:
    """Every row judged, and the collection-level facts no single row can see."""

    judgements: tuple[Qualification, ...]
    duplicate_result_attempts: dict[str, int]
    duplicate_market_slugs: dict[str, int]

    @property
    def count(self) -> int:
        return sum(1 for judgement in self.judgements if judgement.qualifies)

    @property
    def attempt_ids(self) -> set[str]:
        return {
            str(judgement.attempt_id)
            for judgement in self.judgements
            if judgement.qualifies and judgement.attempt_id
        }

    @property
    def slugs(self) -> set[str]:
        return {judgement.slug for judgement in self.judgements if judgement.qualifies}

    @property
    def consistent(self) -> bool:
        return not self.duplicate_result_attempts and not self.duplicate_market_slugs

    def summary(self) -> dict[str, Any]:
        return {
            "qualifying": self.count,
            "duplicate_result_attempts": self.duplicate_result_attempts,
            "duplicate_market_slugs": self.duplicate_market_slugs,
            "consistent": self.consistent,
        }


def qualification_of(
    entry: dict[str, Any],
    attempts: AttemptIndex,
    *,
    row_index: int = 0,
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
    attempt_id = entry.get("attempt_id")
    return Qualification(
        row_index=row_index,
        slug=slug,
        attempt_id=None if attempt_id is None else str(attempt_id),
        qualifies=not reasons,
        reasons=tuple(reasons),
    )


def _attempt_reasons(entry: dict[str, Any], attempts: AttemptIndex) -> list[str]:
    attempt_id = entry.get("attempt_id")
    if not attempt_id:
        return ["the row names no attempt"]
    attempt = str(attempt_id)
    started = attempts.starts.get(attempt, [])
    if not started:
        return [f"no ATTEMPT_STARTED for {attempt}"]
    if len(started) > 1:
        return [f"the attempt has {len(started)} ATTEMPT_STARTED records"]
    start = started[0]

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

    # Present *and* equal, on both records. Treating an absent field as agreement is how a
    # half-written audit trail passes an audit: the field nobody wrote is the field nobody can
    # check, and this contract says every one of them is written.
    for name in JOINED_IDENTITY:
        wanted = entry.get(name)
        for label, event in (("ATTEMPT_STARTED", start), ("terminal", terminal)):
            if name not in event:
                reasons.append(f"{label} is missing {name}")
            elif event.get(name) != wanted:
                reasons.append(f"{label} {name} {event.get(name)!r}, row {wanted!r}")
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


def qualify_all(
    entries: Iterable[dict[str, Any]],
    events: Iterable[dict[str, Any]],
    *,
    epoch: str,
    config_sha256: str,
    source_revision: str,
    source_tree_sha: str | None = None,
    run_mode: str = "ACCEPTANCE_CLEAN",
    verify_latency: bool = True,
) -> QualificationReport:
    """Judge every row, then the collection.

    Two facts belong to the corpus rather than to any row in it. **One result per attempt**: two
    rows naming one attempt are two claims about one market, and choosing between them silently
    would be inventing an answer — so neither counts. **One result per market**: the gate is two
    hundred *markets*, not two hundred JSON lines, so two attempts that both produced a result for
    one slug count once at most, and in an acceptance epoch neither counts and the duplication is
    an integrity fault. Neither should ever happen, which is exactly why it is worth refusing
    rather than tidying away.
    """
    rows = list(entries)
    attempts = AttemptIndex.build(events)

    by_attempt: dict[str, int] = {}
    for entry in rows:
        attempt = entry.get("attempt_id")
        if attempt:
            by_attempt[str(attempt)] = by_attempt.get(str(attempt), 0) + 1
    duplicate_attempts = {attempt: count for attempt, count in by_attempt.items() if count > 1}

    judged: list[Qualification] = []
    for index, entry in enumerate(rows):
        judgement = qualification_of(
            entry,
            attempts,
            row_index=index,
            epoch=epoch,
            config_sha256=config_sha256,
            source_revision=source_revision,
            source_tree_sha=source_tree_sha,
            run_mode=run_mode,
            verify_latency=verify_latency,
        )
        attempt = judgement.attempt_id
        if attempt is not None and attempt in duplicate_attempts:
            judgement = _refuse(
                judgement,
                f"{duplicate_attempts[attempt]} result rows name attempt {attempt}",
            )
        judged.append(judgement)

    slug_counts: dict[str, int] = {}
    for judgement in judged:
        if judgement.qualifies:
            slug_counts[judgement.slug] = slug_counts.get(judgement.slug, 0) + 1
    duplicate_slugs = {slug: count for slug, count in slug_counts.items() if count > 1}
    if duplicate_slugs:
        judged = [
            _refuse(
                judgement,
                f"{duplicate_slugs[judgement.slug]} qualifying results for market {judgement.slug}",
            )
            if judgement.qualifies and judgement.slug in duplicate_slugs
            else judgement
            for judgement in judged
        ]

    return QualificationReport(
        judgements=tuple(judged),
        duplicate_result_attempts=duplicate_attempts,
        duplicate_market_slugs=duplicate_slugs,
    )


def _refuse(judgement: Qualification, reason: str) -> Qualification:
    return Qualification(
        row_index=judgement.row_index,
        slug=judgement.slug,
        attempt_id=judgement.attempt_id,
        qualifies=False,
        reasons=(*judgement.reasons, reason),
    )


def qualifying_rows(
    entries: Iterable[dict[str, Any]],
    events: Iterable[dict[str, Any]],
    **judgement: Any,
) -> list[Qualification]:
    """The judgements alone. Kept for callers that do not need the collection-level facts."""
    return list(qualify_all(entries, events, **judgement).judgements)
