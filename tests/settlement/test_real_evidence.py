"""The production verifier against real committed evidence.

**REAL MARKET DATA.** Every fixture here is a captured recording of real Polymarket markets and
real Polygon chain state — `docs/evidence/p10a-o11-historical.json` and its siblings — not a
constructed one. Where the original fields exist they are consumed as they were recorded rather
than simplified into a tidier shape, because the shape is part of what is being tested.

These are still automated tests and not the real-market gate: they prove the production code
reads real evidence correctly. The gate itself is the live runs recorded in
`docs/evidence/P10-SETTLEMENT-REAL-MARKET.md`.
"""

from __future__ import annotations

import json
from collections import Counter
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from maker5m.domain import Outcome
from maker5m.settlement import (
    CTF_ADDRESS,
    PUSD_ADDRESS,
    AmbiguityReason,
    MarketResolutionTarget,
    RedeemBlocker,
    ResolutionState,
    SettlementPolicy,
    binary_index_sets,
    verify,
)

EVIDENCE = Path(__file__).resolve().parents[2] / "docs" / "evidence"
HISTORICAL = json.loads((EVIDENCE / "p10a-o11-historical.json").read_text("utf-8"))
TIMING = json.loads((EVIDENCE / "p10a-o11-live-timing.json").read_text("utf-8"))
ETHCALL = json.loads((EVIDENCE / "p10-real-ethcall.json").read_text("utf-8"))
REPLAY = json.loads((EVIDENCE / "p10-production-verifier-p10a55.json").read_text("utf-8"))

RESOLVER = "0x58e1745bedda7312c4cddb72618923da1b90efde"
CHAINLINK_STREAM = "https://data.chain.link/streams/btc-usd-twap-60s-streams"


def markets() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = HISTORICAL["markets"]
    return rows


CORPUS_POLICY = SettlementPolicy(block_tag="captured-latest")
"""The finality rule the P10A corpus was actually captured under. Naming it here rather than
replaying under `finalized` keeps the evidence honest: this corpus is not finalized evidence."""


def committed_attestations() -> dict[str, Any]:
    """The real endpoint attestations recorded when the replay was committed.

    Read from evidence rather than constructed, and deliberately not backdated: the corpus
    predates the attestation boundary, so these attest the endpoints as of the replay. Tests stay
    offline by consuming what that run recorded.
    """
    from maker5m.settlement import ProviderAttestation

    stored = REPLAY["attestation"]["attestations"]
    return {
        name: ProviderAttestation(
            provider_id=row["provider_id"],
            endpoint_fingerprint=row["endpoint_fingerprint"],
            chain_id=row["chain_id"],
            ctf_code_bytes=row["ctf_code_bytes"],
            collateral_code_bytes=row["collateral_code_bytes"],
            collateral_decimals=row["collateral_decimals"],
            attested_at_block=row["attested_at_block"],
            error=row["error"],
        )
        for name, row in stored.items()
    }


def build(
    market: dict[str, Any],
) -> tuple[MarketResolutionTarget, tuple[Any, ...], tuple[Any, ...]]:
    from tools.p10_replay_corpus import advisory_from, readings_from, target_from

    return (
        target_from(market),
        readings_from(market, committed_attestations()),
        advisory_from(market),
    )


# -- the corpus is what we think it is -------------------------------------------------------


def test_the_corpus_is_real_and_non_trivial() -> None:
    assert HISTORICAL["provenance"] == "REAL_PUBLIC_MARKET_DATA"
    assert len(markets()) == 55
    spacing = sorted({b["t0"] - a["t0"] for a, b in pairwise(markets())})
    assert spacing == [-300], "the corpus must be consecutive 5-minute markets"


def test_every_market_names_the_chainlink_rule_source() -> None:
    sources = {market["gamma"]["resolution_source"] for market in markets()}
    assert sources == {CHAINLINK_STREAM}


def test_every_market_was_resolved_by_the_same_specialised_resolver() -> None:
    """Not the UMA adapter. Recorded from real ConditionResolution events."""
    oracles = {market["resolution_log"]["oracle"] for market in markets()}
    assert oracles == {RESOLVER}


def test_no_market_in_the_corpus_is_negrisk() -> None:
    assert {market["gamma"]["neg_risk"] for market in markets()} == {False}


# -- the production verifier over real readings ------------------------------------------------


def test_the_production_verifier_resolves_every_real_market() -> None:
    policy = SettlementPolicy(minimum_agreeing_providers=3, block_tag="captured-latest")
    states: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    for market in markets():
        target, readings, advisory = build(market)
        decision = verify(target, readings, advisory, policy)
        states[decision.state.value] += 1
        if decision.winning_outcome:
            outcomes[decision.winning_outcome.value] += 1
    assert states == Counter({"RESOLVED": 55}), dict(states)
    assert outcomes == Counter({"UP": 27, "DOWN": 28}), dict(outcomes)


def test_the_verifier_agrees_with_the_stored_payout_vector_market_by_market() -> None:
    """Checked against the recorded chain state, not against the verifier's own answer."""
    policy = SettlementPolicy(minimum_agreeing_providers=3, block_tag="captured-latest")
    for market in markets():
        target, readings, advisory = build(market)
        decision = verify(target, readings, advisory, policy)
        stored = market["chain"]["publicnode"]["payout_numerators"]
        expected = Outcome.UP if stored == [1, 0] else Outcome.DOWN
        assert decision.winning_outcome is expected, target.slug
        assert decision.payout is not None
        assert list(decision.payout.numerators) == stored


def test_the_unattested_provider_contributes_no_reading_at_all() -> None:
    """1rpc fails identity for real — a usage-limit error — so it never enters the evidence.

    This is the production rule, not a corpus quirk: an endpoint that has not proved which chain
    and contracts it serves does not get to produce a reading that someone downstream might
    count. Its 29 rate-limited answers in this corpus are gone for the same reason its 26 good
    ones are.
    """
    rejected = {row["provider_id"] for row in REPLAY["attestation"]["rejected_at_replay"]}
    assert "1rpc" in rejected
    assert "1rpc" not in REPLAY["attestation"]["attested_at_replay"]

    raw = {provider for market in markets() for provider in market["chain"]}
    assert "1rpc" in raw, "the corpus really does contain its readings"

    for market in markets():
        _, readings, _ = build(market)
        assert all(reading.provider_id != "1rpc" for reading in readings)
        assert all(reading.attested for reading in readings)


def test_real_provider_errors_are_never_counted_as_agreement() -> None:
    """An attested provider that fails one read is absent for that read, not concurring."""
    policy = SettlementPolicy(minimum_agreeing_providers=2, block_tag="captured-latest")
    market = markets()[0]
    target, readings, advisory = build(market)
    assert len(readings) == 3

    broken = (readings[0]._replace(payout=None, error="RpcError: timeout"), *readings[1:])
    assert not broken[0].answered

    decision = verify(target, broken, advisory, policy)
    assert decision.state is ResolutionState.RESOLVED, "two attested providers still agree"
    assert broken[0].provider_id not in decision.answering_providers
    assert broken[0].provider_id not in decision.agreeing_providers


def test_a_quorum_higher_than_the_attested_providers_can_never_be_met() -> None:
    """Three endpoints pass identity, so demanding four refuses every market, not some of them.

    Before the trust boundary this returned 26 RESOLVED and 29 INSUFFICIENT, because a fourth
    provider that had merely *answered* could make up the number. It cannot any more.
    """
    policy = SettlementPolicy(minimum_agreeing_providers=4, block_tag="captured-latest")
    states = Counter(verify(*build(market), policy).state.value for market in markets())
    assert states == Counter({"INSUFFICIENT_EVIDENCE": 55}), dict(states)


def test_corpus_readings_are_refused_by_a_finalized_policy() -> None:
    """The corpus was captured at `latest`. It must not be able to pass as finalized evidence."""
    decision = verify(*build(markets()[0]), SettlementPolicy())
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.FINALITY_POLICY_MISMATCH in decision.reasons


# -- outcome mapping, from the real corpus ------------------------------------------------------


def test_slot_zero_is_up_and_slot_one_is_down_across_the_real_corpus() -> None:
    labels: Counter[str] = Counter()
    for market in markets():
        outcomes = market["gamma"]["outcomes"]
        stored = market["chain"]["publicnode"]["payout_numerators"]
        slot = stored.index(1)
        labels[f"slot{slot}={outcomes[slot]}"] += 1
    assert labels == Counter({"slot0=Up": 27, "slot1=Down": 28}), dict(labels)


# -- the recorded live timing --------------------------------------------------------------------


def test_the_real_timing_corpus_is_consecutive_and_chain_first() -> None:
    watches = TIMING["watches"]
    assert len(watches) >= 6
    gaps = {b["t0"] - a["t0"] for a, b in pairwise(watches)}
    assert gaps == {300}
    for item in watches:
        seen = item["seconds_after_end"]
        assert "ctf_payout_denominator" in seen, item["slug"]
        assert 0 < seen["ctf_payout_denominator"] < 300


# -- the real eth_call validation ----------------------------------------------------------------


def test_the_real_contract_accepted_every_redemption_encoding() -> None:
    assert ETHCALL["kind"] == "REAL_CHAIN_ETH_CALL_SIMULATION"
    assert ETHCALL["ctf_address"] == CTF_ADDRESS
    assert ETHCALL["collateral_token"] == PUSD_ADDRESS
    assert ETHCALL["index_sets"] == list(binary_index_sets())
    assert ETHCALL["parent_collection_id"] == "0x" + "00" * 32
    assert ETHCALL["accepted"] == ETHCALL["markets"] >= 6
    assert all(row["accepted_by_contract"] for row in ETHCALL["rows"])
    shapes = {tuple(row["payout_numerators"]) for row in ETHCALL["rows"]}
    assert shapes == {(1, 0), (0, 1)}, "both winning slots must be exercised"


def test_the_eth_call_evidence_does_not_claim_a_redemption() -> None:
    """Wording matters: nobody held a position and no transaction was sent."""
    note = ETHCALL["note"].lower()
    assert "does not prove collateral moved" in note
    assert "not a redemption" in note


# -- the committed production replay --------------------------------------------------------------


def test_the_committed_replay_matches_a_fresh_one() -> None:
    assert REPLAY["markets"] == 55
    assert REPLAY["states"] == {"RESOLVED": 55}
    assert REPLAY["outcomes"] == {"DOWN": 28, "UP": 27}
    assert REPLAY["mismatches"] == []


@pytest.mark.parametrize("index", [0, 17, 42, 54])
def test_individual_replay_rows_reproduce(index: int) -> None:
    row = REPLAY["rows"][index]
    market = next(m for m in markets() if m["gamma"]["slug"] == row["slug"])
    decision = verify(
        *build(market), SettlementPolicy(minimum_agreeing_providers=3, block_tag="captured-latest")
    )
    assert decision.state.value == row["state"]
    assert (None if decision.winning_outcome is None else decision.winning_outcome.value) == row[
        "winning_outcome"
    ]


# -- controlled local corruption of REAL readings -------------------------------------------
#
# The market and chain data below are the genuine recorded ones; only our COPY is corrupted.
# That is what CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET means, and none of it is a claim that
# Polygon or Polymarket ever disagreed with themselves — in 55 real markets, they did not.


def test_corrupting_one_real_provider_reading_fails_closed() -> None:
    from maker5m.settlement import PayoutVector, Redeemer, SettlementPreconditions

    policy = SettlementPolicy(minimum_agreeing_providers=3, block_tag="captured-latest")
    market = markets()[0]
    target, readings, advisory = build(market)

    clean = verify(target, readings, advisory, policy)
    assert clean.state is ResolutionState.RESOLVED, "the real reading must resolve first"

    corrupted = list(readings)
    for index, reading in enumerate(corrupted):
        if reading.answered and reading.payout and reading.payout.resolved:
            payout = reading.payout
            corrupted[index] = reading._replace(
                payout=PayoutVector(
                    payout.denominator,
                    tuple(reversed(payout.numerators)),
                    payout.outcome_slot_count,
                )
            )
            break

    decision = verify(target, tuple(corrupted), advisory, policy)
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.PROVIDER_DISAGREEMENT in decision.reasons
    plan, blockers = Redeemer().prepare(target, decision, SettlementPreconditions())
    assert plan is None
    assert RedeemBlocker.RESOLUTION_AMBIGUOUS in blockers


def test_corrupting_a_real_advisory_winner_fails_closed() -> None:
    from maker5m.settlement import Redeemer, SettlementPreconditions

    policy = SettlementPolicy(minimum_agreeing_providers=3, block_tag="captured-latest")
    market = markets()[1]
    target, readings, advisory = build(market)
    clean = verify(target, readings, advisory, policy)
    assert clean.state is ResolutionState.RESOLVED

    flipped = tuple(
        item._replace(winning_slot=1 - item.winning_slot)
        if item.conclusive and item.winning_slot in (0, 1)
        else item
        for item in advisory
    )
    decision = verify(target, readings, flipped, policy)
    assert decision.state is ResolutionState.AMBIGUOUS
    assert AmbiguityReason.ADVISORY_DISAGREEMENT in decision.reasons
    plan, _ = Redeemer().prepare(target, decision, SettlementPreconditions())
    assert plan is None


def test_removing_the_corruption_restores_normal_resolution() -> None:
    """The fault is ours and reversible; the underlying evidence never changed."""
    policy = SettlementPolicy(minimum_agreeing_providers=3, block_tag="captured-latest")
    for market in markets()[:5]:
        target, readings, advisory = build(market)
        assert verify(target, readings, advisory, policy).state is ResolutionState.RESOLVED


# -- the live settlement runs -----------------------------------------------------------------

LIVE_PRE = json.loads(
    (EVIDENCE / "p10-live-resolution-1787712600-1787714100.json").read_text("utf-8")
)
LIVE_POST = json.loads(
    (EVIDENCE / "p10-live-resolution-1787714700-1787716200.json").read_text("utf-8")
)
END_TO_END = json.loads(
    (EVIDENCE / "p10-end-to-end-btc-updown-5m-1787716800.json").read_text("utf-8")
)


def consecutive(run: dict[str, Any]) -> bool:
    ends = [int(watch["t0"]) for watch in run["watches"]]
    return all(later - earlier == 300 for earlier, later in pairwise(ends))


@pytest.mark.parametrize("run", [LIVE_PRE, LIVE_POST])
def test_each_live_run_is_six_consecutive_real_markets(run: dict[str, Any]) -> None:
    assert len(run["watches"]) == 6
    assert consecutive(run)
    assert all(watch["final_decision"]["state"] == "RESOLVED" for watch in run["watches"])


def test_the_two_live_runs_do_not_overlap_each_other_or_the_o11_corpus() -> None:
    """§31 wants NEW markets, so this is worth asserting rather than assuming."""
    pre = {watch["slug"] for watch in LIVE_PRE["watches"]}
    post = {watch["slug"] for watch in LIVE_POST["watches"]}
    o11 = {f"btc-updown-5m-{market['t0']}" for market in markets()}
    assert not pre & post
    assert not (pre | post) & o11


def test_the_corrected_run_never_became_ambiguous() -> None:
    for watch in LIVE_POST["watches"]:
        assert "AMBIGUOUS" not in watch["distinct_states"], watch["slug"]
        assert watch["redeem_blockers"] == []
        assert watch["redeem_plan"] is not None


def test_the_corrected_run_emitted_no_risk_signal_at_all() -> None:
    """A run with nothing wrong must cost the risk trace nothing."""
    assert all(watch["risk_records"] == [] for watch in LIVE_POST["watches"])


def test_rate_limited_providers_never_counted_toward_the_live_quorum() -> None:
    for run in (LIVE_PRE, LIVE_POST):
        absent = {
            identity["provider_id"]
            for identity in run["provider_identities"]
            if not identity["trustworthy"]
        }
        assert absent, "the run is only interesting because one provider was unusable"
        for watch in run["watches"]:
            assert not absent & set(watch["final_decision"]["agreeing_providers"])


def ambiguous_polls(run: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        poll
        for watch in run["watches"]
        for poll in watch["polls_retained"]
        if poll["state"] == "AMBIGUOUS"
    ]


def test_the_pre_correction_run_really_did_halt_on_nothing() -> None:
    """The defect is claimed from real data, so the real data has to still show it."""
    halts = ambiguous_polls(LIVE_PRE)
    assert len(halts) == 3
    assert all(poll["reasons"] == ["FINALITY_DISAGREEMENT"] for poll in halts)
    assert all(watch["final_decision"]["state"] == "RESOLVED" for watch in LIVE_PRE["watches"])


def test_those_splits_were_not_finality_lag_and_the_corrected_ones_were() -> None:
    """0 of 3 coherent before pinning the block, 3 of 3 after. Same providers, minutes apart."""

    def coherent(run: dict[str, Any]) -> list[bool]:
        out: list[bool] = []
        for watch in run["watches"]:
            for poll in watch["polls_retained"]:
                rows = [
                    row
                    for row in poll["per_provider"]
                    if row["answered"] and row["block"] is not None
                ]
                seen = [row for row in rows if row["resolved"]]
                silent = [row for row in rows if not row["resolved"]]
                if seen and silent:
                    earliest = min(row["block"] for row in seen)
                    out.append(all(row["block"] < earliest for row in silent))
        return out

    before, after = coherent(LIVE_PRE), coherent(LIVE_POST)
    assert len(before) == 3 and not any(before)
    assert len(after) == 3 and all(after)


def test_replaying_the_recorded_halts_through_the_current_verifier_clears_them() -> None:
    from maker5m.settlement import PayoutVector, ProviderResolution

    # These polls predate the attestation boundary, so they carry no proof of their own. The
    # attestations attached are the real ones recorded by the corpus replay, for the same three
    # endpoints — reused, not invented, and not backdated: what is being replayed here is the
    # quorum logic, which is what halted.
    attestations = committed_attestations()
    policy = SettlementPolicy()
    cleared = 0
    for watch in LIVE_PRE["watches"]:
        final = watch["final_decision"]["payout"]
        payout = PayoutVector(
            denominator=int(final["denominator"]),
            numerators=tuple(int(v) for v in final["numerators"]),
            outcome_slot_count=int(final["outcome_slot_count"]),
        )
        target = MarketResolutionTarget(
            slug=watch["slug"],
            condition_id=watch["condition_id"],
            up_token_id=watch["up_token_id"],
            down_token_id=watch["down_token_id"],
        )
        for poll in watch["polls_retained"]:
            if poll["state"] != "AMBIGUOUS":
                continue
            readings = tuple(
                ProviderResolution(
                    provider_id=str(row["provider"]),
                    chain_id=137 if row["answered"] else None,
                    block_tag="finalized",
                    block_number=row["block"],
                    condition_id=watch["condition_id"],
                    payout=(
                        (
                            payout
                            if row["resolved"]
                            else PayoutVector(
                                denominator=0, numerators=(0, 0), outcome_slot_count=2
                            )
                        )
                        if row["answered"]
                        else None
                    ),
                    error=None if row["answered"] else "did not answer",
                    source_endpoint_fingerprint=(
                        attestations[str(row["provider"])].endpoint_fingerprint
                    ),
                    attestation=attestations.get(str(row["provider"])),
                )
                # 1rpc never passed identity, so production would not have created a reading
                # from it at all. Filtering here is that rule, not a convenience.
                for row in poll["per_provider"]
                if str(row["provider"]) in attestations
            )
            decision = verify(target, readings, (), policy)
            assert decision.state is ResolutionState.UNRESOLVED, poll
            assert not decision.reasons
            cleared += 1
    assert cleared == 3


def test_the_end_to_end_run_is_a_real_lifecycle_that_placed_nothing() -> None:
    assert END_TO_END["provenance"] == "REAL_PUBLIC_MARKET_DATA"
    assert END_TO_END["phases_observed"] == ["PREARM", "QUOTE", "ENDGAME", "SETTLING", "DONE"]
    assert END_TO_END["settlement_state_trajectory"] == ["UNRESOLVED", "RESOLVED"]
    assert END_TO_END["cycles"] > 10_000
    assert END_TO_END["feed_counters"]["clob_messages"] > 10_000
    assert END_TO_END["feed_counters"]["malformed"] == 0
    assert END_TO_END["orders_sent"] == 0
    assert END_TO_END["redemptions_sent"] == 0
    assert END_TO_END["live_trading_enabled"] is False


def test_the_end_to_end_run_does_not_dress_up_a_zero() -> None:
    """Two zeros agreeing is not an economic result, and the record must say so."""
    reconciliation = END_TO_END["reconciliation"]
    assert reconciliation["matches_to_the_last_money_unit"] is True
    assert reconciliation["paper_settlement_pnl"] == 0
    assert reconciliation["ledger_pnl_if_winner"] == 0
    limitation = END_TO_END["limitation"]
    assert "UNRUN" in limitation and "P14" in limitation
    assert "nothing to settle" in limitation


# -- the trust-boundary run -------------------------------------------------------------------

TRUST = json.loads((EVIDENCE / "p10-trust-boundary-1787733300-1787735100.json").read_text("utf-8"))


def test_the_trust_boundary_run_is_seven_consecutive_new_real_markets() -> None:
    watches = TRUST["watches"]
    assert len(watches) == 7
    assert consecutive(TRUST)

    newest_before = max(
        int(watch["t0"]) for run in (LIVE_PRE, LIVE_POST) for watch in run["watches"]
    )
    assert min(int(watch["t0"]) for watch in watches) > newest_before


def test_every_resolved_market_used_three_distinct_trusted_providers() -> None:
    attested = set(TRUST["attested_providers"])
    assert len(attested) == 3
    for watch in TRUST["watches"]:
        final = watch["final_decision"]
        assert final["state"] == "RESOLVED"
        agreeing = final["agreeing_providers"]
        assert len(agreeing) == len(set(agreeing)) == 3, watch["slug"]
        assert set(agreeing) <= attested


def test_the_untrusted_provider_is_recorded_and_never_counted() -> None:
    untrusted = {row["provider_id"] for row in TRUST["untrusted_providers"]}
    assert untrusted == {"1rpc"}
    assert untrusted.isdisjoint(TRUST["attested_providers"])
    for watch in TRUST["watches"]:
        final = watch["final_decision"]
        assert untrusted.isdisjoint(final["agreeing_providers"])
        assert untrusted.isdisjoint(final["answering_providers"])
        for poll in watch["polls_retained"]:
            assert untrusted.isdisjoint(row["provider"] for row in poll["per_provider"])


def test_the_configured_endpoints_were_distinct_by_id_and_by_url() -> None:
    ids = TRUST["distinct_provider_ids"]
    urls = TRUST["distinct_endpoint_fingerprints"]
    assert len(ids) == len(set(ids)) == 3
    assert len(urls) == len(set(urls)) == 3


def test_every_verdict_names_a_concrete_finalized_block() -> None:
    assert TRUST["policy"]["block_tag"] == "finalized"
    for watch in TRUST["watches"]:
        final = watch["final_decision"]
        assert final["block_tag"] == "finalized"
        assert isinstance(final["authoritative_block"], int)
        assert final["authoritative_block"] > 0


def test_no_false_ambiguity_in_the_corrected_run() -> None:
    for watch in TRUST["watches"]:
        assert "AMBIGUOUS" not in watch["distinct_states"], watch["slug"]


def injected_watch() -> dict[str, Any]:
    return next(watch for watch in TRUST["watches"] if watch["injected_decision"])


def test_the_controlled_contradiction_failed_closed() -> None:
    watch = injected_watch()
    injected = watch["injected_decision"]
    assert injected["state"] == "AMBIGUOUS"
    assert injected["reasons"] == ["PROVIDER_DISAGREEMENT"]
    assert watch["redeem_plan"] is None
    assert watch["redeem_blockers"] == ["RESOLUTION_AMBIGUOUS"]
    assert watch["final_decision"]["state"] == "RESOLVED", "the real market was fine"


def test_a_clean_reread_of_the_real_chain_did_not_lift_the_halt() -> None:
    """O16 on real data: the latch is what makes the reread insufficient."""
    watch = injected_watch()
    assert watch["recovery_decision"]["state"] == "RESOLVED"

    records = watch["risk_records"]
    halt, cleared = records[0], records[1]
    assert halt["signal"] == "RESOLUTION_SAFETY_UPDATE"
    assert halt["state"] == "HALTED"
    assert halt["latched"] == ["RESOLUTION_AMBIGUOUS"]

    assert cleared["signal"] == "RESOLUTION_SAFETY_UPDATE"
    assert cleared["active"] == [], "the condition went away"
    assert cleared["latched"] == ["RESOLUTION_AMBIGUOUS"], "the halt did not"
    assert cleared["state"] == "RECOVERING"
    assert cleared["allows_place"] is False


def test_only_the_explicit_reconciliation_restored_safe() -> None:
    records = injected_watch()["risk_records"]
    confirmed = next(row for row in records if row["signal"] == "RECONCILIATION_CONFIRMED")
    assert confirmed["latched"] == []
    assert confirmed["state"] == "SAFE"
    assert confirmed["allows_place"] is True
    assert confirmed["risk_sequence"] > records[1]["risk_sequence"]
    assert TRUST["final_risk_state"] == "SAFE"


def test_the_risk_sequence_is_contiguous_and_ordered() -> None:
    records = [row for watch in TRUST["watches"] for row in watch["risk_records"]]
    sequences = [row["risk_sequence"] for row in records]
    assert sequences == sorted(sequences)
    assert sequences == list(range(sequences[0], sequences[0] + len(sequences)))


def test_the_quiet_markets_cost_the_risk_trace_nothing() -> None:
    quiet = [watch for watch in TRUST["watches"] if not watch["injected_decision"]]
    assert len(quiet) == 6
    assert all(watch["risk_records"] == [] for watch in quiet)
