"""The P6 live acceptance evidence, asserted from the committed capture manifests.

Two full `btc-updown-5m-*` markets were captured read-only and verified by the P5 replay
engine. The journals are ~150-200 MB each and live outside Git (see `docs/evidence/README.md`
and `STATUS.md`); these manifests are the committed, machine-checkable record of what they
contain and what verification returned.

A manifest is a record of a run, not a substitute for one. It is asserted here so that a
later change which quietly breaks an adapter cannot leave the claimed evidence standing
unchallenged in the documentation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from maker5m.numeric import SCALE_DECIMALS
from maker5m.replay import JournalProvenance

EVIDENCE = Path(__file__).resolve().parents[2] / "docs" / "evidence"
MANIFESTS = sorted(EVIDENCE.glob("p6-capture-*.manifest.json"))
PRIMARY = "p6-capture-btc-updown-5m-1787647500.manifest.json"


def load_manifest(name: str) -> dict[str, object]:
    data: dict[str, object] = json.loads((EVIDENCE / name).read_text(encoding="utf-8"))
    return data


def test_capture_evidence_exists() -> None:
    assert MANIFESTS, "the P6 live acceptance gate requires at least one captured market"


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.name)
def test_each_capture_is_real_and_read_only(path: Path) -> None:
    """Provenance is LIVE_PAPER: real market data, no real orders."""
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["provenance"] == JournalProvenance.LIVE_PAPER.value
    assert data["provenance"] != JournalProvenance.SYNTHETIC.value
    assert data["slug"].startswith("btc-updown-5m-")


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.name)
def test_each_capture_covers_the_whole_market_lifecycle(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["phases"]) == {"PREARM", "QUOTE", "ENDGAME", "SETTLING", "DONE"}
    assert data["end_ns"] - data["t0_ns"] == 300 * 1_000_000_000
    assert data["event_kinds"]["PhaseEvent"] == 4


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.name)
def test_p5_verified_every_decision_and_the_final_state(path: Path) -> None:
    """The first real-data exercise of the replay engine."""
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["replay_verified"] is True
    assert data["final_state_matches"] is True
    assert data["byte_roundtrip_identical"] is True
    assert data["replay_steps"] == data["steps"]
    assert data["steps"] > 10_000


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.name)
def test_spot_alone_drove_decisions_in_the_live_run(path: Path) -> None:
    """Invariant I11, demonstrated on real traffic rather than only with a fake transport."""
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["event_kinds"]["SpotTick"] > 1_000
    assert data["counters"]["spot_messages"] > 1_000


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.name)
def test_the_heartbeat_was_answered(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["counters"]["pongs"] > 0, "the documented PING/PONG heartbeat produced no PONG"


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.name)
def test_nothing_was_malformed_dropped_or_unhandled(path: Path) -> None:
    counters = json.loads(path.read_text(encoding="utf-8"))["counters"]
    assert counters["malformed"] == 0
    assert counters["clob_unhandled"] == 0
    assert counters["unhandled_kinds"] == {}
    assert counters["telemetry_dropped"] == 0


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.name)
def test_o10_live_traffic_is_exactly_representable(path: Path) -> None:
    """The residual O10 validation, measured over the whole captured market."""
    precision = json.loads(path.read_text(encoding="utf-8"))["precision"]
    assert precision["polymarket_price"]["max_decimals"] <= SCALE_DECIMALS
    assert precision["polymarket_size"]["max_decimals"] <= SCALE_DECIMALS
    assert precision["polymarket_price"]["samples"] > 10_000
    assert precision["polymarket_size"]["samples"] > 10_000


def test_o12_evidence_shows_metadata_driven_btc_precision() -> None:
    """The BTC feed's own precision, over thousands of live messages."""
    precision = load_manifest(PRIMARY)["precision"]
    btc = precision["binance_price"]  # type: ignore[index]
    assert btc["samples"] > 1_000
    assert btc["max_decimals"] == 8
    assert btc["min_decimals"] == 8


def test_the_venue_announced_a_tick_change_during_a_captured_market() -> None:
    """Venue tick and strategy tick are genuinely different, not just theoretically.

    The venue changed its legal price increment mid-market and the finest observed price
    carried three decimals, while the replica stayed on its documented 0.01 grid throughout.
    """
    data = load_manifest(PRIMARY)
    assert data["venue_tick_changes"] == 4
    price = data["precision"]["polymarket_price"]  # type: ignore[index]
    assert price["max_decimals"] == 3
    assert "0.001" in price["examples"].values()


def test_prearm_completed_before_the_next_market_started() -> None:
    for path in MANIFESTS:
        data = json.loads(path.read_text(encoding="utf-8"))
        prearm = data["prearm"]
        assert prearm["slug"], "no next market was pre-armed"
        assert prearm["slack_ns"] > 0, "pre-arm did not finish before the next T0"
        assert prearm["ready_ns"] < prearm["next_t0_ns"]


def test_clock_health_was_sampled_without_being_corrected() -> None:
    health = load_manifest(PRIMARY)["clock_health"]
    assert health["samples"] > 10_000  # type: ignore[index]
    assert isinstance(health["max_abs_offset_ns"], int)  # type: ignore[index]


def test_the_journal_is_recorded_as_external_with_a_digest() -> None:
    """Too large for Git, so it must be identifiable by digest instead."""
    data = load_manifest(PRIMARY)
    assert len(str(data["sha256"])) == 64
    assert int(str(data["journal_bytes"])) > 1_000_000
    status = (EVIDENCE.parent / "STATUS.md").read_text(encoding="utf-8")
    assert str(data["sha256"]) in status, "STATUS.md must record the artefact digest"
    assert "tools/capture_market.py" in status, "STATUS.md must record how to reproduce it"
