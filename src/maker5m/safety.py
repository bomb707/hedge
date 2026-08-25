"""Global trading-enablement switch.

This module is deliberately tiny, dependency-free, and importable from anywhere. It exists
so that "is this build allowed to send orders?" has exactly one answer in exactly one place,
rather than being spread across configuration files that can drift.

``LIVE_TRADING_ENABLED`` stays ``False`` until phase P14, and flipping it is gated on the
full Canonical section 35 acceptance checklist plus explicit human authorisation
(``docs/DEVELOPMENT_PLAN.md``, P14). It is not a configuration knob and must never be
toggled by an agent, a test, or an environment variable.
"""

from typing import Final

LIVE_TRADING_ENABLED: Final[bool] = False
"""Whether this build may submit real orders. See module docstring before changing."""

LIVE_TRADING_DISABLED_REASON: Final[str] = (
    "Phase 0: repository baseline only. No execution path, feeds, credentials, or signing "
    "exist. Live trading is unlocked at P14 after the Canonical section 35 checklist."
)
