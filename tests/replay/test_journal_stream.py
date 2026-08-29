"""The streaming journal writer produces the canonical journal, byte for byte.

`encode_journal` is the definition of the format and stays the definition; everything here
compares against it rather than restating it. A test that re-derived the expected bytes from the
same rules the encoder uses would be checking a paraphrase.

The journals built here are **SUPPORTING UNIT TEST ONLY**: synthetic, and evidence about this
code and nothing else. The byte-identity proof on real `p13-corpus-6` journals lives in
`test_real_journal_bytes.py`, which runs against the immutable corpus files.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from maker5m.replay import (
    Journal,
    decode_journal,
    encode_journal,
    iter_encoded_journal,
    write_journal_stream,
)
from maker5m.replay.codec import encode_line
from maker5m.replay.writer import PARTIAL_SUFFIX
from tests.replay.corpus import synthetic_run

RUN = synthetic_run()
JOURNAL = RUN.journal


def test_streamed_lines_concatenate_to_encode_journal() -> None:
    """The contract that makes the writer safe: joining the stream *is* the encoder."""
    assert b"".join(iter_encoded_journal(JOURNAL)) == encode_journal(JOURNAL)


def test_every_streamed_line_is_one_record_and_one_newline() -> None:
    lines = list(iter_encoded_journal(JOURNAL))
    assert len(lines) == len(JOURNAL.steps) + 1
    for line in lines:
        assert line.endswith(b"\n")
        assert line.count(b"\n") == 1
        # Exactly `encode_line(record) + b"\n"`, so the record round-trips on its own.
        assert encode_line(json.loads(line[:-1])) == line[:-1]


def test_written_file_is_byte_identical_to_encode_journal(tmp_path: Path) -> None:
    path = tmp_path / "market.journal.ndjson"
    result = write_journal_stream(path, JOURNAL)
    expected = encode_journal(JOURNAL)
    assert path.read_bytes() == expected
    assert result.bytes_written == len(expected)
    assert result.records == len(JOURNAL.steps) + 1


def test_writer_reports_the_exact_size_and_digest_of_what_it_wrote(tmp_path: Path) -> None:
    path = tmp_path / "market.journal.ndjson"
    result = write_journal_stream(path, JOURNAL)
    raw = path.read_bytes()
    assert result.sha256 == hashlib.sha256(raw).hexdigest()
    assert result.bytes_written == len(raw)
    assert result.path == path


def test_decoding_the_streamed_file_gives_back_the_same_journal(tmp_path: Path) -> None:
    path = tmp_path / "market.journal.ndjson"
    write_journal_stream(path, JOURNAL)
    decoded = decode_journal(path.read_bytes())
    assert decoded == JOURNAL


def test_re_encoding_the_streamed_bytes_reproduces_them(tmp_path: Path) -> None:
    path = tmp_path / "market.journal.ndjson"
    write_journal_stream(path, JOURNAL)
    raw = path.read_bytes()
    assert encode_journal(decode_journal(raw)) == raw


class _RaisingSteps:
    """A step sequence that fails after the first item. Proves laziness, and proves cleanup."""

    def __init__(self, first: Any) -> None:
        self.first = first
        self.reached = 0

    def __iter__(self) -> Iterator[Any]:
        self.reached += 1
        yield self.first
        raise RuntimeError("the second step was reached")


def _lazy_journal() -> tuple[Journal, _RaisingSteps]:
    steps = _RaisingSteps(JOURNAL.steps[0])
    journal = cast(Journal, type("_Lazy", (), {"header": JOURNAL.header, "steps": steps})())
    return journal, steps


def test_the_stream_does_not_build_the_whole_journal_before_yielding() -> None:
    """Two lines come out before the third step is even looked at."""
    journal, _ = _lazy_journal()
    stream = iter_encoded_journal(journal)
    assert next(stream).endswith(b"\n")  # the header
    assert next(stream).endswith(b"\n")  # the first step
    with pytest.raises(RuntimeError):
        next(stream)


def test_encode_journal_by_contrast_needs_every_step_at_once() -> None:
    """The comparison that makes the previous test mean something."""
    journal, _ = _lazy_journal()
    with pytest.raises(RuntimeError):
        encode_journal(journal)


def test_a_failed_write_leaves_no_journal_at_the_real_path(tmp_path: Path) -> None:
    """A short journal is a different market. It must not be there to be read."""
    journal, _ = _lazy_journal()
    path = tmp_path / "market.journal.ndjson"
    with pytest.raises(RuntimeError):
        write_journal_stream(path, journal)
    assert not path.exists()
    assert not path.with_name(path.name + PARTIAL_SUFFIX).exists()
    assert list(tmp_path.iterdir()) == []


def test_the_writer_creates_the_directory_it_needs(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "deeper" / "market.journal.ndjson"
    result = write_journal_stream(path, JOURNAL)
    assert path.exists()
    assert result.bytes_written == len(encode_journal(JOURNAL))


def test_encode_journal_is_still_exported_and_unchanged() -> None:
    """P5's API is not renamed, wrapped or deprecated by any of this."""
    import maker5m.replay as replay

    assert replay.encode_journal is encode_journal
    assert callable(replay.encode_journal)
    assert isinstance(encode_journal(JOURNAL), bytes)
