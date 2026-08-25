"""Deterministic recorder.

Runs the **production** deterministic core — ``reduce_event`` from P2 and
``StrategyEngine.decide`` from P4, unchanged — over an ordered event stream and captures each
resulting decision.

The replay sequence is frozen here, and production must eventually use exactly this ordering:

```text
state = MarketState.initial(market_definition)
for event in events (ingress order):
    state    = reduce_event(state, event)
    decision = engine.decide(state)
```

The decision is taken **after** the event has been reduced. There is no replay-specific branch
anywhere in ``market/``, ``accounting/``, ``strategy/``, or ``numeric/`` — a static test
enforces that those packages cannot even import this one.

No network, no filesystem, no clock.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from maker5m.market.events import Event
from maker5m.market.reducer import reduce_event
from maker5m.market.state import MarketDefinition, MarketState
from maker5m.replay.journal import Journal, JournalHeader, ReplayStep
from maker5m.replay.schema import JournalProvenance
from maker5m.strategy.config import StrategyConfig
from maker5m.strategy.engine import StrategyEngine

__all__ = ["RecordedRun", "record"]


@dataclass(frozen=True, slots=True)
class RecordedRun:
    """A journal plus the final state the run ended in."""

    journal: Journal
    final_state: MarketState

    @property
    def step_count(self) -> int:
        return self.journal.step_count


def record(
    market: MarketDefinition,
    config: StrategyConfig,
    events: Iterable[Event],
    *,
    provenance: JournalProvenance,
    description: str = "",
) -> RecordedRun:
    """Record a run of the real deterministic core.

    ``provenance`` has no default on purpose. Whether a journal is synthetic, reconstructed
    from a real wallet, or captured live is the single most important thing about it, and it
    is invisible in the data — so the caller has to say.
    """
    engine = StrategyEngine(config)
    state = MarketState.initial(market)
    steps: list[ReplayStep] = []
    for event in events:
        state = reduce_event(state, event)
        steps.append(ReplayStep(event=event, decision=engine.decide(state)))
    return RecordedRun(
        journal=Journal(
            header=JournalHeader(
                market=market,
                config=config,
                provenance=provenance,
                description=description,
            ),
            steps=tuple(steps),
        ),
        final_state=state,
    )
