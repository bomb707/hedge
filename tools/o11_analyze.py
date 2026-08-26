"""Turn the O11 evidence into the agreement matrix, honestly.

The rule that shapes every count here: **missing is never agreement.** A source that had no
opinion about a market is recorded as missing, not as agreeing with whatever the chain said.
"""

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def chain_winner(numerators: list[int], denominator: int | None) -> str:
    """Which slot the on-chain payout pays, or a name for the shapes that are not binary."""
    if not denominator:
        return "UNRESOLVED"
    non_zero = [index for index, value in enumerate(numerators) if value]
    if len(numerators) != 2:
        return f"NON_BINARY_SLOTS_{len(numerators)}"
    if len(non_zero) != 1:
        return "FRACTIONAL_OR_TIED"
    if sum(numerators) != denominator:
        return "UNEXPECTED_DENOMINATOR"
    return f"SLOT_{non_zero[0]}"


def gamma_winner(market: dict[str, Any]) -> str:
    prices = market.get("outcome_prices")
    outcomes = market.get("outcomes")
    if not prices or not outcomes or len(prices) != len(outcomes):
        return "MISSING"
    winners = [index for index, price in enumerate(prices) if str(price) == "1"]
    if len(winners) != 1:
        return "MISSING" if not winners else "AMBIGUOUS"
    return f"SLOT_{winners[0]}"


def clob_winner(view: dict[str, Any]) -> str:
    if not view.get("available"):
        return "MISSING"
    tokens = view.get("tokens") or []
    winners = [index for index, token in enumerate(tokens) if token.get("winner")]
    if len(winners) != 1:
        return "MISSING" if not winners else "AMBIGUOUS"
    return f"SLOT_{winners[0]}"


def analyse(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    markets = payload["markets"]

    matrix: dict[str, Counter[str]] = {"gamma_outcome_prices": Counter(), "clob_winner": Counter()}
    chain_shapes: Counter[str] = Counter()
    rpc_agreement: Counter[str] = Counter()
    providers_per_market: Counter[int] = Counter()
    resolution_delays: list[int] = []
    oracles: Counter[str] = Counter()
    resolution_sources: Counter[str] = Counter()
    slot_labels: Counter[str] = Counter()
    disagreements: list[dict[str, Any]] = []

    for market in markets:
        gamma = market["gamma"]
        readings = market["chain"]
        primary = readings["publicnode"]
        truth = chain_winner(primary["payout_numerators"], primary["payout_denominator"])
        chain_shapes[truth] += 1
        resolution_sources[str(gamma.get("resolution_source"))] += 1

        # Outcome-slot labelling: what does the venue call the slot the chain pays?
        outcomes = gamma.get("outcomes") or []
        if truth.startswith("SLOT_") and outcomes:
            index = int(truth.removeprefix("SLOT_"))
            if index < len(outcomes):
                slot_labels[f"{truth}={outcomes[index]}"] += 1

        for name, verdict in (
            ("gamma_outcome_prices", gamma_winner(gamma)),
            ("clob_winner", clob_winner(market["clob"])),
        ):
            if verdict == "MISSING":
                matrix[name]["missing"] += 1
            elif verdict == "AMBIGUOUS":
                matrix[name]["ambiguous"] += 1
            elif not truth.startswith("SLOT_"):
                matrix[name]["chain_not_binary"] += 1
            elif verdict == truth:
                matrix[name]["agree"] += 1
            else:
                matrix[name]["disagree"] += 1
                disagreements.append(
                    {"slug": gamma["slug"], "source": name, "source_says": verdict, "chain": truth}
                )

        # Only providers that actually answered can agree about anything. A provider that
        # rate-limited us is recorded as absent, never as concurring.
        answered = {
            name: json.dumps(
                [r["payout_denominator"], r["payout_numerators"], r["outcome_slot_count"]]
            )
            for name, r in readings.items()
            if r["error"] is None and r["payout_denominator"] is not None
        }
        providers_per_market[len(answered)] += 1
        distinct = set(answered.values())
        if len(distinct) == 1:
            rpc_agreement["markets_where_all_answering_providers_agree"] += 1
        elif len(distinct) > 1:
            rpc_agreement["markets_with_provider_disagreement"] += 1
        else:
            rpc_agreement["markets_with_no_provider_answer"] += 1
        if len(answered) < 2:
            rpc_agreement["markets_without_independent_confirmation"] += 1

        log = market["resolution_log"]
        if log.get("found"):
            oracles[str(log.get("oracle"))] += 1
            block_time = log.get("block_timestamp")
            if isinstance(block_time, int):
                resolution_delays.append(block_time - (market["t0"] + 300))

    delays = sorted(resolution_delays)

    def quantile(fraction: float) -> int | None:
        if not delays:
            return None
        return delays[min(len(delays) - 1, int(len(delays) * fraction))]

    return {
        "markets": len(markets),
        "captured_utc": payload["captured_utc"],
        "chain_outcome_shapes": dict(chain_shapes),
        "slot_labels": dict(slot_labels),
        "resolution_sources": dict(resolution_sources),
        "oracles_observed": dict(oracles),
        "agreement_matrix": {name: dict(counts) for name, counts in matrix.items()},
        "disagreements": disagreements,
        "rpc_agreement": dict(rpc_agreement),
        "providers_answering_per_market": dict(sorted(providers_per_market.items())),
        "min_providers_answering": min(providers_per_market) if providers_per_market else 0,
        "rpc_providers": list(payload["rpc_providers"]),
        "onchain_resolution_delay_seconds_after_market_end": {
            "count": len(delays),
            "min": delays[0] if delays else None,
            "p50": quantile(0.50),
            "p90": quantile(0.90),
            "max": delays[-1] if delays else None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("historical", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = analyse(args.historical)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
