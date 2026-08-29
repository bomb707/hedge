"""The audit trail must never make a live market wait, and must never count a market twice.

**SUPPORTING SOFTWARE TEST ONLY.** These are concurrency and counting contracts, not market
evidence. What the markets did comes from the corpus.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from maker5m.bot import AuditIO, CorpusIndex, qualify_all
from maker5m.bot.attempts import AttemptLedger
from tests.bot.test_multi_market import acceptance, build_identity, paper


class SlowLedger(AttemptLedger):
    """A ledger whose writes take real time. Subclassed because the real one uses `__slots__`."""

    delay: float = 0.25
    seen_threads: list[int] = []  # noqa: RUF012 - shared on purpose, one test at a time

    def start(self, **fields: Any) -> str:
        import threading

        SlowLedger.seen_threads.append(threading.get_ident())
        time.sleep(self.delay)
        return "attempt-1"

    def finish(self, attempt_id: str, **fields: Any) -> bool:
        time.sleep(self.delay)
        return True

    def events(self) -> list[dict[str, Any]]:
        time.sleep(self.delay)
        return []


class SlowCorpus(CorpusIndex):
    delay: float = 0.25

    def append(self, entry: dict[str, Any]) -> bool:
        time.sleep(self.delay)
        return True


def audit_for(tmp_path: Path) -> AuditIO:
    io = AuditIO(
        corpus=CorpusIndex(path=tmp_path / "corpus.jsonl"),
        ledger=AttemptLedger(path=tmp_path / "attempts.jsonl"),
    )
    io.start()
    return io


async def heartbeat(stop: asyncio.Event, ticks: list[float]) -> None:
    """A stand-in for the ingress consumer: it must keep getting the loop back."""
    while not stop.is_set():
        ticks.append(time.perf_counter())
        await asyncio.sleep(0.005)


# -- §18: the event loop keeps running while the audit thread blocks ---------------------------


def test_the_event_loop_keeps_running_while_audit_io_blocks(tmp_path: Path) -> None:
    """A 250 ms fsync must not cost a live market 250 ms of ingestion.

    Observable progress, not a source scan: the heartbeat is what the ingress consumer would be
    doing, and the assertion is that it kept doing it.
    """
    slow = 0.25
    SlowLedger.delay = slow
    io = AuditIO(
        corpus=CorpusIndex(path=tmp_path / "corpus.jsonl"),
        ledger=SlowLedger(path=tmp_path / "attempts.jsonl"),
    )
    io.start()

    async def scenario() -> tuple[int, float]:
        stop = asyncio.Event()
        ticks: list[float] = []
        beat = asyncio.create_task(heartbeat(stop, ticks))
        started = time.perf_counter()
        attempt = await io.start_attempt(slug="s", t0_ns=1, identity={})
        elapsed = time.perf_counter() - started
        stop.set()
        await beat
        assert attempt == "attempt-1"
        return len(ticks), elapsed

    try:
        ticks, elapsed = asyncio.run(scenario())
    finally:
        io.stop()

    assert elapsed >= slow, "the caller did wait for its own record, as it must"
    assert ticks > 10, f"but the loop kept running: only {ticks} heartbeats in {elapsed:.2f}s"


def test_the_same_work_done_inline_does_stall_the_loop(tmp_path: Path) -> None:
    """The contrast, measured rather than asserted: this is what P13E's `_finalize` did.

    Calling the audit write directly from the coroutine — which is all the previous version did —
    stops the heartbeat for the duration. The point of the audit owner is not that the caller
    stops waiting; it is that *everything else* does not.
    """
    SlowLedger.delay = 0.25
    ledger = SlowLedger(path=tmp_path / "attempts.jsonl")

    async def inline() -> int:
        stop = asyncio.Event()
        ticks: list[float] = []
        beat = asyncio.create_task(heartbeat(stop, ticks))
        await asyncio.sleep(0.02)
        before = len(ticks)
        ledger.start(slug="s")  # synchronous, on the loop, exactly as before
        during = len(ticks) - before
        stop.set()
        await beat
        return during

    assert asyncio.run(inline()) == 0, "the loop made no progress at all while it waited"


def test_every_audit_operation_leaves_the_loop(tmp_path: Path) -> None:
    """Append, terminal and the full audit, each blocking, each with the loop still alive."""
    SlowLedger.delay = 0.15
    SlowCorpus.delay = 0.15
    io = AuditIO(
        corpus=SlowCorpus(path=tmp_path / "corpus.jsonl"),
        ledger=SlowLedger(path=tmp_path / "attempts.jsonl"),
    )
    io.start()

    async def scenario() -> int:
        stop = asyncio.Event()
        ticks: list[float] = []
        beat = asyncio.create_task(heartbeat(stop, ticks))
        await io.append_row({"slug": "s"})
        await io.finish_attempt("attempt-1", corpus_appended=True)
        await io.full_audit(
            {
                "epoch": "e",
                "config_sha256": "cfg",
                "source_revision": "rev",
                "verify_latency": False,
            }
        )
        stop.set()
        await beat
        return len(ticks)

    try:
        ticks = asyncio.run(scenario())
    finally:
        io.stop()

    assert ticks > 20, f"the loop ran only {ticks} times across three blocking audit calls"


def test_audit_work_runs_on_one_thread_that_is_not_the_loop(tmp_path: Path) -> None:
    """Serialised by construction: these are append-only files whose order is their meaning."""
    import threading

    SlowLedger.delay = 0.01
    SlowLedger.seen_threads = []
    io = AuditIO(
        corpus=CorpusIndex(path=tmp_path / "corpus.jsonl"),
        ledger=SlowLedger(path=tmp_path / "attempts.jsonl"),
    )
    io.start()

    async def scenario() -> int:
        await asyncio.gather(*(io.start_attempt(slug=f"s{n}") for n in range(5)))
        return threading.get_ident()

    try:
        loop_thread = asyncio.run(scenario())
    finally:
        io.stop()

    seen = SlowLedger.seen_threads
    assert len(set(seen)) == 1, "one audit thread"
    assert seen[0] != loop_thread, "and it is not the event loop's"


# -- §19: incremental judging reads one artifact, not the whole history ------------------------


def test_finalising_a_market_reads_only_its_own_latency_artifact(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """P13E re-read every historical artifact after every market: 20,100 for a 200-market run."""
    import maker5m.bot.qualify as qualify_module

    supervisor = acceptance(paper(tmp_path))
    build = build_identity(supervisor.config)
    reads: list[str] = []

    def counted(path: Path, **kwargs: Any) -> dict[str, Any]:
        reads.append(path.name)
        return {"series_ns": {"clob_receive_to_decide": [1], "spot_receive_to_decide": [2]}}

    monkeypatch.setattr(qualify_module, "read_latency", counted)

    for index in range(200):
        slug = f"btc-updown-5m-{index}"
        attempt = supervisor.ledger.start(slug=slug, t0_ns=index, identity=build)
        row = {
            "slug": slug,
            "attempt_id": attempt,
            "verification_status": "COMPLETE",
            "evidence_eligible": True,
            "working_tree_clean": True,
            "latency_artifact": {"path": str(tmp_path / f"{slug}.latency.json.xz"), "sha256": "x"},
            **build,
        }
        supervisor.corpus.append(row)
        supervisor.ledger.finish(attempt, slug=slug, corpus_appended=True, **build)

    # One newly finalised market, judged on its own.
    slug = "btc-updown-5m-200"
    attempt = supervisor.ledger.start(slug=slug, t0_ns=200, identity=build)
    fresh = {
        "slug": slug,
        "attempt_id": attempt,
        "verification_status": "COMPLETE",
        "evidence_eligible": True,
        "working_tree_clean": True,
        "latency_artifact": {"path": str(tmp_path / f"{slug}.latency.json.xz"), "sha256": "x"},
        **build,
    }
    supervisor.corpus.append(fresh)
    supervisor.ledger.finish(attempt, slug=slug, corpus_appended=True, **build)

    reads.clear()
    asyncio.run(supervisor._admit(fresh, slug))
    assert reads == [f"{slug}.latency.json.xz"], f"read {len(reads)} artifacts, expected 1"
    assert supervisor.completed == 1

    # The full audit is where historical revalidation belongs, and it happens once.
    reads.clear()
    report = qualify_all(
        supervisor.corpus.entries(), supervisor.ledger.events(), **supervisor._expectation()
    )
    assert report.count == 201
    assert len(reads) == 201


# -- §15-§17: one result per attempt, one result per market ------------------------------------


def rows_and_events(
    tmp_path: Path, supervisor: Any, *, slug: str, attempt: str, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    build = build_identity(supervisor.config)
    return {
        "slug": slug,
        "attempt_id": attempt,
        "verification_status": "COMPLETE",
        "evidence_eligible": True,
        "working_tree_clean": True,
        **build,
        **(extra or {}),
    }


def judge(supervisor: Any) -> Any:
    return qualify_all(
        supervisor.corpus.entries(),
        supervisor.ledger.events(),
        **supervisor._expectation(verify_latency=False),
    )


def test_two_result_rows_for_one_attempt_count_zero(tmp_path: Path) -> None:
    """§15. Choosing between two claims about one market would be inventing an answer."""
    supervisor = acceptance(paper(tmp_path))
    build = build_identity(supervisor.config)
    attempt = supervisor.ledger.start(slug="btc-updown-5m-1", t0_ns=1, identity=build)
    row = rows_and_events(tmp_path, supervisor, slug="btc-updown-5m-1", attempt=attempt)
    supervisor.corpus.append(row)
    supervisor.corpus.append(dict(row))
    supervisor.ledger.finish(attempt, slug="btc-updown-5m-1", corpus_appended=True, **build)

    report = judge(supervisor)
    assert report.count == 0, "neither of them counts"
    assert report.duplicate_result_attempts == {attempt: 2}
    assert all("result rows name attempt" in " ".join(j.reasons) for j in report.judgements)


def test_two_attempts_producing_one_market_count_it_once_at_most(tmp_path: Path) -> None:
    """§16. The gate is two hundred markets, not two hundred JSON lines."""
    supervisor = acceptance(paper(tmp_path))
    build = build_identity(supervisor.config)
    for _ in range(2):
        attempt = supervisor.ledger.start(slug="btc-updown-5m-1", t0_ns=1, identity=build)
        supervisor.corpus.append(
            rows_and_events(tmp_path, supervisor, slug="btc-updown-5m-1", attempt=attempt)
        )
        supervisor.ledger.finish(attempt, slug="btc-updown-5m-1", corpus_appended=True, **build)

    report = judge(supervisor)
    assert report.count == 0
    assert report.duplicate_market_slugs == {"btc-updown-5m-1": 2}
    assert report.consistent is False


def test_a_refused_row_does_not_contaminate_its_neighbours_aggregates(tmp_path: Path) -> None:
    """§17. Selecting by slug let a refused row's counts in on a qualifying row's ticket."""
    from tools.p13_corpus_report import report as build_report

    supervisor = acceptance(paper(tmp_path))
    build = build_identity(supervisor.config)

    good_attempt = supervisor.ledger.start(slug="btc-updown-5m-1", t0_ns=1, identity=build)
    good = rows_and_events(
        tmp_path,
        supervisor,
        slug="btc-updown-5m-1",
        attempt=good_attempt,
        extra={
            "decisions": 100,
            "action_counts": {"KEEP": 200},
            "quality_l3": {"total": {"AT_FRONT": 200}, "fractions": {}, "by_reason": {}},
            "risk_states": {"SAFE": 100},
        },
    )
    supervisor.corpus.append(good)
    supervisor.ledger.finish(good_attempt, slug="btc-updown-5m-1", corpus_appended=True, **build)

    # The same slug, a different attempt, never finished, and carrying absurd counts.
    bad_attempt = supervisor.ledger.start(slug="btc-updown-5m-1", t0_ns=1, identity=build)
    supervisor.corpus.append(
        rows_and_events(
            tmp_path,
            supervisor,
            slug="btc-updown-5m-1",
            attempt=bad_attempt,
            extra={
                "decisions": 999_999,
                "action_counts": {"KEEP": 999_999},
                "quality_l3": {"total": {"AT_FRONT": 999_999}, "fractions": {}, "by_reason": {}},
                "risk_states": {"SAFE": 999_999},
            },
        )
    )

    evidence = build_report(supervisor.corpus, ledger=supervisor.ledger)
    assert evidence["totals"]["decisions"] != 999_999 + 100
    assert 999_999 not in evidence["action_counts"].values()
    assert evidence["risk_states"].get("SAFE", 0) < 999_999
