"""Lossless cold archiving of a closed market. Plane 3, and allowed to be slow.

A real five-minute market produced **853,237,760 bytes** of queryable SQLite — about 5 KB per
decision, ~10 GB per trading hour, ~170 GB for P13's 200-market corpus. That is not a practical
final representation, and it is P11's problem: P12 owns the UI and P15 owns strategy research,
but how telemetry is durably represented is this phase's job.

The measurement, on that exact file:

```text
raw           853,237,760
gzip -6        21,381,697   40x    3.7 s
zstd -19       13,965,457   61x   65.8 s
xz / lzma -6   11,167,576   76x    8.5 s
```

So: `lzma`. It wins on ratio by a wide margin, costs eight seconds of a background thread once
per market, and is in the standard library — no service, no dependency, no format nobody else
can open in five years. The compression is over the whole database file, which is why the ratio
is so extreme: consecutive decision records are nearly identical, and a whole-file window sees
that where a per-row encoder cannot.

**Lossless, and nothing is dropped to achieve it.** No sampling, no field removal, no rounding,
no float. The archive restores to a byte-identical database, which is checked by hash before the
raw file is allowed to be deleted — and it is never deleted by this module without that check
passing.
"""

from __future__ import annotations

import hashlib
import lzma
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "ARCHIVE_SUFFIX",
    "ArchiveResult",
    "archive_store",
    "restore_store",
    "verify_archive",
]

ARCHIVE_SUFFIX: Final[str] = ".sqlite3.xz"
PRESET: Final[int] = 6
"""`lzma` preset 6. Preset 9 was not measurably better on this data and costs more time; the
point of the exercise was the simplest measured solution, not the smallest possible file."""

CHUNK: Final[int] = 1 << 20


def _digest(path: Path) -> tuple[int, str]:
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            sha.update(chunk)
            size += len(chunk)
    return size, sha.hexdigest()


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    """What archiving produced, and what it proved before saying so."""

    archive_path: Path
    raw_bytes: int
    raw_sha256: str
    archive_bytes: int
    archive_sha256: str
    restored_sha256: str
    verified: bool
    compress_seconds: float
    verify_seconds: float

    @property
    def ratio(self) -> float:
        return self.raw_bytes / self.archive_bytes if self.archive_bytes else 0.0

    def summary(self) -> dict[str, object]:
        return {
            "archive_path": str(self.archive_path),
            "raw_bytes": self.raw_bytes,
            "raw_sha256": self.raw_sha256,
            "archive_bytes": self.archive_bytes,
            "archive_sha256": self.archive_sha256,
            "restored_sha256": self.restored_sha256,
            "verified": self.verified,
            "ratio": round(self.ratio, 2),
            "compress_seconds": round(self.compress_seconds, 2),
            "verify_seconds": round(self.verify_seconds, 2),
        }


def archive_store(database: Path, *, remove_raw: bool = False) -> ArchiveResult:
    """Compress a closed database, then prove the archive restores to it exactly.

    The proof is not optional and not a checksum of the compressed bytes: the archive is
    decompressed to a temporary file and hashed, and only a match sets ``verified``. Deleting the
    only copy of a market's telemetry on the strength of an unverified archive would be the
    single worst thing this package could do, so ``remove_raw`` does nothing unless the round
    trip actually succeeded.
    """
    from time import perf_counter

    raw_bytes, raw_sha = _digest(database)
    archive = database.with_suffix(".sqlite3.xz")

    started = perf_counter()
    with (
        database.open("rb") as source,
        lzma.open(archive, "wb", preset=PRESET) as target,
    ):
        shutil.copyfileobj(source, target, CHUNK)
    compress_seconds = perf_counter() - started

    archive_bytes, archive_sha = _digest(archive)

    started = perf_counter()
    restored_sha = _restored_digest(archive)
    verify_seconds = perf_counter() - started
    verified = restored_sha == raw_sha

    if remove_raw and verified:
        database.unlink()

    return ArchiveResult(
        archive_path=archive,
        raw_bytes=raw_bytes,
        raw_sha256=raw_sha,
        archive_bytes=archive_bytes,
        archive_sha256=archive_sha,
        restored_sha256=restored_sha,
        verified=verified,
        compress_seconds=compress_seconds,
        verify_seconds=verify_seconds,
    )


def _restored_digest(archive: Path) -> str:
    """Hash the decompressed stream without writing it out.

    Streaming rather than restoring to disk first: the question is whether the bytes come back,
    and answering it should not need another 853 MB of free space.
    """
    sha = hashlib.sha256()
    with lzma.open(archive, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            sha.update(chunk)
    return sha.hexdigest()


def restore_store(archive: Path, destination: Path) -> Path:
    """Decompress an archive back to a queryable database. The supported P12/P15 read path."""
    with lzma.open(archive, "rb") as source, destination.open("wb") as target:
        shutil.copyfileobj(source, target, CHUNK)
    return destination


def verify_archive(archive: Path, expected_sha256: str) -> bool:
    """Whether this archive still restores to the database it claims to hold."""
    return _restored_digest(archive) == expected_sha256
