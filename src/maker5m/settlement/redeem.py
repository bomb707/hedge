"""Planning a redemption, and making it impossible to send one before P14.

``redeemPositions`` is the **only** operation in this system that may destroy outcome tokens, and
it may only run after authoritative final resolution. There is no sell, hedge, flatten, merge,
split, or convert path anywhere — Canonical §18 forbids them and invariant I15 forbids
pre-settlement flattening.

The plan is a pure value. Building one is not authorisation to send it: :class:`Redeemer` refuses
before any credential is read, any key is touched, or any socket is opened, and there is
deliberately no flag, environment variable, or configuration key that lifts the refusal. P14 is
the first phase permitted to send the transaction, and reaching it requires a source edit and a
review.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Final

from maker5m.domain import Outcome
from maker5m.settlement.contracts import (
    CTF_ADDRESS,
    PARENT_COLLECTION_ID,
    PUSD_ADDRESS,
    binary_index_sets,
    encode_redeem_positions,
)
from maker5m.settlement.resolution import (
    AmbiguityReason,
    MarketResolutionTarget,
    ResolutionDecision,
    ResolutionState,
)

__all__ = [
    "RedeemBlocker",
    "RedeemPlan",
    "Redeemer",
    "RedemptionDisabledError",
    "SettlementPreconditions",
    "plan_redemption",
]


class RedemptionDisabledError(RuntimeError):
    """Sending a redemption is not possible in this build. P14 owns that."""


class RedeemBlocker(Enum):
    """Why a redemption was not planned. Every one of these is fail-closed."""

    NOT_RESOLVED = "NOT_RESOLVED"
    RESOLUTION_AMBIGUOUS = "RESOLUTION_AMBIGUOUS"
    ORDER_STATE_UNCERTAIN = "ORDER_STATE_UNCERTAIN"
    """An order may still be working against these very tokens. DONE is not proof that every
    cancellation reached the venue."""

    LIVE_ORDERS_PRESENT = "LIVE_ORDERS_PRESENT"
    POSITION_MISMATCH = "POSITION_MISMATCH"
    LEDGER_INCONSISTENT = "LEDGER_INCONSISTENT"
    NOTHING_TO_REDEEM = "NOTHING_TO_REDEEM"
    UNSUPPORTED_MARKET_STRUCTURE = "UNSUPPORTED_MARKET_STRUCTURE"


@dataclass(frozen=True, slots=True)
class SettlementPreconditions:
    """Everything outside resolution that must hold before a redemption may be planned.

    Deliberately *not* "risk is SAFE". After DONE the BTC and CLOB feeds may legitimately be
    stale or disconnected, and a finalized on-chain payout does not stop being finalized because
    a websocket dropped. What matters is settlement trust specifically.
    """

    occupying_orders: int = 0
    order_state_uncertain: bool = False
    position_mismatch: bool = False
    ledger_inconsistent: bool = False

    def blockers(self) -> tuple[RedeemBlocker, ...]:
        found: list[RedeemBlocker] = []
        if self.order_state_uncertain:
            found.append(RedeemBlocker.ORDER_STATE_UNCERTAIN)
        if self.occupying_orders:
            found.append(RedeemBlocker.LIVE_ORDERS_PRESENT)
        if self.position_mismatch:
            found.append(RedeemBlocker.POSITION_MISMATCH)
        if self.ledger_inconsistent:
            found.append(RedeemBlocker.LEDGER_INCONSISTENT)
        return tuple(found)


@dataclass(frozen=True, slots=True)
class RedeemPlan:
    """Exactly what would be sent, and nothing that could send it."""

    to: str
    collateral_token: str
    parent_collection_id: str
    condition_id: str
    index_sets: tuple[int, ...]
    calldata: str
    winning_outcome: Outcome
    authoritative_block: int | None
    value: int = 0
    """Wei. ``redeemPositions`` is not payable; this is stated rather than implied."""

    def summary(self) -> dict[str, object]:
        return {
            "to": self.to,
            "collateral_token": self.collateral_token,
            "parent_collection_id": self.parent_collection_id,
            "condition_id": self.condition_id,
            "index_sets": list(self.index_sets),
            "calldata": self.calldata,
            "calldata_bytes": (len(self.calldata) - 2) // 2,
            "winning_outcome": self.winning_outcome.value,
            "authoritative_block": self.authoritative_block,
            "value": self.value,
        }


def plan_redemption(
    target: MarketResolutionTarget,
    decision: ResolutionDecision,
    preconditions: SettlementPreconditions,
    *,
    has_balance: bool = True,
) -> tuple[RedeemPlan | None, tuple[RedeemBlocker, ...]]:
    """Build a plan, or explain in typed terms why there is none.

    There is no optimistic branch. Every path that is not "authoritatively resolved, mapped, and
    otherwise consistent" returns ``None`` with reasons.
    """
    blockers: list[RedeemBlocker] = []

    if target.neg_risk:
        blockers.append(RedeemBlocker.UNSUPPORTED_MARKET_STRUCTURE)
    if decision.state is ResolutionState.AMBIGUOUS:
        blockers.append(RedeemBlocker.RESOLUTION_AMBIGUOUS)
    elif decision.state is not ResolutionState.RESOLVED or decision.winning_outcome is None:
        blockers.append(RedeemBlocker.NOT_RESOLVED)
    if AmbiguityReason.UNSUPPORTED_MARKET_STRUCTURE in decision.reasons:
        blockers.append(RedeemBlocker.UNSUPPORTED_MARKET_STRUCTURE)

    blockers.extend(preconditions.blockers())
    if not has_balance:
        blockers.append(RedeemBlocker.NOTHING_TO_REDEEM)

    if blockers:
        return None, tuple(dict.fromkeys(blockers))

    assert decision.winning_outcome is not None
    index_sets = binary_index_sets()
    calldata = encode_redeem_positions(
        collateral_token=PUSD_ADDRESS,
        parent_collection_id=PARENT_COLLECTION_ID,
        condition_id=target.condition_id,
        index_sets=index_sets,
    )
    plan = RedeemPlan(
        to=CTF_ADDRESS,
        collateral_token=PUSD_ADDRESS,
        parent_collection_id=PARENT_COLLECTION_ID,
        condition_id=target.condition_id,
        index_sets=index_sets,
        calldata=calldata,
        winning_outcome=decision.winning_outcome,
        authoritative_block=decision.authoritative_block,
    )
    return plan, ()


REDEMPTION_ENABLED: Final[bool] = False
"""Whether this build may send a redemption. **Never true before P14.**

A module constant, like ``LIVE_TRADING_ENABLED``, and for the same reason: a flag, an environment
variable, or a config key would each be a way to send real transactions without the review that
is supposed to gate them. Unlocking requires editing source and having that edit reviewed.
"""


@dataclass(frozen=True, slots=True)
class Redeemer:
    """Prepares redemptions. Cannot send one.

    The refusal happens *before* anything is read, signed, or opened, so an accidental call
    cannot get far enough to touch a key. P14 owns the transport that would follow.
    """

    def prepare(
        self,
        target: MarketResolutionTarget,
        decision: ResolutionDecision,
        preconditions: SettlementPreconditions,
        *,
        has_balance: bool = True,
    ) -> tuple[RedeemPlan | None, tuple[RedeemBlocker, ...]]:
        """Pure planning. Safe to call anywhere; produces a value, never an effect."""
        return plan_redemption(target, decision, preconditions, has_balance=has_balance)

    def submit(self, plan: RedeemPlan) -> None:
        """Always raises. The transport itself is P14 work and does not exist here."""
        from maker5m.safety import LIVE_TRADING_ENABLED

        raise RedemptionDisabledError(
            "redemption transport is disabled: "
            f"REDEMPTION_ENABLED={REDEMPTION_ENABLED}, "
            f"LIVE_TRADING_ENABLED={LIVE_TRADING_ENABLED}. "
            f"Refused before reading any credential or opening any socket for condition "
            f"{plan.condition_id}. P14 is the first phase permitted to send this."
        )
