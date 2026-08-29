"""Memory and collector diagnostics: what they read, and what they do when they cannot read it.

Two separate obligations here. The parsers must be right, because a misread `smaps_rollup` field
would put a wrong number in an acceptance document. And every reader must fail to `None` on a
platform that does not have it, because a diagnostic that raises would take a market down — the
collector is not allowed to be less reliable for having been instrumented.

`GcEventLog` gets the most attention. P13's corpus reported a per-market maximum full-collection
pause that was really a *running* maximum over the whole process: forty markets were credited
with a pause only one of them had caused. Attribution from events is the correction, so the
overlap arithmetic is tested at every boundary rather than in the easy middle.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from pathlib import Path
from typing import Any

import pytest

from maker5m.bot import UiPlane, diagnostics
from maker5m.bot import session as session_module
from maker5m.bot.resources import GcEventLog, GcObserver
from tests.bot.test_multi_market import session

# -- /proc parsers ---------------------------------------------------------------------------

STATUS_SAMPLE = """Name:\tpython3
Threads:\t12
VmSize:\t 5242880 kB
VmRSS:\t 4194304 kB
RssAnon:\t 4000000 kB
RssFile:\t  194304 kB
RssShmem:\t       0 kB
VmData:\t 4500000 kB
VmSwap:\t       0 kB
"""

ROLLUP_SAMPLE = """5b71-7fff ---p 00000000 00:00 0                          [rollup]
Rss:             4194304 kB
Pss:             4100000 kB
Private_Clean:         0 kB
Private_Dirty:   4000000 kB
Shared_Clean:     194304 kB
Shared_Dirty:          0 kB
Anonymous:       4000000 kB
AnonHugePages:         0 kB
Swap:                  0 kB
"""


def test_status_is_parsed_into_bytes_not_kilobytes() -> None:
    parsed = diagnostics._parse_kb_table(STATUS_SAMPLE, diagnostics.STATUS_FIELDS)
    assert parsed["VmRSS"] == 4194304 * 1024
    assert parsed["RssAnon"] == 4000000 * 1024
    assert parsed["RssFile"] == 194304 * 1024
    assert parsed["VmSwap"] == 0


def test_a_count_without_a_unit_is_not_scaled() -> None:
    """`Threads: 12` is twelve threads, not twelve kilobytes of thread."""
    parsed = diagnostics._parse_kb_table(STATUS_SAMPLE, diagnostics.STATUS_FIELDS)
    assert parsed["Threads"] == 12


def test_rollup_is_parsed_and_the_header_line_is_ignored() -> None:
    parsed = diagnostics._parse_kb_table(ROLLUP_SAMPLE, diagnostics.ROLLUP_FIELDS)
    assert parsed["Pss"] == 4100000 * 1024
    assert parsed["Private_Dirty"] == 4000000 * 1024
    assert parsed["Shared_Clean"] == 194304 * 1024
    assert parsed["AnonHugePages"] == 0


def test_a_field_the_kernel_did_not_print_is_none_and_not_zero() -> None:
    """The distinction the whole module exists to keep: absent is not empty."""
    parsed = diagnostics._parse_kb_table("VmRSS:\t 100 kB\n", diagnostics.STATUS_FIELDS)
    assert parsed["VmRSS"] == 100 * 1024
    assert parsed["RssAnon"] is None
    assert parsed["VmSwap"] is None
    assert set(parsed) == set(diagnostics.STATUS_FIELDS)


def test_an_unreadable_proc_file_gives_every_field_as_none(monkeypatch: Any) -> None:
    def refuse(*_: Any, **__: Any) -> str:
        raise OSError("no /proc here")

    monkeypatch.setattr(Path, "read_text", refuse)
    assert set(diagnostics.memory_status().values()) == {None}
    assert set(diagnostics.smaps_rollup().values()) == {None}


# -- glibc ------------------------------------------------------------------------------------


def test_mallinfo2_falls_back_to_none_without_a_libc(monkeypatch: Any) -> None:
    monkeypatch.setattr(diagnostics, "_libc", lambda: None)
    assert diagnostics.mallinfo2() is None
    assert diagnostics.malloc_trim(0) is None


def test_mallinfo2_falls_back_to_none_when_the_symbol_is_missing(monkeypatch: Any) -> None:
    class _NoSymbols:
        def __getattr__(self, name: str) -> Any:
            raise AttributeError(name)

    monkeypatch.setattr(diagnostics, "_libc", lambda: _NoSymbols())
    assert diagnostics.mallinfo2() is None
    assert diagnostics.malloc_trim(0) is None


def test_a_snapshot_survives_a_platform_with_nothing_to_offer(monkeypatch: Any) -> None:
    """The collector must not be less reliable for having been instrumented."""

    def refuse(*_: Any, **__: Any) -> str:
        raise OSError("no /proc here")

    monkeypatch.setattr(Path, "read_text", refuse)
    monkeypatch.setattr(diagnostics, "_libc", lambda: None)
    taken = diagnostics.snapshot("nowhere")
    assert taken.rss_bytes is None
    assert taken.malloc is None
    assert taken.compact()["rss"] is None
    assert taken.compact()["arena"] is None
    assert taken.compact()["threads"] >= 1


def test_a_real_snapshot_on_this_platform_reports_something() -> None:
    taken = diagnostics.snapshot("here", tracked_objects=True)
    assert taken.rss_bytes is not None and taken.rss_bytes > 0
    assert taken.heap["allocated_blocks"] > 0
    assert taken.heap["tracked_objects"] is not None
    assert sum(taken.threads.values()) >= 1


def test_tracked_objects_is_none_unless_it_was_asked_for() -> None:
    """It is the expensive one. Absent means not measured, never zero objects."""
    assert diagnostics.python_heap()["tracked_objects"] is None
    assert diagnostics.python_heap(tracked_objects=True)["tracked_objects"] is not None


# -- the quiescent probe ------------------------------------------------------------------------


def test_the_probe_refuses_while_a_market_is_still_resident() -> None:
    probe = diagnostics.quiescent_probe("mid-market", live_sessions=1)
    assert probe.refused is not None
    assert probe.verdict() == "NOT_QUIESCENT"
    assert probe.before is None and probe.after_gc is None
    assert probe.collected is None


def test_the_probe_runs_when_nothing_is_live() -> None:
    probe = diagnostics.quiescent_probe("quiet", live_sessions=0, tracked_objects=False)
    assert probe.refused is None
    assert probe.collected is not None
    assert probe.before is not None and probe.after_trim is not None
    assert probe.verdict() in {
        "PYTHON_CYCLIC_RETENTION",
        "NATIVE_FREE_HEAP_RETAINED",
        "MIXED",
        "NOTHING_RELEASED",
        "NOT_MEASURED",
    }


# -- generation-2 attribution --------------------------------------------------------------------


def _log(*events: tuple[int, int, int]) -> GcEventLog:
    log = GcEventLog()
    for generation, start, end in events:
        log.record(generation, start, end)
    return log


def test_generation_zero_is_counted_but_not_recorded() -> None:
    """950,584 of them in seventeen hours. The counter is the right instrument."""
    log = _log((0, 10, 20), (1, 30, 40))
    assert len(log) == 1
    assert log.generations[0] == 1


def test_a_market_window_sees_only_the_collections_inside_it() -> None:
    log = _log((2, 100, 200), (2, 500, 600), (2, 900, 1000))
    window = log.window(400, 800)
    assert window.collections == {2: 1}
    assert window.total_pause_ns == {2: 100}
    assert window.longest_pause_ns == {2: 100}
    assert [event["start_ns"] for event in window.events] == [500]


def test_a_collection_straddling_the_start_counts_and_both_figures_are_kept() -> None:
    """The process paid the whole pause; this market waited for the part inside its window."""
    log = _log((2, 90, 150))
    window = log.window(100, 200)
    assert window.collections == {2: 1}
    assert window.total_pause_ns == {2: 60}
    assert window.overlap_pause_ns == {2: 50}


def test_a_collection_straddling_the_end_counts_the_same_way() -> None:
    log = _log((2, 150, 260))
    window = log.window(100, 200)
    assert window.total_pause_ns == {2: 110}
    assert window.overlap_pause_ns == {2: 50}


def test_a_collection_that_only_touches_the_boundary_does_not_count() -> None:
    """Half-open: `[from, to)`. A pause that ended exactly at the start was not in the window."""
    assert _log((2, 0, 100)).window(100, 200).collections == {}
    assert _log((2, 200, 300)).window(100, 200).collections == {}


def test_generations_are_kept_apart() -> None:
    log = _log((1, 100, 110), (1, 120, 125), (2, 130, 400))
    window = log.window(0, 1000)
    assert window.collections == {1: 2, 2: 1}
    assert window.total_pause_ns == {1: 15, 2: 270}
    assert window.longest_pause_ns == {1: 10, 2: 270}
    # Only generation 2 is listed event by event.
    assert [event["generation"] for event in window.events] == [2]


def test_a_running_maximum_is_not_a_market_maximum() -> None:
    """The defect this class exists to correct, stated as a test.

    Two markets, two collections. The second market's pause is smaller than the first's, and a
    cumulative maximum would report the first's figure for both.
    """
    log = _log((2, 100, 1_100), (2, 2_000, 2_050))
    first = log.window(0, 1_500)
    second = log.window(1_500, 3_000)
    assert first.longest_pause_ns == {2: 1_000}
    assert second.longest_pause_ns == {2: 50}
    assert second.longest_pause_ns[2] < first.longest_pause_ns[2]


def test_a_market_with_no_full_collection_says_so_with_nothing() -> None:
    window = _log((2, 100, 200)).window(300, 400)
    assert window.collections == {}
    assert window.events == ()
    assert window.summary()["collections"] == {}


def test_overflow_is_counted_rather_than_silently_truncating() -> None:
    log = GcEventLog(limit=2)
    for index in range(5):
        log.record(2, index * 10, index * 10 + 1)
    assert len(log) == 2
    assert log.dropped == 3
    assert log.window(0, 1000).summary()["dropped_events"] == 3


def test_the_observer_records_events_and_keeps_its_cumulative_totals() -> None:
    import gc

    observer = GcObserver()
    observer.install()
    try:
        gc.collect(2)
        gc.collect(2)
    finally:
        observer.remove()
    assert observer.collections.get(2, 0) >= 2
    assert len(observer.events) >= 2
    window = observer.window(0, 2**62)
    assert window.collections.get(2, 0) >= 2
    assert window.longest_pause_ns.get(2, 0) > 0


def test_the_observer_summary_says_its_maximum_is_process_wide() -> None:
    """Whoever reads this next must not repeat the corpus's mistake."""
    summary = GcObserver().summary(tracked_objects=False)
    assert summary["max_pause_is_process_wide"] is True
    assert summary["tracked_objects"] is None


def test_the_observer_can_be_installed_twice_without_doubling_its_callback() -> None:
    import gc

    observer = GcObserver()
    observer.install()
    observer.install()
    before = len(gc.callbacks)
    observer.remove()
    assert len(gc.callbacks) == before - 1
    observer.remove()
    assert observer._on_gc not in gc.callbacks


@pytest.mark.parametrize("generation", [0, 1, 2])
def test_the_recording_floor_is_explicit(generation: int) -> None:
    log = GcEventLog(min_generation=2)
    log.record(generation, 0, 1)
    assert len(log) == (1 if generation >= 2 else 0)


# -- isolation: none of this may reach a trading path ---------------------------------------------

FORBIDDEN_ON_THE_HOT_PATH = (
    "snapshot(",
    "checkpoint(",
    "sample_resources(",
    "memory_status(",
    "smaps_rollup(",
    "mallinfo2(",
    "malloc_trim(",
    "gc.collect(",
    "write_journal_stream(",
)


def test_the_ingress_path_reads_no_diagnostics() -> None:
    """The source of the two methods every event goes through, checked for the new calls.

    Behavioural tests cover what these methods do. This one covers what they must never start
    doing: a `/proc` read or a heap walk inside `_observe` would be a self-inflicted pause of
    exactly the kind this phase is measuring.
    """
    for name in ("_observe", "_on_tick"):
        source = inspect.getsource(getattr(session_module.MarketSession, name))
        for call in FORBIDDEN_ON_THE_HOT_PATH:
            assert call not in source, f"{name} must not call {call}"


def test_a_slow_checkpoint_does_not_stall_the_event_loop(tmp_path: Path, monkeypatch: Any) -> None:
    """A `/proc` read that took a third of a second would be invisible to the loop."""
    real = diagnostics.snapshot

    def slow(label: str, **kwargs: Any) -> Any:
        time.sleep(0.3)
        return real(label, **kwargs)

    monkeypatch.setattr(session_module, "snapshot", slow)
    market = session(tmp_path, UiPlane(directory=tmp_path / "ui"), 1_700_000_000, "slow")

    async def drive() -> int:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            deadline = time.monotonic() + 0.35
            while time.monotonic() < deadline:
                ticks += 1
                await asyncio.sleep(0.005)

        await asyncio.gather(market.checkpoint("slow-read"), ticker())
        return ticks

    ticks = asyncio.run(drive())
    assert ticks > 20, f"the loop only advanced {ticks} times; the reading blocked it"
    assert [entry["label"] for entry in market.checkpoints] == ["slow-read"]


def test_a_checkpoint_that_cannot_be_taken_is_an_incident_not_a_crash(
    tmp_path: Path, monkeypatch: Any
) -> None:
    def refuse(*_: Any, **__: Any) -> Any:
        raise OSError("no /proc here")

    monkeypatch.setattr(session_module, "snapshot", refuse)
    market = session(tmp_path, UiPlane(directory=tmp_path / "ui"), 1_700_000_000, "broken")
    asyncio.run(market.checkpoint("nowhere"))
    assert market.checkpoints == []
    assert any("checkpoint nowhere failed" in incident for incident in market.incidents)
