"""Aggregate the P13 corpus into the evidence a phase gate can be read from.

**REAL MARKET DATA.** Every number here comes from corpus entries and their latency artifacts,
each written by the supervisor from one real `btc-updown-5m` market: real Polymarket books, real
Binance spot, real Polygon settlement reads, and shadow execution throughout.

What is exhaustive and what is sampled, stated once so no figure below has to be guessed at:

* **classification is exhaustive** — every side of every decision, so the L3 denominator is the
  market rather than a sample of it;
* **actions are exhaustive** — the same denominator;
* **latency is sampled**, under P8's accepted policy, and the artifacts hold every raw sample so
  the merged quantiles here are exact over the samples that were taken;
* **queue position is a `SHADOW_ESTIMATE`** — modelled, never a venue queue position, because no
  order was ever sent.

What this tool does not do is decide anything. It counts, it distributes, and it says which
markets were eligible and why the others were not. Choosing a queue threshold, a stale bar or a
quote centre is P15's work, on this data.
"""

from __future__ import annotations

import argparse
import json
import lzma
from collections import Counter
from pathlib import Path
from typing import Any

from maker5m.bot import AttemptIndex, AttemptLedger, CorpusIndex, qualify_all, read_latency
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


def merged_latency(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Exact quantiles over the concatenated live samples of every eligible market.

    Concatenated, not averaged. A corpus p99 taken as the p99 of per-market p99s is not the p99
    of anything — it discards the tail it exists to describe. The artifacts keep every raw
    sample precisely so this can be done properly.

    A market whose artifact is missing, unreadable or hashes differently from what its row
    recorded contributes nothing and is named in `refused`.
    """
    series: dict[str, list[int]] = {}
    hot_path: list[int] = []
    per_market_hot_max: dict[str, int] = {}
    used: list[str] = []
    refused: list[dict[str, str]] = []
    for entry in entries:
        artifact = entry.get("latency_artifact") or {}
        path = artifact.get("path")
        slug = str(entry.get("slug"))
        if not path:
            refused.append({"slug": slug, "reason": "no latency artifact recorded"})
            continue
        try:
            payload = read_latency(
                Path(str(path)),
                expected_sha256=artifact.get("sha256"),
                # Identity, not just integrity. A hash proves the bytes are the bytes that were
                # written; it says nothing about which market they were written for, and a row
                # pointing at another market's artifact would otherwise merge its samples in.
                expected_identity={
                    "slug": entry.get("slug"),
                    "market_id": entry.get("market_id"),
                    "condition_id": entry.get("condition_id"),
                    "t0_ns": entry.get("t0_ns"),
                    "source_revision": entry.get("source_revision"),
                    "source_tree_sha": entry.get("source_tree_sha"),
                    "config_sha256": entry.get("config_sha256"),
                    "epoch": entry.get("epoch"),
                    "run_mode": entry.get("run_mode"),
                    "sample_every": entry.get("sample_every"),
                },
            )
        except (OSError, ValueError, lzma.LZMAError) as error:
            refused.append({"slug": slug, "reason": f"{type(error).__name__}: {error}"})
            continue
        for name, values in (payload.get("series_ns") or {}).items():
            series.setdefault(str(name), []).extend(int(value) for value in values)
        observed = [int(value) for value in payload.get("hot_path_observe_ns") or ()]
        hot_path.extend(observed)
        if observed:
            per_market_hot_max[slug] = max(observed)
        used.append(slug)

    return {
        "sampling": "SAMPLED — P8's accepted policy; every sample it took is here",
        "markets_merged": len(used),
        "markets_refused": refused,
        "all_markets_merged": not refused,
        "series_ns": {name: _quantiles(values) for name, values in sorted(series.items())},
        "hot_path_observe_ns": _quantiles(hot_path),
        "hot_path_observe_max_by_market": dict(sorted(per_market_hot_max.items())),
        "note": (
            "Merged from the raw live samples of every eligible market. CLOB and spot are kept "
            "apart: they arrive at different rates through different sockets, and a combined "
            "figure answers a question nobody asked."
        ),
    }


def _quantiles(values: list[int]) -> dict[str, int | None]:
    if not values:
        return {"n": 0, "p50": None, "p95": None, "p99": None, "max": None}
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50": quantile(ordered, 0.50),
        "p95": quantile(ordered, 0.95),
        "p99": quantile(ordered, 0.99),
        "max": ordered[-1],
    }


def accounting(
    rows: list[dict[str, Any]],
    joined: Any,
    ledger: Any,
) -> dict[str, Any]:
    """Audit **every** row for the epoch, not only the ones that passed.

    Auditing the eligible list is auditing the answer. A row that failed is exactly the row worth
    accounting for, so the join runs over all of them and reports what each one was missing.
    """
    attempts = AttemptIndex.build(ledger.events())
    counts = attempts.counts()
    without_start = [
        str(row.get("slug"))
        for row in rows
        if row.get("attempt_id") and str(row.get("attempt_id")) not in attempts.starts
    ]
    no_attempt = [str(row.get("slug")) for row in rows if not row.get("attempt_id")]
    without_terminal = [
        str(row.get("slug"))
        for row in rows
        if row.get("attempt_id")
        and str(row.get("attempt_id")) in attempts.starts
        and not attempts.terminals.get(str(row.get("attempt_id")))
    ]
    identity_problems = {
        f"{judgement.slug}#{judgement.row_index}": list(judgement.reasons)
        for judgement in joined.judgements
        if not judgement.qualifies
    }
    return {
        **counts,
        "duplicate_result_attempts": len(joined.duplicate_result_attempts),
        "duplicate_result_attempt_detail": joined.duplicate_result_attempts,
        "duplicate_market_slugs": joined.duplicate_market_slugs,
        "unique_result_attempt_ids": len(
            {str(row.get("attempt_id")) for row in rows if row.get("attempt_id")}
        ),
        "unique_market_slugs": len({str(row.get("slug")) for row in rows}),
        "duplicate_start_detail": attempts.duplicate_starts(),
        "duplicate_terminal_detail": attempts.duplicates(),
        "ledger_consistent": not attempts.duplicate_starts() and not attempts.duplicates(),
        "corpus_rows": len(rows),
        "qualifying_rows": joined.count,
        "qualifying_markets": len(joined.slugs),
        "rows_without_attempt_id": no_attempt,
        "rows_without_start": without_start,
        "rows_without_finished_terminal": without_terminal,
        "rows_refused_with_reasons": identity_problems,
        "every_row_joins_one_attempt": not (no_attempt or without_start or without_terminal),
        "one_result_per_attempt_and_market": joined.consistent,
        "ledger": ledger.summary(),
        "note": (
            "Every row for this epoch, judged by the same rule the collector counts with. A "
            "market with a start and no terminal record is one the collector was in the middle "
            "of; it counts toward nothing and its artifacts are inventoried, not deleted."
        ),
    }


def report(
    index: CorpusIndex,
    *,
    epoch: str | None = None,
    ledger: Any = None,
    identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entries = index.entries()
    if epoch is not None:
        entries = [e for e in entries if e.get("epoch") == epoch]

    # The same join the collector counts with. An epoch's own rows carry its identity, so the
    # expectation is taken from them rather than invented here; a corpus that disagrees with
    # itself about which build made it fails the join and says so.
    reference = identity or (entries[0] if entries else {})
    joined = (
        qualify_all(
            entries,
            ledger.events(),
            epoch=str(reference.get("epoch")),
            config_sha256=str(reference.get("config_sha256")),
            source_revision=str(reference.get("source_revision")),
            source_tree_sha=reference.get("source_tree_sha"),
            run_mode=str(reference.get("run_mode", "ACCEPTANCE_CLEAN")),
        )
        if ledger is not None
        else None
    )
    judgements = list(joined.judgements) if joined is not None else []
    # Paired by position, not by slug. Two rows can name one market, and selecting by slug would
    # let a refused row's counts into the aggregates on a qualifying neighbour's ticket — which
    # is precisely the contamination the uniqueness rule exists to catch.
    eligible = (
        [entry for entry, judgement in zip(entries, judgements, strict=True) if judgement.qualifies]
        if joined is not None
        else [
            e
            for e in entries
            if e.get("evidence_eligible") is True and e.get("verification_status") == "COMPLETE"
        ]
    )

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
    discovery_leads: list[float] = []
    clob_leads: list[float] = []
    spot_leads: list[float] = []
    commands = 0
    rss_start: list[int] = []
    rss_end: list[int] = []
    threads_end: list[int] = []
    fds_end: list[int] = []
    live_sessions: list[int] = []
    gc_full_pause_ns: list[int] = []

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
        if prearm.get("feed_ready_before_t0"):
            prearm_ready += 1
        leads: tuple[tuple[str, list[float]], ...] = (
            ("discovery_lead_seconds", discovery_leads),
            ("clob_lead_seconds", clob_leads),
            ("spot_lead_seconds", spot_leads),
            ("feed_ready_lead_seconds", prearm_leads),
        )
        for key, target in leads:
            if isinstance(prearm.get(key), int | float):
                target.append(float(prearm[key]))
        settlement = entry.get("settlement")
        settlements[str(None if settlement is None else settlement.get("state"))] += 1
        if settlement is not None and settlement.get("winning_outcome"):
            winners[str(settlement["winning_outcome"])] += 1
        commands += len(entry.get("commands") or ())
        resources = entry.get("resources") or {}
        for name in ("start", "post_release"):
            sample = resources.get(name) or {}
            rss = sample.get("rss_bytes")
            if isinstance(rss, int):
                (rss_start if name == "start" else rss_end).append(rss)
        # Post-release, always: a number taken while the market's own graph is still held says
        # nothing about whether releasing it works.
        released = resources.get("post_release") or {}
        if isinstance(released.get("threads"), int):
            threads_end.append(int(released["threads"]))
        if isinstance(released.get("open_fds"), int):
            fds_end.append(int(released["open_fds"]))
        if isinstance(released.get("live_sessions"), int):
            live_sessions.append(int(released["live_sessions"]))
        gc_summary = resources.get("gc") or {}
        pause = (gc_summary.get("max_pause_ns") or {}).get("2")
        if isinstance(pause, int):
            gc_full_pause_ns.append(pause)

    slugs = [str(e.get("slug")) for e in eligible]
    return {
        "kind": "P13_CORPUS_REPORT",
        "provenance": "REAL_PUBLIC_MARKET_DATA",
        "epoch": epoch,
        "attempted": len(entries),
        "accounting": None
        if ledger is None or joined is None
        else accounting(entries, joined, ledger),
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
        "live_latency": merged_latency(eligible),
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
            "note": (
                "Discovery readiness is when the market's metadata resolved; feed readiness is "
                "when a usable book and a real BTC price both existed. Only the second one means "
                "a market was warm, and only it gates eligibility."
            ),
            "feed_ready_before_t0": prearm_ready,
            "of": len(eligible),
            "discovery_lead_seconds": _spread(discovery_leads),
            "clob_book_ready_lead_seconds": _spread(clob_leads),
            "spot_first_valid_lead_seconds": _spread(spot_leads),
            "feed_ready_lead_seconds": _spread(prearm_leads),
        },
        "settlement_states": dict(sorted(settlements.items())),
        "winners": dict(sorted(winners.items())),
        "resources": {
            "note": (
                "Every figure but the first is taken **after** the market was released. RSS is "
                "the operating system's view and glibc does not return freed arenas promptly, "
                "so `live_sessions` is the number that says whether release works."
            ),
            "rss_bytes_first_market_start": rss_start[0] if rss_start else None,
            "rss_bytes_last_post_release": rss_end[-1] if rss_end else None,
            "rss_bytes_post_release_spread": _spread([float(v) for v in rss_end]),
            "threads_post_release_spread": _spread([float(v) for v in threads_end]),
            "open_fds_post_release_spread": _spread([float(v) for v in fds_end]),
            "live_sessions_post_release": live_sessions,
            "gc_full_collection_max_pause_ns": _spread([float(v) for v in gc_full_pause_ns]),
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
    ledger_path = args.corpus.with_name("attempts.jsonl")
    evidence = report(
        CorpusIndex(path=args.corpus),
        epoch=args.epoch,
        ledger=AttemptLedger(path=ledger_path) if ledger_path.exists() else None,
    )
    if args.out is not None:
        args.out.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(evidence, indent=2, sort_keys=True))
    if args.out is not None:
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
