"""Verified replay and configuration-override replay.

Two operations that must not be confused:

* :func:`verify_replay` re-derives every decision from the journal's **own** recorded
  configuration and asserts each one matches what was recorded. This is the property that
  makes a journal trustworthy evidence.
* :func:`replay_with_config` re-derives decisions under a **different** configuration. Its
  results are expected to differ from the recording, so nothing is compared. Confusing the
  two would either hide a real divergence or report a deliberate config change as corruption.

Comparison is on the **complete** ``DecisionResult``, not just the emitted order. A decision
can be wrong in its centre, its eligibility reasons, or its economics while the final order
happens to look identical, and any of those would invalidate an experiment built on it.
"""

from dataclasses import dataclass

from maker5m.market.events import Event
from maker5m.market.reducer import reduce_event
from maker5m.market.state import MarketState
from maker5m.replay.errors import ReplayDivergenceError
from maker5m.replay.journal import Journal
from maker5m.strategy.config import StrategyConfig
from maker5m.strategy.decision import DecisionResult
from maker5m.strategy.engine import StrategyEngine

__all__ = ["ReplayOutcome", "replay_with_config", "verify_replay"]


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    """The result of running a journal's events through the deterministic core."""

    final_state: MarketState
    decisions: tuple[DecisionResult, ...]
    config: StrategyConfig
    verified: bool
    """True only when each decision was compared against the journal's recorded decision."""

    @property
    def step_count(self) -> int:
        return len(self.decisions)


def _run(journal: Journal, config: StrategyConfig, *, compare: bool) -> ReplayOutcome:
    engine = StrategyEngine(config)
    state = MarketState.initial(journal.header.market)
    decisions: list[DecisionResult] = []

    for index, step in enumerate(journal.steps):
        event: Event = step.event
        state = reduce_event(state, event)
        decision = engine.decide(state)
        if compare and decision != step.decision:
            raise ReplayDivergenceError(
                step_index=index,
                event_id=event.meta.event_id,
                ingress_ordinal=event.meta.ingress_ordinal,
                detail=_describe(step.decision, decision),
            )
        decisions.append(decision)

    return ReplayOutcome(
        final_state=state,
        decisions=tuple(decisions),
        config=config,
        verified=compare,
    )


def verify_replay(journal: Journal) -> ReplayOutcome:
    """Re-derive every decision under the journal's own config and check each one.

    Fails at the **first** divergence rather than reporting only a final-state mismatch: the
    first diverging step is where the cause is, and everything after it is downstream noise.
    """
    return _run(journal, journal.header.config, compare=True)


def replay_with_config(journal: Journal, config: StrategyConfig) -> ReplayOutcome:
    """Re-derive decisions under an override configuration. Nothing is compared.

    The journal, its header, its config snapshot, and its events are never mutated; the
    override is used only to build a fresh engine.
    """
    return _run(journal, config, compare=False)


def _describe(recorded: DecisionResult, replayed: DecisionResult) -> str:
    """A short, specific account of the first difference found."""
    if recorded.orders != replayed.orders:
        return f"orders differ: recorded={recorded.orders} replayed={replayed.orders}"
    recorded_telemetry = recorded.telemetry
    replayed_telemetry = replayed.telemetry
    for field in (
        "phase",
        "raw_centre",
        "centre_unavailable",
        "quantized_centre",
        "base_lot",
        "candidate_up_price",
        "candidate_up_size",
        "candidate_down_price",
        "candidate_down_size",
        "eligibility",
        "endgame",
        "economics",
    ):
        left = getattr(recorded_telemetry, field)
        right = getattr(replayed_telemetry, field)
        if left != right:
            return f"telemetry.{field} differs: recorded={left!r} replayed={right!r}"
    return "decision records differ"
