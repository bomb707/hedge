"""Run the accepted P10A real-market corpus through the *production* verifier.

Not a rerun of the research analyser. The point is that the code which will decide real
redemptions independently consumes the real captured evidence and reaches the same conclusions —
55 settled markets, 27 UP and 28 DOWN, no false ambiguity, no mapping error.

Every provider error in the captured evidence is carried through faithfully, so a market where a
provider rate-limited the original capture is judged on the providers that actually answered.

Read-only: consumes committed evidence, touches no network.
"""

import argparse
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from maker5m.domain import Outcome
from maker5m.settlement import (
    AdvisoryResolution,
    MarketResolutionTarget,
    PayoutVector,
    ProviderResolution,
    SettlementPolicy,
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


def readings_from(market: dict[str, Any]) -> tuple[ProviderResolution, ...]:
    out: list[ProviderResolution] = []
    for provider, reading in sorted(market["chain"].items()):
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
            )
        )
    return tuple(out)


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
    args = parser.parse_args()

    payload = json.loads(args.corpus.read_text(encoding="utf-8"))
    policy = SettlementPolicy(minimum_agreeing_providers=args.min_providers)

    states: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    mismatches: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    for market in payload["markets"]:
        target = target_from(market)
        readings = readings_from(market)
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
        "policy": {
            "minimum_agreeing_providers": policy.minimum_agreeing_providers,
            "block_tag": "captured-latest (the corpus was read at `latest`)",
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
