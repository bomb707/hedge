"""The live latency a market actually experienced, kept after the market is gone.

Why this exists
---------------
P8's analyzer holds the real distributions — CLOB and spot receive-to-decide, receive-to-reconcile,
and the three stage durations — and they die with the session. P13's first two corpora kept only
`hot_path_observe_ns`, which mixes triggering kinds and is not P8's per-trigger decision-latency
contract, so the one number a live paper phase exists to produce was being thrown away every five
minutes.

**A replay cannot stand in for it.** Replaying a journal measures how fast this machine
re-derives decisions today, with no sockets, no contention and a warm cache. It is a useful
number and it is not the latency the market was traded at.

What this writes
----------------
One immutable artifact per market: every raw sample, losslessly, `lzma`-compressed. Not a sketch,
not quantiles — quantiles cannot be merged across two hundred markets without lying, and the
corpus report has to be able to compute an exact p99 over the whole collection. Roughly twelve
thousand sampled cycles per market compress to well under a megabyte.

Latency remains **sampled** exactly as P8 accepted; this changes what survives, not what is
measured. Writing and compressing happen on the cold path, after the market has closed.
"""

from __future__ import annotations

import hashlib
import json
import lzma
from pathlib import Path
from typing import Any, Final

__all__ = [
    "LATENCY_SCHEMA_VERSION",
    "LatencyArtifact",
    "read_latency",
    "validate_latency_identity",
    "write_latency",
]

LATENCY_SCHEMA_VERSION: Final[int] = 1

SERIES: Final[tuple[str, ...]] = (
    "clob_receive_to_decide",
    "spot_receive_to_decide",
    "phase_receive_to_decide",
    "fill_receive_to_decide",
    "clob_receive_to_reconcile",
    "spot_receive_to_reconcile",
    "phase_receive_to_reconcile",
    "fill_receive_to_reconcile",
    "receive_to_reconcile",
    "decide_duration",
    "prepare_duration",
    "reconcile_duration",
    "keep_cycle",
    "acting_cycle",
)
"""Everything P8 measured, kept apart by trigger. CLOB and spot are never merged for reporting."""


class LatencyArtifact:
    """One market's live latency, on disk, identified by hash."""

    __slots__ = ("bytes", "path", "series", "sha256")

    def __init__(self, path: Path, size: int, sha256: str, series: dict[str, int]) -> None:
        self.path = path
        self.bytes = size
        self.sha256 = sha256
        self.series = series

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "bytes": self.bytes,
            "sha256": self.sha256,
            "schema_version": LATENCY_SCHEMA_VERSION,
            "sample_counts": self.series,
            "lossless": True,
            "note": (
                "Every raw sample, not a sketch. Quantiles do not merge across markets, and the "
                "corpus report computes exact quantiles over the concatenated live samples."
            ),
        }


def _payload(analyzer: Any, *, identity: dict[str, Any], hot_path_ns: list[int]) -> dict[str, Any]:
    latency = analyzer.latency
    series: dict[str, list[int]] = {}
    for name in SERIES:
        distribution = getattr(latency, name, None)
        if distribution is not None:
            series[name] = list(distribution.samples)
    return {
        "schema_version": LATENCY_SCHEMA_VERSION,
        "kind": "P13_LIVE_LATENCY",
        "provenance": "REAL_PUBLIC_MARKET_DATA",
        "note": (
            "Latency measured on the live market, by P8, under its accepted sampling policy. "
            "Not a replay: re-deriving decisions from a journal measures this machine today, "
            "not the market that was traded."
        ),
        **identity,
        "sampling": {
            "policy": "P8 SamplingPolicy — unchanged",
            "sample_every": getattr(analyzer.sampling, "sample_every", None),
            "cycles_with_stage_timing": analyzer.stages_captured,
            "observations_processed": analyzer.processed,
        },
        "series_ns": series,
        "hot_path_observe_ns": list(hot_path_ns),
    }


def write_latency(
    path: Path, analyzer: Any, *, identity: dict[str, Any], hot_path_ns: list[int]
) -> LatencyArtifact:
    """Write one market's samples, compressed, and return its identity. Cold path only."""
    payload = _payload(analyzer, identity=identity, hot_path_ns=hot_path_ns)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    compressed = lzma.compress(raw, preset=6)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(compressed)
    return LatencyArtifact(
        path=path,
        size=len(compressed),
        sha256=hashlib.sha256(compressed).hexdigest(),
        series={name: len(values) for name, values in payload["series_ns"].items()},
    )


TEXT_IDENTITY: Final[tuple[str, ...]] = (
    "slug",
    "market_id",
    "condition_id",
    "source_revision",
    "source_tree_sha",
    "config_sha256",
    "epoch",
    "run_mode",
)
"""What an artifact must agree with its corpus row about. All of them, present, and exact.

`condition_id` and `t0_ns` were optional — compared when both sides carried them — which
contradicts the rule stated three lines away: missing is not equality. For a v1 artifact they are
part of the contract, so an artifact that omits one cannot satisfy a comparison against it."""


def _exact_str(value: Any) -> bool:
    return type(value) is str


def _exact_int(value: Any) -> bool:
    """A real integer. `True` is an `int` in Python and is not a schema version."""
    return type(value) is int


def validate_latency_identity(payload: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    """Check that this artifact belongs to the market whose row names it.

    A hash proves a file is the file that was written. It says nothing about *which market* it
    was written for, so a row pointing at another market's artifact — by an edit, a copy, a
    rebuilt index — passed every check P13C had while describing a different five minutes
    entirely.

    Exact equality, no coercion, and **missing is not equality**: an artifact that omits the
    field cannot satisfy a comparison against it. Returns the mismatches; empty means it agrees.
    """
    problems: list[str] = []
    version = payload.get("schema_version")
    if not _exact_int(version) or version != LATENCY_SCHEMA_VERSION:
        # `True == 1` and `1.0 == 1` in Python, so plain equality would accept a boolean or a
        # float as a schema version. "Exact equality, no coercion" has to be written as well as
        # said.
        problems.append(
            f"schema_version {version!r} ({type(version).__name__}), expected "
            f"{LATENCY_SCHEMA_VERSION} as an int"
        )
    if payload.get("kind") != "P13_LIVE_LATENCY":
        problems.append(f"kind {payload.get('kind')!r}")
    if payload.get("provenance") != "REAL_PUBLIC_MARKET_DATA":
        problems.append(f"provenance {payload.get('provenance')!r}")

    for field_name in TEXT_IDENTITY:
        want = expected.get(field_name)
        got = payload.get(field_name)
        if field_name not in payload:
            problems.append(f"{field_name} is absent from the artifact")
        elif not _exact_str(got):
            problems.append(f"{field_name} {got!r} is a {type(got).__name__}, not a str")
        elif got != want:
            problems.append(f"{field_name} {got!r}, the row says {want!r}")

    if "t0_ns" not in payload:
        problems.append("t0_ns is absent from the artifact")
    elif not _exact_int(payload["t0_ns"]):
        problems.append(
            f"t0_ns {payload['t0_ns']!r} is a {type(payload['t0_ns']).__name__}, not an int"
        )
    elif payload["t0_ns"] != expected.get("t0_ns"):
        problems.append(f"t0_ns {payload['t0_ns']!r}, the row says {expected.get('t0_ns')!r}")

    recorded = (payload.get("sampling") or {}).get("sample_every")
    wanted = expected.get("sample_every")
    if recorded is None:
        problems.append("sampling.sample_every is absent from the artifact")
    elif not _exact_int(recorded):
        problems.append(f"sample_every {recorded!r} is a {type(recorded).__name__}, not an int")
    elif wanted is not None and recorded != wanted:
        problems.append(f"sample_every {recorded!r}, the run used {wanted!r}")
    return problems


def read_latency(
    path: Path,
    *,
    expected_sha256: str | None = None,
    expected_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read one artifact back, refusing anything that is not this market's latency.

    Two different claims are checked. The hash says the bytes are the bytes that were written;
    the identity says they were written for *this* market, by *this* build. A file can satisfy
    the first and fail the second, which is exactly the case a swapped or stale artifact
    produces.
    """
    compressed = path.read_bytes()
    digest = hashlib.sha256(compressed).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError(
            f"{path.name} hashes to {digest[:12]}…, the corpus recorded {expected_sha256[:12]}…"
        )
    parsed: dict[str, Any] = json.loads(lzma.decompress(compressed).decode("utf-8"))
    version = parsed.get("schema_version")
    # Typed here too, not only inside the identity check: a caller that passes no expected
    # identity still must not be able to read `True`, `1.0` or `"1"` as schema 1.
    if not _exact_int(version) or version != LATENCY_SCHEMA_VERSION:
        raise ValueError(
            f"{path.name} declares latency schema {version!r} "
            f"({type(version).__name__}), this build reads {LATENCY_SCHEMA_VERSION} as an int"
        )
    if expected_identity is not None:
        problems = validate_latency_identity(parsed, expected_identity)
        if problems:
            raise ValueError(f"{path.name} is not this market's latency: {'; '.join(problems)}")
    return parsed
