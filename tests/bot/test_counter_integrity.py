"""The completion counter, driven through the production finalisation path.

**SUPPORTING UNIT TEST ONLY.** Counting is software mechanics. What the markets did comes from
the corpus.

The previous round's tests recreated the intended sequence by hand and passed while the shipped
`_finalize` incremented twice — once when the corpus row landed and again when the terminal
record did. A test that reproduces the code it is checking checks the reproduction. These call
`Supervisor._finalize` itself.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from maker5m.bot import MarketSession, UiPlane
from maker5m.bot.attempts import FINISHED, AttemptLedger
from tests.bot.test_multi_market import (
    T0_A,
    acceptance,
    build_identity,
    collected,
    paper,
)


def finished_market(tmp_path: Path, supervisor: Any, index: int) -> MarketSession:
    """One market in exactly the state `_finalize` receives, with its attempt registered."""
    session = collected(tmp_path / f"m{index}", UiPlane(directory=tmp_path / "ui"))
    session.slug = f"btc-updown-5m-{T0_A + index * 300}"
    session.identity = type(session.identity)(
        market_id=f"0xmarket-{index}",
        slug=session.slug,
        condition_id=f"0xcondition-{index}",
        provenance=session.identity.provenance,
    )
    session.latency_path = session.config.evidence_dir / f"{session.slug}.latency.json.xz"
    build = build_identity(session.config)
    asyncio.run(
        session.write_latency_artifact(
            {
                **build,
                "slug": session.slug,
                "market_id": session.identity.market_id,
                "condition_id": session.identity.condition_id,
                "t0_ns": session.t0_ns,
            }
        )
    )
    session.attempt_id = supervisor.ledger.start(
        slug=session.slug, t0_ns=session.t0_ns, identity=build
    )
    return session


def stub_cold(
    supervisor: Any, monkeypatch: Any, *, status: str = "COMPLETE", replay: str = "EXACT"
) -> None:
    """Replace only what would reach a network, a disk store or a child process.

    `_finalize` itself — the ordering, the ledger call, the counting — is the code under test and
    is not touched.
    """

    async def cold(session: MarketSession) -> dict[str, Any]:
        return {
            "verification_status": status,
            "replay": {"status": replay, "byte_roundtrip_identical": True},
        }

    async def journal(session: MarketSession) -> None:
        return None

    async def settle(session: MarketSession, fn: Any) -> None:
        return None

    monkeypatch.setattr(type(supervisor), "_cold_result", staticmethod(cold), raising=False)
    monkeypatch.setattr(MarketSession, "write_journal", journal)
    monkeypatch.setattr(MarketSession, "settle", settle)
    monkeypatch.setattr(MarketSession, "close_store", lambda self: None)
    monkeypatch.setattr(MarketSession, "publish_close", lambda self, cold: None)


def finalize(supervisor: Any, session: MarketSession) -> None:
    supervisor.lifecycles += 1
    asyncio.run(supervisor._finalize(session))


def test_one_market_counts_once(tmp_path: Path, monkeypatch: Any) -> None:
    """The shipped code counted it twice: `(8 durable)` over four rows in a live collector log."""
    supervisor = acceptance(paper(tmp_path))
    stub_cold(supervisor, monkeypatch)
    session = finished_market(tmp_path, supervisor, 0)

    assert supervisor.completed == 0
    finalize(supervisor, session)

    assert supervisor.completed_this_process == 1
    assert supervisor.completed == 1
    assert supervisor.qualifying_now() == 1
    assert len(supervisor.corpus.entries()) == 1
    assert supervisor.halted_for_integrity is False


def test_ten_markets_count_ten(tmp_path: Path, monkeypatch: Any) -> None:
    supervisor = acceptance(paper(tmp_path))
    stub_cold(supervisor, monkeypatch)
    for index in range(10):
        finalize(supervisor, finished_market(tmp_path, supervisor, index))

    assert supervisor.completed_this_process == 10
    assert supervisor.completed == 10
    assert supervisor.qualifying_now() == 10
    assert len(supervisor.corpus.entries()) == 10


def test_the_runtime_total_always_equals_the_durable_count(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """§5. A run must never be able to say 200 while the corpus holds 100."""
    supervisor = acceptance(paper(tmp_path))
    stub_cold(supervisor, monkeypatch)
    for index in range(4):
        finalize(supervisor, finished_market(tmp_path, supervisor, index))
        assert supervisor.completed == supervisor.qualifying_now()


def test_the_target_stops_at_exactly_two_hundred(tmp_path: Path) -> None:
    """The running set decides when to *ask*; the full durable audit decides the answer."""
    supervisor = acceptance(paper(tmp_path))
    supervisor.target_markets = 200

    supervisor.qualified_attempts = {f"attempt-{index}" for index in range(100)}
    assert asyncio.run(supervisor._still_collecting(launched=0)) is True

    supervisor.qualified_attempts = {f"attempt-{index}" for index in range(199)}
    assert asyncio.run(supervisor._still_collecting(launched=0)) is True

    # The set says 200. The corpus holds nothing, so the audit says 0 and collection continues:
    # durable truth wins over the running count, never the other way round.
    supervisor.qualified_attempts = {f"attempt-{index}" for index in range(200)}
    assert asyncio.run(supervisor._still_collecting(launched=0)) is True
    assert supervisor.last_durable_count == 0
    assert supervisor.qualified_attempts == set(), "the durable answer replaced the running set"


def test_a_failed_terminal_record_counts_zero_through_the_real_path(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """§8, driven through `_finalize` rather than through a copy of its logic."""

    class RefusingTerminal(AttemptLedger):
        def finish(self, attempt_id: str, *, event: str = FINISHED, **detail: Any) -> bool:
            self.errors.append("OSError: no space left on device")
            return False

    supervisor = acceptance(paper(tmp_path))
    supervisor.audit.ledger = RefusingTerminal(path=tmp_path / "attempts.jsonl")
    stub_cold(supervisor, monkeypatch)
    session = finished_market(tmp_path, supervisor, 0)

    finalize(supervisor, session)

    assert len(supervisor.corpus.entries()) == 1, "the row is retained"
    assert supervisor.completed_this_process == 0
    assert supervisor.completed == 0
    assert supervisor.qualifying_now() == 0
    assert supervisor.ledger_failures == 1
    assert supervisor.halted_for_integrity is True
    assert supervisor._may_launch(launched=0) is False


def test_a_market_that_did_not_verify_counts_zero(tmp_path: Path, monkeypatch: Any) -> None:
    supervisor = acceptance(paper(tmp_path))
    stub_cold(supervisor, monkeypatch, status="INCOMPLETE")
    finalize(supervisor, finished_market(tmp_path, supervisor, 0))

    assert supervisor.completed == 0
    assert supervisor.qualifying_now() == 0
    assert len(supervisor.corpus.entries()) == 1, "and is retained"
