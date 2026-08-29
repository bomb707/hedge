"""Allocator maintenance may only happen where no market can be quoting.

**SUPPORTING SOFTWARE TEST ONLY.** These are contract and isolation tests. Whether a real trim
disturbs a real feed is not answerable here and is answered by the controlled real pilot — a
`malloc_trim` takes the allocator's locks process-wide, and a responsive event loop is evidence
about the event loop and nothing else. That distinction is the reason the last test in this file
is named the way it is.

The window comes from the canonical phase machine, not from a number chosen here: quoting stops
at `T0+280` and the next market's quoting begins at its own `T0+3`, which is `T0+303` on the
closing market's clock. Ten of those twenty-three seconds are reserved.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from pathlib import Path
from typing import Any

import pytest

from maker5m.bot import maintenance as maintenance_module
from maker5m.bot.maintenance import (
    MAINTENANCE_MARGIN_S,
    AllocatorMaintenance,
    maintenance_window,
)
from maker5m.market.phase import CANONICAL_PHASE_CONFIG, Phase, phase_at
from maker5m.market.timebase import NANOS_PER_SECOND, TimestampNs

T0 = 1_787_949_900
"""A real market boundary from the corpus. Any multiple of 300 would do; this one existed."""


class FakeSession:
    """Only what the contract reads: an identity and a T0. Phase is derived, never asserted."""

    def __init__(self, t0_seconds: int, slug: str) -> None:
        self.t0_ns = t0_seconds * NANOS_PER_SECOND
        self.slug = slug


def at(offset_s: float) -> int:
    return int((T0 + offset_s) * NANOS_PER_SECOND)


def rollover_sessions() -> list[FakeSession]:
    """The two markets that exist at a rollover: the one closing and the one opening."""
    return [FakeSession(T0, "closing"), FakeSession(T0 + 300, "opening")]


# -- the window --------------------------------------------------------------------------------


@pytest.mark.parametrize("offset", [3, 100, 239])
def test_maintenance_is_refused_while_a_market_is_in_quote(offset: float) -> None:
    window = maintenance_window(at(offset), rollover_sessions())
    assert not window.allowed
    assert "quoting" in window.reason
    assert window.phases["closing"] == Phase.QUOTE.value


def test_the_instant_of_t0_is_prearm_and_is_still_refused() -> None:
    """Quoting begins at T0+3, so T0 itself is PREARM — and the gap is 280 seconds away."""
    window = maintenance_window(at(0), rollover_sessions())
    assert not window.allowed
    assert window.phases["closing"] == Phase.PREARM.value
    assert "still quoting for another" in window.reason


@pytest.mark.parametrize("offset", [240, 260, 279, 279.9])
def test_maintenance_is_refused_while_a_market_is_in_endgame(offset: float) -> None:
    window = maintenance_window(at(offset), rollover_sessions())
    assert not window.allowed
    assert "quoting" in window.reason
    assert window.phases["closing"] == Phase.ENDGAME.value


@pytest.mark.parametrize("offset", [280, 285, 290, 292.9])
def test_maintenance_is_allowed_in_the_settling_prearm_gap(offset: float) -> None:
    window = maintenance_window(at(offset), rollover_sessions())
    assert window.allowed, window.reason
    assert window.phases["closing"] == Phase.SETTLING.value
    assert window.phases["opening"] == Phase.PREARM.value
    assert window.seconds_to_quote_start is not None
    assert window.seconds_to_quote_start >= MAINTENANCE_MARGIN_S


def test_the_window_opens_exactly_at_the_stop_quoting_boundary() -> None:
    """One tenth of a second earlier is ENDGAME, and ENDGAME is refused."""
    assert not maintenance_window(at(279.9), rollover_sessions()).allowed
    assert maintenance_window(at(280.0), rollover_sessions()).allowed


def test_the_window_closes_a_full_margin_before_the_next_market_quotes() -> None:
    """Quoting resumes at T0+303. The last permitted instant is T0+293."""
    assert maintenance_window(at(292.99), rollover_sessions()).allowed
    late = maintenance_window(at(293.01), rollover_sessions())
    assert not late.allowed
    assert "margin" in late.reason


@pytest.mark.parametrize("offset", [293.5, 295, 300, 302])
def test_a_missed_window_is_skipped_rather_than_run_late(offset: float) -> None:
    window = maintenance_window(at(offset), rollover_sessions())
    assert not window.allowed
    assert window.reason


def test_the_margin_is_configurable_and_is_what_closes_the_window() -> None:
    generous = maintenance_window(at(295), rollover_sessions(), margin_s=5.0)
    assert generous.allowed
    strict = maintenance_window(at(295), rollover_sessions(), margin_s=15.0)
    assert not strict.allowed


def test_the_derived_phases_agree_with_the_canonical_phase_machine() -> None:
    """The contract must not carry its own idea of what a phase is."""
    for offset in (0, 3, 120, 240, 280, 299, 301):
        window = maintenance_window(at(offset), [FakeSession(T0, "closing")])
        expected = phase_at(
            TimestampNs(T0 * NANOS_PER_SECOND), TimestampNs(at(offset)), CANONICAL_PHASE_CONFIG
        )
        assert window.phases["closing"] == expected.value


def test_a_session_from_an_unrelated_market_still_blocks_if_it_is_quoting() -> None:
    """The gap is only a gap if *every* live market is out of it."""
    stray = FakeSession(T0 + 150, "off-grid")
    window = maintenance_window(at(285), [*rollover_sessions(), stray])
    assert not window.allowed
    assert "off-grid" in window.reason


def test_shutdown_refuses_maintenance() -> None:
    window = maintenance_window(at(285), rollover_sessions(), shutting_down=True)
    assert not window.allowed
    assert "shutting down" in window.reason


def test_a_rollover_already_maintained_is_refused() -> None:
    first = maintenance_window(at(285), rollover_sessions())
    assert first.allowed and first.rollover == T0 + 300
    again = maintenance_window(at(288), rollover_sessions(), completed=[T0 + 300])
    assert not again.allowed
    assert "already" in again.reason


def test_an_empty_process_may_still_maintain_between_markets() -> None:
    """No sessions is not a reason to refuse; it is the quietest case there is."""
    assert maintenance_window(at(285), []).allowed


# -- one per rollover --------------------------------------------------------------------------


def test_exactly_one_trim_per_rollover_is_claimed() -> None:
    owner = AllocatorMaintenance()
    first = owner.consider(at(281), rollover_sessions())
    assert owner.claim(first) is True
    second = owner.consider(at(285), rollover_sessions())
    assert not second.allowed
    assert owner.claim(second) is False
    assert owner.completed == {T0 + 300}


def test_the_next_rollover_is_a_new_claim() -> None:
    owner = AllocatorMaintenance()
    assert owner.claim(owner.consider(at(281), rollover_sessions())) is True
    later = owner.consider(
        at(281 + 300), [FakeSession(T0 + 300, "closing"), FakeSession(T0 + 600, "opening")]
    )
    assert later.allowed
    assert owner.claim(later) is True
    assert owner.completed == {T0 + 300, T0 + 600}


def test_a_refused_window_can_never_be_claimed() -> None:
    owner = AllocatorMaintenance()
    assert owner.claim(owner.consider(at(100), rollover_sessions())) is False
    assert owner.completed == set()


def test_refusals_are_counted_rather_than_discarded() -> None:
    owner = AllocatorMaintenance()
    for offset in (100, 120, 250):
        owner.consider(at(offset), rollover_sessions())
    assert sum(owner.refusals.values()) == 3


def test_the_policy_is_not_adaptive() -> None:
    """It must not consult memory, market size or activity. That would be a policy search."""
    source = inspect.getsource(AllocatorMaintenance.consider) + inspect.getsource(
        maintenance_module.maintenance_window
    )
    for forbidden in ("rss", "fordblks", "uordblks", "journal", "decisions"):
        assert forbidden not in source.lower(), f"the window decision looked at {forbidden}"
    assert AllocatorMaintenance().summary()["policy"]["adaptive"] is False


# -- the trim itself ----------------------------------------------------------------------------


def test_a_trim_records_both_readings_and_what_it_released() -> None:
    owner = AllocatorMaintenance()
    window = owner.consider(at(285), rollover_sessions())
    record = owner.trim(window)
    assert record["rollover"] == T0 + 300
    assert record["duration_ns"] >= 0
    assert record["error"] is None
    assert set(record["before"]) == set(record["after"])
    assert record["before"]["rss"] is not None
    assert owner.events == [record]


def test_an_unsupported_platform_releases_nothing_and_says_so(monkeypatch: Any) -> None:
    """`returned: None` is a fact about the platform, never a measurement of zero."""
    monkeypatch.setattr(maintenance_module, "malloc_trim", lambda _pad: None)
    owner = AllocatorMaintenance()
    record = owner.trim(owner.consider(at(285), rollover_sessions()))
    assert record["returned"] is None
    assert record["error"] is None
    assert owner.summary()["unsupported"] == 1
    assert owner.summary()["successful"] == 0


def test_a_failing_trim_is_recorded_and_does_not_raise(monkeypatch: Any) -> None:
    def explode(_pad: int) -> bool:
        raise OSError("no allocator here")

    monkeypatch.setattr(maintenance_module, "malloc_trim", explode)
    owner = AllocatorMaintenance()
    record = owner.trim(owner.consider(at(285), rollover_sessions()))
    assert record["returned"] is None
    assert "OSError" in str(record["error"])
    assert owner.summary()["errors"]


def test_a_platform_without_mallinfo_still_produces_a_record(monkeypatch: Any) -> None:
    monkeypatch.setattr(maintenance_module, "mallinfo2", lambda: None)
    owner = AllocatorMaintenance()
    record = owner.trim(owner.consider(at(285), rollover_sessions()))
    assert record["before"]["arena"] is None
    assert record["before"]["fordblks"] is None
    assert record["duration_ns"] >= 0


def test_the_summary_reports_a_run_with_no_trims_as_nothing_not_zero() -> None:
    summary = AllocatorMaintenance().summary()
    assert summary["trims"] == 0
    assert summary["duration_ns"]["p50"] is None
    assert summary["released_rss_bytes"]["p50"] is None


# -- isolation ----------------------------------------------------------------------------------


HOT_PATH_MODULES = (
    "src/maker5m/feeds/pipeline.py",
    "src/maker5m/feeds/merger.py",
    "src/maker5m/strategy/engine.py",
    "src/maker5m/risk/engine.py",
    "src/maker5m/risk/overlay.py",
    "src/maker5m/execution/executor.py",
    "src/maker5m/telemetry/observation.py",
    "src/maker5m/persistence/worker.py",
)

FORBIDDEN = ("malloc_trim", "mallinfo2", "maintenance", "gc.collect", "smaps")


@pytest.mark.parametrize("path", HOT_PATH_MODULES)
def test_no_hot_path_module_imports_or_calls_maintenance(path: str) -> None:
    """Plane 1 must not be able to reach any of this, by import or by name."""
    source = Path(path)
    if not source.is_file():
        pytest.skip(f"{path} is not in this build")
    text = source.read_text("utf-8").lower()
    for name in FORBIDDEN:
        assert name not in text, f"{path} refers to {name}"


def test_the_ingress_methods_do_not_reach_maintenance() -> None:
    from maker5m.bot.session import MarketSession

    for method in ("_observe", "_on_tick"):
        source = inspect.getsource(getattr(MarketSession, method)).lower()
        for name in (*FORBIDDEN, "trim("):
            assert name not in source, f"MarketSession.{method} refers to {name}"


def test_a_blocking_maintenance_call_does_not_stall_the_event_loop() -> None:
    """Proves **Python-loop** isolation only.

    A `malloc_trim` that takes half a second still holds the allocator's locks for half a second,
    and a feed thread allocating a book update in that time waits regardless of which thread made
    the call. This test says the coroutine scheduler kept running. It does not say ingress was
    unaffected — only the real pilot can say that, and the claim is not made anywhere else.
    """
    owner = AllocatorMaintenance()
    window = owner.consider(at(285), rollover_sessions())

    def slow(_window: Any) -> dict[str, Any]:
        time.sleep(0.5)
        return {"duration_ns": 500_000_000}

    async def drive() -> int:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            deadline = time.monotonic() + 0.55
            while time.monotonic() < deadline:
                ticks += 1
                await asyncio.sleep(0.005)

        await asyncio.gather(asyncio.to_thread(slow, window), ticker())
        return ticks

    assert asyncio.run(drive()) > 30


def test_maintenance_holds_no_strategy_or_risk_state() -> None:
    """It is allowed to know the clock and the phase. Nothing else."""
    owner = AllocatorMaintenance()
    fields = set(owner.__dataclass_fields__)
    assert fields == {"enabled", "margin_s", "shutting_down", "completed", "events", "refusals"}
    window = owner.consider(at(285), rollover_sessions())
    assert set(window.summary()) == {
        "allowed",
        "reason",
        "rollover",
        "now_ns",
        "seconds_since_stop_quoting",
        "seconds_to_quote_start",
        "phases",
    }
