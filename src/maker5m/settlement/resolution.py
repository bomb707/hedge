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

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Final, NamedTuple

from maker5m.domain import Outcome, ParameterStatus
from maker5m.settlement.contracts import CHAIN_ID, PUSD_DECIMALS

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

    DUPLICATE_PROVIDER_ID = "DUPLICATE_PROVIDER_ID"
    """The same provider appears twice in one evidence set.

    Not a disagreement — a malformed claim of independence. Three readings from one endpoint are
    one opinion repeated, and counting them would let a single RPC authorise a redemption on its
    own, which is the only thing the quorum exists to prevent."""

    ATTESTATION_BINDING_MISMATCH = "ATTESTATION_BINDING_MISMATCH"
    """A reading carrying a proof that describes a different provider or a different endpoint.

    Kept separate from ``PROVIDER_NOT_ATTESTED`` because the two say different things to whoever
    reads the audit. A missing proof is usually a wiring mistake; a proof belonging to somebody
    else is evidence being moved around, and an auditor should not have to guess which happened."""

    PROVIDER_NOT_ATTESTED = "PROVIDER_NOT_ATTESTED"
    """A reading arrived from an endpoint that never proved which chain and contracts it serves.

    Reaching the verifier at all means the caller broke the contract, so this fails closed rather
    than quietly dropping the reading: silently ignoring it would turn a wiring bug into a
    smaller-than-configured quorum that still looked like consensus."""

    FINALITY_POLICY_MISMATCH = "FINALITY_POLICY_MISMATCH"
    """Readings taken under different finality rules, presented as one quorum.

    Two providers on ``latest`` and one on ``finalized`` are not three finalized confirmations,
    however much they agree."""

    MISSING_AUTHORITATIVE_BLOCK = "MISSING_AUTHORITATIVE_BLOCK"
    """Agreement with no concrete block behind it.

    A redemption has to be able to name the finalized chain state that authorised it. "Some
    providers said so recently" is not that."""


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


class ProviderAttestation(NamedTuple):
    """What one endpoint proved about itself before any of its answers were allowed to count.

    Identity is a trust boundary, not a diagnostic. An endpoint that is healthy but pointed at
    another chain answers confidently about the wrong world, and an endpoint serving a contract
    that is not the Conditional Tokens Framework answers confidently about the wrong contract.
    Neither failure looks like an error at the reading — both look like data.

    So the check is carried, not printed. A reading without a valid attestation attached cannot
    reach a quorum, and the only code that can attach one is the code that performed the checks.
    """

    provider_id: str
    endpoint_fingerprint: str
    """Normalised endpoint URL. Two ids sharing one fingerprint are one opinion twice."""

    chain_id: int | None = None
    ctf_code_bytes: int = 0
    collateral_code_bytes: int = 0
    collateral_decimals: int | None = None
    attested_at_block: int | None = None
    attested_at_ns: int | None = None
    error: str | None = None

    @property
    def valid(self) -> bool:
        return (
            self.error is None
            and self.chain_id == CHAIN_ID
            and self.ctf_code_bytes > 0
            and self.collateral_code_bytes > 0
            and self.collateral_decimals == PUSD_DECIMALS
        )

    def summary(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "endpoint_fingerprint": self.endpoint_fingerprint,
            "chain_id": self.chain_id,
            "ctf_code_bytes": self.ctf_code_bytes,
            "collateral_code_bytes": self.collateral_code_bytes,
            "collateral_decimals": self.collateral_decimals,
            "attested_at_block": self.attested_at_block,
            "valid": self.valid,
            "error": self.error,
        }


class ProviderResolution(NamedTuple):
    """One RPC provider's answer about one condition, at one concrete block."""

    provider_id: str
    chain_id: int | None
    block_tag: str
    """The finality policy this reading was taken under, not the block it was pinned to."""

    block_number: int | None
    condition_id: str
    payout: PayoutVector | None
    error: str | None = None
    source_endpoint_fingerprint: str = ""
    """Which endpoint actually produced this reading.

    Recorded by the reader from its own endpoint, deliberately not copied out of the attestation.
    The proof must not be allowed to supply both sides of its own comparison — that is what made
    the earlier check vacuous."""

    attestation: ProviderAttestation | None = None
    """Set only by a reader that actually performed the identity checks. ``None`` means the
    reading is not eligible for a quorum, whatever it says."""

    @property
    def answered(self) -> bool:
        return self.error is None and self.payout is not None

    @property
    def bound(self) -> bool:
        """Whether the proof describes this reading's own provider and endpoint."""
        attestation = self.attestation
        return (
            attestation is not None
            and attestation.provider_id == self.provider_id
            and attestation.endpoint_fingerprint == self.source_endpoint_fingerprint
        )

    @property
    def attested(self) -> bool:
        return self.attestation is not None and self.attestation.valid and self.bound

    def summary(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "chain_id": self.chain_id,
            "block_tag": self.block_tag,
            "block_number": self.block_number,
            "condition_id": self.condition_id,
            "payout": None if self.payout is None else self.payout.summary(),
            "error": self.error,
            "source_endpoint_fingerprint": self.source_endpoint_fingerprint,
            "attested": self.attested,
            "binding_valid": self.bound,
            "attestation": None if self.attestation is None else self.attestation.summary(),
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
    """The finality rule every counted reading must have been taken under.

    Load-bearing, not descriptive: a reading recording a different tag is rejected rather than
    absorbed, because two providers on ``latest`` and one on ``finalized`` are not three
    finalized confirmations however much they agree.

    A confirmation-depth fallback was once exposed here and did nothing. It has been removed
    rather than left as a knob with no effect; if ``finalized`` ever proves unavailable on an
    endpoint we need, the fallback can be added with its own evidence.
    """

    require_binary_singleton: bool = True
    """This strategy's economics are binary. Anything else is ambiguous *for us*, which is not
    the same as invalid on chain."""

    require_unanimous_resolution: bool = False
    """Whether every answering provider must report the resolution, not merely a quorum of them.

    OPERATIONAL. ``True`` is the strict reading and it is genuinely defensible in the abstract;
    it is also unusable here. Live observation halted 3 of 6 and 3 of 9 real markets under it,
    every time because one provider briefly failed to serve state it demonstrably had at that
    block — not because the chain said two different things. Kept reachable and tested, because
    the argument for it is about trust in providers rather than about the data."""

    status: ParameterStatus = SETTLEMENT_POLICY_STATUS

    def __post_init__(self) -> None:
        if self.minimum_agreeing_providers < 2:
            raise ValueError(
                "at least two independent providers are required; one provider cannot "
                f"authorise a redemption, got {self.minimum_agreeing_providers}"
            )
        if not self.block_tag:
            raise ValueError("a finality policy is required; readings are checked against it")


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
    untrusted_providers: tuple[str, ...] = ()
    """Endpoints excluded before they could answer. Recorded so a small quorum is visibly small
    rather than silently so."""

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
            "untrusted_providers": list(self.untrusted_providers),
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


def _binding_fault(reading: ProviderResolution) -> str:
    """Say which half of the binding failed, so the audit does not have to guess."""
    attestation = reading.attestation
    assert attestation is not None
    if attestation.provider_id != reading.provider_id:
        return (
            f"reading from {reading.provider_id!r} carries a proof for {attestation.provider_id!r}"
        )
    return (
        f"{reading.provider_id}: read from "
        f"{reading.source_endpoint_fingerprint or '<unrecorded>'!r} but the proof describes "
        f"{attestation.endpoint_fingerprint!r}"
    )


def _repeated_fingerprints(readings: tuple[ProviderResolution, ...]) -> list[str]:
    """Provider ids that reach the same endpoint under different names."""
    by_fingerprint: dict[str, list[str]] = {}
    for reading in readings:
        attestation = reading.attestation
        if attestation is None or not attestation.endpoint_fingerprint:
            continue
        by_fingerprint.setdefault(attestation.endpoint_fingerprint, []).append(reading.provider_id)
    return sorted(
        f"{fingerprint} <- {', '.join(sorted(ids))}"
        for fingerprint, ids in by_fingerprint.items()
        if len(set(ids)) > 1
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

    # The policy names the finality rule; the readings do not get a vote on it. Taking this from
    # `provider_readings[0]` — as this once did — meant whichever provider happened to be first
    # in the tuple defined what the audit record claimed the quorum was taken under.
    block_tag = policy.block_tag

    # Independence is a property of the evidence *set*, so it is checked before anything the
    # individual readings say. Three answers from one endpoint are one opinion repeated, and no
    # amount of agreement between them makes them three.
    counts = Counter(reading.provider_id for reading in provider_readings)
    repeated = sorted(name for name, count in counts.items() if count > 1)
    if repeated:
        return _ambiguous(
            (AmbiguityReason.DUPLICATE_PROVIDER_ID,),
            answering=answering,
            advisory=advisory_readings,
            detail=f"provider id repeated in one evidence set: {', '.join(repeated)}",
            block_tag=block_tag,
        )

    # Checked here as well as at the reader, because this function is pure and must be able to
    # judge a ProviderResolution somebody built by hand. The reader refuses a foreign proof
    # before it sends a request; this refuses one that never went through a reader at all.
    misbound = sorted(
        _binding_fault(reading)
        for reading in provider_readings
        if reading.attestation is not None and not reading.bound
    )
    if misbound:
        return _ambiguous(
            (AmbiguityReason.ATTESTATION_BINDING_MISMATCH,),
            answering=answering,
            advisory=advisory_readings,
            detail=f"proof does not describe the reading it is attached to: {'; '.join(misbound)}",
            block_tag=block_tag,
        )

    unattested = sorted(
        reading.provider_id for reading in provider_readings if not reading.attested
    )
    if unattested:
        return _ambiguous(
            (AmbiguityReason.PROVIDER_NOT_ATTESTED,),
            answering=answering,
            advisory=advisory_readings,
            detail="readings from endpoints that never proved their chain and contracts: "
            f"{', '.join(unattested)}",
            block_tag=block_tag,
        )

    duplicate_endpoints = _repeated_fingerprints(provider_readings)
    if duplicate_endpoints:
        return _ambiguous(
            (AmbiguityReason.DUPLICATE_PROVIDER_ID,),
            answering=answering,
            advisory=advisory_readings,
            detail=f"distinct provider ids sharing one endpoint: {', '.join(duplicate_endpoints)}",
            block_tag=block_tag,
        )

    wrong_finality = sorted(
        reading.provider_id
        for reading in provider_readings
        if reading.answered and reading.block_tag != policy.block_tag
    )
    if wrong_finality:
        return _ambiguous(
            (AmbiguityReason.FINALITY_POLICY_MISMATCH,),
            answering=answering,
            advisory=advisory_readings,
            detail=f"policy requires {policy.block_tag!r}; readings taken under another rule: "
            f"{', '.join(wrong_finality)}",
            block_tag=block_tag,
        )

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

    # Contradiction first, absence second. A provider reporting a *different* payout vector is
    # dangerous; a provider reporting nothing yet is merely not evidence.
    distinct = {reading.payout for reading in resolved}
    if len(distinct) != 1:
        return _ambiguous(
            (AmbiguityReason.PROVIDER_DISAGREEMENT,),
            answering=answering,
            advisory=advisory_readings,
            detail=f"{len(distinct)} distinct payout vectors across answering providers",
            block_tag=block_tag,
        )

    if len(resolved) != len(answered):
        # Not enough positive agreement yet — which is a reason to wait, not to fault.
        #
        # This branch used to be ambiguity, and that was wrong twice over. The quorum is named
        # `minimum_agreeing_providers` but was being applied to providers that merely *answered*,
        # so unanimity was required by accident. And live observation showed the split is not
        # what it looks like: drpc reported a payout absent at the same block, and once at a
        # block *ahead* of a provider that had it. That is one provider's internal
        # inconsistency, not the chain contradicting itself, and it is not something a
        # cross-provider block comparison can adjudicate.
        #
        # So: wait. Nothing here authorises a redemption, and nothing here halts trading either.
        behind = sorted(reading.provider_id for reading in answered if reading not in resolved)
        if policy.require_unanimous_resolution:
            return _ambiguous(
                (AmbiguityReason.FINALITY_DISAGREEMENT,),
                answering=answering,
                advisory=advisory_readings,
                detail="some providers report resolved and others unresolved; "
                f"unresolved: {', '.join(behind)}",
                block_tag=block_tag,
            )
        if len(resolved) < policy.minimum_agreeing_providers:
            return ResolutionDecision(
                state=ResolutionState.UNRESOLVED,
                answering_providers=answering,
                advisory=advisory_readings,
                block_tag=block_tag,
                authoritative_block=min(
                    (r.block_number for r in answered if r.block_number is not None), default=None
                ),
                detail=(
                    f"{len(resolved)} of {policy.minimum_agreeing_providers} required providers "
                    f"report the resolution; waiting on {', '.join(behind)}"
                ),
            )

    payout = next(iter(distinct))
    assert payout is not None
    agreeing = tuple(sorted(reading.provider_id for reading in resolved))

    # A redemption has to be able to name the finalized chain state that authorised it.
    blockless = sorted(reading.provider_id for reading in resolved if reading.block_number is None)
    if blockless:
        return _ambiguous(
            (AmbiguityReason.MISSING_AUTHORITATIVE_BLOCK,),
            answering=answering,
            advisory=advisory_readings,
            payout=payout,
            detail=f"agreeing providers without a concrete block: {', '.join(blockless)}",
            block_tag=block_tag,
        )
    authoritative_block = min(
        reading.block_number for reading in resolved if reading.block_number is not None
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
