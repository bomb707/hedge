"""The market phase machine.

One source of truth: **the phase is a pure function of the event timestamp**
(``phase_at``). There is no stored phase field anywhere, which makes it structurally
impossible for a recorded phase to drift out of agreement with the clock the events carry.
``PhaseEvent`` exists to put the transition into the event journal explicitly -- so replay
and telemetry can see it, and so a feed layer can force the core to observe a boundary in a
quiet market -- and the reducer *validates* it against ``phase_at`` rather than trusting it.
A ``PhaseEvent`` can never move the market to a phase its own timestamp does not imply.

Timing is Canonical section 6.1 and section 31, all CONFIRMED:

```text
T0 + 3 s    begin active quoting        PREARM  -> QUOTE
T0 + 240 s  enter endgame               QUOTE   -> ENDGAME
T0 + 280 s  cancel all live orders      ENDGAME -> SETTLING
T0 + 300 s  window closes               SETTLING-> DONE
```

Boundaries are half-open and exact: an event exactly at ``T0+240`` is already ENDGAME. All
comparisons are integer comparisons on nanoseconds, so there is no epsilon and no float
anywhere in the decision.
"""

from dataclasses import dataclass
from enum import Enum
from itertools import pairwise
from typing import Final

from maker5m.market.errors import MarketDefinitionError
from maker5m.market.timebase import DurationNs, TimestampNs, seconds

__all__ = ["CANONICAL_PHASE_CONFIG", "Phase", "PhaseConfig", "phase_at"]


class Phase(Enum):
    """Canonical market lifecycle phases (Canonical section 31)."""

    PREARM = "PREARM"
    QUOTE = "QUOTE"
    ENDGAME = "ENDGAME"
    SETTLING = "SETTLING"
    DONE = "DONE"


@dataclass(frozen=True, slots=True)
class PhaseConfig:
    """Offsets from ``T0`` that define the phase boundaries.

    ``version`` identifies the timing configuration in the replay journal, so a replay can
    tell which boundaries applied to a recorded run. Changing an offset without changing the
    version would make two runs indistinguishable while behaving differently.
    """

    quote_start_offset: DurationNs
    endgame_offset: DurationNs
    stop_quoting_offset: DurationNs
    duration: DurationNs
    version: str

    def __post_init__(self) -> None:
        if not self.version:
            raise MarketDefinitionError("phase config version must not be empty")
        if self.quote_start_offset < 0:
            raise MarketDefinitionError(
                f"quote_start_offset must not be negative, got {self.quote_start_offset}"
            )
        ordered = (
            self.quote_start_offset,
            self.endgame_offset,
            self.stop_quoting_offset,
            self.duration,
        )
        if not all(a < b for a, b in pairwise(ordered)):
            raise MarketDefinitionError(
                "phase offsets must be strictly increasing: "
                f"quote_start={self.quote_start_offset} endgame={self.endgame_offset} "
                f"stop={self.stop_quoting_offset} duration={self.duration}"
            )


CANONICAL_PHASE_CONFIG: Final = PhaseConfig(
    quote_start_offset=seconds(3),
    endgame_offset=seconds(240),
    stop_quoting_offset=seconds(280),
    duration=seconds(300),
    version="canonical-v1",
)
"""Canonical section 6.1 timing. Every offset is CONFIRMED."""


def phase_at(t0: TimestampNs, at: TimestampNs, config: PhaseConfig) -> Phase:
    """The phase in force at ``at``, exactly.

    Pure: no clock, no state, integer comparisons only. An ``at`` before ``t0`` yields
    ``PREARM``, which is correct -- the market is discovered and pre-armed during the
    previous window (Canonical section 21).
    """
    elapsed = at - t0
    if elapsed < config.quote_start_offset:
        return Phase.PREARM
    if elapsed < config.endgame_offset:
        return Phase.QUOTE
    if elapsed < config.stop_quoting_offset:
        return Phase.ENDGAME
    if elapsed < config.duration:
        return Phase.SETTLING
    return Phase.DONE
