"""Every market the collector touched, recorded before it touched it.

**SUPPORTING UNIT TEST ONLY.** Ledger durability, crash recovery and the attempt/corpus join are
software mechanics. What the markets did comes from the corpus.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from maker5m.bot import PaperConfig
from maker5m.bot.attempts import ABORTED, FINISHED, STARTED, AttemptLedger, LedgerWriteError


def identity() -> dict[str, Any]:
    return {
        "epoch": "p13c-pilot-1",
        "config_sha256": "cfg",
        "source_revision": "rev",
        "source_tree_sha": "tree",
        "working_tree_clean": True,
        "run_mode": "ACCEPTANCE_CLEAN",
    }


def test_a_start_is_durable_before_anything_else_happens(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    ledger = AttemptLedger(path=path)
    attempt = ledger.start(slug="btc-updown-5m-1", t0_ns=1, identity=identity())

    written = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    assert len(written) == 1
    assert written[0]["event"] == STARTED
    assert written[0]["attempt_id"] == attempt
    assert written[0]["slug"] == "btc-updown-5m-1"
    assert written[0]["run_mode"] == "ACCEPTANCE_CLEAN"
    assert written[0]["pid"] > 0


def test_a_ledger_that_cannot_be_written_refuses_the_attempt(tmp_path: Path) -> None:
    """§11. Fail closed: a market nobody recorded is worse than a market nobody collected."""
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")
    ledger = AttemptLedger(path=blocked / "attempts.jsonl")

    with pytest.raises(LedgerWriteError):
        ledger.start(slug="btc-updown-5m-1", t0_ns=1, identity=identity())
    assert ledger.errors


def test_a_terminal_record_is_appended_beside_the_start(tmp_path: Path) -> None:
    ledger = AttemptLedger(path=tmp_path / "attempts.jsonl")
    attempt = ledger.start(slug="btc-updown-5m-1", t0_ns=1, identity=identity())
    ledger.finish(attempt, verification_status="COMPLETE", evidence_eligible=True)

    events = ledger.events()
    assert [event["event"] for event in events] == [STARTED, FINISHED]
    assert events[0]["attempt_id"] == events[1]["attempt_id"]
    assert events[0]["slug"] == "btc-updown-5m-1", "the start is never rewritten"
    assert ledger.open_attempts() == []


def test_a_process_that_died_leaves_an_open_attempt_that_the_next_one_finds(
    tmp_path: Path,
) -> None:
    """§15. `p13-corpus-1` attempted fourteen markets and left twelve rows. The other two
    existed nowhere at all."""
    path = tmp_path / "attempts.jsonl"
    journal = tmp_path / "btc-updown-5m-2.journal.ndjson"
    journal.write_bytes(b"orphan")

    dead = AttemptLedger(path=path)
    dead.start(slug="btc-updown-5m-1", t0_ns=1, identity=identity())
    dead.finish(dead.events()[0]["attempt_id"], verification_status="COMPLETE")
    abandoned = dead.start(
        slug="btc-updown-5m-2",
        t0_ns=2,
        identity=identity(),
        expected_journal=str(journal),
        expected_store=str(tmp_path / "btc-updown-5m-2.p11.sqlite3"),
    )
    # The process dies here: no terminal record, no corpus row.

    revived = AttemptLedger(path=path)
    open_before = revived.open_attempts()
    assert [attempt["attempt_id"] for attempt in open_before] == [abandoned]

    def inventory(attempt: dict[str, Any]) -> dict[str, Any]:
        found = {}
        candidate = Path(str(attempt.get("expected_journal", "")))
        if candidate.exists():
            found[candidate.name] = {"path": str(candidate), "bytes": candidate.stat().st_size}
        return found

    recovered = revived.recover(inventory=inventory)

    assert len(recovered) == 1
    assert revived.open_attempts() == [], "closed off, once"
    terminal = revived.events()[-1]
    assert terminal["event"] == ABORTED
    assert terminal["attempt_id"] == abandoned
    assert terminal["evidence_eligible"] is False
    assert terminal["verification_status"] == "ABORTED"
    assert "btc-updown-5m-2.journal.ndjson" in terminal["orphan_artifacts"]
    assert journal.exists(), "the orphan is inventoried, never deleted"

    summary = revived.summary()
    assert summary["attempts_started"] == 2
    assert summary["attempts_terminal"] == 2
    assert summary["open_attempts"] == 0


def test_recovery_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    ledger = AttemptLedger(path=path)
    ledger.start(slug="btc-updown-5m-1", t0_ns=1, identity=identity())

    assert len(ledger.recover()) == 1
    assert ledger.recover() == [], "a second start-up finds nothing left open"


def test_a_torn_ledger_tail_does_not_swallow_the_next_event(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    ledger = AttemptLedger(path=path)
    ledger.start(slug="btc-updown-5m-1", t0_ns=1, identity=identity())
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"event": "ATTEMPT_FINI')

    revived = AttemptLedger(path=path)
    revived.start(slug="btc-updown-5m-2", t0_ns=2, identity=identity())

    lines = path.read_text("utf-8").splitlines()
    assert len(lines) == 3
    assert [event["slug"] for event in revived.events()] == [
        "btc-updown-5m-1",
        "btc-updown-5m-2",
    ]


def test_every_corpus_row_names_an_attempt(tmp_path: Path) -> None:
    """§14. The join that lets the final report account for every launched market."""
    from maker5m.bot import UiPlane
    from tests.bot.test_multi_market import acceptance, clean_cold, collected, paper

    supervisor = acceptance(paper(tmp_path))
    session = collected(tmp_path, UiPlane(directory=tmp_path / "ui"))
    session.attempt_id = supervisor.ledger.start(
        slug=session.slug, t0_ns=session.t0_ns, identity=identity()
    )

    entry = supervisor._entry(session, clean_cold())
    assert entry["attempt_id"] == session.attempt_id

    started = {
        event["attempt_id"] for event in supervisor.ledger.events() if event["event"] == STARTED
    }
    assert entry["attempt_id"] in started


def test_the_ledger_lives_beside_the_corpus(tmp_path: Path) -> None:
    from maker5m.bot import Supervisor

    config = PaperConfig(
        evidence_dir=tmp_path / "markets",
        corpus_path=tmp_path / "corpus.jsonl",
        ui_dir=tmp_path / "ui",
    )
    supervisor = Supervisor(config=config)
    assert supervisor.ledger.path == tmp_path / "attempts.jsonl"
