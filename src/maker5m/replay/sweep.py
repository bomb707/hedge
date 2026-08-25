"""Deterministic parameter sweeps.

Runs the same recorded event stream under alternative strategy configurations, producing one
reproducible decision trajectory per candidate. This is the machinery every OPEN item
eventually closes through — O01's centre source, O03's base lot, O04's grid policy, O05/O06's
magnitudes, O13's tie rule.

**It is not an optimizer, and it does not score anything.** Deciding which trajectory is
"better" requires an empirical objective measured against real target-wallet or live-paper
data, none of which exists yet. A ranking produced from synthetic journals would look like
evidence and be nothing of the sort. The sweep produces trajectories; judging them is P15's
job, once there is something real to judge against.

Sweeps never mutate the journal, its header, its config snapshot, or its events — every run
starts from ``MarketState.initial`` and builds a fresh engine, so running A, then B, then A
again yields identical A results.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from maker5m.market.state import MarketState
from maker5m.replay.errors import ReplayError
from maker5m.replay.journal import Journal
from maker5m.replay.verifier import replay_with_config
from maker5m.strategy.config import StrategyConfig
from maker5m.strategy.decision import DecisionResult

__all__ = ["SweepCandidate", "SweepResult", "SweepRun", "run_sweep"]


@dataclass(frozen=True, slots=True)
class SweepCandidate:
    """One named configuration to run the journal under."""

    label: str
    config: StrategyConfig

    def __post_init__(self) -> None:
        if not self.label:
            raise ReplayError("sweep candidate label must not be empty")


@dataclass(frozen=True, slots=True)
class SweepRun:
    """The trajectory one candidate produced. Carries no score and no ranking."""

    label: str
    config: StrategyConfig
    decisions: tuple[DecisionResult, ...]
    final_state: MarketState

    @property
    def step_count(self) -> int:
        return len(self.decisions)


@dataclass(frozen=True, slots=True)
class SweepResult:
    """All candidate runs over one journal, in the order they were requested."""

    runs: tuple[SweepRun, ...]

    def by_label(self, label: str) -> SweepRun:
        for run in self.runs:
            if run.label == label:
                return run
        raise KeyError(label)

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(run.label for run in self.runs)


def run_sweep(journal: Journal, candidates: Sequence[SweepCandidate]) -> SweepResult:
    """Run ``journal``'s events under each candidate configuration.

    Recorded decisions are deliberately **not** compared against: a candidate config is
    expected to decide differently, and treating that as a divergence would make the whole
    facility useless. Use :func:`~maker5m.replay.verifier.verify_replay` when the question is
    whether the journal is self-consistent.
    """
    labels = [candidate.label for candidate in candidates]
    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    if duplicates:
        raise ReplayError(f"sweep candidate labels must be unique; duplicated: {duplicates}")

    runs = tuple(
        SweepRun(
            label=candidate.label,
            config=candidate.config,
            decisions=(outcome := replay_with_config(journal, candidate.config)).decisions,
            final_state=outcome.final_state,
        )
        for candidate in candidates
    )
    return SweepResult(runs=runs)
