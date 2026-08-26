"""O11 live study: which source becomes final first, and how long after the market ends?

Historical snapshots cannot answer this. A market that settled an hour ago looks identical in
every source now, so the ordering has to be watched as it happens.

This follows consecutive real ``btc-updown-5m`` markets from before they end until the
Conditional Tokens contract reports a payout, and records the first moment each source changed,
on one synchronized local clock. Read-only throughout: no orders, no credentials, no writes.
"""

import argparse
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

USER_AGENT: Final = "hedge-research/0.1 (P10A O11 resolution research; read-only)"
GAMMA: Final = "https://gamma-api.polymarket.com"
CLOB: Final = "https://clob.polymarket.com"
CTF_ADDRESS: Final = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
RPC: Final = "https://polygon-bor-rpc.publicnode.com"
SEL_PAYOUT_DENOMINATOR: Final = "0xdd34de67"
SEL_PAYOUT_NUMERATORS: Final = "0x0504c814"

POLL_SECONDS: Final = 1.0
"""One sample per second per source while a market is settling.

Fine enough to place events in order — settlement takes tens of seconds — and coarse enough not
to become load. Polling continues for ``FOLLOW_AFTER_CHAIN`` seconds *past* the chain payout, so
a venue source that catches up later is still recorded; see that constant for why.
"""

WATCH_BEFORE_END: Final = 20
WATCH_AFTER_END: Final = 290
FOLLOW_AFTER_CHAIN: Final = 120
"""Keep watching after the chain reports a payout.

The first draft stopped at the chain event, which meant only sources that *beat* the chain could
ever be observed - the question this study exists to answer would have been answered by the
method rather than by the data. Venue metadata that catches up later is now recorded too.
"""


def _curl(url: str, *, data: str | None = None, timeout: int = 10) -> str:
    cmd = ["curl", "-s", "-m", str(timeout), "-H", f"User-Agent: {USER_AGENT}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", data]
    cmd.append(url)
    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _word(value: str) -> str:
    return value[2:].rjust(64, "0") if value.startswith("0x") else value.rjust(64, "0")


def chain_payout(condition_id: str) -> tuple[int | None, list[int]]:
    """(payoutDenominator, payoutNumerators). ``None`` denominator means unresolved."""

    def call(data: str) -> int | None:
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_call",
                "params": [{"to": CTF_ADDRESS, "data": data}, "latest"],
            }
        )
        payload = _json(_curl(RPC, data=body))
        if not isinstance(payload, dict) or "result" not in payload:
            return None
        result = payload["result"]
        return None if not result or result == "0x" else int(result, 16)

    denominator = call(SEL_PAYOUT_DENOMINATOR + _word(condition_id))
    if not denominator:
        return denominator, []
    numerators = [
        call(SEL_PAYOUT_NUMERATORS + _word(condition_id) + format(index, "064x")) or 0
        for index in range(2)
    ]
    return denominator, numerators


@dataclass(slots=True)
class Watch:
    """One market, watched from before it ends until the chain pays out."""

    slug: str
    t0: int
    condition_id: str = ""
    end_epoch: int = 0
    first_seen: dict[str, float] = field(default_factory=dict)
    observations: list[dict[str, object]] = field(default_factory=list)
    final_numerators: list[int] = field(default_factory=list)
    note: str = ""

    def mark(self, source: str, at: float) -> None:
        self.first_seen.setdefault(source, at)

    def summary(self) -> dict[str, object]:
        end = float(self.end_epoch)
        return {
            "slug": self.slug,
            "t0": self.t0,
            "condition_id": self.condition_id,
            "end_epoch": self.end_epoch,
            "final_payout_numerators": self.final_numerators,
            "seconds_after_end": {
                source: round(at - end, 3) for source, at in sorted(self.first_seen.items())
            },
            "first_seen_epoch": {
                source: round(at, 3) for source, at in sorted(self.first_seen.items())
            },
            "samples": len(self.observations),
            "note": self.note,
        }


def discover(slug: str) -> dict[str, Any] | None:
    events = _json(_curl(f"{GAMMA}/events?slug={slug}"))
    if not events:
        return None
    markets = events[0].get("markets") or []
    return markets[0] if markets else None


def watch_one(t0: int) -> Watch:
    slug = f"btc-updown-5m-{t0}"
    watch = Watch(slug=slug, t0=t0, end_epoch=t0 + 300)

    market = discover(slug)
    if market is None:
        watch.note = "market not published by Gamma"
        return watch
    watch.condition_id = str(market.get("conditionId") or "")
    if not watch.condition_id:
        watch.note = "no conditionId"
        return watch

    print(f"  watching {slug} (ends in {watch.end_epoch - int(time.time())}s)", flush=True)
    deadline = watch.end_epoch + WATCH_AFTER_END
    chain_seen_at: float | None = None
    wanted = {
        "gamma_closed",
        "gamma_outcome_prices",
        "clob_winner_flag",
        "ctf_payout_denominator",
    }
    while time.time() < deadline:
        now = time.time()
        if chain_seen_at is not None and (
            wanted <= set(watch.first_seen) or now - chain_seen_at > FOLLOW_AFTER_CHAIN
        ):
            break

        gamma_market = discover(slug)
        if gamma_market is not None:
            prices = gamma_market.get("outcomePrices")
            if isinstance(prices, str):
                prices = json.loads(prices)
            if gamma_market.get("closed"):
                watch.mark("gamma_closed", now)
            if prices and set(map(str, prices)) != {"0.5"} and "1" in map(str, prices):
                watch.mark("gamma_outcome_prices", now)
            if gamma_market.get("umaResolutionStatus") == "resolved":
                watch.mark("gamma_uma_status_resolved", now)

        book = _json(_curl(f"{CLOB}/markets/{watch.condition_id}"))
        if isinstance(book, dict) and book.get("condition_id"):
            if book.get("closed"):
                watch.mark("clob_closed", now)
            if any(token.get("winner") for token in book.get("tokens", [])):
                watch.mark("clob_winner_flag", now)

        if chain_seen_at is None:
            denominator, numerators = chain_payout(watch.condition_id)
            if denominator:
                chain_seen_at = now
                watch.mark("ctf_payout_denominator", now)
                watch.final_numerators = numerators
                watch.observations.append(
                    {"at": round(now, 3), "denominator": denominator, "numerators": numerators}
                )
                print(
                    f"    chain payout at +{now - watch.end_epoch:.1f}s numerators={numerators}",
                    flush=True,
                )
            else:
                watch.observations.append({"at": round(now, 3), "denominator": None})

        time.sleep(POLL_SECONDS)

    if "ctf_payout_denominator" not in watch.first_seen:
        watch.note = "chain did not report a payout inside the watch window"
    missing = sorted(wanted - set(watch.first_seen))
    if missing:
        # Recorded as not-observed-in-window, never as "agreed" or "arrived at the same time".
        watch.note = (watch.note + "; " if watch.note else "") + (
            f"not observed within the window: {', '.join(missing)}"
        )
    return watch


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", type=int, default=6)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    watches: list[Watch] = []
    while len(watches) < args.markets:
        now = int(time.time())
        t0 = (now // 300) * 300
        # Pick up the market currently running, provided there is time to arm before it ends.
        if t0 + 300 - now < WATCH_BEFORE_END:
            t0 += 300
        while time.time() < t0 + 300 - WATCH_BEFORE_END:
            time.sleep(2)
        watches.append(watch_one(t0))
        print(f"  [{len(watches)}/{args.markets}] {watches[-1].summary()}", flush=True)

    args.out.write_text(
        json.dumps(
            {
                "kind": "P10A_O11_LIVE_TIMING",
                "provenance": "REAL_PUBLIC_MARKET_DATA",
                "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "poll_seconds": POLL_SECONDS,
                "rpc": RPC,
                "ctf_address": CTF_ADDRESS,
                "note": (
                    "One synchronized local clock (time.time). Offsets are seconds after the "
                    "market's scheduled end, T0 + 300."
                ),
                "watches": [watch.summary() for watch in watches],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(f"wrote {len(watches)} watches to {args.out}", flush=True)


if __name__ == "__main__":
    main()
