"""What writing a journal actually costs the process that recorded it. Measured on real journals.

P13's corpus ended at 4.26 GB of resident memory with a flat tracked-object count. The largest
single allocation the collector makes is the journal: `encode_journal` builds every line and then
joins them, and a P13 journal is 37 to 443 MB. That is a *hypothesis* about where the resident
bytes come from, and this tool exists to test it rather than assert it.

Each measurement runs in its own process, so no reading is contaminated by the one before. The
journals are the real ones from `p13-corpus-6`, opened read-only and never modified; the decoded
object graph is the real recorded market. Nothing here is synthetic.

Run:

    python -m tools.p13_journal_memory compare --journal A --journal B --out DIR
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from maker5m.bot.diagnostics import mallinfo2, malloc_trim, memory_status, smaps_rollup
from maker5m.replay import decode_journal, encode_journal, encode_line, write_journal_stream

MODES = ("legacy", "encode", "stream")


def _legacy_encode_journal(journal: object) -> bytes:
    """`encode_journal` exactly as `p13-corpus-6` ran it, kept here and nowhere else.

    The corpus was collected against a version that built a list of every line, then joined a
    second sequence of ``line + b"\\n"``. Measuring today's implementation and calling it "the
    baseline" would understate what the accepted run actually paid, so the failing version is
    reproduced verbatim for the comparison and is not importable from the package.
    """
    from maker5m.replay.codec import _enc_decision, _enc_event, _enc_header
    from maker5m.replay.schema import RecordType

    lines = [encode_line(_enc_header(journal.header))]  # type: ignore[attr-defined]
    lines.extend(
        encode_line(
            {
                "record_type": RecordType.STEP.value,
                "index": index,
                "event": _enc_event(step.event),
                "decision": _enc_decision(step.decision),
            }
        )
        for index, step in enumerate(journal.steps)  # type: ignore[attr-defined]
    )
    return b"".join(line + b"\n" for line in lines)


@dataclass(slots=True)
class Peak:
    """Highest resident set seen while something was running. Sampled, not inferred."""

    every_s: float = 0.002
    stop: threading.Event = field(default_factory=threading.Event)
    highest: int = 0
    samples: int = 0
    _thread: threading.Thread | None = None

    def _loop(self) -> None:
        while not self.stop.wait(self.every_s):
            rss = memory_status().get("VmRSS")
            if rss is not None:
                self.highest = max(self.highest, rss)
                self.samples += 1

    def __enter__(self) -> Peak:
        self._thread = threading.Thread(target=self._loop, name="peak-rss", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def _reading(label: str) -> dict[str, Any]:
    status = memory_status()
    rollup = smaps_rollup()
    info = mallinfo2()
    return {
        "label": label,
        "rss": status.get("VmRSS"),
        "rss_anon": status.get("RssAnon"),
        "rss_file": status.get("RssFile"),
        "vm_data": status.get("VmData"),
        "pss": rollup.get("Pss"),
        "private_dirty": rollup.get("Private_Dirty"),
        "arena": None if info is None else info["arena"],
        "hblkhd": None if info is None else info["hblkhd"],
        "uordblks": None if info is None else info["uordblks"],
        "fordblks": None if info is None else info["fordblks"],
    }


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 22):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def measure(journal_path: Path, mode: str, out_dir: Path) -> dict[str, Any]:
    """One journal, one encoding path, one process. Every number read from `/proc`."""
    import gc

    readings: list[dict[str, Any]] = [_reading("start")]
    original_sha, original_size = _sha256_file(journal_path)
    readings.append(_reading("after_hash_original"))

    raw = journal_path.read_bytes()
    readings.append(_reading("after_read_bytes"))
    started = perf_counter_ns()
    journal = decode_journal(raw)
    decode_ns = perf_counter_ns() - started
    steps = len(journal.steps)
    del raw
    readings.append(_reading("after_decode"))
    gc.collect()
    readings.append(_reading("after_decode_collect"))

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{journal_path.name}.{mode}"
    readings.append(_reading("before_encode"))

    written_sha = ""
    written_size = 0
    with Peak() as peak:
        started = perf_counter_ns()
        if mode in ("encode", "legacy"):
            blob = encode_journal(journal) if mode == "encode" else _legacy_encode_journal(journal)
            target.write_bytes(blob)
            written_size = len(blob)
            written_sha = hashlib.sha256(blob).hexdigest()
            del blob
        else:
            result = write_journal_stream(target, journal)
            written_size = result.bytes_written
            written_sha = result.sha256
        encode_ns = perf_counter_ns() - started
    readings.append(_reading("after_output"))
    peak_rss = peak.highest

    del journal
    readings.append(_reading("after_delete_journal"))
    gc.collect(2)
    readings.append(_reading("after_gc_collect"))
    trimmed = malloc_trim(0)
    readings.append(_reading("after_malloc_trim"))

    on_disk_sha, on_disk_size = _sha256_file(target)
    target.unlink(missing_ok=True)

    return {
        "journal": str(journal_path),
        "mode": mode,
        "original_sha256": original_sha,
        "original_bytes": original_size,
        "written_sha256": written_sha,
        "written_bytes": written_size,
        "on_disk_sha256": on_disk_sha,
        "on_disk_bytes": on_disk_size,
        "bytes_identical": on_disk_sha == original_sha and on_disk_size == original_size,
        "steps": steps,
        "decode_seconds": round(decode_ns / 1e9, 3),
        "encode_seconds": round(encode_ns / 1e9, 3),
        "peak_rss_during_encode": peak_rss,
        "peak_samples": peak.samples,
        "trimmed": trimmed,
        "readings": readings,
    }


def _child(journal: Path, mode: str, out_dir: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.p13_journal_memory",
            "one",
            "--journal",
            str(journal),
            "--mode",
            mode,
            "--out",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {"journal": str(journal), "mode": mode, "failed": proc.stderr[-2000:]}
    return dict(json.loads(proc.stdout))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    one = sub.add_parser("one", help="measure a single journal in this process")
    one.add_argument("--journal", type=Path, required=True)
    one.add_argument("--mode", choices=MODES, required=True)
    one.add_argument("--out", type=Path, required=True)

    compare = sub.add_parser("compare", help="one fresh process per journal per mode")
    compare.add_argument("--journal", type=Path, action="append", required=True)
    compare.add_argument("--out", type=Path, required=True)
    compare.add_argument("--report", type=Path, default=None)

    args = parser.parse_args()
    if args.command == "one":
        print(json.dumps(measure(args.journal, args.mode, args.out)))
        return

    results = []
    for journal in args.journal:
        for mode in MODES:
            print(f"# {journal.name} {mode}", file=sys.stderr, flush=True)
            results.append(_child(journal, mode, args.out))
    report = {"results": results}
    text = json.dumps(report, indent=2)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, "utf-8")
    print(text)


if __name__ == "__main__":
    main()
