"""The corpus index: append-only, restart-safe, and never a rewritten history.

**SUPPORTING UNIT TEST ONLY.** These prove indexing, resume and refusal mechanics. Nothing here
proves anything about a market — queue quality, stale rate and fill opportunity come from real
captures and from nowhere else.
"""

from __future__ import annotations

import json
from pathlib import Path

from maker5m.bot import CorpusIndex


def entry(slug: str, status: str = "COMPLETE", *, eligible: bool = True) -> dict[str, object]:
    return {
        "slug": slug,
        "verification_status": status,
        "evidence_eligible": eligible,
        "decisions": 124_272,
    }


def test_an_appended_entry_survives_being_read_back(tmp_path: Path) -> None:
    index = CorpusIndex(path=tmp_path / "corpus.jsonl")
    assert index.append(entry("btc-updown-5m-1")) is True
    assert index.entries() == [entry("btc-updown-5m-1")]


def test_a_second_attempt_appends_rather_than_replacing(tmp_path: Path) -> None:
    """A market that went badly is evidence. Overwriting it would destroy the only record."""
    index = CorpusIndex(path=tmp_path / "corpus.jsonl")
    index.append(entry("btc-updown-5m-1", "INCOMPLETE", eligible=False))
    index.append(entry("btc-updown-5m-1"))
    assert index.attempts("btc-updown-5m-1") == 2
    assert [e["verification_status"] for e in index.entries()] == ["INCOMPLETE", "COMPLETE"]


def test_resume_skips_a_completed_slug_and_nothing_else(tmp_path: Path) -> None:
    index = CorpusIndex(path=tmp_path / "corpus.jsonl")
    index.append(entry("done-1"))
    index.append(entry("failed-1", "INCOMPLETE", eligible=False))
    index.append(entry("corrupt-1", "CORRUPT", eligible=False))
    assert index.completed_slugs() == {"done-1"}


def test_a_truncated_final_line_does_not_cost_the_entries_before_it(tmp_path: Path) -> None:
    """A kill mid-append leaves half a line. Everything written before it still stands."""
    path = tmp_path / "corpus.jsonl"
    index = CorpusIndex(path=path)
    index.append(entry("done-1"))
    index.append(entry("done-2"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"slug": "done-3", "verificat')

    assert [e["slug"] for e in index.entries()] == ["done-1", "done-2"]
    assert index.completed_slugs() == {"done-1", "done-2"}
    assert index.stats().truncated_lines == 1


def test_an_odd_value_is_recorded_as_text_rather_than_losing_the_entry(tmp_path: Path) -> None:
    """A stray type must not cost a market its corpus line. It is stringified and kept."""
    path = tmp_path / "corpus.jsonl"
    index = CorpusIndex(path=path)
    assert index.append({"slug": "odd", "value": {1, 2, 3}}) is True
    assert isinstance(index.entries()[0]["value"], str)
    assert path.read_text("utf-8").count("\n") == 1


def test_an_unwritable_index_is_counted_rather_than_raised(tmp_path: Path) -> None:
    """The corpus is Plane 3. A full disk is recorded; it does not end the run."""
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")
    index = CorpusIndex(path=blocked / "corpus.jsonl")
    assert index.append(entry("done-1")) is False
    assert index.append_errors == 1
    assert index.error_samples


def test_the_index_counts_what_it_holds(tmp_path: Path) -> None:
    index = CorpusIndex(path=tmp_path / "corpus.jsonl")
    index.append(entry("a"))
    index.append(entry("b"))
    index.append(entry("c", "INCOMPLETE", eligible=False))
    index.append(entry("d", "UNSUPPORTED", eligible=False))
    index.append(entry("e", "COMPLETE", eligible=False))
    stats = index.stats()
    assert stats.attempted == 5
    assert stats.complete == 3
    assert stats.incomplete == 1
    assert stats.unsupported == 1
    assert stats.eligible == 2, "COMPLETE is not the same claim as evidence-eligible"


def test_entries_are_one_json_object_per_line(tmp_path: Path) -> None:
    path = tmp_path / "corpus.jsonl"
    index = CorpusIndex(path=path)
    index.append(entry("a"))
    index.append(entry("b"))
    lines = path.read_text("utf-8").splitlines()
    assert len(lines) == 2
    assert all(isinstance(json.loads(line), dict) for line in lines)
