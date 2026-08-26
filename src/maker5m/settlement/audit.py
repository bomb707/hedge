"""A compact, versioned record of one settlement decision.

Enough to answer "why was this redemption planned, or why was it not?" without re-reading the
chain: every provider's answer including its errors, the quorum that was required and the one
that was reached, the payout vector exactly as the contract held it, the outcome mapping that was
checked, the advisory observations, and the typed reasons for anything that failed.

No credential, no signature, no key, no wallet appears here, and there is nowhere for one to go.
P11 owns durable persistence; this is an in-memory value that serialises to plain JSON.
"""

from dataclasses import dataclass
from typing import Final

from maker5m.settlement.payout import PaperSettlement
from maker5m.settlement.redeem import RedeemBlocker, RedeemPlan
from maker5m.settlement.resolution import (
    MarketResolutionTarget,
    ProviderResolution,
    ResolutionDecision,
    SettlementPolicy,
)

__all__ = ["SETTLEMENT_SCHEMA_VERSION", "SettlementRecord"]

SETTLEMENT_SCHEMA_VERSION: Final[int] = 1


@dataclass(frozen=True, slots=True)
class SettlementRecord:
    """One market's settlement evidence."""

    target: MarketResolutionTarget
    policy: SettlementPolicy
    provider_readings: tuple[ProviderResolution, ...]
    decision: ResolutionDecision
    settlement: PaperSettlement | None = None
    plan: RedeemPlan | None = None
    blockers: tuple[RedeemBlocker, ...] = ()
    captured_utc: str = ""
    provenance: str = "REAL_PUBLIC_MARKET_DATA"

    def summary(self) -> dict[str, object]:
        return {
            "schema_version": SETTLEMENT_SCHEMA_VERSION,
            "provenance": self.provenance,
            "captured_utc": self.captured_utc,
            "market": {
                "slug": self.target.slug,
                "condition_id": self.target.condition_id,
                "up_token_id": self.target.up_token_id,
                "down_token_id": self.target.down_token_id,
                "outcome_slot_count": self.target.outcome_slot_count,
                "up_slot": self.target.up_slot,
                "down_slot": self.target.down_slot,
                "neg_risk": self.target.neg_risk,
            },
            "policy": {
                "minimum_agreeing_providers": self.policy.minimum_agreeing_providers,
                "block_tag": self.policy.block_tag,
                "confirmation_depth": self.policy.confirmation_depth,
                "require_binary_singleton": self.policy.require_binary_singleton,
                "require_unanimous_resolution": self.policy.require_unanimous_resolution,
                "status": self.policy.status.value,
            },
            "provider_readings": [reading.summary() for reading in self.provider_readings],
            "resolution": self.decision.summary(),
            "paper_settlement": None if self.settlement is None else self.settlement.summary(),
            "redeem_plan": None if self.plan is None else self.plan.summary(),
            "redeem_blockers": [blocker.value for blocker in self.blockers],
            "note": (
                "Paper settlement is what the authoritative payout vector entitles these "
                "balances to. It is not proof that collateral has moved: no redemption "
                "transaction has been sent and none can be in this build."
            ),
        }
