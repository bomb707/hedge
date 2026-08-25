"""The in-memory journal model.

A journal is a header plus an ordered sequence of ``(event, decision)`` steps. It is the unit
that makes a run reproducible: everything needed to rebuild the market, rebuild the strategy
configuration, and re-derive every decision is inside it.

The header carries the **complete** strategy configuration, not a reference to defaults. A
journal recorded under ``OBSERVED_ADJACENT`` must still replay under ``OBSERVED_ADJACENT``
after this build's default changes — otherwise every O01/O03/O04/O05/O06/O13 experiment would
silently drift with the code.
"""

from dataclasses import dataclass

from maker5m.market.events import Event
from maker5m.market.state import MarketDefinition
from maker5m.replay.schema import SCHEMA_VERSION, JournalProvenance
from maker5m.strategy.config import StrategyConfig
from maker5m.strategy.decision import DecisionResult

__all__ = ["Journal", "JournalHeader", "ReplayStep"]


@dataclass(frozen=True, slots=True)
class JournalHeader:
    """Everything needed to reconstruct a fresh market and its strategy configuration."""

    market: MarketDefinition
    config: StrategyConfig
    provenance: JournalProvenance
    description: str = ""
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ReplayStep:
    """One event and the decision it produced, in ingress order."""

    event: Event
    decision: DecisionResult


@dataclass(frozen=True, slots=True)
class Journal:
    """A recorded run: header plus ordered steps."""

    header: JournalHeader
    steps: tuple[ReplayStep, ...]

    @property
    def step_count(self) -> int:
        return len(self.steps)
