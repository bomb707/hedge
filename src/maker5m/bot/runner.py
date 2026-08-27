"""The paper bot's entry point. `python -m maker5m.bot.runner`.

There is deliberately no flag here that could send an order. Not `--live`, not `--trade`, not
`--send-orders`, and no environment variable: `LIVE_TRADING_ENABLED` is a module constant in
`maker5m.safety` and unlocking it is a P14 decision made in source, under the Canonical §35
checklist, not an argument someone can pass by accident at three in the morning.

What the arguments do control is where evidence goes, how many markets to collect, and when to
stop. Everything else is the frozen configuration the corpus records.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from maker5m.bot.config import PaperConfig, config_identity
from maker5m.bot.supervisor import Supervisor
from maker5m.safety import LIVE_TRADING_ENABLED
from maker5m.settlement import REDEMPTION_ENABLED

__all__ = ["build", "main"]


def build(root: Path, *, keep_raw_store: bool = False, epoch: str = "p13-corpus-1") -> PaperConfig:
    """One paper configuration rooted at a directory. Journals and stores live outside git."""
    return PaperConfig(
        evidence_dir=root / "markets",
        corpus_path=root / "corpus.jsonl",
        ui_dir=root / "ui",
        keep_raw_store=keep_raw_store,
        epoch=epoch,
    )


async def _run(config: PaperConfig, markets: int | None) -> Supervisor:
    supervisor = Supervisor(config=config, target_markets=markets)
    identity = config_identity(config)
    print(
        json.dumps(
            {
                "starting": "P13 live paper",
                "epoch": config.epoch,
                "config_sha256": identity["config_sha256"],
                "source_revision": identity["source_revision"],
                "live_trading_enabled": LIVE_TRADING_ENABLED,
                "redemption_enabled": REDEMPTION_ENABLED,
                "evidence_dir": str(config.evidence_dir),
                "corpus": str(config.corpus_path),
                "ui": str(config.ui_dir),
                "target_markets": markets,
                "already_complete": len(supervisor.corpus.completed_slugs()),
            },
            indent=2,
        ),
        flush=True,
    )
    await supervisor.run()
    return supervisor


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="where journals, stores and the corpus live")
    parser.add_argument("--markets", type=int, default=None, help="stop after this many COMPLETE")
    parser.add_argument("--epoch", type=str, default="p13-corpus-1")
    parser.add_argument(
        "--keep-raw-store",
        action="store_true",
        help="keep the raw SQLite file after its archive verifies (650 MB per market)",
    )
    args = parser.parse_args()
    if LIVE_TRADING_ENABLED or REDEMPTION_ENABLED:  # pragma: no cover - both are constant False
        raise SystemExit("refusing to run: this build is not a paper build")

    config = build(args.root, keep_raw_store=args.keep_raw_store, epoch=args.epoch)
    started = time.time()
    supervisor = asyncio.run(_run(config, args.markets))
    stats = supervisor.corpus.stats()
    print(
        json.dumps(
            {
                "finished": "P13 live paper",
                "elapsed_seconds": round(time.time() - started, 1),
                "attempted_this_process": supervisor.attempted,
                "completed_this_process": supervisor.completed,
                "skipped_already_collected": len(supervisor.skipped),
                "corpus": stats.summary(),
                "orders_sent": 0,
                "redemptions_sent": 0,
            },
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
