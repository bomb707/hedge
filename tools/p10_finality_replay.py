"""Replay recorded live polls through the corrected verifier — P10, provider-quorum correction.

**REAL MARKET DATA, RECONSTRUCTED READINGS.** The polls come from a live run of
`tools/p10_settlement_run.py` against real Polygon providers and real Polymarket markets. What
is reconstructed is only the shape: each poll recorded per-provider `answered`, `block_number`
and `resolved`, so a provider that reported resolved is rebuilt with the payout vector that
market finally settled to, and one that reported unresolved is rebuilt with a zero denominator.

That reconstruction is exact for the branch under test — it turns on which providers were
resolved and at which blocks, both of which were recorded verbatim — and it is not a claim to
have replayed the full payout comparison, which the recording does not carry per poll.

Usage: p10_finality_replay.py <live-settlement.json> [...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from maker5m.settlement import (
    MarketResolutionTarget,
    PayoutVector,
    ProviderResolution,
    ResolutionState,
    SettlementPolicy,
    verify,
)


def rebuild(
    poll: dict[str, Any], condition_id: str, final: PayoutVector
) -> tuple[ProviderResolution, ...]:
    out: list[ProviderResolution] = []
    for row in poll["per_provider"]:
        answered = bool(row["answered"])
        payout = (
            final
            if row["resolved"]
            else PayoutVector(denominator=0, numerators=(0, 0), outcome_slot_count=2)
        )
        out.append(
            ProviderResolution(
                provider_id=str(row["provider"]),
                chain_id=137 if answered else None,
                block_tag="finalized",
                block_number=row["block"],
                condition_id=condition_id,
                payout=payout if answered else None,
                error=None if answered else "provider did not answer this poll",
            )
        )
    return tuple(out)


def main() -> None:
    paths = [Path(arg) for arg in sys.argv[1:]]
    if not paths:
        raise SystemExit(__doc__)

    tolerant = SettlementPolicy()
    strict = SettlementPolicy(require_unanimous_resolution=True)
    rows: list[dict[str, Any]] = []

    for path in paths:
        run = json.loads(path.read_text("utf-8"))
        for watch in run["watches"]:
            final = watch.get("final_decision")
            if not final or not final.get("payout"):
                continue
            payout = PayoutVector(
                denominator=int(final["payout"]["denominator"]),
                numerators=tuple(int(v) for v in final["payout"]["numerators"]),
                outcome_slot_count=int(final["payout"]["outcome_slot_count"]),
            )
            target = MarketResolutionTarget(
                slug=watch["slug"],
                condition_id=watch["condition_id"],
                up_token_id=watch["up_token_id"],
                down_token_id=watch["down_token_id"],
                neg_risk=bool(watch["neg_risk"]),
            )
            for poll in watch.get("polls", []):
                if "per_provider" not in poll:
                    continue
                readings = rebuild(poll, watch["condition_id"], payout)
                before = verify(target, readings, (), strict)
                after = verify(target, readings, (), tolerant)
                if before.state is not after.state:
                    rows.append(
                        {
                            "slug": watch["slug"],
                            "at": poll["at"],
                            "recorded_state": poll["state"],
                            "recorded_reasons": poll.get("reasons", []),
                            "strict": before.state.value,
                            "corrected": after.state.value,
                            "blocks": {
                                r["provider"]: (r["block"], r["resolved"])
                                for r in poll["per_provider"]
                            },
                            "detail": after.detail,
                        }
                    )

    halts_removed = sum(
        1
        for row in rows
        if row["strict"] == ResolutionState.AMBIGUOUS.value
        and row["corrected"] == ResolutionState.UNRESOLVED.value
    )
    print(
        json.dumps(
            {
                "kind": "P10_FINALITY_LAG_REPLAY",
                "provenance": "REAL_PUBLIC_MARKET_DATA",
                "sources": [str(p) for p in paths],
                "polls_whose_verdict_changed": len(rows),
                "false_halts_removed": halts_removed,
                "verdicts_newly_ambiguous": len(rows) - halts_removed,
                "changed": rows,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
