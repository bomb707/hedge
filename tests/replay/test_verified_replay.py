"""Verified replay: every recorded decision must follow from the recorded stream."""

from __future__ import annotations

import dataclasses

import pytest

from maker5m.accounting import RebateMode
from maker5m.market import OwnFill, Phase
from maker5m.numeric import ShareUnits, money_from_whole, share_from_whole
from maker5m.replay import (
    ReplayDivergenceError,
    decode_journal,
    encode_journal,
    verify_replay,
)
from maker5m.replay.journal import Journal, ReplayStep
from maker5m.strategy import DesiredOrder, DesiredOrders
from tests.replay.corpus import synthetic_run

RUN = synthetic_run()
JOURNAL = RUN.journal


def test_the_corpus_is_non_trivial() -> None:
    assert JOURNAL.step_count >= 35
    phases = {step.decision.telemetry.phase for step in JOURNAL.steps}
    assert phases == {Phase.PREARM, Phase.QUOTE, Phase.ENDGAME, Phase.SETTLING, Phase.DONE}
    favourites = {
        step.decision.telemetry.endgame.favourite
        for step in JOURNAL.steps
        if step.decision.telemetry.endgame is not None
    }
    assert len(favourites) == 2, "the corpus must exercise both endgame favourites"


def test_the_corpus_contains_equal_timestamps_with_distinct_ordinals() -> None:
    seen: dict[int, list[int]] = {}
    for step in JOURNAL.steps:
        seen.setdefault(step.event.meta.timestamp, []).append(step.event.meta.ingress_ordinal)
    ties = [ordinals for ordinals in seen.values() if len(ordinals) > 1]
    assert ties, "the corpus must include a timestamp tie"
    assert all(len(set(ordinals)) == len(ordinals) for ordinals in ties)


def test_verified_replay_reproduces_every_decision() -> None:
    outcome = verify_replay(JOURNAL)
    assert outcome.verified
    assert outcome.step_count == JOURNAL.step_count
    for index, (step, decision) in enumerate(zip(JOURNAL.steps, outcome.decisions, strict=True)):
        assert decision == step.decision, f"step {index}"


def test_verified_replay_reproduces_the_final_state_exactly() -> None:
    outcome = verify_replay(JOURNAL)
    assert outcome.final_state == RUN.final_state
    assert outcome.final_state.ledger == RUN.final_state.ledger
    assert outcome.final_state.last_ingress_ordinal == RUN.final_state.last_ingress_ordinal
    assert outcome.final_state.phase is Phase.DONE


def test_replay_after_a_full_encode_decode_cycle_still_verifies() -> None:
    restored = decode_journal(encode_journal(JOURNAL))
    outcome = verify_replay(restored)
    assert outcome.final_state == RUN.final_state
    assert outcome.decisions == tuple(step.decision for step in JOURNAL.steps)


def test_replay_is_repeatable() -> None:
    first = verify_replay(JOURNAL)
    for _ in range(5):
        assert verify_replay(JOURNAL).decisions == first.decisions


def test_the_mandatory_accounting_example_is_reached_exactly_mid_stream() -> None:
    """120 UP at $72 and 100 DOWN at $50 -> -$2 / -$22, through real fills."""
    matches = [
        step
        for step in JOURNAL.steps
        if step.decision.telemetry.economics.n_up == share_from_whole(120)
        and step.decision.telemetry.economics.n_down == share_from_whole(100)
    ]
    assert matches
    economics = matches[0].decision.telemetry.economics
    assert economics.cost_up == money_from_whole(72)
    assert economics.cost_down == money_from_whole(50)
    assert economics.total_cost == money_from_whole(122)
    assert economics.inventory == share_from_whole(20)
    assert economics.pnl_if_up_without_rebate == money_from_whole(-2)
    assert economics.pnl_if_down_without_rebate == money_from_whole(-22)


def test_the_journal_ledger_matches_the_reducer_ledger_at_every_step() -> None:
    outcome = verify_replay(JOURNAL)
    for step, decision in zip(JOURNAL.steps, outcome.decisions, strict=True):
        assert decision.telemetry.economics == step.decision.telemetry.economics


# -- tamper detection ----------------------------------------------------------------------


def replace_step(index: int, **changes: object) -> Journal:
    steps = list(JOURNAL.steps)
    steps[index] = dataclasses.replace(steps[index], **changes)  # type: ignore[arg-type]
    return dataclasses.replace(JOURNAL, steps=tuple(steps))


def first_quoting_step() -> int:
    return next(
        index for index, step in enumerate(JOURNAL.steps) if not step.decision.orders.is_empty
    )


def test_a_tampered_recorded_decision_is_rejected() -> None:
    index = first_quoting_step()
    tampered = replace_step(
        index,
        decision=dataclasses.replace(JOURNAL.steps[index].decision, orders=DesiredOrders()),
    )
    with pytest.raises(ReplayDivergenceError) as info:
        verify_replay(tampered)
    assert info.value.step_index == index
    assert info.value.event_id == JOURNAL.steps[index].event.meta.event_id
    assert info.value.ingress_ordinal == JOURNAL.steps[index].event.meta.ingress_ordinal
    assert "orders differ" in info.value.detail


def test_a_tampered_order_size_is_rejected() -> None:
    index = first_quoting_step()
    original = JOURNAL.steps[index].decision
    assert original.orders.up is not None
    swapped = DesiredOrders(
        up=DesiredOrder(
            original.orders.up.outcome,
            original.orders.up.price,
            ShareUnits(original.orders.up.size + 1),
        ),
        down=original.orders.down,
    )
    with pytest.raises(ReplayDivergenceError):
        verify_replay(replace_step(index, decision=dataclasses.replace(original, orders=swapped)))


def test_a_tampered_telemetry_field_is_rejected_even_when_orders_match() -> None:
    """A decision can be wrong while its emitted order looks identical."""
    index = first_quoting_step()
    original = JOURNAL.steps[index].decision
    economics = dataclasses.replace(
        original.telemetry.economics,
        pnl_if_up_without_rebate=money_from_whole(9999),
    )
    tampered = dataclasses.replace(
        original, telemetry=dataclasses.replace(original.telemetry, economics=economics)
    )
    with pytest.raises(ReplayDivergenceError) as info:
        verify_replay(replace_step(index, decision=tampered))
    assert "telemetry.economics differs" in info.value.detail


def test_a_tampered_fill_amount_is_detected() -> None:
    index, event = next(
        (i, step.event) for i, step in enumerate(JOURNAL.steps) if isinstance(step.event, OwnFill)
    )
    tampered_fill = dataclasses.replace(event.fill, shares=share_from_whole(999))
    with pytest.raises(ReplayDivergenceError) as info:
        verify_replay(replace_step(index, event=dataclasses.replace(event, fill=tampered_fill)))
    assert info.value.step_index == index


def test_a_tampered_event_ordinal_is_detected() -> None:
    index = first_quoting_step()
    event = JOURNAL.steps[index].event
    meta = dataclasses.replace(event.meta, ingress_ordinal=event.meta.ingress_ordinal + 50)
    from maker5m.market.errors import EventOrderError

    tampered = replace_step(index, event=dataclasses.replace(event, meta=meta))
    with pytest.raises((ReplayDivergenceError, EventOrderError)):
        verify_replay(tampered)


def test_a_tampered_event_timestamp_is_detected() -> None:
    """Moving an event across a phase boundary changes the decision that follows from it."""
    from maker5m.market import TimestampNs
    from tests.replay.corpus import at

    index = first_quoting_step()
    event = JOURNAL.steps[index].event
    meta = dataclasses.replace(event.meta, timestamp=TimestampNs(at(250)))
    with pytest.raises(ReplayDivergenceError) as info:
        verify_replay(replace_step(index, event=dataclasses.replace(event, meta=meta)))
    assert info.value.step_index == index


def test_verification_fails_at_the_first_divergence_not_the_last() -> None:
    early = first_quoting_step()
    late = max(
        index for index, step in enumerate(JOURNAL.steps) if not step.decision.orders.is_empty
    )
    assert late > early
    steps = list(JOURNAL.steps)
    for index in (early, late):
        steps[index] = dataclasses.replace(
            steps[index],
            decision=dataclasses.replace(steps[index].decision, orders=DesiredOrders()),
        )
    with pytest.raises(ReplayDivergenceError) as info:
        verify_replay(dataclasses.replace(JOURNAL, steps=tuple(steps)))
    assert info.value.step_index == early


def test_an_untampered_journal_verifies_after_all_of_that() -> None:
    assert verify_replay(JOURNAL).verified


def test_estimated_and_realised_rebate_views_survive_replay() -> None:
    outcome = verify_replay(JOURNAL)
    economics = outcome.decisions[-1].telemetry.economics
    ledger = RUN.final_state.ledger
    assert economics.pnl_if_up_without_rebate == ledger.pnl_if_up(RebateMode.WITHOUT_REBATE)
    assert economics.pnl_if_up_estimated_rebate == ledger.pnl_if_up(RebateMode.ESTIMATED_REBATE)


def test_replay_does_not_mutate_the_journal() -> None:
    before = encode_journal(JOURNAL)
    verify_replay(JOURNAL)
    assert encode_journal(JOURNAL) == before
    assert isinstance(JOURNAL.steps[0], ReplayStep)
