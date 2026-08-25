"""Polymarket BTC 5-minute post-only maker bot.

Replication build. The strategy source of truth is
``docs/strategy/Polymarket_5m_Maker_Bot_Canonical_Strategy_Spec.md``; structure is defined
by ``docs/ARCHITECTURE_SSOT.md``; the rules that may not be broken are in
``docs/INVARIANTS.md``.

No trading functionality exists yet. See ``docs/STATUS.md`` for the current phase.
"""

from maker5m.safety import LIVE_TRADING_ENABLED

__all__ = ["LIVE_TRADING_ENABLED", "__version__"]

__version__ = "0.0.0"
