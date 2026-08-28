"""Does this process still grow? The predeclared test, applied to a real collection run.

P13's accepted corpus failed its resource gate on a number: post-release resident memory rose
from 36 MB to 4,262 MB over 202 markets, an all-run slope of +10.26 MB per market and +31.46 over
the last fifty, with no plateau anywhere. This tool applies the test that was declared *before*
the validation run was collected, so the threshold cannot be chosen to fit the result:

* the first ten markets are warm-up and are excluded;
* over the rest, ordinary least squares gives the slope of post-release RSS against market index;
* the 95% confidence interval for that slope must contain zero — no statistically supported
  continuing trend;
* the point slope must be no more than a tenth of the failure slope: **+1.026 MB per market**;
* later windows must not resume growth;
* sessions, file descriptors, threads, tasks, lifecycles and the cold backlog stay bounded.

It also reports the per-market generation-2 attribution that the corpus could not produce, and
the memory checkpoints that say *where* in a market's cold path a step in RSS appears.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

MB = 1024 * 1024

WARMUP_MARKETS = 10
"""Predeclared. The first ten markets of a fresh process are its warm-up, not its trend."""

FAILURE_SLOPE_MB = 10.26
"""The measured all-run slope of the failing corpus, in MB per market. The anchor, not a choice."""

SLOPE_CEILING_MB = FAILURE_SLOPE_MB / 10
"""+1.026 MB/market. A tenth of the measured failure. Declared before the run, not after it."""


# -- statistics ----------------------------------------------------------------------------------


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function. Lentz's method."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-16:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta ``I_x(a, b)``."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _t_cdf(t: float, df: float) -> float:
    """Student's t CDF. Exact enough that the interval is not an approximation of an interval."""
    x = df / (df + t * t)
    tail = 0.5 * _betai(df / 2.0, 0.5, x)
    return 1.0 - tail if t > 0 else tail


def t_critical(df: float, confidence: float = 0.95) -> float:
    """Two-sided critical value, by bisection on the CDF. No third-party statistics package."""
    target = 1.0 - (1.0 - confidence) / 2.0
    low, high = 0.0, 100.0
    for _ in range(200):
        middle = (low + high) / 2.0
        if _t_cdf(middle, df) < target:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


@dataclass(frozen=True, slots=True)
class Trend:
    """Least squares on (index, value), with the interval that decides the gate."""

    n: int
    slope: float
    intercept: float
    slope_stderr: float
    ci_low: float
    ci_high: float
    r_squared: float

    def summary(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "slope_per_market": self.slope,
            "intercept": self.intercept,
            "slope_stderr": self.slope_stderr,
            "ci95_low": self.ci_low,
            "ci95_high": self.ci_high,
            "ci95_includes_zero": self.ci_low <= 0.0 <= self.ci_high,
            "r_squared": self.r_squared,
        }


def trend(values: list[float]) -> Trend | None:
    """Slope of `values` against position, with a 95% interval. ``None`` under three points."""
    n = len(values)
    if n < 3:
        return None
    xs = [float(index) for index in range(n)]
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0.0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values, strict=True))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    residuals = [y - (intercept + slope * x) for x, y in zip(xs, values, strict=True)]
    sse = sum(r * r for r in residuals)
    sst = sum((y - mean_y) ** 2 for y in values)
    df = n - 2
    stderr = math.sqrt(sse / df / sxx) if df > 0 and sse > 0 else 0.0
    critical = t_critical(df) if df > 0 else 0.0
    return Trend(
        n=n,
        slope=slope,
        intercept=intercept,
        slope_stderr=stderr,
        ci_low=slope - critical * stderr,
        ci_high=slope + critical * stderr,
        r_squared=0.0 if sst == 0.0 else 1.0 - sse / sst,
    )


def median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


# -- the run ---------------------------------------------------------------------------------


def _checkpoint(row: dict[str, Any], label: str) -> dict[str, Any] | None:
    for entry in (row.get("resources") or {}).get("checkpoints") or ():
        if entry.get("label") == label:
            return dict(entry)
    return None


def _post_release_rss(row: dict[str, Any]) -> int | None:
    """The gate's metric: resident memory once the market is gone. The baseline's own field."""
    resources = row.get("resources") or {}
    post = resources.get("post_release") or {}
    value = post.get("rss_bytes")
    return int(value) if isinstance(value, int) else None


def _settled_rss(row: dict[str, Any]) -> int | None:
    settled = _checkpoint(row, "post_release_settled")
    if settled is None:
        return None
    value = settled.get("rss")
    return int(value) if isinstance(value, int) else None


def load(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def per_market(row: dict[str, Any]) -> dict[str, Any]:
    resources = row.get("resources") or {}
    checkpoints = resources.get("checkpoints") or []
    gc_window = resources.get("gc_window") or {}
    journal = row.get("journal") or {}
    quality = row.get("quality") or {}
    return {
        "slug": row.get("slug"),
        "t0_ns": row.get("t0_ns"),
        "status": row.get("verification_status"),
        "replay": (row.get("replay") or {}).get("status"),
        "eligible": row.get("evidence_eligible"),
        "journal_bytes": journal.get("bytes"),
        "journal_writer_agrees": journal.get("writer_agrees"),
        "decisions": quality.get("decisions") or row.get("decisions"),
        "post_release_rss": _post_release_rss(row),
        "settled_rss": _settled_rss(row),
        "checkpoints": [
            {
                "label": entry.get("label"),
                "rss": entry.get("rss"),
                "rss_anon": entry.get("rss_anon"),
                "rss_file": entry.get("rss_file"),
                "uordblks": entry.get("uordblks"),
                "fordblks": entry.get("fordblks"),
                "arena": entry.get("arena"),
                "hblkhd": entry.get("hblkhd"),
                "allocated_blocks": entry.get("allocated_blocks"),
                "tracked_objects": entry.get("tracked_objects"),
                "threads": entry.get("threads"),
            }
            for entry in checkpoints
        ],
        "gc": {
            "collections": gc_window.get("collections"),
            "gen2_total_pause_ns": (gc_window.get("total_pause_ns") or {}).get("2"),
            "gen2_longest_pause_ns": (gc_window.get("longest_pause_ns") or {}).get("2"),
            "gen2_events": [
                event for event in (gc_window.get("events") or ()) if event.get("generation") == 2
            ],
            "dropped_events": gc_window.get("dropped_events"),
        },
        "hot_path_max_ns": (row.get("latency") or {}).get("hot_path", {}).get("max"),
        "live_sessions": (resources.get("post_release") or {}).get("live_sessions"),
        "open_fds": (resources.get("post_release") or {}).get("open_fds"),
        "threads": (resources.get("post_release") or {}).get("threads"),
        "pending_tasks": (resources.get("post_release") or {}).get("pending_tasks"),
        "cold_backlog": resources.get("cold_backlog"),
        "market_lifecycles": resources.get("market_lifecycles"),
    }


def steps(markets: list[dict[str, Any]]) -> dict[str, Any]:
    """Where in a market's cold path resident memory changes, averaged over the run.

    A checkpoint's *delta* is what it costs; the answer to "where does the step appear" is the
    label whose delta does not come back.
    """
    deltas: dict[str, list[float]] = {}
    for market in markets:
        points = market["checkpoints"]
        for before, after in pairwise(points):
            if before.get("rss") is None or after.get("rss") is None:
                continue
            key = f"{before['label']} -> {after['label']}"
            deltas.setdefault(key, []).append((after["rss"] - before["rss"]) / MB)
    return {
        key: {
            "n": len(values),
            "median_mb": median(values),
            "mean_mb": sum(values) / len(values),
            "total_mb": sum(values),
        }
        for key, values in deltas.items()
    }


def gate(markets: list[dict[str, Any]], *, metric: str = "post_release_rss") -> dict[str, Any]:
    """The predeclared test. Nothing here is decided after seeing the numbers."""
    series = [(market[metric] or 0) / MB for market in markets if market.get(metric) is not None]
    after_warmup = series[WARMUP_MARKETS:]
    fitted = trend(after_warmup)
    windows: dict[str, float | None] = {}
    if after_warmup:
        quarter = max(1, len(after_warmup) // 4)
        for index in range(4):
            chunk = after_warmup[index * quarter : (index + 1) * quarter]
            windows[f"q{index + 1}_median_mb"] = median(chunk)
    all_run = trend(series)
    bounded = {
        "live_sessions_max": max(
            (m["live_sessions"] for m in markets if m["live_sessions"] is not None), default=None
        ),
        "open_fds_max": max(
            (m["open_fds"] for m in markets if m["open_fds"] is not None), default=None
        ),
        "threads_max": max(
            (m["threads"] for m in markets if m["threads"] is not None), default=None
        ),
        "pending_tasks_max": max(
            (m["pending_tasks"] for m in markets if m["pending_tasks"] is not None), default=None
        ),
        "cold_backlog_max": max(
            (m["cold_backlog"] for m in markets if m["cold_backlog"] is not None), default=None
        ),
        "lifecycles_max": max(
            (m["market_lifecycles"] for m in markets if m["market_lifecycles"] is not None),
            default=None,
        ),
    }
    passes_slope = fitted is not None and fitted.slope <= SLOPE_CEILING_MB
    passes_ci = fitted is not None and fitted.ci_low <= 0.0 <= fitted.ci_high
    return {
        "metric": metric,
        "predeclared": {
            "warmup_markets": WARMUP_MARKETS,
            "failure_slope_mb_per_market": FAILURE_SLOPE_MB,
            "slope_ceiling_mb_per_market": SLOPE_CEILING_MB,
            "ci95_must_include_zero": True,
        },
        "n_markets": len(series),
        "first_mb": series[0] if series else None,
        "last_mb": series[-1] if series else None,
        "min_mb": min(series) if series else None,
        "max_mb": max(series) if series else None,
        "all_run_trend": None if all_run is None else all_run.summary(),
        "after_warmup_trend": None if fitted is None else fitted.summary(),
        "after_warmup_windows": windows,
        "bounded": bounded,
        "slope_within_ceiling": passes_slope,
        "ci95_includes_zero": passes_ci,
        "resource_stability": "PASSED" if (passes_slope and passes_ci) else "NOT PASSED",
    }


def report(path: Path, *, epoch: str | None = None) -> dict[str, Any]:
    rows = [row for row in load(path) if epoch is None or row.get("epoch") == epoch]
    rows.sort(key=lambda row: int(row.get("t0_ns") or 0))
    markets = [per_market(row) for row in rows]
    gen2 = [
        event
        for market in markets
        for event in market["gc"]["gen2_events"]
        if isinstance(event.get("duration_ns"), int)
    ]
    durations = sorted(event["duration_ns"] for event in gen2)
    return {
        "corpus": str(path),
        "epoch": epoch,
        "markets": len(markets),
        "gate": gate(markets),
        "gate_settled_metric": gate(markets, metric="settled_rss"),
        "rss_steps": steps(markets),
        "gc": {
            "gen2_events_attributed": len(gen2),
            "markets_with_a_full_collection": sum(
                1 for market in markets if market["gc"]["gen2_events"]
            ),
            "gen2_pause_ns": {
                "n": len(durations),
                "p50": durations[len(durations) // 2] if durations else None,
                "max": durations[-1] if durations else None,
                "total": sum(durations),
            },
            "hot_path_max_ns": max(
                (m["hot_path_max_ns"] for m in markets if m["hot_path_max_ns"] is not None),
                default=None,
            ),
        },
        "integrity": {
            "all_complete": all(market["status"] == "COMPLETE" for market in markets),
            "all_replay_exact": all(market["replay"] == "EXACT" for market in markets),
            "all_eligible": all(bool(market["eligible"]) for market in markets),
            "journal_writer_agrees": [
                market["slug"] for market in markets if market["journal_writer_agrees"] is False
            ],
        },
        "per_market": markets,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--epoch", type=str, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    result = report(args.corpus, epoch=args.epoch)
    text = json.dumps(result, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, "utf-8")
    summary = {key: value for key, value in result.items() if key != "per_market"}
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
