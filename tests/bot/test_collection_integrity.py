"""What the collector must refuse to do: accumulate, miscount, or lose a row silently.

**SUPPORTING UNIT TEST ONLY.** Backlog bounds, resume arithmetic and append durability are
software mechanics. Nothing here says anything about a market.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from maker5m.bot import CorpusIndex, PaperConfig, Supervisor
from maker5m.bot.supervisor import MAX_COLD_BACKLOG
from tests.bot.test_multi_market import clean_cold, collected, paper


def _never(event: asyncio.Event, name: str) -> asyncio.Task[None]:
    """A cold task that finishes only when the test lets it."""

    async def wait() -> None:
        await event.wait()

    return asyncio.create_task(wait(), name=name)


def row(slug: str, *, epoch: str, config: str, revision: str) -> dict[str, Any]:
    return {
        "slug": slug,
        "epoch": epoch,
        "config_sha256": config,
        "source_revision": revision,
        "verification_status": "COMPLETE",
        "evidence_eligible": True,
    }


# -- §14-17: the cold backlog is bounded -------------------------------------------------------


def test_the_launch_loop_waits_for_cold_capacity(tmp_path: Path) -> None:
    """§17. Cold tasks that never finish must stop the collector launching more markets."""

    async def scenario() -> tuple[bool, int]:
        supervisor = Supervisor(config=paper(tmp_path))
        stuck = asyncio.Event()
        for index in range(MAX_COLD_BACKLOG):
            supervisor.cold.add(_never(stuck, f"stuck-{index}"))
        try:
            # A deadline already in the past: no capacity, and the answer must be "no".
            allowed = await supervisor._cold_capacity(deadline=0.0)
            return allowed, len(supervisor.cold)
        finally:
            stuck.set()
            await asyncio.gather(*list(supervisor.cold), return_exceptions=True)

    allowed, held = asyncio.run(scenario())
    assert allowed is False
    assert held == MAX_COLD_BACKLOG, "and nothing was launched on top of them"


def test_capacity_returns_when_a_cold_task_finishes(tmp_path: Path) -> None:
    async def scenario() -> bool:
        supervisor = Supervisor(config=paper(tmp_path))
        release = asyncio.Event()
        for index in range(MAX_COLD_BACKLOG):
            supervisor.cold.add(_never(release, f"cold-{index}"))
        asyncio.get_running_loop().call_later(0.05, release.set)
        return await supervisor._cold_capacity(deadline=time.time() + 100)

    assert asyncio.run(scenario()) is True


def test_a_skipped_slot_is_recorded_rather_than_silently_missing(tmp_path: Path) -> None:
    """§16. A missing five-minute window must be visible in the corpus, with its reason."""
    supervisor = Supervisor(config=paper(tmp_path))
    supervisor._record_skip(
        "btc-updown-5m-1787826000", 1_787_826_000, "COLD_BACKLOG_CAP", "three markets in flight"
    )
    entry = supervisor.corpus.entries()[0]
    assert entry["verification_status"] == "NOT_STARTED"
    assert entry["evidence_eligible"] is False
    assert entry["skip_reason"] == "COLD_BACKLOG_CAP"
    assert supervisor.skipped_slots == [
        {"slug": "btc-updown-5m-1787826000", "reason": "COLD_BACKLOG_CAP"}
    ]


def test_the_backlog_high_water_is_recorded(tmp_path: Path) -> None:
    async def scenario() -> int:
        supervisor = Supervisor(config=paper(tmp_path))
        release = asyncio.Event()
        for index in range(MAX_COLD_BACKLOG):
            supervisor.cold.add(_never(release, f"cold-{index}"))
            if len(supervisor.cold) > supervisor.cold_high_water:
                supervisor.cold_high_water = len(supervisor.cold)
        release.set()
        await asyncio.gather(*list(supervisor.cold), return_exceptions=True)
        return supervisor.cold_high_water

    assert asyncio.run(scenario()) == MAX_COLD_BACKLOG


# -- §20-23: a market counts when its row is durable -------------------------------------------


def test_a_market_whose_row_cannot_be_written_does_not_count(tmp_path: Path) -> None:
    """§22. The verifier liking a market is not the same as having recorded it."""

    class RefusingIndex(CorpusIndex):
        def append(self, entry: dict[str, Any]) -> bool:
            self.append_errors += 1
            return False

    supervisor = Supervisor(config=paper(tmp_path))
    supervisor.corpus = RefusingIndex(path=tmp_path / "corpus.jsonl")
    ui_dir = tmp_path / "ui"
    from maker5m.bot import UiPlane

    session = collected(tmp_path, UiPlane(directory=ui_dir))
    entry = supervisor._entry(session, clean_cold())
    assert entry["evidence_eligible"] is True

    appended = supervisor.corpus.append(entry)
    if appended:  # pragma: no cover - the fixture refuses by construction
        supervisor.completed_this_process += 1

    assert appended is False
    assert supervisor.completed == 0
    assert supervisor.corpus.append_errors == 1


def test_the_remaining_target_counts_what_the_corpus_already_holds(tmp_path: Path) -> None:
    """§23. A restart after 150 collects 50 more, not 200 more."""
    config = paper(tmp_path)
    supervisor = Supervisor(config=config, target_markets=200)
    identity = supervisor.identity
    for index in range(150):
        supervisor.corpus.append(
            row(
                f"btc-updown-5m-{index}",
                epoch=config.epoch,
                config=str(identity["config_sha256"]),
                revision=str(identity["source_revision"]),
            )
        )

    supervisor.completed_existing = supervisor.corpus.qualifying(
        epoch=config.epoch,
        config_sha256=str(identity["config_sha256"]),
        source_revision=str(identity["source_revision"]),
    )
    assert supervisor.completed_existing == 150
    assert supervisor.completed == 150
    assert supervisor._keep_going(launched=0) is True

    supervisor.completed_this_process = 50
    assert supervisor.completed == 200
    assert supervisor._keep_going(launched=0) is False, "stops at exactly 200 durable rows"


def test_rows_from_another_epoch_config_or_build_do_not_count(tmp_path: Path) -> None:
    index = CorpusIndex(path=tmp_path / "corpus.jsonl")
    index.append(row("a", epoch="p13-corpus-2", config="cfg", revision="rev"))
    index.append(row("b", epoch="other", config="cfg", revision="rev"))
    index.append(row("c", epoch="p13-corpus-2", config="other", revision="rev"))
    index.append(row("d", epoch="p13-corpus-2", config="cfg", revision="other"))
    assert index.qualifying(epoch="p13-corpus-2", config_sha256="cfg", source_revision="rev") == 1


# -- §24: a torn tail must not eat the next entry ----------------------------------------------


def test_appending_after_a_torn_line_keeps_both_the_wreck_and_the_new_row(
    tmp_path: Path,
) -> None:
    """§24. A kill mid-append leaves a fragment. The next append must not weld itself to it."""
    path = tmp_path / "corpus.jsonl"
    index = CorpusIndex(path=path)
    index.append({"slug": "row-1", "verification_status": "COMPLETE", "evidence_eligible": True})
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"slug": "row-2", "verificat')

    reopened = CorpusIndex(path=path)
    assert reopened.append(
        {"slug": "row-3", "verification_status": "COMPLETE", "evidence_eligible": True}
    )

    lines = path.read_text("utf-8").splitlines()
    assert len(lines) == 3, "the fragment is still there, on its own line"
    assert json.loads(lines[0])["slug"] == "row-1"
    with pytest.raises(json.JSONDecodeError):
        json.loads(lines[1])
    assert json.loads(lines[2])["slug"] == "row-3"

    assert [entry["slug"] for entry in reopened.entries()] == ["row-1", "row-3"]
    assert reopened.stats().truncated_lines == 1
    assert reopened.torn_lines_closed == 1


def test_a_file_that_ends_cleanly_is_not_padded(tmp_path: Path) -> None:
    path = tmp_path / "corpus.jsonl"
    index = CorpusIndex(path=path)
    index.append({"slug": "row-1"})
    index.append({"slug": "row-2"})
    assert index.torn_lines_closed == 0
    assert path.read_text("utf-8").count("\n\n") == 0


# -- §25: the corpus knows which software made it ----------------------------------------------


def test_the_identity_records_whether_the_tree_was_clean() -> None:
    from maker5m.bot.config import config_identity, source_identity

    identity = source_identity()
    assert set(identity) == {
        "source_revision",
        "source_tree_sha",
        "working_tree_clean",
        "modified_tracked_files",
    }
    assert isinstance(identity["working_tree_clean"], bool)

    full = config_identity(
        PaperConfig(evidence_dir=Path("a"), corpus_path=Path("b"), ui_dir=Path("c"))
    )
    assert full["source_tree_sha"] == identity["source_tree_sha"]
    assert full["classification_mode"] == "EVERY_DECISION"


def test_collection_knobs_are_part_of_the_configuration_identity() -> None:
    """§26. Two runs that settle differently are not the same collection."""
    from maker5m.bot.config import config_identity

    base = PaperConfig(evidence_dir=Path("a"), corpus_path=Path("b"), ui_dir=Path("c"))
    for changed in (
        PaperConfig(
            evidence_dir=Path("a"), corpus_path=Path("b"), ui_dir=Path("c"), settle_timeout_s=900
        ),
        PaperConfig(
            evidence_dir=Path("a"), corpus_path=Path("b"), ui_dir=Path("c"), settle_poll_s=1.0
        ),
        PaperConfig(
            evidence_dir=Path("a"),
            corpus_path=Path("b"),
            ui_dir=Path("c"),
            keep_raw_store=True,
        ),
    ):
        assert config_identity(base)["config_sha256"] != config_identity(changed)["config_sha256"]

    moved = PaperConfig(
        evidence_dir=Path("elsewhere"), corpus_path=Path("elsewhere"), ui_dir=Path("elsewhere")
    )
    assert config_identity(base)["config_sha256"] == config_identity(moved)["config_sha256"], (
        "where the evidence is written is not what the experiment is"
    )


# -- §27: the post-release measurement ---------------------------------------------------------


def test_finish_measures_after_the_market_has_been_let_go(tmp_path: Path) -> None:
    from maker5m.bot import UiPlane

    session = collected(tmp_path, UiPlane(directory=tmp_path / "ui"))
    session.hot_path_ns.extend([100, 200, 300])
    session.started_resources = None
    session.finish()

    assert session.hot_path_tiers["n"] == 3, "taken before the release cleared it"
    assert session.hot_path_ns == []
    assert session.released_resources is not None
    assert session.released_resources.threads >= 1
    assert os.getpid() > 0


def test_a_released_session_stops_being_live(tmp_path: Path) -> None:
    """§31. "Released" is a fact about objects, not a reading of the resident set.

    glibc does not hand freed arenas back promptly, so a process that has let go of everything
    still reads high. This counts what exists: if a market's session is gone from the weak set,
    its pipeline, recorded stream and analyzer went with it.
    """
    import gc

    from maker5m.bot import UiPlane
    from maker5m.bot.resources import LIVE_SESSIONS, sample_resources

    gc.collect()
    before = sample_resources().live_sessions
    session = collected(tmp_path, UiPlane(directory=tmp_path / "ui"))
    assert sample_resources().live_sessions == before + 1

    session.finish()
    del session
    gc.collect()
    assert sample_resources().live_sessions == before
    assert len(LIVE_SESSIONS) == before


def test_the_launch_loop_does_not_hold_the_markets_it_launched(tmp_path: Path) -> None:
    """§31. Four markets left four live sessions: `release()` cleared their insides while the
    loop's own bookkeeping kept the objects themselves alive."""
    import ast
    import inspect

    import maker5m.bot.supervisor as supervisor_module

    source = inspect.getsource(supervisor_module.Supervisor._loop)
    tree = ast.parse(source.lstrip())
    annotations = [
        ast.unparse(node.annotation)
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign) and ast.unparse(node.target) == "launched"
    ]
    assert annotations == ["set[str]"], "the loop remembers slugs, not sessions"


def test_the_collector_measures_what_the_garbage_collector_costs() -> None:
    """A full collection is proportional to tracked objects and lands wherever it lands."""
    import gc

    from maker5m.bot.resources import GcObserver

    observer = GcObserver()
    observer.install()
    try:
        gc.collect(2)
    finally:
        observer.remove()

    summary = observer.summary()
    assert summary["collections"].get("2", 0) >= 1
    assert summary["max_pause_ns"].get("2", 0) > 0
    assert summary["tracked_objects"] > 0


def test_full_collections_are_paced_not_disabled() -> None:
    """The measured fix: forty times rarer, never switched off.

    A full collection over the retained journal graph finds almost nothing — `ReplayStep` holds
    an event and a decision, which hold tuples, integers and strings, and reference counting
    frees all of it. But an asyncio process does produce cycles elsewhere, so something has to
    collect them eventually.
    """
    import gc

    from maker5m.bot.resources import GEN2_EVERY, pace_full_collections

    before = gc.get_threshold()
    try:
        allocations, gen1, gen2 = pace_full_collections()
        assert gen2 == GEN2_EVERY
        assert gen2 > before[2], "rarer than the default"
        assert gc.isenabled(), "and still enabled"
        assert allocations == before[0] and gen1 == before[1], "only the full-collection pace"
    finally:
        gc.set_threshold(*before)


def test_the_gc_pace_is_part_of_the_collection_identity() -> None:
    """§26. It changes how the collector behaves, so two runs that differ in it differ."""
    from maker5m.bot.config import config_identity

    identity = config_identity(
        PaperConfig(evidence_dir=Path("a"), corpus_path=Path("b"), ui_dir=Path("c"))
    )
    assert identity["gc_full_collection_every"] > 10
