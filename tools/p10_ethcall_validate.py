"""Validate the redemption calldata against the real deployed Conditional Tokens contract.

``eth_call`` only. No transaction is signed, broadcast, or paid for, and the sender is an address
with no balance and no key. What this proves is narrow and worth stating precisely: that the
selector, the CTF address, the pUSD collateral address, the zero parent collection id, the
condition id, the index-set bit masks, and the ABI encoding are all **accepted by the contract**.

What it does **not** prove: that any collateral moved. Nobody holds a position, so a successful
call redeems nothing. Calling this "redeemed" would be the overclaim the whole phase is written
to avoid.

Read-only. LIVE_TRADING_ENABLED is False and no redemption transport exists in this build.
"""

import argparse
import json
import time
import urllib.request
from pathlib import Path
from typing import Any, Final

from maker5m.settlement import CTF_ADDRESS, PARENT_COLLECTION_ID, PUSD_ADDRESS
from maker5m.settlement.contracts import binary_index_sets, encode_redeem_positions
from maker5m.settlement.reader import DEFAULT_RPC_ENDPOINTS, USER_AGENT

ZERO_SENDER: Final = "0x0000000000000000000000000000000000000001"
"""An address nobody controls, holding nothing. Redeeming zero balance is a no-op that still
exercises every decode the contract performs on the calldata."""


def rpc(url: str, method: str, params: list[Any]) -> dict[str, Any]:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    request = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        payload: dict[str, Any] = json.loads(response.read())
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--markets", type=int, default=8)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    payload = json.loads(args.corpus.read_text(encoding="utf-8"))
    url = dict(DEFAULT_RPC_ENDPOINTS)["publicnode"]

    def nums(market: dict[str, Any]) -> list[int]:
        numerators: list[int] = market["chain"]["publicnode"]["payout_numerators"]
        return numerators

    # Deliberately mixed: an UP-resolved and a DOWN-resolved condition prove the encoding does
    # not accidentally depend on which slot won.
    up = [m for m in payload["markets"] if m["chain"]["publicnode"]["payout_numerators"] == [1, 0]]
    down = [
        m for m in payload["markets"] if m["chain"]["publicnode"]["payout_numerators"] == [0, 1]
    ]
    half = max(1, args.markets // 2)
    chosen = up[:half] + down[:half]

    rows: list[dict[str, Any]] = []
    for market in chosen:
        condition_id = market["gamma"]["condition_id"]
        calldata = encode_redeem_positions(
            collateral_token=PUSD_ADDRESS,
            parent_collection_id=PARENT_COLLECTION_ID,
            condition_id=condition_id,
            index_sets=binary_index_sets(),
        )
        response = rpc(
            url,
            "eth_call",
            [{"from": ZERO_SENDER, "to": CTF_ADDRESS, "data": calldata}, "latest"],
        )
        accepted = "result" in response
        rows.append(
            {
                "slug": market["gamma"]["slug"],
                "condition_id": condition_id,
                "payout_numerators": nums(market),
                "calldata_bytes": (len(calldata) - 2) // 2,
                "accepted_by_contract": accepted,
                "result": response.get("result"),
                "error": response.get("error"),
            }
        )
        print(
            f"  {market['gamma']['slug']}  nums={nums(market)}"
            f"  accepted={accepted}  {response.get('error') or ''}",
            flush=True,
        )
        time.sleep(1.0)

    report = {
        "kind": "REAL_CHAIN_ETH_CALL_SIMULATION",
        "provenance": "REAL_PUBLIC_MARKET_DATA",
        "note": (
            "eth_call against the real deployed CTF. Proves the calldata encoding is accepted. "
            "Does NOT prove collateral moved: the sender holds no position and no transaction "
            "was sent. This is not a redemption."
        ),
        "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rpc": url,
        "ctf_address": CTF_ADDRESS,
        "collateral_token": PUSD_ADDRESS,
        "parent_collection_id": PARENT_COLLECTION_ID,
        "index_sets": list(binary_index_sets()),
        "sender": ZERO_SENDER,
        "markets": len(rows),
        "accepted": sum(1 for row in rows if row["accepted_by_contract"]),
        "rows": rows,
    }
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(f"\naccepted {report['accepted']}/{report['markets']} -> {args.out}")


if __name__ == "__main__":
    main()
