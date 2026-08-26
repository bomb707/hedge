"""The settlement trust boundary: who may count, and on what evidence.

**SUPPORTING UNIT TEST ONLY.** Nothing here is market evidence. These are software-correctness
tests for the rules that decide whose answers form a quorum — a trust boundary, so it is checked
by construction rather than by observing that it has not yet been crossed.

The fake transport is a fake on purpose: to prove a *refusal*, the call must be one that never
happens, and a real endpoint cannot demonstrate a request it did not receive.
"""

from __future__ import annotations

from typing import Any

import pytest

from maker5m.settlement import (
    AmbiguityReason,
    AttestationBindingError,
    AttestedProvider,
    DuplicateEndpointError,
    EndpointSet,
    MarketResolutionTarget,
    PayoutVector,
    ProviderAttestation,
    ProviderResolution,
    ResolutionState,
    RpcEndpoint,
    SettlementPolicy,
    endpoint_fingerprint,
    verify,
)
from maker5m.settlement.reader import CtfReader, ProviderIdentity

CONDITION = "0x38cd5ae6f1edc9b5256aec2104570c7b65edcf523a923c427cf7174f5c81ad8a"
POLICY = SettlementPolicy()
TARGET = MarketResolutionTarget(
    slug="btc-updown-5m-1787678100",
    condition_id=CONDITION,
    up_token_id="110812484999043231239085940561002422009675005555903980849640432881019677328146",
    down_token_id="102646035968219316183082673461659256686129432733854586945044223308275343208922",
)


def attestation(provider: str, *, url: str = "", **overrides: Any) -> ProviderAttestation:
    base: dict[str, Any] = {
        "provider_id": provider,
        "endpoint_fingerprint": url or f"https://{provider}.example/rpc",
        "chain_id": 137,
        "ctf_code_bytes": 15_007,
        "collateral_code_bytes": 61,
        "collateral_decimals": 6,
        "attested_at_block": 92_665_370,
    }
    base.update(overrides)
    return ProviderAttestation(**base)


def reading(
    provider: str,
    numerators: tuple[int, ...] = (1, 0),
    *,
    denominator: int = 1,
    block: int | None = 92_665_372,
    block_tag: str = "finalized",
    url: str = "",
    attested: ProviderAttestation | None = None,
    source: str | None = None,
) -> ProviderResolution:
    """A correctly bound reading unless `attested` or `source` is overridden to break it."""
    fingerprint = url or f"https://{provider}.example/rpc"
    return ProviderResolution(
        provider_id=provider,
        chain_id=137,
        block_tag=block_tag,
        block_number=block,
        condition_id=CONDITION,
        payout=PayoutVector(denominator=denominator, numerators=numerators, outcome_slot_count=2),
        source_endpoint_fingerprint=fingerprint if source is None else source,
        attestation=attestation(provider, url=url) if attested is None else attested,
    )


# -- §15 one provider can never become three --------------------------------------------------


def test_three_distinct_providers_resolve() -> None:
    """The control. Everything below differs from this in one way only."""
    decision = verify(TARGET, (reading("a"), reading("b"), reading("c")), (), POLICY)
    assert decision.state is ResolutionState.RESOLVED


def test_one_provider_repeated_three_times_does_not_resolve() -> None:
    decision = verify(TARGET, (reading("a"), reading("a"), reading("a")), (), POLICY)
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.DUPLICATE_PROVIDER_ID in decision.reasons
    assert decision.winning_outcome is None


def test_two_providers_where_one_speaks_twice_is_not_three() -> None:
    decision = verify(TARGET, (reading("a"), reading("a"), reading("b")), (), POLICY)
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.DUPLICATE_PROVIDER_ID in decision.reasons


def test_a_repeated_provider_contradicting_itself_is_still_refused() -> None:
    """And refused as malformed evidence, not as a disagreement between two providers."""
    decision = verify(
        TARGET, (reading("a", (1, 0)), reading("a", (0, 1)), reading("b")), (), POLICY
    )
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.DUPLICATE_PROVIDER_ID in decision.reasons


def test_duplication_is_refused_before_the_quorum_is_ever_counted() -> None:
    """Otherwise a quorum of one, repeated, would authorise a redemption."""
    single = SettlementPolicy(minimum_agreeing_providers=2)
    decision = verify(TARGET, (reading("a"), reading("a")), (), single)
    assert decision.state is not ResolutionState.RESOLVED
    assert AmbiguityReason.DUPLICATE_PROVIDER_ID in decision.reasons


def test_two_names_for_one_endpoint_are_not_two_providers() -> None:
    shared = "https://one.example/rpc"
    decision = verify(
        TARGET,
        (reading("a", url=shared), reading("b", url=shared), reading("c")),
        (),
        POLICY,
    )
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.DUPLICATE_PROVIDER_ID in decision.reasons
    assert "one.example" in decision.detail


# -- §15 E/F configuration-time rejection ------------------------------------------------------


def test_a_clean_endpoint_set_is_accepted() -> None:
    assert (
        len(
            EndpointSet(
                (RpcEndpoint("a", "https://a.example"), RpcEndpoint("b", "https://b.example"))
            )
        )
        == 2
    )


def test_duplicate_provider_id_is_rejected_at_configuration_time() -> None:
    with pytest.raises(DuplicateEndpointError, match="more than once"):
        EndpointSet(
            (RpcEndpoint("a", "https://one.example"), RpcEndpoint("a", "https://two.example"))
        )


def test_duplicate_endpoint_url_is_rejected_at_configuration_time() -> None:
    with pytest.raises(DuplicateEndpointError, match="URL configured more than once"):
        EndpointSet(
            (RpcEndpoint("a", "https://one.example"), RpcEndpoint("b", "https://one.example"))
        )


@pytest.mark.parametrize(
    "second",
    [
        "https://one.example/",
        "HTTPS://ONE.EXAMPLE",
        "https://One.Example/?",
    ],
)
def test_the_same_url_written_differently_is_still_the_same_url(second: str) -> None:
    with pytest.raises(DuplicateEndpointError):
        EndpointSet((RpcEndpoint("a", "https://one.example"), RpcEndpoint("b", second)))


def test_the_fingerprint_does_not_claim_to_prove_organisational_independence() -> None:
    """Two different URLs are treated as different, whoever actually runs them."""
    assert endpoint_fingerprint("https://a.example") != endpoint_fingerprint("https://b.example")


def test_an_empty_endpoint_set_is_rejected() -> None:
    with pytest.raises(DuplicateEndpointError):
        EndpointSet(())


# -- §16 identity attestation is load-bearing --------------------------------------------------


def identity(**overrides: Any) -> ProviderIdentity:
    base: dict[str, Any] = {
        "provider_id": "a",
        "endpoint_fingerprint": "https://a.example/rpc",
        "chain_id": 137,
        "ctf_code_bytes": 15_007,
        "collateral_code_bytes": 61,
        "collateral_decimals": 6,
        "finalized_block": 92_665_370,
    }
    base.update(overrides)
    return ProviderIdentity(**base)


def test_a_complete_identity_is_trusted() -> None:
    assert identity().trustworthy


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chain_id", 1),
        ("chain_id", None),
        ("ctf_code_bytes", 0),
        ("collateral_code_bytes", 0),
        ("collateral_decimals", 18),
        ("collateral_decimals", None),
        ("error", "RpcError: usage limit"),
    ],
)
def test_each_identity_failure_alone_withholds_trust(field: str, value: Any) -> None:
    assert not identity(**{field: value}).trustworthy


def test_an_unattested_provider_cannot_count_however_valid_its_answer_looks() -> None:
    unattested = reading("c")._replace(attestation=None)
    decision = verify(TARGET, (reading("a"), reading("b"), unattested), (), POLICY)
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.PROVIDER_NOT_ATTESTED in decision.reasons
    assert "c" in decision.detail


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chain_id", 1),
        ("ctf_code_bytes", 0),
        ("collateral_code_bytes", 0),
        ("collateral_decimals", 18),
        ("error", "RpcError: usage limit"),
    ],
)
def test_a_failed_attestation_cannot_count_either(field: str, value: Any) -> None:
    failed = reading("c", attested=attestation("c", **{field: value}))
    decision = verify(TARGET, (reading("a"), reading("b"), failed), (), POLICY)
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.PROVIDER_NOT_ATTESTED in decision.reasons


def test_two_attested_providers_cannot_form_a_quorum_of_three() -> None:
    """The excluded third is absent, so this is insufficient evidence — not a disagreement."""
    decision = verify(TARGET, (reading("a"), reading("b")), (), POLICY)
    assert decision.state is ResolutionState.INSUFFICIENT_EVIDENCE
    assert AmbiguityReason.INSUFFICIENT_QUORUM in decision.reasons


def test_three_attested_providers_may_form_a_quorum() -> None:
    assert verify(TARGET, (reading("a"), reading("b"), reading("c")), (), POLICY).redeemable


# -- §18 finality policy -----------------------------------------------------------------------


def test_readings_taken_under_a_different_finality_rule_cannot_count() -> None:
    decision = verify(
        TARGET,
        (reading("a"), reading("b", block_tag="latest"), reading("c", block_tag="latest")),
        (),
        POLICY,
    )
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.FINALITY_POLICY_MISMATCH in decision.reasons
    assert "b" in decision.detail and "c" in decision.detail


def test_a_matching_finality_rule_resolves() -> None:
    decision = verify(
        TARGET,
        (reading("a"), reading("b"), reading("c")),
        (),
        POLICY,
    )
    assert decision.state is ResolutionState.RESOLVED


def test_the_decision_records_the_policy_tag_not_whichever_reading_came_first() -> None:
    """It once took this from provider_readings[0], so ordering decided the audit record."""
    decision = verify(TARGET, (reading("a"), reading("b"), reading("c")), (), POLICY)
    assert decision.block_tag == POLICY.block_tag

    refused = verify(TARGET, (reading("a", block_tag="latest"),), (), POLICY)
    assert refused.block_tag == POLICY.block_tag


def test_a_policy_without_a_finality_rule_is_refused() -> None:
    with pytest.raises(ValueError, match="finality policy"):
        SettlementPolicy(block_tag="")


# -- §11 a concrete authoritative block --------------------------------------------------------


def test_agreement_without_a_concrete_block_cannot_authorise_redemption() -> None:
    decision = verify(
        TARGET,
        (reading("a", block=None), reading("b", block=None), reading("c", block=None)),
        (),
        POLICY,
    )
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.MISSING_AUTHORITATIVE_BLOCK in decision.reasons


def test_one_agreeing_provider_without_a_block_is_enough_to_refuse() -> None:
    decision = verify(
        TARGET,
        (reading("a"), reading("b"), reading("c", block=None)),
        (),
        POLICY,
    )
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.MISSING_AUTHORITATIVE_BLOCK in decision.reasons


def test_a_resolved_decision_always_names_its_block() -> None:
    decision = verify(TARGET, (reading("a"), reading("b"), reading("c")), (), POLICY)
    assert decision.state is ResolutionState.RESOLVED
    assert decision.authoritative_block == 92_665_372


# -- §17 atomicity: no moving-tag fallback -----------------------------------------------------


class FakeTransport:
    """Records every JSON-RPC call and answers from a script."""

    def __init__(self, block: dict[str, str] | None) -> None:
        self.calls: list[tuple[str, list[Any]]] = []
        self.block = block

    def __call__(self, method: str, params: list[Any]) -> Any:
        self.calls.append((method, params))
        if method == "eth_chainId":
            return "0x89"
        if method == "eth_getBlockByNumber":
            return self.block
        if method == "eth_getCode":
            return "0x" + "60" * 32
        if method == "eth_call":
            data = params[0]["data"]
            if data.startswith("0xd42dc0c2"):  # getOutcomeSlotCount
                return "0x" + "0" * 63 + "2"
            return "0x" + "0" * 63 + "1"
        raise AssertionError(f"unexpected method {method}")

    @property
    def block_args(self) -> list[str]:
        return [params[1] for method, params in self.calls if method == "eth_call"]


class ScriptedReader(CtfReader):
    """The production reader with only its transport replaced.

    Subclassed rather than patched so the logic under test is the shipped logic: every decision
    about ordering, pinning, and error handling still comes from `CtfReader.read_condition`.
    """

    transport: FakeTransport

    def _rpc(self, method: str, params: list[Any]) -> Any:
        return self.transport(method, params)


def fake_reader(
    block: dict[str, str] | None, *, endpoint: RpcEndpoint | None = None
) -> tuple[ScriptedReader, FakeTransport]:
    reader = ScriptedReader(endpoint or RpcEndpoint("fake", "https://fake.example"))
    transport = FakeTransport(block)
    reader.transport = transport
    return reader, transport


def test_no_payout_call_happens_when_the_finalized_block_is_unavailable() -> None:
    reader, transport = fake_reader(None)
    result = reader.read_condition(CONDITION, block_tag="finalized")

    assert result.error is not None
    assert "moving tag" in result.error
    assert not result.answered
    assert [method for method, _ in transport.calls if method == "eth_call"] == []


def test_every_payout_call_is_pinned_to_the_concrete_block() -> None:
    reader, transport = fake_reader({"number": hex(92_665_372)})
    result = reader.read_condition(CONDITION, block_tag="finalized")

    assert result.block_number == 92_665_372
    assert transport.block_args, "the payout calls must have happened"
    assert set(transport.block_args) == {hex(92_665_372)}
    assert "finalized" not in transport.block_args
    assert "latest" not in transport.block_args


def test_a_reading_gathered_without_an_attestation_is_not_eligible() -> None:
    """Gathering is not trusting: the reader may read, and the verifier still refuses."""
    reader, _ = fake_reader({"number": hex(92_665_372)})
    result = reader.read_condition(CONDITION, block_tag="finalized")
    assert result.answered
    assert not result.attested


# -- P10C: the proof must describe the thing it is attached to ---------------------------------
#
# SUPPORTING UNIT TEST ONLY. Independent review found that an attestation was validated for
# internal consistency and never compared against the provider or endpoint that produced the
# reading, so a proof obtained for endpoint A could be attached to a reading from endpoint B.
#
# These are software-refusal paths. Nothing here claims a Python value is unforgeable — the
# claim is narrower: a mismatched one fails closed at every layer that could act on it.


def endpoint(name: str) -> RpcEndpoint:
    return RpcEndpoint(name, f"https://{name}.example/rpc")


def identity_for(name: str, **overrides: Any) -> ProviderIdentity:
    base: dict[str, Any] = {
        "provider_id": name,
        "endpoint_fingerprint": endpoint(name).fingerprint,
        "chain_id": 137,
        "ctf_code_bytes": 15_007,
        "collateral_code_bytes": 61,
        "collateral_decimals": 6,
        "finalized_block": 92_665_370,
    }
    base.update(overrides)
    return ProviderIdentity(**base)


# A. correctly bound


def test_an_identity_bound_to_its_own_endpoint_is_accepted() -> None:
    provider = AttestedProvider(endpoint=endpoint("a"), identity=identity_for("a"))
    assert provider.provider_id == "a"
    assert provider.identity.endpoint_fingerprint == endpoint("a").fingerprint


def test_identify_records_the_endpoint_it_actually_identified() -> None:
    reader, _ = fake_reader({"number": hex(92_665_372)}, endpoint=endpoint("a"))
    result = reader.identify()
    assert result.provider_id == "a"
    assert result.endpoint_fingerprint == endpoint("a").fingerprint


def test_an_attestation_carries_the_endpoint_without_being_told_it() -> None:
    """`to_attestation()` takes no endpoint, so there is no argument to re-point."""
    proof = identity_for("a").to_attestation()
    assert proof.endpoint_fingerprint == endpoint("a").fingerprint
    assert not hasattr(ProviderIdentity, "attestation")


# B, C, D. rejected bindings


def test_an_identity_for_another_provider_cannot_be_attached() -> None:
    with pytest.raises(AttestationBindingError, match="describes one endpoint only"):
        AttestedProvider(endpoint=endpoint("b"), identity=identity_for("a"))


def test_an_identity_obtained_from_another_endpoint_cannot_be_attached() -> None:
    """The exact bypass independent review found: same name, different endpoint."""
    borrowed = identity_for("b", endpoint_fingerprint=endpoint("a").fingerprint)
    with pytest.raises(AttestationBindingError, match="but is being attached to"):
        AttestedProvider(endpoint=endpoint("b"), identity=borrowed)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chain_id", 1),
        ("ctf_code_bytes", 0),
        ("collateral_code_bytes", 0),
        ("collateral_decimals", 18),
        ("error", "RpcError: usage limit"),
    ],
)
def test_an_untrustworthy_identity_cannot_become_an_attested_provider(
    field: str, value: Any
) -> None:
    with pytest.raises(AttestationBindingError):
        AttestedProvider(endpoint=endpoint("a"), identity=identity_for("a", **{field: value}))


# E, F. the pure verifier re-checks, on readings it never saw created


def test_a_reading_carrying_another_providers_proof_is_refused() -> None:
    forged = reading("b", attested=attestation("a", url=endpoint("a").fingerprint))
    decision = verify(TARGET, (reading("c"), reading("d"), forged), (), POLICY)
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.ATTESTATION_BINDING_MISMATCH in decision.reasons
    assert "carries a proof for 'a'" in decision.detail


def test_a_reading_from_another_endpoint_than_its_proof_is_refused() -> None:
    forged = reading("b", source=endpoint("a").fingerprint)
    decision = verify(TARGET, (reading("c"), reading("d"), forged), (), POLICY)
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.ATTESTATION_BINDING_MISMATCH in decision.reasons
    assert "the proof describes" in decision.detail


def test_a_reading_that_records_no_source_at_all_is_refused() -> None:
    """A proof cannot supply both sides of its own comparison."""
    decision = verify(TARGET, (reading("a"), reading("b"), reading("c", source="")), (), POLICY)
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.ATTESTATION_BINDING_MISMATCH in decision.reasons


def test_a_binding_mismatch_is_not_reported_as_a_missing_proof() -> None:
    """The two say different things to an auditor and are kept apart."""
    forged = reading("b", attested=attestation("a"))
    misbound = verify(TARGET, (reading("c"), reading("d"), forged), (), POLICY)
    missing = verify(
        TARGET,
        (reading("c"), reading("d"), reading("b")._replace(attestation=None)),
        (),
        POLICY,
    )
    assert misbound.reasons == (AmbiguityReason.ATTESTATION_BINDING_MISMATCH,)
    assert missing.reasons == (AmbiguityReason.PROVIDER_NOT_ATTESTED,)


# G. the reader refuses a foreign proof before spending any network call


def test_a_reader_refuses_an_attestation_belonging_to_another_provider() -> None:
    reader, transport = fake_reader({"number": hex(92_665_372)}, endpoint=endpoint("b"))
    with pytest.raises(AttestationBindingError, match="offered to reader"):
        reader.read_condition(
            CONDITION, block_tag="finalized", attestation=identity_for("a").to_attestation()
        )
    assert transport.calls == [], "refused before any request was sent"


def test_a_reader_refuses_an_attestation_describing_another_endpoint() -> None:
    reader, transport = fake_reader({"number": hex(92_665_372)}, endpoint=endpoint("b"))
    borrowed = identity_for("b", endpoint_fingerprint=endpoint("a").fingerprint).to_attestation()
    with pytest.raises(AttestationBindingError, match="attestation describes"):
        reader.read_condition(CONDITION, block_tag="finalized", attestation=borrowed)
    assert transport.calls == []


def test_a_reader_records_its_own_endpoint_as_the_source() -> None:
    reader, _ = fake_reader({"number": hex(92_665_372)}, endpoint=endpoint("a"))
    result = reader.read_condition(
        CONDITION, block_tag="finalized", attestation=identity_for("a").to_attestation()
    )
    assert result.source_endpoint_fingerprint == endpoint("a").fingerprint
    assert result.bound
    assert result.attested


def test_a_diagnostic_read_without_a_proof_still_works_and_still_does_not_count() -> None:
    reader, _ = fake_reader({"number": hex(92_665_372)}, endpoint=endpoint("a"))
    result = reader.read_condition(CONDITION, block_tag="finalized")
    assert result.answered
    assert result.source_endpoint_fingerprint == endpoint("a").fingerprint
    assert not result.attested


# H, I, J. what a quorum can and cannot be made of


def test_three_correctly_bound_providers_resolve() -> None:
    decision = verify(TARGET, (reading("a"), reading("b"), reading("c")), (), POLICY)
    assert decision.state is ResolutionState.RESOLVED
    assert decision.redeemable


def test_two_correct_plus_one_foreign_proof_cannot_make_a_quorum_of_three() -> None:
    forged = reading("c", attested=attestation("a", url=endpoint("a").fingerprint))
    decision = verify(TARGET, (reading("a"), reading("b"), forged), (), POLICY)
    assert decision.state is not ResolutionState.RESOLVED
    assert decision.winning_outcome is None
    assert AmbiguityReason.ATTESTATION_BINDING_MISMATCH in decision.reasons


def test_a_hand_built_apparently_valid_foreign_attestation_cannot_satisfy_quorum() -> None:
    """It is a NamedTuple, so it can be built. It still cannot count."""
    fabricated = ProviderAttestation(
        provider_id="a",
        endpoint_fingerprint=endpoint("a").fingerprint,
        chain_id=137,
        ctf_code_bytes=15_007,
        collateral_code_bytes=61,
        collateral_decimals=6,
    )
    assert fabricated.valid, "internally consistent, and that was always the problem"

    smuggled = reading("c", attested=fabricated)
    decision = verify(TARGET, (reading("a"), reading("b"), smuggled), (), POLICY)
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.ATTESTATION_BINDING_MISMATCH in decision.reasons


def test_both_layers_refuse_the_same_bypass() -> None:
    """§11: the constructor stops it, and the verifier stops it again if the constructor is
    bypassed by building the value directly."""
    identity_a = identity_for("a")

    with pytest.raises(AttestationBindingError):
        AttestedProvider(endpoint=endpoint("b"), identity=identity_a)

    hand_built = ProviderResolution(
        provider_id="b",
        chain_id=137,
        block_tag="finalized",
        block_number=92_665_372,
        condition_id=CONDITION,
        payout=PayoutVector(denominator=1, numerators=(1, 0), outcome_slot_count=2),
        source_endpoint_fingerprint=endpoint("b").fingerprint,
        attestation=identity_a.to_attestation(),
    )
    assert hand_built.answered, "it looks like a perfectly good reading"
    assert not hand_built.bound
    assert not hand_built.attested

    decision = verify(TARGET, (reading("c"), reading("d"), hand_built), (), POLICY)
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.ATTESTATION_BINDING_MISMATCH in decision.reasons
