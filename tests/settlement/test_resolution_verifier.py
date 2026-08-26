"""The resolution verdict, and every way it must refuse to reach one.

**SUPPORTING UNIT TEST ONLY.** Constructed provider readings driven through the pure verifier.
They prove the decision logic; they prove nothing about real venue or chain behaviour. The
real-market evidence is separate and lives in `docs/evidence/P10-SETTLEMENT-REAL-MARKET.md`.
"""

from __future__ import annotations

import pytest

from maker5m.domain import Outcome
from maker5m.market.timebase import TimestampNs
from maker5m.settlement import (
    AdvisoryResolution,
    AmbiguityReason,
    MarketResolutionTarget,
    PayoutVector,
    ProviderAttestation,
    ProviderResolution,
    ResolutionState,
    SettlementPolicy,
    verify,
)

CONDITION = "0x38cd5ae6f1edc9b5256aec2104570c7b65edcf523a923c427cf7174f5c81ad8a"
POLICY = SettlementPolicy()


def target(**overrides: object) -> MarketResolutionTarget:
    base = MarketResolutionTarget(
        slug="btc-updown-5m-1787678100",
        condition_id=CONDITION,
        up_token_id="110812484999043231239085940561002422009675005555903980849640432881019677328146",
        down_token_id="102646035968219316183082673461659256686129432733854586945044223308275343208922",
    )
    return base._replace(**overrides)  # type: ignore[arg-type]


def attestation(provider: str, *, url: str = "", **overrides: object) -> ProviderAttestation:
    """A passing attestation. Failing ones are built by overriding one field."""
    base = {
        "provider_id": provider,
        "endpoint_fingerprint": url or f"https://{provider}.example/rpc",
        "chain_id": 137,
        "ctf_code_bytes": 12_345,
        "collateral_code_bytes": 6_789,
        "collateral_decimals": 6,
        "attested_at_block": 92_665_370,
    }
    base.update(overrides)
    return ProviderAttestation(**base)  # type: ignore[arg-type]


def reading(
    provider: str,
    numerators: tuple[int, ...] = (1, 0),
    denominator: int = 1,
    slots: int = 2,
    *,
    chain_id: int = 137,
    condition_id: str = CONDITION,
    block: int | None = 92_665_372,
    error: str | None = None,
    block_tag: str = "finalized",
    attested: ProviderAttestation | None = None,
    url: str = "",
) -> ProviderResolution:
    payout = (
        None
        if error
        else PayoutVector(denominator=denominator, numerators=numerators, outcome_slot_count=slots)
    )
    return ProviderResolution(
        provider_id=provider,
        chain_id=None if error else chain_id,
        block_tag=block_tag,
        block_number=None if error else block,
        condition_id=condition_id,
        payout=payout,
        error=error,
        source_endpoint_fingerprint=url or f"https://{provider}.example/rpc",
        attestation=attestation(provider, url=url) if attested is None else attested,
    )


def three(**kwargs: object) -> tuple[ProviderResolution, ...]:
    return tuple(reading(name, **kwargs) for name in ("a", "b", "c"))  # type: ignore[arg-type]


# -- the happy paths -------------------------------------------------------------------------


def test_agreeing_providers_resolve_up() -> None:
    decision = verify(target(), three(numerators=(1, 0)), (), POLICY)
    assert decision.state is ResolutionState.RESOLVED
    assert decision.winning_outcome is Outcome.UP
    assert decision.redeemable
    assert decision.agreeing_providers == ("a", "b", "c")
    assert decision.authoritative_block == 92_665_372


def test_agreeing_providers_resolve_down() -> None:
    decision = verify(target(), three(numerators=(0, 1)), (), POLICY)
    assert decision.state is ResolutionState.RESOLVED
    assert decision.winning_outcome is Outcome.DOWN


def test_an_unresolved_condition_is_not_ambiguous() -> None:
    """The distinction the whole design turns on: waiting is not faulting."""
    decision = verify(target(), three(numerators=(0, 0), denominator=0), (), POLICY)
    assert decision.state is ResolutionState.UNRESOLVED
    assert decision.reasons == ()
    assert not decision.redeemable


def test_advisory_agreement_is_recorded_but_not_required() -> None:
    advisory = (
        AdvisoryResolution("gamma", available=True, winning_slot=0),
        AdvisoryResolution("clob", available=True, winning_slot=0),
    )
    decision = verify(target(), three(numerators=(1, 0)), advisory, POLICY)
    assert decision.state is ResolutionState.RESOLVED
    assert len(decision.advisory) == 2


def test_advisory_absence_does_not_override_chain_consensus() -> None:
    """P10A measured Gamma and the CLOB arriving over two minutes late. Silence is not a veto."""
    advisory = (
        AdvisoryResolution("gamma", available=False),
        AdvisoryResolution("clob", available=True, winning_slot=None, detail="no winner yet"),
    )
    decision = verify(target(), three(numerators=(1, 0)), advisory, POLICY)
    assert decision.state is ResolutionState.RESOLVED
    assert decision.winning_outcome is Outcome.UP


# -- every way it refuses ----------------------------------------------------------------------


def test_provider_disagreement_is_ambiguous() -> None:
    readings = (reading("a", (1, 0)), reading("b", (0, 1)), reading("c", (1, 0)))
    decision = verify(target(), readings, (), POLICY)
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.PROVIDER_DISAGREEMENT in decision.reasons
    assert not decision.redeemable


def test_a_split_between_resolved_and_unresolved_is_a_wait_not_a_contradiction() -> None:
    """One provider not showing the resolution is an absence of evidence, not contrary evidence.

    This assertion is the inverse of what it originally said. See the module note above: the
    original reading of this split cost 6 false halts across 15 real markets.
    """
    readings = (
        reading("a", (1, 0)),
        reading("b", (0, 0), denominator=0),
        reading("c", (1, 0)),
    )
    decision = verify(target(), readings, (), POLICY)
    assert decision.state is ResolutionState.UNRESOLVED
    assert not decision.reasons
    assert "waiting on b" in decision.detail


def test_too_few_providers_is_insufficient_not_ambiguous() -> None:
    readings = (reading("a", (1, 0)), reading("b", error="timeout"))
    decision = verify(target(), readings, (), POLICY)
    assert decision.state is ResolutionState.INSUFFICIENT_EVIDENCE
    assert AmbiguityReason.INSUFFICIENT_QUORUM in decision.reasons


def test_a_timeout_is_never_counted_as_agreement() -> None:
    readings = (reading("a", (1, 0)), reading("b", (1, 0)), reading("c", error="rate limited"))
    decision = verify(target(), readings, (), POLICY)
    assert decision.state is ResolutionState.INSUFFICIENT_EVIDENCE
    assert "c" not in decision.answering_providers


def test_a_single_provider_can_never_authorise() -> None:
    decision = verify(target(), (reading("a", (1, 0)),), (), POLICY)
    assert not decision.redeemable
    assert decision.state is ResolutionState.INSUFFICIENT_EVIDENCE


def test_a_policy_below_two_providers_is_refused() -> None:
    with pytest.raises(ValueError, match="one provider cannot"):
        SettlementPolicy(minimum_agreeing_providers=1)


def test_the_wrong_chain_is_ambiguous() -> None:
    readings = (reading("a", chain_id=1), reading("b"), reading("c"))
    decision = verify(target(), readings, (), POLICY)
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.WRONG_CHAIN in decision.reasons


def test_a_reading_for_another_condition_is_ambiguous() -> None:
    readings = (reading("a", condition_id="0x" + "11" * 32), reading("b"), reading("c"))
    decision = verify(target(), readings, (), POLICY)
    assert AmbiguityReason.CONDITION_MISMATCH in decision.reasons


def test_an_unexpected_slot_count_is_ambiguous() -> None:
    decision = verify(target(), three(numerators=(1, 0, 0), slots=3), (), POLICY)
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.UNEXPECTED_SLOT_COUNT in decision.reasons


def test_a_fractional_payout_is_preserved_not_forced() -> None:
    """A tie is valid CTF state. This strategy has no economics for it, which is not the same
    as it being invalid, so the vector survives into the decision."""
    decision = verify(target(), three(numerators=(1, 1), denominator=2), (), POLICY)
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.NON_BINARY_PAYOUT in decision.reasons
    assert decision.payout == PayoutVector(2, (1, 1), 2)
    assert decision.winning_outcome is None


def test_a_partial_payout_is_ambiguous() -> None:
    decision = verify(target(), three(numerators=(3, 1), denominator=4), (), POLICY)
    assert decision.state is ResolutionState.AMBIGUOUS
    assert decision.payout is not None and decision.payout.numerators == (3, 1)


def test_advisory_disagreement_is_ambiguous() -> None:
    advisory = (AdvisoryResolution("gamma", available=True, winning_slot=1),)
    decision = verify(target(), three(numerators=(1, 0)), advisory, POLICY)
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.ADVISORY_DISAGREEMENT in decision.reasons
    assert not decision.redeemable


def test_a_negrisk_market_fails_closed() -> None:
    decision = verify(target(neg_risk=True), three(numerators=(1, 0)), (), POLICY)
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.UNSUPPORTED_MARKET_STRUCTURE in decision.reasons


def test_a_slot_that_maps_to_neither_outcome_is_ambiguous() -> None:
    decision = verify(target(up_slot=5, down_slot=6), three(numerators=(1, 0)), (), POLICY)
    assert AmbiguityReason.OUTCOME_MAPPING_MISMATCH in decision.reasons


def test_swapped_slots_resolve_to_the_other_outcome_not_silently_to_up() -> None:
    """If a market ever declared the reverse mapping, the verifier must follow the metadata."""
    decision = verify(target(up_slot=1, down_slot=0), three(numerators=(1, 0)), (), POLICY)
    assert decision.state is ResolutionState.RESOLVED
    assert decision.winning_outcome is Outcome.DOWN


# -- purity ------------------------------------------------------------------------------------


def test_verification_is_pure_and_repeatable() -> None:
    readings = three(numerators=(0, 1))
    advisory = (AdvisoryResolution("gamma", available=True, winning_slot=1),)
    first = verify(target(), readings, advisory, POLICY)
    for _ in range(20):
        assert verify(target(), readings, advisory, POLICY).summary() == first.summary()


def test_the_verifier_reads_no_clock_or_network() -> None:
    import ast
    from pathlib import Path

    import maker5m

    source = (Path(maker5m.__file__).parent / "settlement" / "resolution.py").read_text("utf-8")
    tree = ast.parse(source)
    banned = {"time", "datetime", "random", "urllib", "socket", "http", "requests", "os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned


# -- absence of evidence is not contrary evidence ---------------------------------------------
#
# Motivated by real data, not by tidiness. Across 15 live settlements one provider repeatedly
# failed to serve a payout it demonstrably had at that block — twice at a block *ahead* of a
# provider that had it — and the verifier called that a chain contradiction and halted trading.
# See docs/evidence/P10-SETTLEMENT-REAL-MARKET.md.

UNSETTLED = (0, 0)


def test_a_quorum_of_agreeing_providers_resolves_without_unanimity() -> None:
    decision = verify(
        target(),
        (
            reading("publicnode", (1, 0)),
            reading("drpc", (1, 0)),
            reading("quiknode", (1, 0)),
            reading("slow", UNSETTLED, denominator=0),
        ),
        (),
        POLICY,
    )
    assert decision.state is ResolutionState.RESOLVED
    assert decision.winning_outcome is Outcome.UP
    assert "slow" not in decision.agreeing_providers


def test_below_the_quorum_it_waits_and_names_who_is_missing() -> None:
    decision = verify(
        target(),
        (
            reading("publicnode", (1, 0)),
            reading("drpc", (1, 0)),
            reading("quiknode", UNSETTLED, denominator=0),
        ),
        (),
        POLICY,
    )
    assert decision.state is ResolutionState.UNRESOLVED
    assert not decision.reasons
    assert "2 of 3" in decision.detail
    assert "quiknode" in decision.detail


def test_a_contradicting_payout_is_still_ambiguous_even_at_quorum() -> None:
    """Waiting is for absence. A provider that says something else is a different matter."""
    decision = verify(
        target(),
        (
            reading("publicnode", (1, 0)),
            reading("drpc", (1, 0)),
            reading("quiknode", (0, 1)),
        ),
        (),
        POLICY,
    )
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.PROVIDER_DISAGREEMENT in decision.reasons


def test_contradiction_is_checked_before_quorum_is_counted() -> None:
    """Two providers disagreeing must not be reported as merely 'waiting for a third'."""
    decision = verify(
        target(),
        (
            reading("publicnode", (1, 0)),
            reading("drpc", (0, 1)),
            reading("quiknode", UNSETTLED, denominator=0),
        ),
        (),
        POLICY,
    )
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.PROVIDER_DISAGREEMENT in decision.reasons


def test_the_strict_unanimous_reading_remains_available() -> None:
    strict = SettlementPolicy(require_unanimous_resolution=True)
    decision = verify(
        target(),
        (
            reading("publicnode", (1, 0)),
            reading("drpc", (1, 0)),
            reading("quiknode", (1, 0)),
            reading("slow", UNSETTLED, denominator=0),
        ),
        (),
        strict,
    )
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.FINALITY_DISAGREEMENT in decision.reasons
    assert "slow" in decision.detail


def test_waiting_for_a_provider_does_not_halt_execution() -> None:
    """The whole point of the correction: no risk signal, so no halt."""
    from maker5m.settlement import resolution_safety_signal

    decision = verify(
        target(),
        (
            reading("publicnode", (1, 0)),
            reading("drpc", (1, 0)),
            reading("quiknode", UNSETTLED, denominator=0),
        ),
        (),
        POLICY,
    )
    assert decision.state is ResolutionState.UNRESOLVED
    assert (
        resolution_safety_signal(decision, as_of_ingress_ordinal=1, now_ns=TimestampNs(1)) is None
    )
