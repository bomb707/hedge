"""What "unsafe" means, named exactly.

Canonical §28.1 lists eleven operational kill switches and Detailed §38 adds the twelfth — an
unexpected taker fill, which Canonical §11 calls an execution bug rather than an acceptable
outcome. Each is a distinct typed reason, never a boolean or a free-text string, because the
recovery path differs by reason: a stale feed clears when data resumes, a position mismatch
does not clear until someone has established which side is wrong.

These are **operational safety** conditions. None of them is a trading signal, and nothing here
changes what the strategy economically wants (Canonical §28: "These are safety controls, not
changes to the economic strategy").
"""

from enum import Enum
from typing import Final

__all__ = ["REQUIRES_RECONCILIATION", "RiskReason", "RiskState"]


class RiskState(Enum):
    """The authoritative risk verdict. One state, never a bag of unrelated booleans."""

    SAFE = "SAFE"
    """Normal execution allowed."""

    HALTED = "HALTED"
    """At least one condition is currently active. New risk is forbidden."""

    RECOVERING = "RECOVERING"
    """Conditions have cleared but reconciliation or confirmation is outstanding.

    A distinct state rather than an early return to SAFE: one healthy message is evidence that
    a feed is alive again, not that our view of the world is correct again.
    """


class RiskReason(Enum):
    """Canonical §28.1's eleven conditions, plus Detailed §38's taker fill."""

    CLOB_STALE = "CLOB_STALE"
    """Canonical §28.1 "market data stale"."""

    SPOT_STALE = "SPOT_STALE"
    """Canonical §28.1 "external BTC feed stale"."""

    CLOB_CONTINUITY_UNCERTAIN = "CLOB_CONTINUITY_UNCERTAIN"
    """Canonical §28.1 "CLOB sequence gap unresolved".

    Polymarket publishes no documented monotonic sequence, so P6 detects continuity loss
    conservatively — a disconnect, a heartbeat failure, a malformed message, an unknown token,
    or a resubscription. This reason covers all of them, because the consequence is the same:
    the book cannot be trusted until a fresh authoritative snapshot arrives.
    """

    CLOCK_DRIFT = "CLOCK_DRIFT"
    ORDER_STATE_UNCERTAIN = "ORDER_STATE_UNCERTAIN"
    MAKER_ONLY_UNCERTAIN = "MAKER_ONLY_UNCERTAIN"
    POSITION_MISMATCH = "POSITION_MISMATCH"
    COST_LEDGER_MISMATCH = "COST_LEDGER_MISMATCH"
    API_ERROR_RATE = "API_ERROR_RATE"
    RATE_LIMIT_UNCERTAIN = "RATE_LIMIT_UNCERTAIN"
    OPERATOR_HALT = "OPERATOR_HALT"
    """An operator asked the bot to stop placing. OPERATIONAL, not a market condition.

    Deliberately **not** in ``REQUIRES_RECONCILIATION``. It is the one condition whose evidence
    is a person deciding, so a person deciding again is enough to clear it — and it has to clear
    *only itself*: releasing an operator halt must never be a way to sweep away a stale feed or
    an unreconciled position that is still true."""

    RESOLUTION_AMBIGUOUS = "RESOLUTION_AMBIGUOUS"

    TAKER_FILL = "TAKER_FILL"
    """Detailed §38, invariant I07. An intentional taker fill is an execution bug.

    Recording it changes future risk permission only. There is no recovery trade, no hedge, and
    no flattening — the fill stays in the ledger exactly as executed (I15).
    """


REQUIRES_RECONCILIATION: Final[frozenset[RiskReason]] = frozenset(
    {
        RiskReason.ORDER_STATE_UNCERTAIN,
        RiskReason.POSITION_MISMATCH,
        RiskReason.COST_LEDGER_MISMATCH,
        RiskReason.TAKER_FILL,
        RiskReason.RESOLUTION_AMBIGUOUS,
    }
)
"""Reasons whose condition going quiet is *not* evidence that the problem is resolved.

An unknown order does not become known because the socket reconnected; a position mismatch does
not become a match because the next snapshot happened to agree; and contradictory settlement
evidence does not become trustworthy because the next read of the same chain looked tidier.
These require an explicit reconciliation result before SAFE can return, and until one arrives
the engine stays in RECOVERING however healthy everything looks.

``RESOLUTION_AMBIGUOUS`` is here as of the P10 trust-boundary round (O16, CLOSED — OPERATIONAL
safety policy). It was previously an ordinary flag, and the intended stickiness rested on
``maker5m.settlement.safety`` choosing never to emit ``flag=False``. That made a safety property
depend on one caller's restraint rather than on this contract, so the contract now states it.

The feed reasons are deliberately absent: their conditions already encode their own evidence.
``CLOB_CONTINUITY_UNCERTAIN`` clears only when P6 reports HEALTHY *and* is no longer awaiting a
snapshot, which is exactly the "fresh authoritative snapshot" requirement.
"""
