"""Watch real markets settle through the production P10 stack.

Follows consecutive real ``btc-updown-5m`` markets from before they end until the production
:func:`maker5m.settlement.verify` reaches ``RESOLVED`` under the configured quorum and finality
policy, recording the whole state trajectory rather than only the endpoint.

``--inject`` applies a **controlled local corruption** to one market's readings — a wrong advisory
winner, or one provider reporting a different payout — to prove the integration fails closed. The
market data underneath stays real and untouched; only our copy of it is corrupted, which is why
the evidence is labelled ``CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`` and never described as a venue
or chain incident.

Read-only: no order, no credential, no transaction, no redemption. ``LIVE_TRADING_ENABLED`` is
``False`` and the redemption transport refuses before touching anything.
"""

import argparse
import json
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from maker5m.settlement import (
    DEFAULT_RPC_ENDPOINTS,
    AdvisoryResolution,
    CtfReader,
    MarketResolutionTarget,
    PayoutVector,
    ProviderResolution,
    Redeemer,
    ResolutionState,
    RpcEndpoint,
    SettlementPolicy,
    SettlementPreconditions,
    verify,
)
from maker5m.settlement.reader import USER_AGENT

GAMMA: Final = "https://gamma-api.polymarket.com"
CLOB: Final = "https://clob.polymarket.com"
POLL_SECONDS: Final = 2.0
"""Settlement is a cold path. Two seconds is ample against an ~85 s resolution and is polite to
public endpoints."""

WATCH_BEFORE_END: Final = 20
WATCH_AFTER_END: Final = 280


def _get(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())
    except Exception:
        return None


def gamma_market(slug: str) -> dict[str, Any] | None:
    events = _get(f"{GAMMA}/events?slug={slug}")
    if not events:
        return None
    markets = events[0].get("markets") or []
    return markets[0] if markets else None


def advisories(slug: str, condition_id: str) -> tuple[AdvisoryResolution, ...]:
    market = gamma_market(slug)
    prices: list[Any] = []
    if market is not None:
        raw = market.get("outcomePrices")
        prices = json.loads(raw) if isinstance(raw, str) else (raw or [])
    winners = [index for index, price in enumerate(prices) if str(price) == "1"]
    gamma_view = AdvisoryResolution(
        source="gamma_outcome_prices",
        available=bool(prices),
        winning_slot=winners[0] if len(winners) == 1 else None,
    )
    book = _get(f"{CLOB}/markets/{condition_id}")
    tokens = book.get("tokens", []) if isinstance(book, dict) else []
    clob_winners = [index for index, token in enumerate(tokens) if token.get("winner")]
    clob_view = AdvisoryResolution(
        source="clob_winner",
        available=isinstance(book, dict) and bool(book.get("condition_id")),
        winning_slot=clob_winners[0] if len(clob_winners) == 1 else None,
    )
    return (gamma_view, clob_view)


def corrupt(
    readings: tuple[ProviderResolution, ...],
    advisory: tuple[AdvisoryResolution, ...],
    kind: str,
) -> tuple[tuple[ProviderResolution, ...], tuple[AdvisoryResolution, ...]]:
    """Corrupt a *copy* of genuine readings. The market and chain are untouched."""
    if kind == "advisory":
        flipped = tuple(
            item._replace(winning_slot=1 - item.winning_slot)
            if item.conclusive and item.winning_slot in (0, 1)
            else item
            for item in advisory
        )
        if flipped == advisory:  # nothing conclusive yet: assert a winner that is not the chain's
            flipped = (
                AdvisoryResolution("gamma_outcome_prices", available=True, winning_slot=1),
                *advisory[1:],
            )
        return readings, flipped
    if kind == "provider":
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
        return tuple(corrupted), advisory
    raise ValueError(f"unknown injection kind: {kind}")


@dataclass(slots=True)
class Watch:
    slug: str
    t0: int
    condition_id: str = ""
    up_token_id: str = ""
    down_token_id: str = ""
    neg_risk: bool = False
    states: list[dict[str, Any]] = field(default_factory=list)
    resolved_at: float | None = None
    final: dict[str, Any] | None = None
    injection: str = ""
    injected_decision: dict[str, Any] | None = None
    redeem_plan: dict[str, Any] | None = None
    redeem_blockers: list[str] = field(default_factory=list)
    note: str = ""

    def summary(self) -> dict[str, Any]:
        end = float(self.t0 + 300)
        return {
            "slug": self.slug,
            "t0": self.t0,
            "end_epoch": self.t0 + 300,
            "condition_id": self.condition_id,
            "up_token_id": self.up_token_id,
            "down_token_id": self.down_token_id,
            "neg_risk": self.neg_risk,
            "state_trajectory": [row["state"] for row in self.states],
            "distinct_states": sorted({row["state"] for row in self.states}),
            "samples": len(self.states),
            "resolved_seconds_after_end": (
                None if self.resolved_at is None else round(self.resolved_at - end, 3)
            ),
            "final_decision": self.final,
            "injection": self.injection,
            "injected_decision": self.injected_decision,
            "redeem_plan": self.redeem_plan,
            "redeem_blockers": self.redeem_blockers,
            "note": self.note,
        }


def watch(
    t0: int, policy: SettlementPolicy, endpoints: tuple[RpcEndpoint, ...], injection: str = ""
) -> Watch:
    slug = f"btc-updown-5m-{t0}"
    result = Watch(slug=slug, t0=t0, injection=injection)
    market = gamma_market(slug)
    if market is None or not market.get("conditionId"):
        result.note = "market not published by Gamma"
        return result

    tokens = market.get("clobTokenIds")
    tokens = json.loads(tokens) if isinstance(tokens, str) else (tokens or ["", ""])
    result.condition_id = str(market["conditionId"])
    result.up_token_id, result.down_token_id = str(tokens[0]), str(tokens[1])
    result.neg_risk = bool(market.get("negRisk"))

    target = MarketResolutionTarget(
        slug=slug,
        condition_id=result.condition_id,
        up_token_id=result.up_token_id,
        down_token_id=result.down_token_id,
        neg_risk=result.neg_risk,
    )
    readers = [CtfReader(endpoint) for endpoint in endpoints]
    print(f"  watching {slug} (ends in {t0 + 300 - int(time.time())}s)", flush=True)

    deadline = t0 + 300 + WATCH_AFTER_END
    while time.time() < deadline:
        now = time.time()
        readings = tuple(
            reader.read_condition(result.condition_id, block_tag=policy.block_tag)
            for reader in readers
        )
        advisory = advisories(slug, result.condition_id)
        decision = verify(target, readings, advisory, policy)
        result.states.append(
            {
                "at": round(now, 3),
                "state": decision.state.value,
                "answering": list(decision.answering_providers),
            }
        )

        if decision.state is ResolutionState.RESOLVED:
            result.resolved_at = now
            result.final = decision.summary()
            print(
                f"    RESOLVED at +{now - (t0 + 300):.1f}s  "
                f"{decision.winning_outcome.value if decision.winning_outcome else '?'}  "
                f"providers={len(decision.agreeing_providers)}",
                flush=True,
            )
            if injection:
                bad_readings, bad_advisory = corrupt(readings, advisory, injection)
                bad = verify(target, bad_readings, bad_advisory, policy)
                result.injected_decision = bad.summary()
                plan, blockers = Redeemer().prepare(target, bad, SettlementPreconditions())
                result.redeem_plan = None if plan is None else plan.summary()
                result.redeem_blockers = [blocker.value for blocker in blockers]
                print(
                    f"    injected '{injection}' -> {bad.state.value} "
                    f"reasons={[r.value for r in bad.reasons]} plan={plan is not None}",
                    flush=True,
                )
            else:
                plan, blockers = Redeemer().prepare(target, decision, SettlementPreconditions())
                result.redeem_plan = None if plan is None else plan.summary()
                result.redeem_blockers = [blocker.value for blocker in blockers]
            break

        time.sleep(POLL_SECONDS)

    if result.final is None:
        result.note = "did not reach RESOLVED inside the watch window"
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", type=int, default=6)
    parser.add_argument("--min-providers", type=int, default=3)
    parser.add_argument("--inject-on", type=int, default=-1, help="0-based market index")
    parser.add_argument("--inject", default="advisory", choices=("advisory", "provider"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    policy = SettlementPolicy(minimum_agreeing_providers=args.min_providers)
    endpoints = tuple(RpcEndpoint(provider_id=name, url=url) for name, url in DEFAULT_RPC_ENDPOINTS)
    identities = [CtfReader(endpoint).identify().summary() for endpoint in endpoints]
    for identity in identities:
        print(
            f"  provider {identity['provider_id']}: trustworthy={identity['trustworthy']}",
            flush=True,
        )

    watches: list[Watch] = []
    while len(watches) < args.markets:
        now = int(time.time())
        t0 = (now // 300) * 300
        if t0 + 300 - now < WATCH_BEFORE_END:
            t0 += 300
        while time.time() < t0 + 300 - WATCH_BEFORE_END:
            time.sleep(2)
        injection = args.inject if len(watches) == args.inject_on else ""
        watches.append(watch(t0, policy, endpoints, injection))
        print(f"  [{len(watches)}/{args.markets}] done", flush=True)

    args.out.write_text(
        json.dumps(
            {
                "kind": "P10_LIVE_SETTLEMENT",
                "provenance": (
                    "CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET"
                    if args.inject_on >= 0
                    else "REAL_PUBLIC_MARKET_DATA"
                ),
                "note": (
                    "Production ResolutionVerifier over real Polygon and Polymarket data. "
                    "No order, no credential, no transaction, no redemption."
                ),
                "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "policy": {
                    "minimum_agreeing_providers": policy.minimum_agreeing_providers,
                    "block_tag": policy.block_tag,
                    "require_binary_singleton": policy.require_binary_singleton,
                    "status": policy.status.value,
                },
                "provider_identities": identities,
                "watches": [item.summary() for item in watches],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(f"wrote {len(watches)} watches to {args.out}")


if __name__ == "__main__":
    main()
