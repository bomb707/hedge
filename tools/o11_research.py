"""O11 research: which source authoritatively determines a settled market's outcome?

Read-only evidence collection for P10A. Nothing here writes to a venue or a chain, holds a key,
or signs anything. It reads three different things and refuses to conflate them:

* **the rule source** — what the market's own rules name as determining the outcome;
* **venue metadata** — what Gamma and the CLOB currently report;
* **final on-chain settlement state** — what the Conditional Tokens contract actually makes
  redeemable, which is the only thing a redemption can be paid from.

Real markets only. Synthetic data cannot close O11 (`ARCHITECTURE_SSOT.md` §4.4).
"""

import argparse
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

USER_AGENT: Final = "hedge-research/0.1 (P10A O11 resolution research; read-only; contact via repo)"
GAMMA: Final = "https://gamma-api.polymarket.com"
CLOB: Final = "https://clob.polymarket.com"

CTF_ADDRESS: Final = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
"""Conditional Tokens on Polygon, from docs.polymarket.com/resources/contracts."""

CHAIN_ID: Final = 137

RPCS: Final[dict[str, str]] = {
    "publicnode": "https://polygon-bor-rpc.publicnode.com",
    "drpc": "https://polygon.drpc.org",
    "1rpc": "https://1rpc.io/matic",
    "quiknode-public": "https://rpc-mainnet.matic.quiknode.pro",
}

SEL_PAYOUT_DENOMINATOR: Final = "0xdd34de67"
SEL_PAYOUT_NUMERATORS: Final = "0x0504c814"
SEL_OUTCOME_SLOT_COUNT: Final = "0xd42dc0c2"
TOPIC_CONDITION_RESOLUTION: Final = (
    "0xb44d84d3289691f71497564b85d4233648d9dbae8cbdbb4329f301c3a0185894"
)
TOPIC_CONDITION_PREPARATION: Final = (
    "0xab3760c3bd2bb38b5bcf54dc79802ed67338b4cf29f3054ded67ed24661e4177"
)

POLL_SECONDS: Final = 2.0
"""Bounded polling. This is measurement; it must not become load on someone else's service."""


def _curl(url: str, *, data: str | None = None, timeout: int = 25) -> str:
    cmd = ["curl", "-s", "-m", str(timeout), "-H", f"User-Agent: {USER_AGENT}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", data]
    cmd.append(url)
    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout


def gamma(path: str) -> Any:
    text = _curl(f"{GAMMA}{path}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def clob(path: str) -> Any:
    text = _curl(f"{CLOB}{path}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def rpc(provider: str, method: str, params: list[Any]) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    text = _curl(RPCS[provider], data=body, timeout=25)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": {"message": f"undecodable: {text[:120]}"}}


def _word(value: str) -> str:
    return value[2:].rjust(64, "0") if value.startswith("0x") else value.rjust(64, "0")


def _uint(result: str | None) -> int | None:
    if not result or result == "0x":
        return None
    return int(result, 16)


@dataclass(slots=True)
class ChainReading:
    """What one RPC provider says about one condition."""

    provider: str
    block_number: int | None = None
    payout_denominator: int | None = None
    outcome_slot_count: int | None = None
    payout_numerators: list[int] = field(default_factory=list)
    error: str | None = None

    def summary(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "block_number": self.block_number,
            "payout_denominator": self.payout_denominator,
            "outcome_slot_count": self.outcome_slot_count,
            "payout_numerators": self.payout_numerators,
            "error": self.error,
        }

    @property
    def resolved(self) -> bool:
        return bool(self.payout_denominator)


def read_condition(provider: str, condition_id: str) -> ChainReading:
    """Read the full payout state for one condition from one provider."""
    reading = ChainReading(provider=provider)
    head = rpc(provider, "eth_blockNumber", [])
    if "result" in head:
        reading.block_number = _uint(head["result"])
    else:
        reading.error = str(head.get("error"))
        return reading

    def call(data: str) -> int | None:
        response = rpc(provider, "eth_call", [{"to": CTF_ADDRESS, "data": data}, "latest"])
        if "result" not in response:
            reading.error = str(response.get("error"))
            return None
        return _uint(response["result"])

    reading.payout_denominator = call(SEL_PAYOUT_DENOMINATOR + _word(condition_id))
    reading.outcome_slot_count = call(SEL_OUTCOME_SLOT_COUNT + _word(condition_id))
    slots = reading.outcome_slot_count or 0
    for index in range(slots):
        value = call(SEL_PAYOUT_NUMERATORS + _word(condition_id) + format(index, "064x"))
        reading.payout_numerators.append(-1 if value is None else value)
    return reading


@dataclass(slots=True)
class BlockClock:
    """Maps a wall-clock second to an approximate Polygon block.

    Public RPCs cap ``eth_getLogs`` at a 10,000-block range, so a log query needs a window
    rather than "everything since a guess". The seconds-per-block figure is measured from the
    chain itself rather than assumed, because a hard-coded 2 s drifts by hours over a week.
    """

    head_block: int
    head_timestamp: int
    seconds_per_block: float

    @classmethod
    def calibrate(cls, provider: str, *, span: int = 200_000) -> "BlockClock":
        head = rpc(provider, "eth_blockNumber", [])
        head_block = _uint(head["result"])
        assert head_block is not None
        recent = rpc(provider, "eth_getBlockByNumber", [hex(head_block), False])
        older = rpc(provider, "eth_getBlockByNumber", [hex(head_block - span), False])
        newest = _uint(recent["result"]["timestamp"])
        oldest = _uint(older["result"]["timestamp"])
        assert newest is not None and oldest is not None
        return cls(head_block, newest, (newest - oldest) / span)

    def block_at(self, timestamp: int) -> int:
        delta = self.head_timestamp - timestamp
        return max(1, int(self.head_block - delta / self.seconds_per_block))

    def window(self, timestamp: int, *, half: int = 4_500) -> tuple[int, int]:
        centre = self.block_at(timestamp)
        return max(1, centre - half), min(self.head_block, centre + half)


def resolution_log(
    provider: str, condition_id: str, *, from_block: int, to_block: int
) -> dict[str, object]:
    """The ConditionResolution event for this condition, if the range covers it."""
    response = rpc(
        provider,
        "eth_getLogs",
        [
            {
                "address": CTF_ADDRESS,
                "topics": [TOPIC_CONDITION_RESOLUTION, condition_id],
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
            }
        ],
    )
    if "result" not in response or not response["result"]:
        return {"found": False, "detail": str(response.get("error", "no log in range"))}
    log = response["result"][0]
    block_number = _uint(log["blockNumber"])
    stamp = rpc(provider, "eth_getBlockByNumber", [log["blockNumber"], False])
    block_time = _uint(stamp.get("result", {}).get("timestamp")) if "result" in stamp else None
    return {
        "found": True,
        "block_number": block_number,
        "block_timestamp": block_time,
        "transaction_hash": log.get("transactionHash"),
        "oracle": "0x" + log["topics"][2][-40:] if len(log["topics"]) > 2 else None,
        "question_id": log["topics"][3] if len(log["topics"]) > 3 else None,
    }


def preparation_log(
    provider: str, condition_id: str, *, from_block: int, to_block: int
) -> dict[str, object]:
    """The ConditionPreparation event names the oracle this condition was created with."""
    response = rpc(
        provider,
        "eth_getLogs",
        [
            {
                "address": CTF_ADDRESS,
                "topics": [TOPIC_CONDITION_PREPARATION, condition_id],
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
            }
        ],
    )
    if "result" not in response or not response["result"]:
        return {"found": False, "detail": str(response.get("error", "no log in range"))}
    log = response["result"][0]
    return {
        "found": True,
        "block_number": _uint(log["blockNumber"]),
        "oracle": "0x" + log["topics"][2][-40:] if len(log["topics"]) > 2 else None,
        "question_id": log["topics"][3] if len(log["topics"]) > 3 else None,
    }


def market_metadata(slug: str) -> dict[str, object] | None:
    """Gamma's view of one market. Only fields the real API actually exposes."""
    events = gamma(f"/events?slug={slug}")
    if not events:
        return None
    markets = events[0].get("markets") or []
    if not markets:
        return None
    market = markets[0]
    token_ids = market.get("clobTokenIds")
    if isinstance(token_ids, str):
        token_ids = json.loads(token_ids)
    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    prices = market.get("outcomePrices")
    if isinstance(prices, str):
        prices = json.loads(prices)
    return {
        "slug": slug,
        "condition_id": market.get("conditionId"),
        "question_id": market.get("questionID"),
        "closed": market.get("closed"),
        "active": market.get("active"),
        "uma_resolution_status": market.get("umaResolutionStatus"),
        "automatically_resolved": market.get("automaticallyResolved"),
        "resolution_source": market.get("resolutionSource"),
        "outcomes": outcomes,
        "outcome_prices": prices,
        "clob_token_ids": token_ids,
        "start_date": market.get("startDate"),
        "end_date": market.get("endDate"),
        "closed_time": market.get("closedTime"),
        "neg_risk": market.get("negRisk"),
        "crypto_market_config": market.get("cryptoMarketConfig"),
    }


def clob_view(condition_id: str) -> dict[str, object]:
    """The CLOB's own view. Recorded only where the field genuinely exists."""
    payload = clob(f"/markets/{condition_id}")
    if not isinstance(payload, dict) or "condition_id" not in payload:
        return {"available": False}
    return {
        "available": True,
        "closed": payload.get("closed"),
        "active": payload.get("active"),
        "accepting_orders": payload.get("accepting_orders"),
        "tokens": [
            {
                "token_id": token.get("token_id"),
                "outcome": token.get("outcome"),
                "winner": token.get("winner"),
                "price": token.get("price"),
            }
            for token in payload.get("tokens", [])
        ],
    }


SLUG_PREFIX: Final = "btc-updown-5m-"


def slug_for(t0: int) -> str:
    return f"{SLUG_PREFIX}{t0}"


def collect(count: int, *, end_before: int, out: Path) -> None:
    """Walk backwards from a T0, gathering settled markets and their on-chain state."""
    print(f"collecting {count} settled markets ending before {end_before}", flush=True)
    clock = BlockClock.calibrate("publicnode")
    print(
        f"  block clock: head {clock.head_block} at {clock.head_timestamp}, "
        f"{clock.seconds_per_block:.3f} s/block",
        flush=True,
    )

    rows: list[dict[str, object]] = []
    t0 = end_before - (end_before % 300)
    attempts = 0
    while len(rows) < count and attempts < count * 4:
        attempts += 1
        t0 -= 300
        slug = slug_for(t0)
        meta = market_metadata(slug)
        if meta is None or not meta.get("condition_id"):
            continue
        if not meta.get("closed"):
            continue

        condition_id = str(meta["condition_id"])
        # Resolution lands shortly after the market ends; preparation well before it starts.
        low, high = clock.window(t0 + 300)
        prep_low, prep_high = clock.window(t0 - 1800)
        readings = {name: read_condition(name, condition_id).summary() for name in RPCS}
        primary = readings["publicnode"]
        row: dict[str, object] = {
            "t0": t0,
            "gamma": meta,
            "clob": clob_view(condition_id),
            "chain": readings,
            "resolution_log": resolution_log(
                "publicnode", condition_id, from_block=low, to_block=high
            ),
            "preparation_log": preparation_log(
                "publicnode", condition_id, from_block=prep_low, to_block=prep_high
            ),
        }
        rows.append(row)
        den = primary.get("payout_denominator")
        nums = primary.get("payout_numerators")
        print(f"  {slug}  den={den} nums={nums} gamma={meta.get('outcome_prices')}", flush=True)
        time.sleep(POLL_SECONDS)

    out.write_text(
        json.dumps(
            {
                "kind": "P10A_O11_HISTORICAL",
                "provenance": "REAL_PUBLIC_MARKET_DATA",
                "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "ctf_address": CTF_ADDRESS,
                "chain_id": CHAIN_ID,
                "rpc_providers": RPCS,
                "poll_seconds": POLL_SECONDS,
                "markets": rows,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(f"wrote {len(rows)} markets to {out}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--end-before", type=int, default=int(time.time()) - 1800)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    collect(args.count, end_before=args.end_before, out=args.out)


if __name__ == "__main__":
    main()
