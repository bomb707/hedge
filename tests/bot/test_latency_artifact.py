"""The live latency has to outlive the market, exactly and verifiably.

**SUPPORTING UNIT TEST ONLY.** These prove that samples survive, that merging is exact, and that
a corrupt artifact is refused. What the latencies *were* comes from real markets.
"""

from __future__ import annotations

import hashlib
import json
import lzma
from pathlib import Path
from typing import Any

import pytest
from tools.p13_corpus_report import merged_latency

from maker5m.bot import LATENCY_SCHEMA_VERSION, read_latency, write_latency
from maker5m.bot.latency import validate_latency_identity
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


BUILD: dict[str, Any] = {
    "source_revision": "rev",
    "source_tree_sha": "tree",
    "config_sha256": "cfg",
    "epoch": "p13c-pilot-1",
    "run_mode": "ACCEPTANCE_CLEAN",
}


def written(
    tmp_path: Path,
    name: str,
    clob: list[int],
    spot: list[int],
    decide: list[int],
    **identity: Any,
) -> dict[str, Any]:
    fields = {
        "slug": name,
        "market_id": f"0x{name}",
        "condition_id": f"0xcond-{name}",
        "t0_ns": 1_787_811_600_000_000_000,
        **BUILD,
        **identity,
    }
    artifact = write_latency(
        tmp_path / f"{name}.latency.json.xz",
        analyzer_with(clob, spot, decide),
        identity=fields,
        hot_path_ns=[10, 20, 30],
    )
    return {
        "slug": name,
        "market_id": f"0x{name}",
        "condition_id": f"0xcond-{name}",
        "t0_ns": 1_787_811_600_000_000_000,
        **BUILD,
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
        expected_identity=entry,
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


def test_a_row_pointing_at_another_markets_artifact_is_refused(tmp_path: Path) -> None:
    """§16. The file is perfectly hash-valid and perfectly schema-valid. It is the wrong market.

    A hash proves the bytes are the bytes that were written. It says nothing about *which* market
    they were written for, so an edited index, a copied file or a rebuilt directory could point a
    row at another market's latency and satisfy every check P13C had.
    """
    first = written(tmp_path, "market-a", [100] * 5, [200] * 5, [5])
    second = written(tmp_path, "market-b", [900] * 5, [800] * 5, [9])

    # A's row now names B's artifact — path and hash both consistent with each other.
    swapped = {**first, "latency_artifact": second["latency_artifact"]}

    with pytest.raises(ValueError, match="not this market's latency"):
        read_latency(
            Path(swapped["latency_artifact"]["path"]),
            expected_sha256=swapped["latency_artifact"]["sha256"],
            expected_identity=swapped,
        )

    merged = merged_latency([swapped])
    assert merged["markets_merged"] == 0
    assert merged["all_markets_merged"] is False
    reason = merged["markets_refused"][0]["reason"]
    assert "slug" in reason and "market_id" in reason
    assert merged["series_ns"] == {}, "and not one of its samples was merged in"


@pytest.mark.parametrize(
    "field_name", ["source_revision", "config_sha256", "source_tree_sha", "epoch", "run_mode"]
)
def test_an_artifact_from_another_build_is_refused(tmp_path: Path, field_name: str) -> None:
    """§17. Right market, wrong build. Not this row's evidence."""
    entry = written(tmp_path, "market-a", [1, 2], [3], [4], **{field_name: "something-else"})

    with pytest.raises(ValueError, match=field_name):
        read_latency(
            Path(entry["latency_artifact"]["path"]),
            expected_sha256=entry["latency_artifact"]["sha256"],
            expected_identity=entry,
        )
    assert merged_latency([entry])["markets_merged"] == 0


def test_an_artifact_missing_an_identity_field_is_refused(tmp_path: Path) -> None:
    """Missing is not equality. An artifact that omits the field cannot satisfy it."""
    from maker5m.bot.latency import validate_latency_identity

    entry = written(tmp_path, "market-a", [1], [2], [3])
    payload = read_latency(Path(entry["latency_artifact"]["path"]))
    del payload["source_tree_sha"]

    problems = validate_latency_identity(payload, entry)
    assert problems == ["source_tree_sha is absent from the artifact"]


def test_a_sample_every_that_differs_from_the_run_is_refused(tmp_path: Path) -> None:
    from maker5m.bot.latency import validate_latency_identity

    entry = written(tmp_path, "market-a", [1], [2], [3])
    payload = read_latency(Path(entry["latency_artifact"]["path"]))
    assert validate_latency_identity(payload, {**entry, "sample_every": 10}) == []
    assert validate_latency_identity(payload, {**entry, "sample_every": 25}) == [
        "sample_every 10, the run used 25"
    ]


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


# -- §16: exact means typed, and required means present ----------------------------------------


def rewrite(entry: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """Rewrite an artifact's payload in place, keeping its hash consistent with its bytes."""
    path = Path(entry["latency_artifact"]["path"])
    payload = read_latency(path)
    for key, value in changes.items():
        if value is _ABSENT:
            payload.pop(key, None)
        elif key == "sample_every":
            if value is _ABSENT:
                payload["sampling"].pop("sample_every", None)
            else:
                payload["sampling"]["sample_every"] = value
        else:
            payload[key] = value
    raw = lzma.compress(json.dumps(payload).encode())
    path.write_bytes(raw)
    return {
        **entry,
        "latency_artifact": {
            **entry["latency_artifact"],
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
    }


class _Absent:
    pass


_ABSENT = _Absent()


@pytest.mark.parametrize("value", [True, 1.0, "1", None])
def test_a_schema_version_of_the_wrong_type_is_refused(tmp_path: Path, value: Any) -> None:
    """`True == 1` and `1.0 == 1` in Python. "Exact equality" has to be written, not just said."""
    entry = written(tmp_path, "market-a", [1], [2], [3])
    broken = rewrite(entry, schema_version=value)

    with pytest.raises(ValueError, match="schema"):
        read_latency(
            Path(broken["latency_artifact"]["path"]),
            expected_sha256=broken["latency_artifact"]["sha256"],
            expected_identity=broken,
        )


@pytest.mark.parametrize("value", [True, 1_787_811_600_000_000_000.0])
def test_a_t0_of_the_wrong_type_is_refused(tmp_path: Path, value: Any) -> None:
    entry = written(tmp_path, "market-a", [1], [2], [3])
    broken = rewrite(entry, t0_ns=value)
    problems = validate_latency_identity(
        read_latency(Path(broken["latency_artifact"]["path"])), entry
    )
    assert any("t0_ns" in problem for problem in problems)


@pytest.mark.parametrize("value", [10.0, True])
def test_a_sample_every_of_the_wrong_type_is_refused(tmp_path: Path, value: Any) -> None:
    entry = written(tmp_path, "market-a", [1], [2], [3])
    broken = rewrite(entry, sample_every=value)
    payload = read_latency(Path(broken["latency_artifact"]["path"]))
    problems = validate_latency_identity(payload, {**entry, "sample_every": 10})
    assert any("sample_every" in problem for problem in problems)


def test_a_missing_sample_every_is_refused(tmp_path: Path) -> None:
    entry = written(tmp_path, "market-a", [1], [2], [3])
    path = Path(entry["latency_artifact"]["path"])
    payload = read_latency(path)
    del payload["sampling"]["sample_every"]
    path.write_bytes(lzma.compress(json.dumps(payload).encode()))

    problems = validate_latency_identity(read_latency(path), entry)
    assert "sampling.sample_every is absent from the artifact" in problems


@pytest.mark.parametrize("field_name", ["condition_id", "t0_ns"])
def test_a_missing_required_identity_field_is_refused(tmp_path: Path, field_name: str) -> None:
    """§15. These were compared only when present, which contradicts the rule beside them."""
    entry = written(tmp_path, "market-a", [1], [2], [3])
    path = Path(entry["latency_artifact"]["path"])
    payload = read_latency(path)
    del payload[field_name]
    path.write_bytes(lzma.compress(json.dumps(payload).encode()))

    problems = validate_latency_identity(read_latency(path), entry)
    assert f"{field_name} is absent from the artifact" in problems


def test_a_string_identity_field_of_the_wrong_type_is_refused(tmp_path: Path) -> None:
    entry = written(tmp_path, "market-a", [1], [2], [3])
    path = Path(entry["latency_artifact"]["path"])
    payload = read_latency(path)
    payload["slug"] = 12345
    path.write_bytes(lzma.compress(json.dumps(payload).encode()))

    problems = validate_latency_identity(read_latency(path), entry)
    assert any("not a str" in problem for problem in problems)


def test_a_correct_artifact_still_passes_the_strict_validator(tmp_path: Path) -> None:
    entry = written(tmp_path, "market-a", [1, 2], [3], [4])
    payload = read_latency(
        Path(entry["latency_artifact"]["path"]),
        expected_sha256=entry["latency_artifact"]["sha256"],
        expected_identity={**entry, "sample_every": 10},
    )
    assert payload["slug"] == "market-a"
    assert validate_latency_identity(payload, {**entry, "sample_every": 10}) == []


@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_the_reader_refuses_a_mistyped_schema_even_without_an_expected_identity(
    tmp_path: Path, value: Any
) -> None:
    """§20. The reader's own gate, for callers that pass no identity to check against."""
    entry = written(tmp_path, "market-a", [1], [2], [3])
    broken = rewrite(entry, schema_version=value)

    with pytest.raises(ValueError, match="latency schema"):
        read_latency(Path(broken["latency_artifact"]["path"]))
