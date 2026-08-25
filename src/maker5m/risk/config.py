"""Operational thresholds for the risk engine.

Every number here is ``OPERATIONAL`` (invariant I18). The frozen sources establish no stale-feed
timeout, no clock-drift limit, and no API error budget — Canonical §28.1 names the *conditions*
and says nothing about where the lines sit. Choosing values is an engineering decision, and
labelling them CONFIRMED or FITTED would claim evidence that does not exist.

**Feed staleness thresholds are deliberately absent.** P6 owns them, along with the monitor that
applies them and the ``STALE`` transition they produce. P9 consumes the resulting health status
and nothing else. An earlier version of this file copied ``DEFAULT_CLOB_STALE_AFTER`` and
``DEFAULT_SPOT_STALE_AFTER`` in, which meant two places could answer "is this stream stale?" and
disagree — with the wrong one invisible until it mattered. A structural test now forbids the
import.
"""

from dataclasses import dataclass
from typing import Final

from maker5m.domain import ParameterStatus
from maker5m.market.timebase import DurationNs, millis, seconds

__all__ = ["DEFAULT_CLOCK_DRIFT_LIMIT", "RISK_CONFIG_STATUS", "RiskConfig"]

RISK_CONFIG_STATUS: Final = ParameterStatus.OPERATIONAL

DEFAULT_CLOCK_DRIFT_LIMIT: Final[DurationNs] = millis(250)
"""Absolute ingress-clock drift beyond which the market lifecycle cannot be trusted.

Phase boundaries are derived from ``T0``, so a clock wrong by more than a fraction of a second
can put the bot in the wrong phase of a five-minute market. 250 ms is an engineering choice.
"""

DEFAULT_API_ERROR_WINDOW: Final[DurationNs] = seconds(30)
DEFAULT_API_ERROR_THRESHOLD: Final[int] = 5
"""Failures inside the window that constitute "API errors exceed threshold"."""


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Thresholds P9 genuinely owns. All OPERATIONAL, none reconstructed.

    Feed staleness is not here on purpose — see the module docstring.
    """

    clock_drift_limit_ns: DurationNs = DEFAULT_CLOCK_DRIFT_LIMIT
    api_error_window: DurationNs = DEFAULT_API_ERROR_WINDOW
    api_error_threshold: int = DEFAULT_API_ERROR_THRESHOLD

    recovery_confirmations: int = 2
    """Consecutive clear evaluations required before RECOVERING may become SAFE.

    One is not enough. A feed that has just come back can report a single healthy message and
    then fail again, and resuming on the first sign of life is how a flapping connection turns
    into repeated risk. Two is an engineering choice, not an established number.
    """

    status: ParameterStatus = RISK_CONFIG_STATUS

    def __post_init__(self) -> None:
        if self.api_error_threshold < 1:
            raise ValueError(f"api_error_threshold must be >= 1, got {self.api_error_threshold}")
        if self.recovery_confirmations < 1:
            raise ValueError(
                f"recovery_confirmations must be >= 1, got {self.recovery_confirmations}"
            )
        if self.clock_drift_limit_ns <= 0:
            raise ValueError(f"clock_drift_limit_ns must be > 0, got {self.clock_drift_limit_ns}")
