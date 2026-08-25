"""Parameter sweeps: deterministic, non-mutating, and explicitly not a scorer."""

from __future__ import annotations

import dataclasses

import pytest

from maker5m.numeric import parse_share
from maker5m.replay import (
    ReplayError,
    SweepCandidate,
    encode_journal,
    replay_with_config,
    run_sweep,
    verify_replay,
)
from maker5m.strategy import (
    BaseLot,
    GridPolicy,
    StrategyConfig,
    TickRounding,
    default_config,
)
from tests.replay.corpus import synthetic_run

RUN = synthetic_run()
JOURNAL = RUN.journal
RECORDED_CONFIG = JOURNAL.header.config


def with_config(**changes: object) -> StrategyConfig:
    return dataclasses.replace(RECORDED_CONFIG, **changes)  # type: ignore[arg-type]


def o04_candidates() -> list[SweepCandidate]:
    return [
        SweepCandidate("canonical", with_config(grid_policy=GridPolicy.CANONICAL_OFFSET)),
        SweepCandidate("observed", with_config(grid_policy=GridPolicy.OBSERVED_ADJACENT)),
    ]


# -- determinism ------------------------------------------------------------------------------


def test_a_sweep_is_reproducible() -> None:
    first = run_sweep(JOURNAL, o04_candidates())
    for _ in range(3):
        again = run_sweep(JOURNAL, o04_candidates())
        for left, right in zip(first.runs, again.runs, strict=True):
            assert left.decisions == right.decisions
            assert left.final_state == right.final_state


def test_running_a_then_b_then_a_gives_identical_a_results() -> None:
    a, b = o04_candidates()
    first_a = run_sweep(JOURNAL, [a]).runs[0]
    run_sweep(JOURNAL, [b])
    second_a = run_sweep(JOURNAL, [a]).runs[0]
    assert first_a.decisions == second_a.decisions
    assert first_a.final_state == second_a.final_state


def test_candidate_order_is_preserved() -> None:
    result = run_sweep(JOURNAL, o04_candidates())
    assert result.labels == ("canonical", "observed")
    assert result.by_label("observed").config.grid_policy is GridPolicy.OBSERVED_ADJACENT


# -- the O04 question the sweep exists to serve --------------------------------------------------


def test_the_two_o04_policies_produce_different_trajectories_on_one_corpus() -> None:
    """The point of the machinery: the same events, two readings, two decision streams."""
    result = run_sweep(JOURNAL, o04_candidates())
    canonical = result.by_label("canonical").decisions
    observed = result.by_label("observed").decisions
    assert len(canonical) == len(observed) == JOURNAL.step_count
    assert canonical != observed

    differing = [
        index
        for index, (left, right) in enumerate(zip(canonical, observed, strict=True))
        if left != right
    ]
    assert len(differing) > 5, "the corpus should separate the policies at many steps"

    # Every step differs at least in the recorded grid_policy label, which is deliberate:
    # the configuration is part of the decision record. The substantive divergence is in the
    # candidate sizes, and it is on the DOWN side only, exactly as O04 describes.
    sized = [
        index
        for index in differing
        if canonical[index].telemetry.candidate_down_size is not None
        and observed[index].telemetry.candidate_down_size is not None
        and canonical[index].telemetry.candidate_down_size
        != observed[index].telemetry.candidate_down_size
    ]
    assert sized, "the policies must produce different DOWN sizes somewhere in the corpus"
    for index in sized:
        assert canonical[index].telemetry.candidate_up_size == (
            observed[index].telemetry.candidate_up_size
        ), "the O04 conflict is DOWN-side only; the UP side must always agree"


def test_the_sweep_does_not_rank_or_score_anything() -> None:
    """Judging a trajectory needs an empirical objective that does not exist yet."""
    result = run_sweep(JOURNAL, o04_candidates())
    fields = {f.name for f in dataclasses.fields(result.runs[0])}
    assert fields == {"label", "config", "decisions", "final_state"}
    assert not any(name in fields for name in ("score", "rank", "pnl", "best", "fitness"))


def test_o13_tick_policies_produce_reproducible_trajectories() -> None:
    candidates = [
        SweepCandidate(rounding.value, with_config(tick_rounding=rounding))
        for rounding in TickRounding
    ]
    first = run_sweep(JOURNAL, candidates)
    second = run_sweep(JOURNAL, candidates)
    for left, right in zip(first.runs, second.runs, strict=True):
        assert left.decisions == right.decisions


def test_o05_and_o06_magnitudes_produce_reproducible_trajectories() -> None:
    candidates = [
        SweepCandidate(
            f"tilt{tilt}-band{band}",
            with_config(endgame_tilt=parse_share(str(tilt)), endgame_band=parse_share(str(band))),
        )
        for tilt in (20, 30, 40)
        for band in (3, 5)
    ]
    result = run_sweep(JOURNAL, candidates)
    assert len(result.runs) == 6
    assert run_sweep(JOURNAL, candidates).runs[0].decisions == result.runs[0].decisions
    # Different endgame magnitudes must actually change endgame decisions somewhere.
    assert result.by_label("tilt20-band3").decisions != result.by_label("tilt40-band5").decisions


def test_base_lot_candidates_run() -> None:
    candidates = [
        SweepCandidate(
            f"L{whole}",
            with_config(base_lot_selector=default_config(BaseLot.of(whole)).base_lot_selector),
        )
        for whole in (15, 20, 25)
    ]
    result = run_sweep(JOURNAL, candidates)
    assert result.labels == ("L15", "L20", "L25")
    assert result.by_label("L15").decisions != result.by_label("L25").decisions


# -- isolation ---------------------------------------------------------------------------------


def test_a_sweep_never_mutates_the_journal() -> None:
    before = encode_journal(JOURNAL)
    run_sweep(JOURNAL, o04_candidates())
    assert encode_journal(JOURNAL) == before
    assert JOURNAL.header.config == RECORDED_CONFIG


def test_a_sweep_never_mutates_the_recorded_config() -> None:
    original_policy = RECORDED_CONFIG.grid_policy
    run_sweep(JOURNAL, o04_candidates())
    assert JOURNAL.header.config.grid_policy is original_policy


def test_a_sweep_leaves_verification_intact() -> None:
    run_sweep(JOURNAL, o04_candidates())
    assert verify_replay(JOURNAL).verified


def test_events_are_shared_but_never_altered() -> None:
    events_before = tuple(step.event for step in JOURNAL.steps)
    run_sweep(JOURNAL, o04_candidates())
    assert tuple(step.event for step in JOURNAL.steps) == events_before


# -- override replay is not verification ----------------------------------------------------------


def test_override_replay_does_not_compare_against_the_recording() -> None:
    """A candidate config is expected to decide differently; that is not a divergence."""
    outcome = replay_with_config(JOURNAL, with_config(grid_policy=GridPolicy.OBSERVED_ADJACENT))
    assert not outcome.verified
    assert outcome.decisions != tuple(step.decision for step in JOURNAL.steps)


def test_override_replay_with_the_recorded_config_matches_the_recording() -> None:
    outcome = replay_with_config(JOURNAL, RECORDED_CONFIG)
    assert outcome.decisions == tuple(step.decision for step in JOURNAL.steps)


def test_duplicate_candidate_labels_are_rejected() -> None:
    a, _ = o04_candidates()
    with pytest.raises(ReplayError, match="unique"):
        run_sweep(JOURNAL, [a, a])


def test_an_empty_candidate_label_is_rejected() -> None:
    with pytest.raises(ReplayError, match="label"):
        SweepCandidate("", RECORDED_CONFIG)


def test_an_unknown_label_lookup_raises() -> None:
    result = run_sweep(JOURNAL, o04_candidates())
    with pytest.raises(KeyError):
        result.by_label("nope")
