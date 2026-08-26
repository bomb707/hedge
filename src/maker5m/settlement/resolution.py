"""Deciding whether a market's outcome can be trusted enough to redeem against.

O11 established the precedence this implements, and the shape of it matters more than any single
rule: the Conditional Tokens payout vector is the **only** thing that can authorise a redemption,
because it is the only thing that pays. Gamma and the CLOB are advisory — they agreed with the
chain in 55 of 55 real markets but arrive more than two minutes later, so their *absence* proves
nothing and their *disagreement* is a fault rather than a vote.

Four states, and the distinction between two of them is the one most easily got wrong:

```text
UNRESOLVED             every trusted reader says payoutDenominator == 0.
                       A normal waiting state, not an error.
RESOLVED               quorum agreement on a payout vector this strategy can act on.
AMBIGUOUS              conflicting, structurally unexpected, or contradicted evidence.
INSUFFICIENT_EVIDENCE  not enough providers answered to judge either way.
```

``UNRESOLVED`` is not ``AMBIGUOUS``. A market that has simply not settled yet must not raise a
permanent fault, or every market would fault for the ninety seconds before the chain catches up.

Purity
------
:func:`verify` reads no clock, no network, no filesystem. Provider readings and advisory
observations arrive already fetched, so the same inputs always produce the same decision and a
recorded settlement can be re-derived exactly.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Final, NamedTuple

from maker5m.domain import Outcome, ParameterStatus
from maker5m.settlement.contracts import CHAIN_ID

__all__ = [
    "DEFAULT_SETTLEMENT_POLICY",
    "SETTLEMENT_POLICY_STATUS",
    "AdvisoryResolution",
    "AmbiguityReason",
    "MarketResolutionTarget",
    "PayoutVector",
    "ProviderResolution",
    "ResolutionDecision",
    "ResolutionState",
    "SettlementPolicy",
    "verify",
]

SETTLEMENT_POLICY_STATUS: Final = ParameterStatus.OPERATIONAL


class ResolutionState(Enum):
    """How much the evidence supports acting."""

    UNRESOLVED = "UNRESOLVED"
    """Trusted readers agree the condition has no payout yet. Wait; do not fault."""

    RESOLVED = "RESOLVED"
    """Quorum agreement on a payout this strategy can act on."""

    AMBIGUOUS = "AMBIGUOUS"
    """Conflicting, contradicted, or structurally unexpected evidence. Never redeem."""

    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    """Too few providers answered. Distinct from disagreement: nobody contradicted anybody."""


class AmbiguityReason(Enum):
    """Why a decision is not actionable. Typed so the audit says which check failed."""

    PROVIDER_DISAGREEMENT = "PROVIDER_DISAGREEMENT"
    FINALITY_DISAGREEMENT = "FINALITY_DISAGREEMENT"
    """Some providers report resolved and others unresolved at comparable finalized state."""

    INSUFFICIENT_QUORUM = "INSUFFICIENT_QUORUM"
    WRONG_CHAIN = "WRONG_CHAIN"
    CONDITION_MISMATCH = "CONDITION_MISMATCH"
    UNEXPECTED_SLOT_COUNT = "UNEXPECTED_SLOT_COUNT"
    NON_BINARY_PAYOUT = "NON_BINARY_PAYOUT"
    """A valid CTF vector this binary strategy has no economics for. Preserved, never forced."""

    UNEXPECTED_DENOMINATOR = "UNEXPECTED_DENOMINATOR"
    ADVISORY_DISAGREEMENT = "ADVISORY_DISAGREEMENT"
    OUTCOME_MAPPING_MISMATCH = "OUTCOME_MAPPING_MISMATCH"
    UNSUPPORTED_MARKET_STRUCTURE = "UNSUPPORTED_MARKET_STRUCTURE"
    """NegRisk or anything else this redemption path was not built and evidenced for."""


class PayoutVector(NamedTuple):
    """A Conditional Tokens payout, exactly as the contract holds it.

    Deliberately *not* a winner. CTF payouts are a numerator per outcome slot over a shared
    denominator, and fractional or multi-slot vectors are valid contract state. Collapsing this
    into ``winner: UP | DOWN`` at the type level would make an unexpected payout unrepresentable
    and therefore invisible.
    """

    denominator: int
    numerators: tuple[int, ...]
    outcome_slot_count: int

    @property
    def resolved(self) -> bool:
        return self.denominator > 0

    @property
    def is_binary_singleton(self) -> bool:
        """Exactly two slots, one paying the whole denominator and the other nothing."""
        if self.outcome_slot_count != 2 or len(self.numerators) != 2:
            return False
        if self.denominator <= 0:
            return False
        return sorted(self.numerators) == [0, self.denominator]

    @property
    def winning_slot(self) -> int | None:
        """The paying slot, or ``None`` when the vector is not a binary singleton."""
        if not self.is_binary_singleton:
            return None
        return 0 if self.numerators[0] else 1

    def summary(self) -> dict[str, object]:
        return {
            "denominator": self.denominator,
            "numerators": list(self.numerators),
            "outcome_slot_count": self.outcome_slot_count,
        }


class ProviderResolution(NamedTuple):
    """One RPC provider's answer about one condition, at one block."""

    provider_id: str
    chain_id: int | None
    block_tag: str
    block_number: int | None
    condition_id: str
    payout: PayoutVector | None
    error: str | None = None

    @property
    def answered(self) -> bool:
        return self.error is None and self.payout is not None

    def summary(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "chain_id": self.chain_id,
            "block_tag": self.block_tag,
            "block_number": self.block_number,
            "condition_id": self.condition_id,
            "payout": None if self.payout is None else self.payout.summary(),
            "error": self.error,
        }


class AdvisoryResolution(NamedTuple):
    """Gamma or CLOB metadata. Advisory: it may be absent, and absence proves nothing."""

    source: str
    available: bool
    winning_slot: int | None = None
    detail: str = ""

    @property
    def conclusive(self) -> bool:
        return self.available and self.winning_slot is not None

    def summary(self) -> dict[str, object]:
        return {
            "source": self.source,
            "available": self.available,
            "winning_slot": self.winning_slot,
            "detail": self.detail,
        }


class MarketResolutionTarget(NamedTuple):
    """What we believed about the market *before* it settled.

    Captured at discovery, deliberately, so settlement never depends on Gamma being updated
    afterwards to remember which token was which.
    """

    slug: str
    condition_id: str
    up_token_id: str
    down_token_id: str
    outcome_slot_count: int = 2
    up_slot: int = 0
    down_slot: int = 1
    neg_risk: bool = False

    def outcome_for_slot(self, slot: int) -> Outcome | None:
        if slot == self.up_slot:
            return Outcome.UP
        if slot == self.down_slot:
            return Outcome.DOWN
        return None


@dataclass(frozen=True, slots=True)
class SettlementPolicy:
    """OPERATIONAL settlement policy. None of it is reconstructed strategy behaviour."""

    minimum_agreeing_providers: int = 3
    """P10A saw at least three independent answers on every one of 55 markets, so three is
    achievable in practice. It is an engineering choice, not a Polymarket rule."""

    block_tag: str = "finalized"
    """Prefer the chain's own finality rather than counting confirmations. Polygon's
    ``finalized`` tag lagged head by 1-4 blocks across three providers when measured."""

    confirmation_depth: int | None = None
    """Only meaningful as a fallback where ``finalized`` is unsupported. ``None`` means the tag
    is required; a number here is a weaker, explicitly OPERATIONAL substitute."""

    require_binary_singleton: bool = True
    """This strategy's economics are binary. Anything else is ambiguous *for us*, which is not
    the same as invalid on chain."""

    status: ParameterStatus = SETTLEMENT_POLICY_STATUS

    def __post_init__(self) -> None:
        if self.minimum_agreeing_providers < 2:
            raise ValueError(
                "at least two independent providers are required; one provider cannot "
                f"authorise a redemption, got {self.minimum_agreeing_providers}"
            )
        if self.confirmation_depth is not None and self.confirmation_depth < 0:
            raise ValueError(f"confirmation_depth must be >= 0, got {self.confirmation_depth}")


DEFAULT_SETTLEMENT_POLICY: Final = SettlementPolicy()


@dataclass(frozen=True, slots=True)
class ResolutionDecision:
    """The verdict, with everything it was based on."""

    state: ResolutionState
    payout: PayoutVector | None = None
    winning_outcome: Outcome | None = None
    agreeing_providers: tuple[str, ...] = ()
    answering_providers: tuple[str, ...] = ()
    advisory: tuple[AdvisoryResolution, ...] = ()
    reasons: tuple[AmbiguityReason, ...] = ()
    authoritative_block: int | None = None
    block_tag: str = ""
    detail: str = ""
    _unused: bool = field(default=False, repr=False)

    @property
    def redeemable(self) -> bool:
        """Whether resolution *alone* permits a redemption.

        Deliberately narrow. Order state, position consistency, and ledger consistency are
        separate preconditions checked by the coordinator; a resolution verdict cannot know
        about them and must not imply it does.
        """
        return self.state is ResolutionState.RESOLVED and self.winning_outcome is not None

    def summary(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "payout": None if self.payout is None else self.payout.summary(),
            "winning_outcome": None if self.winning_outcome is None else self.winning_outcome.value,
            "agreeing_providers": list(self.agreeing_providers),
            "answering_providers": list(self.answering_providers),
            "advisory": [item.summary() for item in self.advisory],
            "reasons": [reason.value for reason in self.reasons],
            "authoritative_block": self.authoritative_block,
            "block_tag": self.block_tag,
            "detail": self.detail,
        }


def _ambiguous(
    reasons: tuple[AmbiguityReason, ...],
    *,
    answering: tuple[str, ...] = (),
    advisory: tuple[AdvisoryResolution, ...] = (),
    payout: PayoutVector | None = None,
    detail: str = "",
    block_tag: str = "",
) -> ResolutionDecision:
    return ResolutionDecision(
        state=ResolutionState.AMBIGUOUS,
        payout=payout,
        answering_providers=answering,
        advisory=advisory,
        reasons=reasons,
        detail=detail,
        block_tag=block_tag,
    )


def verify(
    target: MarketResolutionTarget,
    provider_readings: tuple[ProviderResolution, ...],
    advisory_readings: tuple[AdvisoryResolution, ...] = (),
    policy: SettlementPolicy = DEFAULT_SETTLEMENT_POLICY,
) -> ResolutionDecision:
    """Decide whether this market's outcome can be trusted. Pure.

    The order of the checks is the order of the doubts: is this even the right chain and
    condition, did enough providers answer, do they agree, is the payout a shape we have
    economics for, does it map onto the tokens we hold, and does anything advisory contradict
    it. Each failure names itself rather than collapsing into a single "no".
    """
    if target.neg_risk:
        return _ambiguous(
            (AmbiguityReason.UNSUPPORTED_MARKET_STRUCTURE,),
            detail="negRisk market: this redemption path was built and evidenced for the "
            "ordinary binary structure only",
        )

    answering = tuple(reading.provider_id for reading in provider_readings if reading.answered)
    block_tag = provider_readings[0].block_tag if provider_readings else policy.block_tag

    wrong_chain = [
        reading.provider_id
        for reading in provider_readings
        if reading.answered and reading.chain_id != CHAIN_ID
    ]
    if wrong_chain:
        return _ambiguous(
            (AmbiguityReason.WRONG_CHAIN,),
            answering=answering,
            advisory=advisory_readings,
            detail=f"providers not on chain {CHAIN_ID}: {', '.join(sorted(wrong_chain))}",
            block_tag=block_tag,
        )

    wrong_condition = [
        reading.provider_id
        for reading in provider_readings
        if reading.answered and reading.condition_id.lower() != target.condition_id.lower()
    ]
    if wrong_condition:
        return _ambiguous(
            (AmbiguityReason.CONDITION_MISMATCH,),
            answering=answering,
            advisory=advisory_readings,
            detail=f"readings for another condition: {', '.join(sorted(wrong_condition))}",
            block_tag=block_tag,
        )

    if len(answering) < policy.minimum_agreeing_providers:
        return ResolutionDecision(
            state=ResolutionState.INSUFFICIENT_EVIDENCE,
            answering_providers=answering,
            advisory=advisory_readings,
            reasons=(AmbiguityReason.INSUFFICIENT_QUORUM,),
            block_tag=block_tag,
            detail=f"{len(answering)} providers answered; "
            f"{policy.minimum_agreeing_providers} required",
        )

    answered = [reading for reading in provider_readings if reading.answered]
    resolved = [reading for reading in answered if reading.payout and reading.payout.resolved]

    if not resolved:
        # Nobody contradicts anybody; the condition simply has not settled. Waiting, not faulting.
        return ResolutionDecision(
            state=ResolutionState.UNRESOLVED,
            answering_providers=answering,
            advisory=advisory_readings,
            block_tag=block_tag,
            authoritative_block=min(
                (r.block_number for r in answered if r.block_number is not None), default=None
            ),
            detail="payoutDenominator is zero at every answering provider",
        )

    if len(resolved) != len(answered):
        split = sorted(reading.provider_id for reading in answered if reading not in resolved)
        return _ambiguous(
            (AmbiguityReason.FINALITY_DISAGREEMENT,),
            answering=answering,
            advisory=advisory_readings,
            detail="some providers report resolved and others unresolved at comparable "
            f"finalized state; unresolved: {', '.join(split)}",
            block_tag=block_tag,
        )

    distinct = {reading.payout for reading in resolved}
    if len(distinct) != 1:
        return _ambiguous(
            (AmbiguityReason.PROVIDER_DISAGREEMENT,),
            answering=answering,
            advisory=advisory_readings,
            detail=f"{len(distinct)} distinct payout vectors across answering providers",
            block_tag=block_tag,
        )

    payout = next(iter(distinct))
    assert payout is not None
    agreeing = tuple(sorted(reading.provider_id for reading in resolved))
    authoritative_block = min(
        (reading.block_number for reading in resolved if reading.block_number is not None),
        default=None,
    )

    if payout.outcome_slot_count != target.outcome_slot_count:
        return _ambiguous(
            (AmbiguityReason.UNEXPECTED_SLOT_COUNT,),
            answering=answering,
            advisory=advisory_readings,
            payout=payout,
            detail=f"chain reports {payout.outcome_slot_count} slots, market metadata says "
            f"{target.outcome_slot_count}",
            block_tag=block_tag,
        )

    if policy.require_binary_singleton and not payout.is_binary_singleton:
        reason = (
            AmbiguityReason.UNEXPECTED_DENOMINATOR
            if payout.denominator not in (0, sum(payout.numerators))
            else AmbiguityReason.NON_BINARY_PAYOUT
        )
        return _ambiguous(
            (reason,),
            answering=answering,
            advisory=advisory_readings,
            payout=payout,
            detail="valid CTF payout that this binary strategy has no economics for; "
            "the vector is preserved and is not forced into a winner",
            block_tag=block_tag,
        )

    slot = payout.winning_slot
    assert slot is not None
    outcome = target.outcome_for_slot(slot)
    if outcome is None:
        return _ambiguous(
            (AmbiguityReason.OUTCOME_MAPPING_MISMATCH,),
            answering=answering,
            advisory=advisory_readings,
            payout=payout,
            detail=f"paying slot {slot} maps to neither UP ({target.up_slot}) nor DOWN "
            f"({target.down_slot}) in this market's metadata",
            block_tag=block_tag,
        )

    contradicting = [
        item for item in advisory_readings if item.conclusive and item.winning_slot != slot
    ]
    if contradicting:
        # Advisory sources never break a tie between providers, but they can raise one.
        return _ambiguous(
            (AmbiguityReason.ADVISORY_DISAGREEMENT,),
            answering=answering,
            advisory=advisory_readings,
            payout=payout,
            detail="advisory sources name a different winner than the payout vector: "
            + ", ".join(f"{item.source}=slot {item.winning_slot}" for item in contradicting),
            block_tag=block_tag,
        )

    return ResolutionDecision(
        state=ResolutionState.RESOLVED,
        payout=payout,
        winning_outcome=outcome,
        agreeing_providers=agreeing,
        answering_providers=answering,
        advisory=advisory_readings,
        authoritative_block=authoritative_block,
        block_tag=block_tag,
        detail=f"{len(agreeing)} providers agree on slot {slot} ({outcome.value})",
    )
