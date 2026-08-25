"""Per-side eligibility, combined by intersection with typed reasons.

A side is live only if every gate that applies to it permits it:

```text
phase_allows  AND  endgame_gate_allows (ENDGAME only)  AND  hard_band_allows
```

Reasons are enumerated rather than free text, because they feed Detailed §35's
``NOT_QUOTING`` classification and later telemetry — "the bot did not quote" is only useful
if the *why* is machine-readable. A free-text message may accompany a reason later; it may
never be the only machine-readable form.

Blocking reasons are accumulated in a fixed order so the result is deterministic and
comparable across a replay (I20).
"""

from dataclasses import dataclass
from enum import Enum

from maker5m.numeric.units import ShareUnits
from maker5m.strategy.errors import StrategyError

__all__ = ["EligibilityReason", "EligibilityResult", "evaluate_eligibility"]


class EligibilityReason(Enum):
    """Why a side is not being quoted."""

    PHASE_NOT_QUOTING = "PHASE_NOT_QUOTING"
    """PREARM, SETTLING, or DONE. No new quotes exist in these phases."""

    CENTRE_UNAVAILABLE = "CENTRE_UNAVAILABLE"
    """The configured quote centre could not price the market. A normal condition."""

    ENDGAME_GATE = "ENDGAME_GATE"
    """Inventory is beyond the endgame band on this side (Canonical §15.2)."""

    HARD_BAND = "HARD_BAND"
    """The one-sided inventory safety wall is reached on this side (I17, A4)."""


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    """Final per-side eligibility with the reasons behind any suppression."""

    up_allowed: bool
    down_allowed: bool
    up_reasons: tuple[EligibilityReason, ...] = ()
    down_reasons: tuple[EligibilityReason, ...] = ()

    def __post_init__(self) -> None:
        if self.up_allowed != (self.up_reasons == ()):
            raise StrategyError(
                "up_allowed must be true exactly when there are no blocking reasons"
            )
        if self.down_allowed != (self.down_reasons == ()):
            raise StrategyError(
                "down_allowed must be true exactly when there are no blocking reasons"
            )


def evaluate_eligibility(
    *,
    quoting_phase: bool,
    centre_available: bool,
    inventory: ShareUnits,
    band_hard: ShareUnits,
    endgame_up_allowed: bool | None = None,
    endgame_down_allowed: bool | None = None,
) -> EligibilityResult:
    """Intersect every applicable gate.

    ``endgame_*_allowed`` are ``None`` outside ENDGAME, where that gate simply does not
    apply — distinct from applying and permitting.

    ``band_hard`` is strictly one-sided (A4, Canonical §32): reaching the wall on one side
    never suppresses the other, and it never changes a price or pulls inventory toward zero.
    It is an eligibility wall and nothing else (I17).
    """
    up: list[EligibilityReason] = []
    down: list[EligibilityReason] = []

    if not quoting_phase:
        up.append(EligibilityReason.PHASE_NOT_QUOTING)
        down.append(EligibilityReason.PHASE_NOT_QUOTING)
    elif not centre_available:
        up.append(EligibilityReason.CENTRE_UNAVAILABLE)
        down.append(EligibilityReason.CENTRE_UNAVAILABLE)
    else:
        if endgame_up_allowed is False:
            up.append(EligibilityReason.ENDGAME_GATE)
        if endgame_down_allowed is False:
            down.append(EligibilityReason.ENDGAME_GATE)
        if inventory >= band_hard:
            up.append(EligibilityReason.HARD_BAND)
        if inventory <= -band_hard:
            down.append(EligibilityReason.HARD_BAND)

    return EligibilityResult(
        up_allowed=not up,
        down_allowed=not down,
        up_reasons=tuple(up),
        down_reasons=tuple(down),
    )
