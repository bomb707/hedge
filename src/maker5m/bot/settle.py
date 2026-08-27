"""Watch the chain for one market's resolution, through P10's production path.

Blocking, on purpose: it is called from a thread, after its market has closed, and the thing it
waits for takes minutes. Nothing it does can reach the market that is trading now.

**No key, no credential, no transaction.** This reads a public condition through attested RPC
endpoints and reports what P10's verifier says about them. `REDEMPTION_ENABLED` is `False` and
nothing here would use it if it were not.
"""

from __future__ import annotations

import time
from typing import Any, Final

from maker5m.settlement import (
    DEFAULT_RPC_ENDPOINTS,
    EndpointSet,
    MarketResolutionTarget,
    RpcEndpoint,
    SettlementPolicy,
    SettlementRecord,
    attest_all,
    verify,
)

__all__ = ["settle_market"]

SETTLE_TIMEOUT_SECONDS: Final[int] = 400
SETTLE_POLL_SECONDS: Final[float] = 5.0


def settle_market(
    market: Any, slug: str, *, timeout_s: int = SETTLE_TIMEOUT_SECONDS
) -> SettlementRecord | None:
    """Poll until the market resolves or the watch window closes. ``None`` means unresolved.

    Unresolved is a recorded state, not a retry loop that runs for ever: a market whose payout
    has not appeared inside the window is evidence about the chain, and P9 already treats an
    unresolved market as a reason not to act rather than a reason to guess.
    """
    condition_id = market.condition_id
    if not condition_id:
        return None
    configured = EndpointSet(
        tuple(RpcEndpoint(provider_id=name, url=url) for name, url in DEFAULT_RPC_ENDPOINTS)
    )
    providers, _ = attest_all(configured)
    policy = SettlementPolicy()
    definition = market.definition
    target = MarketResolutionTarget(
        slug=slug,
        condition_id=condition_id,
        up_token_id=str(definition.up_token_id),
        down_token_id=str(definition.down_token_id),
    )
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        readings = tuple(
            provider.read_condition(condition_id, block_tag=policy.block_tag)
            for provider in providers
        )
        decision = verify(target, readings, (), policy)
        if decision.state.value == "RESOLVED":
            return SettlementRecord(
                target=target, decision=decision, policy=policy, provider_readings=readings
            )
        time.sleep(SETTLE_POLL_SECONDS)
    return None
