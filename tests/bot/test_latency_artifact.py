"""The live latency has to outlive the market, exactly and verifiably.

**SUPPORTING UNIT TEST ONLY.** These prove that samples survive, that merging is exact, and that
a corrupt artifact is refused. What the latencies *were* comes from real markets.
"""

from __future__ import annotations

import json
import lzma
from pathlib import Path
from typing import Any

import pytest
from tools.p13_corpus_report import merged_latency

from maker5m.bot import LATENCY_SCHEMA_VERSION, read_latency, write_latency
from maker5m.telemetry import SamplingPolicy, TelemetryAnalyzer
from maker5m.telemetry.metrics import quantile


def analyzer_with(clob: list[int], spot: list[int], decide: list[int]) -> TelemetryAnalyzer:
    unit = TelemetryAnalyzer(sampling=SamplingPolicy(sample_every=10))
    for value in clob:
        unit.latency.clob_receive_to_decide.add(value)
        unit.latency.clob_receive_to_reconcile.add(value * 2)
    for value in spot:
        unit.latency.spot_receive_to_decide.add(value)
        unit.latency.spot_receive_to_reconcile.add(value * 2)
    for value in decide:
        unit.latency.decide_duration.add(value)
    return unit


def written(
    tmp_path: Path, name: str, clob: list[int], spot: list[int], decide: list[int]
) -> dict[str, Any]:
    artifact = write_latency(
        tmp_path / f"{name}.latency.json.xz",
        analyzer_with(clob, spot, decide),
        identity={"slug": name, "market_id": f"0x{name}", "source_revision": "rev"},
        hot_path_ns=[10, 20, 30],
    )
    return {
        "slug": name,
        "latency_artifact": artifact.summary(),
        "verification_status": "COMPLETE",
        "evidence_eligible": True,
    }


def test_every_sample_survives_the_round_trip(tmp_path: Path) -> None:
    clob = [100, 200, 300, 400]
    entry = written(tmp_path, "market-a", clob, [50, 60], [7, 8, 9])
    payload = read_latency(
        Path(entry["latency_artifact"]["path"]),
        expected_sha256=entry["latency_artifact"]["sha256"],
    )

    assert payload["schema_version"] == LATENCY_SCHEMA_VERSION
    assert payload["provenance"] == "REAL_PUBLIC_MARKET_DATA"
    assert payload["series_ns"]["clob_receive_to_decide"] == clob, "exact, not a sketch"
    assert payload["series_ns"]["spot_receive_to_decide"] == [50, 60]
    assert payload["series_ns"]["clob_receive_to_reconcile"] == [200, 400, 600, 800]
    assert payload["hot_path_observe_ns"] == [10, 20, 30]
    assert payload["market_id"] == "0xmarket-a"


def test_merging_two_markets_gives_the_quantiles_of_the_concatenated_samples(
    tmp_path: Path,
) -> None:
    """§8. Not the quantile of the per-market quantiles, which is the quantile of nothing."""
    first = list(range(1, 101))
    second = list(range(1_000, 1_101))
    entries = [
        written(tmp_path, "market-a", first, [11, 12], [1, 2]),
        written(tmp_path, "market-b", second, [13, 14], [3, 4]),
    ]

    merged = merged_latency(entries)
    combined = sorted(first + second)

    clob = merged["series_ns"]["clob_receive_to_decide"]
    assert merged["markets_merged"] == 2
    assert merged["markets_refused"] == []
    assert clob["n"] == len(combined)
    assert clob["p50"] == quantile(combined, 0.50)
    assert clob["p95"] == quantile(combined, 0.95)
    assert clob["p99"] == quantile(combined, 0.99)
    assert clob["max"] == combined[-1]
    assert merged["series_ns"]["spot_receive_to_decide"]["n"] == 4
    assert merged["hot_path_observe_max_by_market"] == {"market-a": 30, "market-b": 30}


def test_clob_and_spot_are_never_merged_into_one_distribution(tmp_path: Path) -> None:
    entries = [written(tmp_path, "market-a", [100] * 10, [900] * 10, [5])]
    merged = merged_latency(entries)
    assert merged["series_ns"]["clob_receive_to_decide"]["p50"] == 100
    assert merged["series_ns"]["spot_receive_to_decide"]["p50"] == 900


def test_a_corrupt_artifact_is_refused_rather_than_read(tmp_path: Path) -> None:
    """§8. The row is the claim; a file that contradicts it cannot also satisfy it."""
    good = written(tmp_path, "market-a", [1, 2, 3], [4], [5])
    bad = written(tmp_path, "market-b", [10, 20], [30], [40])
    path = Path(bad["latency_artifact"]["path"])
    path.write_bytes(lzma.compress(json.dumps({"schema_version": 1}).encode()))

    with pytest.raises(ValueError, match="hashes to"):
        read_latency(path, expected_sha256=bad["latency_artifact"]["sha256"])

    merged = merged_latency([good, bad])
    assert merged["markets_merged"] == 1
    assert [refusal["slug"] for refusal in merged["markets_refused"]] == ["market-b"]
    assert merged["series_ns"]["clob_receive_to_decide"]["n"] == 3


def test_a_missing_artifact_is_named_rather_than_skipped_silently(tmp_path: Path) -> None:
    entry = written(tmp_path, "market-a", [1], [2], [3])
    Path(entry["latency_artifact"]["path"]).unlink()
    merged = merged_latency([entry])
    assert merged["markets_merged"] == 0
    assert "FileNotFoundError" in merged["markets_refused"][0]["reason"]


def test_an_unknown_schema_version_is_refused(tmp_path: Path) -> None:
    entry = written(tmp_path, "market-a", [1], [2], [3])
    path = Path(entry["latency_artifact"]["path"])
    payload = read_latency(path)
    payload["schema_version"] = 99
    path.write_bytes(lzma.compress(json.dumps(payload).encode()))

    with pytest.raises(ValueError, match="latency schema"):
        read_latency(path)


def test_a_market_without_a_latency_artifact_is_not_evidence(tmp_path: Path) -> None:
    from maker5m.bot import UiPlane
    from tests.bot.test_multi_market import acceptance, clean_cold, collected, paper

    supervisor = acceptance(paper(tmp_path))
    unit = collected(tmp_path, UiPlane(directory=tmp_path / "ui"), latency=False)
    entry = supervisor._entry(unit, clean_cold())

    assert entry["latency_artifact"] is None
    assert entry["evidence_eligible"] is False
    assert any("live latency artifact" in fault for fault in entry["operational_faults"])


def test_a_market_missing_one_trigger_is_not_evidence(tmp_path: Path) -> None:
    """Both feeds drive decisions. A market with samples for only one measured only half of it."""
    import asyncio

    from maker5m.bot import UiPlane
    from tests.bot.test_multi_market import acceptance, clean_cold, collected, paper

    supervisor = acceptance(paper(tmp_path))
    unit = collected(tmp_path, UiPlane(directory=tmp_path / "ui"), latency=False)
    for index in range(10):
        unit.analyzer.latency.clob_receive_to_decide.add(1_000 + index)
    asyncio.run(unit.write_latency_artifact({"source_revision": "rev"}))

    entry = supervisor._entry(unit, clean_cold())
    assert entry["evidence_eligible"] is False
    assert any("one of the two triggers" in fault for fault in entry["operational_faults"])
