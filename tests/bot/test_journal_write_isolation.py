"""Writing a journal must not stall the market that is still trading, and must not change bytes.

**SUPPORTING SOFTWARE TEST ONLY.** These are isolation and byte contracts. What the markets did
comes from the corpus; what the encoder costs in real memory comes from the real-journal
experiment, not from here.

Two markets overlap by design in P13 — one is finalising while the next is past its T0 — so every
diagnostic and every byte of journal output added by the resource work has to go off the event
loop. I19 has no exception for instrumentation.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from maker5m.bot import UiPlane
from maker5m.replay import decode_journal, encode_journal
from tests.bot.test_multi_market import collected
from tests.replay.corpus import synthetic_run


def test_write_journal_produces_the_canonical_bytes_and_reports_them(tmp_path: Path) -> None:
    """The session's own digest is the writer's, and the file is `encode_journal`'s output."""
    market = collected(tmp_path, UiPlane(directory=tmp_path / "ui"), decisions=10)
    market.capture = type("_Capture", (), {"journal": synthetic_run().journal, "counters": None})()
    asyncio.run(market.write_journal())

    expected = encode_journal(synthetic_run().journal)
    raw = market.journal_path.read_bytes()
    assert raw == expected
    assert market.journal_bytes == len(expected)
    assert market.journal_sha256 == hashlib.sha256(expected).hexdigest()
    assert decode_journal(raw) == synthetic_run().journal
    assert not market.incidents


def test_write_journal_checkpoints_around_the_encode_and_the_release(tmp_path: Path) -> None:
    """The three readings that locate a step in resident memory at the journal boundary."""
    market = collected(tmp_path, UiPlane(directory=tmp_path / "ui"), decisions=10)
    market.capture = type("_Capture", (), {"journal": synthetic_run().journal, "counters": None})()
    asyncio.run(market.write_journal())
    assert [entry["label"] for entry in market.checkpoints] == [
        "before_journal_encode",
        "after_journal_write",
        "after_step_release",
    ]


def test_a_journal_that_cannot_be_written_leaves_an_incident_and_no_file(tmp_path: Path) -> None:
    market = collected(tmp_path, UiPlane(directory=tmp_path / "ui"), decisions=10)

    class _Exploding:
        header = synthetic_run().journal.header

        @property
        def steps(self) -> Any:
            raise RuntimeError("the recorded stream is gone")

    market.capture = type("_Capture", (), {"journal": _Exploding(), "counters": None})()
    asyncio.run(market.write_journal())
    assert not market.journal_path.exists()
    assert market.journal_sha256 == ""
    assert any("journal write failed" in incident for incident in market.incidents)
