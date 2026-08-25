"""The hard live-trading gate.

``maker5m.safety.LIVE_TRADING_ENABLED`` is the single answer to "may this build send orders?".
This module makes it structural rather than advisory: a real write executor cannot be
constructed while it is ``False``, and the check happens **before** any credential is touched
or any socket is opened.

There is deliberately no ``--live`` flag, no ``LIVE=true`` environment variable, and no config
key that bypasses the constant. Every one of those would be a way for an operator — or an
agent — to turn on real trading without the P14 review that is supposed to gate it. Changing
the constant requires editing source and passing code review, which is the point.

Mock transports remain freely constructible, so the entire execution path is testable without
ever being armed.
"""

from maker5m.execution.errors import LiveTradingDisabledError
from maker5m.safety import LIVE_TRADING_DISABLED_REASON, LIVE_TRADING_ENABLED

__all__ = ["live_trading_enabled", "require_live_trading_enabled"]


def live_trading_enabled() -> bool:
    """Whether this build may submit real orders. Reads the constant, nothing else."""
    return LIVE_TRADING_ENABLED


def require_live_trading_enabled(what: str) -> None:
    """Refuse to arm a real write path. Raises before any network or credential use.

    ``what`` names the thing being armed, so the failure says which component tried.
    """
    if not LIVE_TRADING_ENABLED:
        raise LiveTradingDisabledError(
            f"refusing to arm {what}: live trading is disabled. "
            f"{LIVE_TRADING_DISABLED_REASON} "
            "Unlocking is a P14 decision gated on the Canonical section 35 checklist plus "
            "explicit human authorisation; there is no flag or environment variable that "
            "bypasses this."
        )
