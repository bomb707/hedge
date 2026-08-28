"""Compact evidence from a resource run. The numbers, without the gigabytes they came from.

A collection epoch's corpus rows carry eleven memory checkpoints and a generation-2 attribution
each. That is the material the resource gate is decided on, and it is small — a few hundred
kilobytes for fifty markets — while the journals and stores it describes are tens of gigabytes
and stay outside git, referenced by hash.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from tools.p13_resource_report import gate, load, per_market, steps


def extract(corpus: Path, *, epoch: str | None, note: str) -> dict[str, Any]:
    rows = [row for row in load(corpus) if epoch is None or row.get("epoch") == epoch]
    rows.sort(key=lambda row: int(row.get("t0_ns") or 0))
    markets = [per_market(row) for row in rows]
    first = rows[0] if rows else {}
    return {
        "note": note,
        "epoch": epoch,
        "corpus": str(corpus),
        "markets": len(markets),
        "identity": {
            "source_revision": first.get("source_revision"),
            "source_tree_sha": first.get("source_tree_sha"),
            "config_sha256": first.get("config_sha256"),
            "run_mode": first.get("run_mode"),
            "working_tree_clean": first.get("working_tree_clean"),
            "live_trading_enabled": first.get("live_trading_enabled"),
        },
        "integrity": {
            "complete": sum(1 for m in markets if m["status"] == "COMPLETE"),
            "replay_exact": sum(1 for m in markets if m["replay"] == "EXACT"),
            "eligible": sum(1 for m in markets if m["eligible"]),
            "journal_writer_disagreements": [
                m["slug"] for m in markets if m["journal_writer_agrees"] is False
            ],
        },
        "gate_post_release": gate(markets),
        "gate_settled": gate(markets, metric="settled_rss"),
        "rss_steps": steps(markets),
        "per_market": markets,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--epoch", type=str, default=None)
    parser.add_argument("--note", type=str, default="")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = extract(args.corpus, epoch=args.epoch, note=args.note)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2), "utf-8")
    print(f"{args.out} {args.out.stat().st_size} bytes, {result['markets']} markets")


if __name__ == "__main__":
    main()
