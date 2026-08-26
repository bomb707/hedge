"""Run the accepted P10A real-market corpus through the *production* verifier.

Not a rerun of the research analyser. The point is that the code which will decide real
redemptions independently consumes the real captured evidence and reaches the same conclusions —
55 settled markets, 27 UP and 28 DOWN, no false ambiguity, no mapping error.

Every provider error in the captured evidence is carried through faithfully, so a market where a
provider rate-limited the original capture is judged on the providers that actually answered.

Read-only. It consumes committed evidence and, unless told not to, performs one live
**identity attestation** of the RPC endpoints — `eth_chainId`, `eth_getCode`, `decimals()`.
No market data is fetched; the settlement answers still come entirely from the committed corpus.

Two things about that attestation are stated rather than glossed:

* It is **contemporaneous with the replay, not with the capture**. The P10A corpus predates the
  attestation boundary, so no proof of endpoint identity exists for the moment those readings
  were taken. Rather than fabricate one, the endpoints are attested now and the record says so.
* The corpus was captured under the chain's **`latest`** tag, not `finalized`. The replay is run
  under a policy that names that rule (`captured-latest`), because pretending otherwise would be
  claiming finalized evidence this corpus does not contain. A reading tagged `captured-latest`
  is correctly refused by a `finalized` policy, which is the finality check doing its job.

An endpoint that fails attestation contributes **no reading at all**, exactly as in production.
"""

import argparse
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from maker5m.domain import Outcome
from maker5m.settlement import (
    DEFAULT_RPC_ENDPOINTS,
    AdvisoryResolution,
    EndpointSet,
    MarketResolutionTarget,
    PayoutVector,
    ProviderAttestation,
    ProviderResolution,
    RpcEndpoint,
    SettlementPolicy,
    attest_all,
    verify,
)


def target_from(market: dict[str, Any]) -> MarketResolutionTarget:
    gamma = market["gamma"]
    tokens = gamma.get("clob_token_ids") or ["", ""]
    return MarketResolutionTarget(
        slug=gamma["slug"],
        condition_id=gamma["condition_id"],
        up_token_id=str(tokens[0]),
        down_token_id=str(tokens[1]),
        neg_risk=bool(gamma.get("neg_risk")),
    )


def readings_from(
    market: dict[str, Any],
    attestations: dict[str, ProviderAttestation] | None = None,
) -> tuple[ProviderResolution, ...]:
    """Rebuild the captured chain readings, dropping any provider that is not attested.

    Dropping rather than flagging is deliberate and mirrors the production coordinator: an
    endpoint that has not proved its identity never produces evidence, so its absence shows up
    as a smaller quorum rather than as a malformed evidence set.
    """
    attested = {} if attestations is None else attestations
    out: list[ProviderResolution] = []
    for provider, reading in sorted(market["chain"].items()):
        attestation = attested.get(provider)
        if attestations is not None and (attestation is None or not attestation.valid):
            continue
        error = reading.get("error")
        payout = (
            None
            if error is not None or reading.get("payout_denominator") is None
            else PayoutVector(
                denominator=int(reading["payout_denominator"]),
                numerators=tuple(int(v) for v in reading["payout_numerators"]),
                outcome_slot_count=int(reading["outcome_slot_count"]),
            )
        )
        out.append(
            ProviderResolution(
                provider_id=provider,
                chain_id=None if error else 137,
                block_tag="captured-latest",
                block_number=reading.get("block_number"),
                condition_id=market["gamma"]["condition_id"],
                payout=payout,
                error=error,
                source_endpoint_fingerprint=(
                    "" if attestation is None else attestation.endpoint_fingerprint
                ),
                attestation=attestation,
            )
        )
    return tuple(out)


def live_attestations() -> tuple[dict[str, ProviderAttestation], list[dict[str, object]]]:
    """Attest the reference endpoints now. The only network this tool touches."""
    trusted, rejected = attest_all(
        EndpointSet(tuple(RpcEndpoint(pid, url) for pid, url in DEFAULT_RPC_ENDPOINTS))
    )
    attestations = {
        provider.provider_id: provider.identity.to_attestation() for provider in trusted
    }
    return attestations, [identity.summary() for identity in rejected]


def advisory_from(market: dict[str, Any]) -> tuple[AdvisoryResolution, ...]:
    gamma = market["gamma"]
    prices = gamma.get("outcome_prices") or []
    winners = [index for index, price in enumerate(prices) if str(price) == "1"]
    gamma_advisory = AdvisoryResolution(
        source="gamma_outcome_prices",
        available=bool(prices),
        winning_slot=winners[0] if len(winners) == 1 else None,
    )
    clob = market.get("clob") or {}
    tokens = clob.get("tokens") or []
    clob_winners = [index for index, token in enumerate(tokens) if token.get("winner")]
    clob_advisory = AdvisoryResolution(
        source="clob_winner",
        available=bool(clob.get("available")),
        winning_slot=clob_winners[0] if len(clob_winners) == 1 else None,
    )
    return (gamma_advisory, clob_advisory)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--min-providers", type=int, default=3)
    parser.add_argument(
        "--no-attest",
        action="store_true",
        help="skip the live identity attestation; the replay then cannot resolve anything, "
        "which is the point of running it that way",
    )
    args = parser.parse_args()

    payload = json.loads(args.corpus.read_text(encoding="utf-8"))
    policy = SettlementPolicy(
        minimum_agreeing_providers=args.min_providers, block_tag="captured-latest"
    )

    attestations: dict[str, ProviderAttestation] = {}
    rejected: list[dict[str, object]] = []
    if not args.no_attest:
        attestations, rejected = live_attestations()

    states: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    mismatches: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    for market in payload["markets"]:
        target = target_from(market)
        readings = readings_from(market, attestations)
        decision = verify(target, readings, advisory_from(market), policy)
        states[decision.state.value] += 1
        if decision.winning_outcome is not None:
            outcomes[decision.winning_outcome.value] += 1

        # Independent check against the captured vector, not against the verifier's own answer.
        stored = market["chain"]["publicnode"]["payout_numerators"]
        expected = Outcome.UP if stored == [1, 0] else Outcome.DOWN if stored == [0, 1] else None
        if decision.winning_outcome is not expected:
            mismatches.append(
                {
                    "slug": target.slug,
                    "stored_numerators": stored,
                    "expected": None if expected is None else expected.value,
                    "verifier": decision.summary(),
                }
            )
        rows.append(
            {
                "slug": target.slug,
                "condition_id": target.condition_id,
                "state": decision.state.value,
                "winning_outcome": (
                    None if decision.winning_outcome is None else decision.winning_outcome.value
                ),
                "agreeing_providers": list(decision.agreeing_providers),
                "answering_providers": list(decision.answering_providers),
                "payout": None if decision.payout is None else decision.payout.summary(),
                "reasons": [reason.value for reason in decision.reasons],
            }
        )

    report = {
        "kind": "P10_PRODUCTION_VERIFIER_OVER_P10A_CORPUS",
        "provenance": payload.get("provenance", "REAL_PUBLIC_MARKET_DATA"),
        "source_corpus": str(args.corpus.name),
        "source_captured_utc": payload.get("captured_utc"),
        "replayed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "attestation": {
            "note": (
                "Endpoint identity attested at replay time, NOT at capture time: the P10A "
                "corpus predates the attestation boundary and carries no proof of endpoint "
                "identity for the moment its readings were taken. No such proof was invented."
            ),
            "attested_at_replay": sorted(attestations),
            "rejected_at_replay": rejected,
            "attestations": {name: value.summary() for name, value in sorted(attestations.items())},
        },
        "policy": {
            "minimum_agreeing_providers": policy.minimum_agreeing_providers,
            "block_tag": policy.block_tag,
            "block_tag_note": (
                "The corpus was captured at the chain's `latest` tag, so the replay policy "
                "names that rule. It is deliberately NOT `finalized`: this evidence is not "
                "finalized evidence, and a `finalized` policy correctly refuses it."
            ),
            "require_binary_singleton": policy.require_binary_singleton,
            "require_unanimous_resolution": policy.require_unanimous_resolution,
            "status": policy.status.value,
        },
        "markets": len(payload["markets"]),
        "states": dict(sorted(states.items())),
        "outcomes": dict(sorted(outcomes.items())),
        "mismatches": mismatches,
        "rows": rows,
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(
        json.dumps(
            {k: report[k] for k in ("markets", "states", "outcomes")}
            | {"mismatches": len(mismatches)},
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
