"""A market counts when the whole record of it is durable, not when part of it is.

**SUPPORTING UNIT TEST ONLY.** Durability ordering, ledger inconsistency and the joined
qualification are software mechanics. What the markets did comes from the corpus.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from maker5m.bot import AttemptIndex, UiPlane, qualifying_rows
from maker5m.bot.attempts import ABORTED, FINISHED, STARTED, AttemptLedger
from tests.bot.test_multi_market import acceptance, clean_cold, collected, paper


class RefusingTerminal(AttemptLedger):
    """A ledger whose START persists and whose terminal record does not.

    Subclassed rather than mocked: `start` really writes and really fsyncs, so the test exercises
    the case that matters — a half-recorded attempt — instead of a ledger that never worked.
    """

    def finish(self, attempt_id: str, *, event: str = FINISHED, **detail: Any) -> bool:
        self.errors.append("OSError: no space left on device")
        return False


def build_of(supervisor: Any) -> dict[str, Any]:
    from tests.bot.test_multi_market import build_identity

    return build_identity(supervisor.config)


def row_for(supervisor: Any, slug: str, attempt: str) -> dict[str, Any]:
    return {
        "slug": slug,
        "attempt_id": attempt,
        "verification_status": "COMPLETE",
        "evidence_eligible": True,
        "working_tree_clean": True,
        **build_of(supervisor),
    }


def judge(supervisor: Any, *, verify_latency: bool = False) -> list[Any]:
    return qualifying_rows(
        supervisor.corpus.entries(),
        supervisor.ledger.events(),
        epoch=supervisor.config.epoch,
        config_sha256=str(supervisor.identity["config_sha256"]),
        source_revision=str(supervisor.identity["source_revision"]),
        source_tree_sha=str(supervisor.identity["source_tree_sha"]),
        verify_latency=verify_latency,
    )


# -- §8: the corpus row alone is not enough ----------------------------------------------------


def test_a_market_whose_terminal_record_failed_does_not_count(tmp_path: Path) -> None:
    """The row is durable, the attempt is half-recorded, and the collector cannot prove it
    finished."""
    supervisor = acceptance(paper(tmp_path))
    supervisor.audit.ledger = RefusingTerminal(path=tmp_path / "attempts.jsonl")

    attempt = supervisor.ledger.start(
        slug="btc-updown-5m-1", t0_ns=1, identity=build_of(supervisor)
    )
    assert supervisor.corpus.append(row_for(supervisor, "btc-updown-5m-1", attempt)) is True

    terminal = supervisor.ledger.finish(attempt, corpus_appended=True)
    if not terminal:
        supervisor._integrity_fault("terminal record could not be written")
    else:  # pragma: no cover - the fixture refuses by construction
        supervisor.qualified_attempts.add(attempt)

    events = [event["event"] for event in supervisor.ledger.events()]
    assert events == [STARTED], "the start is there; the terminal record is not"
    assert len(supervisor.corpus.entries()) == 1, "and the row is retained"
    assert supervisor.completed_this_process == 0
    assert supervisor.completed == 0
    assert supervisor.ledger_failures == 1
    assert supervisor.ledger.errors

    judgement = judge(supervisor)[0]
    assert judgement.qualifies is False
    assert any("no terminal record" in reason for reason in judgement.reasons)


def test_an_acceptance_run_stops_collecting_when_its_ledger_fails(tmp_path: Path) -> None:
    """§5. A corpus whose audit trail cannot be written is not a corpus."""
    supervisor = acceptance(paper(tmp_path))
    assert supervisor._may_launch(launched=0) is True

    supervisor._integrity_fault("the terminal attempt record could not be written")

    assert supervisor.halted_for_integrity is True
    assert supervisor._may_launch(launched=0) is False
    assert supervisor.integrity_faults


def test_an_exploratory_run_records_the_fault_and_carries_on(tmp_path: Path) -> None:
    """Nothing there is being counted, so nothing there is being claimed."""
    supervisor = acceptance(paper(tmp_path))
    supervisor.run_mode = "EXPLORATORY_DIRTY"

    supervisor._integrity_fault("the terminal attempt record could not be written")

    assert supervisor.halted_for_integrity is False
    assert supervisor._may_launch(launched=0) is True
    assert supervisor.integrity_faults


def test_a_terminal_record_that_did_not_see_the_corpus_row_does_not_count(
    tmp_path: Path,
) -> None:
    supervisor = acceptance(paper(tmp_path))
    attempt = supervisor.ledger.start(
        slug="btc-updown-5m-1", t0_ns=1, identity=build_of(supervisor)
    )
    supervisor.corpus.append(row_for(supervisor, "btc-updown-5m-1", attempt))
    supervisor.ledger.finish(attempt, corpus_appended=False, **build_of(supervisor))

    judgement = judge(supervisor)[0]
    assert judgement.qualifies is False
    assert any("corpus row was written" in reason for reason in judgement.reasons)


# -- §9, §10: recovery must not claim what it did not do ---------------------------------------


def test_recovery_that_cannot_be_written_leaves_the_attempt_open(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    dead = AttemptLedger(path=path)
    attempt = dead.start(slug="btc-updown-5m-1", t0_ns=1, identity={"epoch": "e"})

    revived = RefusingTerminal(path=path)
    recovered = revived.recover()

    assert recovered == [], "nothing was recovered, so nothing is reported as recovered"
    assert [event["attempt_id"] for event in revived.open_attempts()] == [attempt]
    assert revived.recovery_failures == [attempt]


def test_an_acceptance_collector_refuses_to_start_when_recovery_fails(tmp_path: Path) -> None:
    supervisor = acceptance(paper(tmp_path))
    supervisor.audit.ledger = RefusingTerminal(path=tmp_path / "attempts.jsonl")
    supervisor.ledger.start(slug="btc-updown-5m-1", t0_ns=1, identity=build_of(supervisor))

    supervisor.recovered_attempts = supervisor.ledger.recover()
    if supervisor.ledger.recovery_failures:
        supervisor._integrity_fault("abandoned attempts could not be closed off")

    assert supervisor.recovered_attempts == []
    assert supervisor.halted_for_integrity is True
    assert supervisor._may_launch(launched=0) is False


# -- §11: two terminal events for one attempt --------------------------------------------------


def test_two_terminal_records_for_one_attempt_are_an_inconsistency(tmp_path: Path) -> None:
    """A dictionary keyed by attempt id would have kept the last one and hidden the other."""
    supervisor = acceptance(paper(tmp_path))
    attempt = supervisor.ledger.start(
        slug="btc-updown-5m-1", t0_ns=1, identity=build_of(supervisor)
    )
    supervisor.corpus.append(row_for(supervisor, "btc-updown-5m-1", attempt))
    supervisor.ledger.finish(attempt, corpus_appended=True, **build_of(supervisor))
    supervisor.ledger.finish(attempt, event=ABORTED, corpus_appended=True, **build_of(supervisor))

    index = AttemptIndex.build(supervisor.ledger.events())
    assert index.duplicates() == {attempt: [FINISHED, ABORTED]}
    assert index.counts()["duplicate_terminal_attempts"] == 1
    assert supervisor.ledger.duplicate_terminals() == {attempt: [FINISHED, ABORTED]}

    judgement = judge(supervisor)[0]
    assert judgement.qualifies is False
    assert any("terminal records" in reason for reason in judgement.reasons)


def test_an_aborted_attempt_never_qualifies(tmp_path: Path) -> None:
    supervisor = acceptance(paper(tmp_path))
    attempt = supervisor.ledger.start(
        slug="btc-updown-5m-1", t0_ns=1, identity=build_of(supervisor)
    )
    supervisor.corpus.append(row_for(supervisor, "btc-updown-5m-1", attempt))
    supervisor.ledger.finish(attempt, event=ABORTED, corpus_appended=True, **build_of(supervisor))

    judgement = judge(supervisor)[0]
    assert judgement.qualifies is False
    assert any(ABORTED in reason for reason in judgement.reasons)


def test_a_terminal_for_the_right_attempt_but_the_wrong_market_does_not_close_it(
    tmp_path: Path,
) -> None:
    """§12. The join is on identity, not only on an id."""
    supervisor = acceptance(paper(tmp_path))
    attempt = supervisor.ledger.start(
        slug="btc-updown-5m-1", t0_ns=1, identity=build_of(supervisor)
    )
    supervisor.corpus.append(row_for(supervisor, "btc-updown-5m-1", attempt))
    supervisor.ledger.finish(
        attempt, corpus_appended=True, **{**build_of(supervisor), "slug": "btc-updown-5m-9"}
    )

    judgement = judge(supervisor)[0]
    assert judgement.qualifies is False
    assert any("terminal slug" in reason for reason in judgement.reasons)


def test_a_row_naming_no_attempt_does_not_qualify(tmp_path: Path) -> None:
    supervisor = acceptance(paper(tmp_path))
    supervisor.corpus.append(
        {
            "slug": "btc-updown-5m-1",
            "verification_status": "COMPLETE",
            "evidence_eligible": True,
            "working_tree_clean": True,
            **build_of(supervisor),
        }
    )
    judgement = judge(supervisor)[0]
    assert judgement.qualifies is False
    assert judgement.reasons == ("the row names no attempt",)


# -- the whole join, on a market built by the real code ----------------------------------------


def test_a_complete_market_with_every_record_in_place_qualifies(tmp_path: Path) -> None:
    supervisor = acceptance(paper(tmp_path))
    session = collected(tmp_path, UiPlane(directory=tmp_path / "ui"))
    build = build_of(supervisor)
    session.attempt_id = supervisor.ledger.start(
        slug=session.slug, t0_ns=session.t0_ns, identity=build
    )
    asyncio.run(
        session.verify_latency_artifact(
            {
                **build,
                "slug": session.slug,
                "market_id": session.identity.market_id,
                "condition_id": session.identity.condition_id,
                "t0_ns": session.t0_ns,
            }
        )
    )
    entry = supervisor._entry(session, clean_cold())
    assert entry["evidence_eligible"] is True, entry["operational_faults"]
    assert supervisor.corpus.append(entry) is True
    assert (
        supervisor.ledger.finish(
            session.attempt_id, slug=session.slug, corpus_appended=True, **build
        )
        is True
    )

    judgement = judge(supervisor, verify_latency=True)[0]
    assert judgement.qualifies is True, judgement.reasons
    assert supervisor.qualifying_now() == 1


def test_a_swapped_latency_artifact_fails_the_joined_qualification(tmp_path: Path) -> None:
    supervisor = acceptance(paper(tmp_path))
    session = collected(tmp_path, UiPlane(directory=tmp_path / "ui"))
    build = build_of(supervisor)
    session.attempt_id = supervisor.ledger.start(
        slug=session.slug, t0_ns=session.t0_ns, identity=build
    )
    entry = supervisor._entry(session, clean_cold())

    # A genuinely different market, with its own identity inside its own artifact.
    from tests.bot.test_multi_market import T0_B, build_identity
    from tests.bot.test_multi_market import session as make_session

    other = make_session(tmp_path, UiPlane(directory=tmp_path / "ui2"), T0_B, "b")
    other.analyzer.latency.clob_receive_to_decide.add(1)
    other.analyzer.latency.spot_receive_to_decide.add(2)
    asyncio.run(other.write_latency_artifact(build_identity(other.config)))
    assert other.latency is not None

    entry["latency_artifact"] = {
        **entry["latency_artifact"],
        "path": str(other.latency_path),
        "sha256": other.latency.sha256,
    }
    supervisor.corpus.append(entry)
    supervisor.ledger.finish(session.attempt_id, slug=session.slug, corpus_appended=True, **build)

    judgement = judge(supervisor, verify_latency=True)[0]
    assert judgement.qualifies is False
    assert any("not this market's latency" in reason for reason in judgement.reasons)
    assert supervisor.qualifying_now() == 0


# -- §9-12: exactly one start, and identity that cannot be absent ------------------------------


def test_two_start_records_for_one_attempt_do_not_qualify(tmp_path: Path) -> None:
    """§10. A dict keyed by attempt id collapsed these and quietly picked a winner."""
    supervisor = acceptance(paper(tmp_path))
    build = build_of(supervisor)
    attempt = supervisor.ledger.start(slug="btc-updown-5m-1", t0_ns=1, identity=build)
    # The same attempt registered twice — a retry, a duplicated line, a confused restart.
    supervisor.ledger._append(
        {"event": STARTED, "attempt_id": attempt, "slug": "btc-updown-5m-1", **build}
    )
    supervisor.corpus.append(row_for(supervisor, "btc-updown-5m-1", attempt))
    supervisor.ledger.finish(attempt, slug="btc-updown-5m-1", corpus_appended=True, **build)

    index = AttemptIndex.build(supervisor.ledger.events())
    assert index.duplicate_starts() == {attempt: 2}
    assert index.counts()["duplicate_start_attempts"] == 1

    judgement = judge(supervisor)[0]
    assert judgement.qualifies is False
    assert judgement.reasons == ("the attempt has 2 ATTEMPT_STARTED records",)


def test_two_disagreeing_start_records_still_fail_on_the_duplication(tmp_path: Path) -> None:
    """Which of the two is right is not the question. Two is already wrong."""
    supervisor = acceptance(paper(tmp_path))
    build = build_of(supervisor)
    attempt = supervisor.ledger.start(slug="btc-updown-5m-1", t0_ns=1, identity=build)
    supervisor.ledger._append(
        {
            "event": STARTED,
            "attempt_id": attempt,
            **{**build, "slug": "btc-updown-5m-9", "epoch": "somewhere-else"},
        }
    )
    supervisor.corpus.append(row_for(supervisor, "btc-updown-5m-1", attempt))
    supervisor.ledger.finish(attempt, slug="btc-updown-5m-1", corpus_appended=True, **build)

    judgement = judge(supervisor)[0]
    assert judgement.qualifies is False
    assert "2 ATTEMPT_STARTED" in judgement.reasons[0]


@pytest.mark.parametrize(
    "field_name",
    ["slug", "epoch", "config_sha256", "source_revision", "source_tree_sha", "run_mode"],
)
def test_a_terminal_missing_a_required_identity_field_does_not_qualify(
    tmp_path: Path, field_name: str
) -> None:
    """§12. Absence is not agreement: the field nobody wrote is the field nobody can check."""
    supervisor = acceptance(paper(tmp_path))
    build = build_of(supervisor)
    attempt = supervisor.ledger.start(slug="btc-updown-5m-1", t0_ns=1, identity=build)
    supervisor.corpus.append(row_for(supervisor, "btc-updown-5m-1", attempt))
    partial = {k: v for k, v in {**build, "slug": "btc-updown-5m-1"}.items() if k != field_name}
    supervisor.ledger.finish(attempt, corpus_appended=True, **partial)

    judgement = judge(supervisor)[0]
    assert judgement.qualifies is False
    assert f"terminal is missing {field_name}" in judgement.reasons


@pytest.mark.parametrize(
    "field_name",
    ["slug", "epoch", "config_sha256", "source_revision", "source_tree_sha", "run_mode"],
)
def test_a_start_missing_a_required_identity_field_does_not_qualify(
    tmp_path: Path, field_name: str
) -> None:
    supervisor = acceptance(paper(tmp_path))
    build = build_of(supervisor)
    attempt = "0" * 32
    partial = {k: v for k, v in {**build, "slug": "btc-updown-5m-1"}.items() if k != field_name}
    supervisor.ledger._append({"event": STARTED, "attempt_id": attempt, **partial})
    supervisor.corpus.append(row_for(supervisor, "btc-updown-5m-1", attempt))
    supervisor.ledger.finish(attempt, corpus_appended=True, **{**build, "slug": "btc-updown-5m-1"})

    judgement = judge(supervisor)[0]
    assert judgement.qualifies is False
    assert f"ATTEMPT_STARTED is missing {field_name}" in judgement.reasons
