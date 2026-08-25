"""Produce the P6 read-only live capture artefact.

Not part of the ``maker5m`` package: a one-shot operator script that composes the public
read-only adapters, records one full 5-minute market, and verifies the result with the P5
replay engine.

Strictly read-only. No credential, no key, no signing, no order endpoint.

Reproduce with::

    .venv/bin/python tools/capture_market.py <output-directory>

It waits for the next suitable ``T0``, so a run takes roughly nine minutes: pre-arm, then the
300-second market, then verification.
"""

import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path

from maker5m.feeds.capture import capture_market
from maker5m.feeds.discovery import discover_market, slug_for
from maker5m.market.timebase import TimestampNs
from maker5m.replay import decode_journal, encode_journal, verify_replay
from maker5m.strategy import BaseLot, default_config

MIN_LEAD_SECONDS = 45
"""Enough lead time to discover, pre-arm, and connect before T0."""


async def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    t0 = ((now // 300) + 1) * 300
    if t0 - now < MIN_LEAD_SECONDS:
        t0 += 300
    slug = slug_for(t0)
    print(f"[{time.strftime('%H:%M:%S')}] target {slug} (T0 in {t0 - now}s)", flush=True)

    market = discover_market(slug)
    # Pre-arm: resolve the FOLLOWING market during this one, before it begins.
    following = discover_market(slug_for(t0 + 300))
    prearm_ready = TimestampNs(time.time_ns())
    slack_s = (following.definition.t0 - prearm_ready) / 1e9
    print(
        f"[{time.strftime('%H:%M:%S')}] pre-armed {following.definition.slug}; "
        f"slack={slack_s:.1f}s",
        flush=True,
    )

    result = await capture_market(
        market,
        default_config(BaseLot.of(15)),
        next_market=following,
        prearm_ready_ns=prearm_ready,
        description=f"P6 read-only public capture of {slug}",
    )

    raw = encode_journal(result.journal)
    (out / f"{slug}.journal.ndjson").write_bytes(raw)

    outcome = verify_replay(decode_journal(raw))
    kinds: dict[str, int] = {}
    for step in result.journal.steps:
        name = type(step.event).__name__
        kinds[name] = kinds.get(name, 0) + 1

    manifest = {
        "slug": slug,
        "market_id": market.definition.market_id,
        "t0_ns": market.definition.t0,
        "end_ns": market.definition.market_end,
        "provenance": result.journal.header.provenance.value,
        "steps": result.journal.step_count,
        "journal_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "event_kinds": kinds,
        "phases": sorted({s.decision.telemetry.phase.value for s in result.journal.steps}),
        "counters": result.counters.summary(),
        "precision": result.precision,
        "clock_health": result.clock_health.summary(),
        "venue_tick_changes": result.venue_tick_changes,
        "prearm": {
            "slug": result.next_market_slug,
            "ready_ns": result.prearm_ready_ns,
            "next_t0_ns": result.next_market_t0_ns,
            "slack_ns": result.prearm_slack_ns,
        },
        "replay_verified": outcome.verified,
        "replay_steps": outcome.step_count,
        "final_state_matches": outcome.final_state == result.final_state,
        "byte_roundtrip_identical": encode_journal(decode_journal(raw)) == raw,
    }
    (out / f"{slug}.manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(json.dumps(manifest, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
