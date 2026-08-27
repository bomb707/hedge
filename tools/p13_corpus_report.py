"""Aggregate the P13 corpus into the evidence a phase gate can be read from.

**REAL MARKET DATA.** Every number here is summed from corpus entries, each of which was written
by the supervisor from one real `btc-updown-5m` market: real Polymarket books, real Binance spot,
real Polygon settlement reads, and shadow execution throughout. Nothing is modelled and nothing
is sampled — the point of a two-hundred-market corpus is that it does not need to be.

What this tool does not do is decide anything. It counts, it distributes, and it says which
markets were eligible and why the others were not. Choosing a queue threshold, a stale bar or a
quote centre is P15's work, on this data.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from maker5m.bot import CorpusIndex
from maker5m.bot.quality import QUALITY_LABELS, QUEUE_PROVENANCE
from maker5m.telemetry.metrics import quantile


def _merge(target: Counter[str], source: Any) -> None:
    if isinstance(source, dict):
        for key, value in source.items():
            if isinstance(value, int):
                target[str(key)] += value


def _nested(target: dict[str, Counter[str]], source: Any) -> None:
    if not isinstance(source, dict):
        return
    for key, row in source.items():
        _merge(target.setdefault(str(key), Counter()), row)


def _fractions(counts: Counter[str]) -> dict[str, float | None]:
    total = sum(counts.values())
    return {
        label: (None if total == 0 else counts.get(label, 0) / total) for label in QUALITY_LABELS
    }


def report(index: CorpusIndex, *, epoch: str | None = None) -> dict[str, Any]:
    entries = index.entries()
    if epoch is not None:
        entries = [e for e in entries if e.get("epoch") == epoch]
    eligible = [
        e
        for e in entries
        if e.get("evidence_eligible") is True and e.get("verification_status") == "COMPLETE"
    ]

    status = Counter(str(e.get("verification_status")) for e in entries)
    replay_status = Counter(str((e.get("replay") or {}).get("status")) for e in entries)
    configs = Counter(str(e.get("config_sha256")) for e in eligible)
    revisions = Counter(str(e.get("source_revision")) for e in eligible)

    quality = Counter[str]()
    reasons = Counter[str]()
    by_outcome: dict[str, Counter[str]] = {}
    by_phase: dict[str, Counter[str]] = {}
    by_bucket: dict[str, Counter[str]] = {}
    risk_states = Counter[str]()
    places = Counter[str]()
    actions = Counter[str]()
    settlements = Counter[str]()
    winners = Counter[str]()
    queue_p50: list[int] = []
    queue_p95: list[int] = []
    stale_fractions: list[float] = []
    at_front_fractions: list[float] = []
    decisions = 0
    exhaustive_markets = 0
    expected_sides = 0
    actual_sides = 0
    action_sides = 0
    risk_records = 0
    clob_messages = 0
    spot_messages = 0
    drops = 0
    gaps = 0
    sink_errors = 0
    prearm_ready = 0
    prearm_leads: list[float] = []
    commands = 0
    rss_start: list[int] = []
    rss_end: list[int] = []
    threads_end: list[int] = []
    fds_end: list[int] = []

    for entry in eligible:
        l3 = entry.get("quality_l3") or {}
        _merge(quality, l3.get("total"))
        _merge(reasons, l3.get("by_reason"))
        _nested(by_outcome, l3.get("by_outcome"))
        _nested(by_phase, l3.get("by_phase"))
        _nested(by_bucket, l3.get("by_time_bucket"))
        queue = l3.get("queue_ahead_shadow_estimate") or {}
        if isinstance(queue.get("p50"), int):
            queue_p50.append(int(queue["p50"]))
        if isinstance(queue.get("p95"), int):
            queue_p95.append(int(queue["p95"]))
        fractions = l3.get("fractions") or {}
        if isinstance(fractions.get("STALE"), float):
            stale_fractions.append(float(fractions["STALE"]))
        if isinstance(fractions.get("AT_FRONT"), float):
            at_front_fractions.append(float(fractions["AT_FRONT"]))

        _merge(risk_states, entry.get("risk_states"))
        _merge(places, entry.get("places_by_risk_state"))
        # From the analyzer's own per-side counters, which P8 increments for both sides of every
        # observation. The first version read `worker.summary()["actions"]`, which does not
        # exist — `WorkerStats` counts rows, not reconciler decisions — so every action total in
        # the first corpus report was silently zero.
        _merge(actions, entry.get("action_counts"))
        classification = entry.get("classification") or {}
        if classification.get("classification_complete") is True:
            exhaustive_markets += 1
        expected_sides += int(classification.get("expected_classifications") or 0)
        actual_sides += int(classification.get("actual_classifications") or 0)
        action_sides += int(entry.get("action_total") or 0)
        decisions += int(entry.get("decisions") or 0)
        risk_records += int(entry.get("risk_records") or 0)
        counters = entry.get("feed_counters") or {}
        clob_messages += int(counters.get("clob_messages") or 0)
        spot_messages += int(counters.get("spot_messages") or 0)
        drops += int(entry.get("dropped_records") or 0)
        gaps += int(entry.get("sequence_gaps") or 0)
        sink_errors += int(entry.get("sink_errors") or 0)
        prearm = entry.get("prearm") or {}
        if prearm.get("ready_before_t0"):
            prearm_ready += 1
        if isinstance(prearm.get("lead_seconds"), int | float):
            prearm_leads.append(float(prearm["lead_seconds"]))
        settlement = entry.get("settlement")
        settlements[str(None if settlement is None else settlement.get("state"))] += 1
        if settlement is not None and settlement.get("winning_outcome"):
            winners[str(settlement["winning_outcome"])] += 1
        commands += len(entry.get("commands") or ())
        resources = entry.get("resources") or {}
        for name, target in (("start", rss_start), ("end", rss_end)):
            sample = resources.get(name) or {}
            if isinstance(sample.get("rss_bytes"), int):
                target.append(int(sample["rss_bytes"]))
        end = resources.get("end") or {}
        if isinstance(end.get("threads"), int):
            threads_end.append(int(end["threads"]))
        if isinstance(end.get("open_fds"), int):
            fds_end.append(int(end["open_fds"]))

    slugs = [str(e.get("slug")) for e in eligible]
    return {
        "kind": "P13_CORPUS_REPORT",
        "provenance": "REAL_PUBLIC_MARKET_DATA",
        "epoch": epoch,
        "attempted": len(entries),
        "status_counts": dict(sorted(status.items())),
        "evidence_eligible": len(eligible),
        "replay_status_counts": dict(sorted(replay_status.items())),
        "config_identities": dict(configs),
        "source_revisions": dict(revisions),
        "first_slug": slugs[0] if slugs else None,
        "last_slug": slugs[-1] if slugs else None,
        "totals": {
            "decisions": decisions,
            "risk_records": risk_records,
            "clob_messages": clob_messages,
            "spot_messages": spot_messages,
            "dropped_records": drops,
            "sequence_gaps": gaps,
            "sink_errors": sink_errors,
            "operator_commands": commands,
        },
        "l3": {
            "provenance": QUEUE_PROVENANCE,
            "note": (
                "P8's classification, aggregated over **every** side of every decision — the "
                "denominator is `side_opportunities.expected`. Latency remains sampled, and is "
                "described as sampled wherever it appears. Queue figures are a shadow model, "
                "never a venue queue position, and STALE is P6's verdict carried through P8."
            ),
            "total": {label: quality.get(label, 0) for label in QUALITY_LABELS},
            "fractions": _fractions(quality),
            "by_reason": dict(sorted(reasons.items())),
            "by_outcome": {k: dict(sorted(v.items())) for k, v in sorted(by_outcome.items())},
            "by_phase": {k: dict(sorted(v.items())) for k, v in sorted(by_phase.items())},
            "by_time_bucket": {k: dict(sorted(v.items())) for k, v in sorted(by_bucket.items())},
        },
        "per_market_distributions": {
            "stale_fraction": _spread(stale_fractions),
            "at_front_fraction": _spread(at_front_fractions),
            "queue_ahead_p50_shadow_estimate": _spread([float(v) for v in queue_p50]),
            "queue_ahead_p95_shadow_estimate": _spread([float(v) for v in queue_p95]),
        },
        "side_opportunities": {
            "rule": "two per decision observation, UP and DOWN, on every decision",
            "expected": expected_sides,
            "classified": actual_sides,
            "actions": action_sides,
            "markets_exhaustive": exhaustive_markets,
            "of_markets": len(eligible),
            "all_exhaustive": exhaustive_markets == len(eligible),
        },
        "action_counts": dict(sorted(actions.items())),
        "risk_states": dict(sorted(risk_states.items())),
        "places_by_risk_state": dict(sorted(places.items())),
        "prearm": {
            "ready_before_t0": prearm_ready,
            "of": len(eligible),
            "lead_seconds": _spread(prearm_leads),
        },
        "settlement_states": dict(sorted(settlements.items())),
        "winners": dict(sorted(winners.items())),
        "resources": {
            "rss_bytes_first_market_start": rss_start[0] if rss_start else None,
            "rss_bytes_last_market_end": rss_end[-1] if rss_end else None,
            "rss_bytes_end_spread": _spread([float(v) for v in rss_end]),
            "threads_end_spread": _spread([float(v) for v in threads_end]),
            "open_fds_end_spread": _spread([float(v) for v in fds_end]),
        },
        "safety": {
            "orders_sent": 0,
            "redemptions_sent": 0,
            "live_trading_enabled": False,
            "redemption_enabled": False,
        },
        "sampling": {
            "classification": "EXHAUSTIVE — every side of every decision",
            "latency": "SAMPLED — P8's accepted policy, unchanged",
            "queue_estimate": "SHADOW_ESTIMATE — modelled, never a venue queue position",
        },
        "not_claimed": (
            "This is a measurement, not a strategy conclusion. No OPEN item is closed by it, no "
            "queue or stale threshold is proposed, and no parameter was tuned to produce it. "
            "P15 owns the experiments."
        ),
    }


def _spread(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"n": 0, "p50": None, "p90": None, "min": None, "max": None}
    ordered = sorted(values)
    scaled = [round(value * 1_000_000) for value in ordered]
    return {
        "n": len(ordered),
        "min": ordered[0],
        "p50": quantile(scaled, 0.50) / 1_000_000,
        "p90": quantile(scaled, 0.90) / 1_000_000,
        "max": ordered[-1],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--epoch", type=str, default=None)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    evidence = report(CorpusIndex(path=args.corpus), epoch=args.epoch)
    if args.out is not None:
        args.out.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(evidence, indent=2, sort_keys=True))
    if args.out is not None:
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
