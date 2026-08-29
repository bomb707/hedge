"""Write a canonical journal to disk one line at a time, hashing as it goes.

The bytes are the codec's; nothing here decides what a journal looks like. What it decides is
that a 443 MB journal never exists as a single `bytes` object in the process that recorded it.
`encode_journal` is still the definition of the format and still exists for every fixture and
round-trip that compares against it — this writer consumes `iter_encoded_journal`, whose
concatenation *is* `encode_journal`, so "streamed" and "encoded" are the same file by
construction rather than by hope.

Two other things come out of writing rather than joining, and both are wanted anyway: the size
and the SHA-256 are computed from the bytes as they leave, so nothing has to read the file back
to learn what was written, and a write that fails leaves a `.partial` file that is removed
rather than a truncated journal at the real path that would decode as a shorter market.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from maker5m.replay.codec import iter_encoded_journal
from maker5m.replay.journal import Journal

__all__ = ["PARTIAL_SUFFIX", "JournalWrite", "write_journal_stream"]

PARTIAL_SUFFIX: Final[str] = ".partial"
"""What an unfinished journal is called. Never the final name, so nothing can read a half file."""


@dataclass(frozen=True, slots=True)
class JournalWrite:
    """What was written, measured while writing it. No second pass over the file."""

    path: Path
    bytes_written: int
    sha256: str
    records: int

    def summary(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "bytes_written": self.bytes_written,
            "sha256": self.sha256,
            "records": self.records,
        }


def write_journal_stream(
    path: Path,
    journal: Journal,
    *,
    fsync: bool = True,
    buffer_bytes: int = 1 << 20,
) -> JournalWrite:
    """Stream a journal to `path`. Returns its exact size, digest and record count.

    The whole file passes through a one-megabyte buffer. `fsync` is on because the cold child
    reads this file in another process and the corpus row claims its digest; it is off the event
    loop by construction — this function is called from a worker thread, never from Plane 1.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + PARTIAL_SUFFIX)
    digest = hashlib.sha256()
    written = 0
    records = 0
    try:
        with partial.open("wb", buffering=buffer_bytes) as handle:
            for line in iter_encoded_journal(journal):
                handle.write(line)
                digest.update(line)
                written += len(line)
                records += 1
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        partial.replace(path)
        if fsync:
            _sync_directory(path.parent)
    except BaseException:
        # A journal that is not complete is not a journal. Leaving the partial file behind at the
        # real path would give the cold verifier a shorter market to succeed on.
        partial.unlink(missing_ok=True)
        raise
    return JournalWrite(
        path=path, bytes_written=written, sha256=digest.hexdigest(), records=records
    )


def _sync_directory(directory: Path) -> None:
    """Make the rename itself durable. Best effort: not every filesystem allows it."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - platform dependent
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - some filesystems refuse this
        pass
    finally:
        os.close(fd)
