"""Warm at T0, not warm once.

**SUPPORTING UNIT TEST ONLY.** These drive the readiness bookkeeping `capture_market` keeps,
with the disconnects a real pre-arm window can contain. They prove a rule about provenance; what
the feeds actually did on a given day comes from the corpus.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from maker5m.bot import MarketSession, UiPlane
from maker5m.market.timebase import NANOS_PER_SECOND, TimestampNs
from tests.bot.test_multi_market import T0_A, paper, session

SECOND = NANOS_PER_SECOND


class Warmth:
    """The exact bookkeeping `capture_market` performs, driven by hand.

    Reproduced rather than imported because the loop it lives in owns a websocket; the rule is
    three lines and this drives those three lines with the events a real window produces.
    """

    def __init__(self, session: MarketSession) -> None:
        self.session = session
        self.state: dict[str, int | None] = {
            "clob_ready_since_ns": None,
            "spot_ready_since_ns": None,
        }

    def hold(self, key: str, ready: bool, at_ns: int) -> None:
        if not ready:
            self.state[key] = None
        elif self.state[key] is None:
            self.state[key] = at_ns

    def clob(self, *, ready: bool, at: float) -> None:
        when = int(self.session.t0_ns - at * SECOND)
        if ready:
            self.session._note_warm("clob_book_ready_ns", TimestampNs(when))
        self.hold("clob_ready_since_ns", ready, when)

    def spot(self, *, ready: bool, at: float) -> None:
        when = int(self.session.t0_ns - at * SECOND)
        if ready:
            self.session._note_warm("spot_first_valid_ns", TimestampNs(when))
        self.hold("spot_ready_since_ns", ready, when)

    def cross_t0(self) -> dict[str, Any]:
        self.session._note_prearm(
            {
                "at_ns": self.session.t0_ns,
                "clob_ready": self.state["clob_ready_since_ns"] is not None,
                "spot_ready": self.state["spot_ready_since_ns"] is not None,
                "clob_ready_since_ns": self.state["clob_ready_since_ns"],
                "spot_ready_since_ns": self.state["spot_ready_since_ns"],
            }
        )
        return self.session.prearm_summary()


def warmth(tmp_path: Path) -> Warmth:
    return Warmth(session(tmp_path, UiPlane(directory=tmp_path / "ui"), T0_A, "a"))


def test_a_clob_disconnect_before_t0_makes_the_market_unwarm(tmp_path: Path) -> None:
    """Case A: ready at T0-29, spot at T0-28, CLOB drops at T0-10 and never returns."""
    unit = warmth(tmp_path)
    unit.clob(ready=True, at=29)
    unit.spot(ready=True, at=28)
    unit.clob(ready=False, at=10)
    prearm = unit.cross_t0()

    assert prearm["clob_ready_at_t0"] is False
    assert prearm["feed_ready_before_t0"] is False
    assert prearm["feed_ready_ns"] is None
    assert prearm["first_clob_lead_seconds"] == 29.0, "the diagnostic milestone is untouched"


def test_a_recovered_clob_measures_its_lead_from_the_recovery(tmp_path: Path) -> None:
    """Case B: the same, then fresh snapshots at T0-4. Warm — for four seconds, not twenty-eight."""
    unit = warmth(tmp_path)
    unit.clob(ready=True, at=29)
    unit.spot(ready=True, at=28)
    unit.clob(ready=False, at=10)
    unit.clob(ready=True, at=4)
    prearm = unit.cross_t0()

    assert prearm["feed_ready_before_t0"] is True
    assert prearm["clob_lead_seconds"] == 4.0
    assert prearm["feed_ready_lead_seconds"] == 4.0
    assert prearm["first_clob_lead_seconds"] == 29.0


def test_a_spot_disconnect_before_t0_makes_the_market_unwarm(tmp_path: Path) -> None:
    """Case C: a book throughout, a spot tick at T0-29, and the feed gone by T0-5."""
    unit = warmth(tmp_path)
    unit.clob(ready=True, at=29)
    unit.spot(ready=True, at=29)
    unit.spot(ready=False, at=5)
    prearm = unit.cross_t0()

    assert prearm["spot_ready_at_t0"] is False
    assert prearm["feed_ready_before_t0"] is False


def test_a_recovered_spot_measures_its_lead_from_the_new_tick(tmp_path: Path) -> None:
    """Case D: one fresh tick at T0-1 is a warm market, with a one-second lead."""
    unit = warmth(tmp_path)
    unit.clob(ready=True, at=29)
    unit.spot(ready=True, at=29)
    unit.spot(ready=False, at=5)
    unit.spot(ready=True, at=1)
    prearm = unit.cross_t0()

    assert prearm["feed_ready_before_t0"] is True
    assert prearm["spot_lead_seconds"] == 1.0
    assert prearm["feed_ready_lead_seconds"] == 1.0
    assert prearm["first_spot_lead_seconds"] == 29.0


def test_a_market_that_never_crossed_t0_is_not_warm(tmp_path: Path) -> None:
    """No boundary snapshot means no claim. A capture that died before T0 proves nothing."""
    unit = warmth(tmp_path)
    unit.clob(ready=True, at=29)
    unit.spot(ready=True, at=28)

    assert unit.session.feed_ready_ns is None
    assert unit.session.prearm_summary()["feed_ready_before_t0"] is False


def test_an_unwarm_market_is_not_evidence(tmp_path: Path) -> None:
    from tests.bot.test_multi_market import acceptance, clean_cold, collected

    supervisor = acceptance(paper(tmp_path))
    unit = collected(tmp_path, UiPlane(directory=tmp_path / "ui"), warm=False)
    driver = Warmth(unit)
    driver.clob(ready=True, at=29)
    driver.spot(ready=True, at=28)
    driver.clob(ready=False, at=10)
    driver.cross_t0()

    entry = supervisor._entry(unit, clean_cold())
    assert entry["evidence_eligible"] is False
    assert any("not warm before T0" in fault for fault in entry["operational_faults"])
