"""Does the operator dashboard's latency mean what P8 says it means? Checked on real cycles.

**REAL MARKET DATA.** The event stream is a complete P6 capture of a real `btc-updown-5m`
market, replayed through the production ingress path — `IngressMerger.submit`, with P8's own
deterministic sampler deciding which cycles are stage-stamped — so the timings are real readings
taken around real strategy calls on real book and spot events.

The comparison is exhaustive rather than illustrative. Every cycle P8's analyzer records a
latency for is compared, figure by figure, against what `latency_sample` would publish to the
UI for that same observation. P12C would fail this on `decide_ns`: it published
`decide_done - raw_receive`, which is receive-to-decide, under the name of P8's `decide_duration`.

Read-only. No venue, no credential, no order, no chain call.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from maker5m.execution import Executor, RecordingTransport, VenueAdapter
from maker5m.feeds import BookTracker, IngressMerger, MarketDataPipeline
from maker5m.feeds.venue import VenueMarketRules
from maker5m.market import MarketState
from maker5m.numeric import parse_price, parse_share
from maker5m.persistence.records import latency_sample
from maker5m.replay.codec import _dec_event, _dec_header
from maker5m.strategy import BaseLot, StrategyEngine, default_config
from maker5m.telemetry import (
    InstrumentedRun,
    ObservationBuffer,
    SamplingPolicy,
    TelemetryAnalyzer,
    perf_now_ns,
)
from maker5m.telemetry.observation import (
    NOT_CAPTURED,
    OBS_DECIDE_DONE_NS,
    OBS_RECONCILE_DONE_NS,
)


def load_events(journal_path: Path, limit: int) -> list[Any]:
    """The event half of each journal line. The strategy recomputes its own decisions."""
    events: list[Any] = []
    with journal_path.open("rb") as handle:
        handle.readline()  # header
        for line in handle:
            if len(events) >= limit:
                break
            events.append(_dec_event(json.loads(line)["event"]))
    return events


def replay(journal: Path, limit: int) -> tuple[list[Any], Any]:
    """Real events through the production ingress path, with P8 sampling stage stamps."""
    with journal.open("rb") as handle:
        header = _dec_header(json.loads(handle.readline()))
    definition = header.market
    events = load_events(journal, limit)

    engine = StrategyEngine(default_config(BaseLot.of(15)))
    merger = IngressMerger(
        engine=engine,
        state=MarketState.initial(definition),
        clock=lambda: definition.t0,
        market_id=definition.market_id,
    )
    # The same wiring the real runner does. Without this the merger has no perf clock, takes
    # no stage readings, and the whole comparison would quietly compare two empty lists.
    merger.perf_clock = perf_now_ns
    pipeline = MarketDataPipeline(
        merger=merger,
        books=BookTracker(definition.up_token_id, definition.down_token_id),
    )
    sampling = SamplingPolicy(10)
    harness = InstrumentedRun(
        pipeline=pipeline,
        engine=engine,
        rules=VenueMarketRules(parse_price("0.01"), parse_share("5"), source="p12d-replay"),
        executor=Executor(adapter=VenueAdapter(RecordingTransport())),
        buffer=ObservationBuffer(capacity=1 << 18),
        sampling=sampling,
        enabled=True,
    )

    for event in events:
        ordinal = merger.advance_ordinal()
        kind = type(event).__name__
        start = perf_now_ns()
        # The real thing: `submit` reduces, decides, and takes the two stage readings itself
        # when the sampler selects this cycle. Nothing here re-times anything.
        decision = merger.submit(event, measure_stages=sampling.selects(ordinal, kind))
        harness.observe(kind, start, decision)

    return list(harness.buffer.records), definition


def compare(observations: list[Any]) -> dict[str, Any]:
    """Every cycle the analyzer measures, against what the UI would publish for it."""
    analyzer = TelemetryAnalyzer().run(observations)
    latency = analyzer.latency

    expected: dict[str, list[int]] = {
        "decide_duration": [],
        "prepare_duration": [],
        "reconcile_duration": [],
        "receive_to_reconcile": [],
    }
    examples: list[dict[str, int]] = []
    projected = 0
    for observation in observations:
        # The analyzer's own gate, restated rather than guessed: an unsampled cycle has no
        # stages and contributes nothing to either side.
        if (
            observation[OBS_DECIDE_DONE_NS] == NOT_CAPTURED
            or observation[OBS_RECONCILE_DONE_NS] == NOT_CAPTURED
        ):
            continue
        sample = latency_sample(observation)
        if sample is None:
            continue
        projected += 1
        if "decide_ns" in sample:
            expected["decide_duration"].append(sample["decide_ns"])
        expected["prepare_duration"].append(sample["prepare_ns"])
        expected["reconcile_duration"].append(sample["reconcile_ns"])
        expected["receive_to_reconcile"].append(sample["receive_to_reconcile_ns"])
        if len(examples) < 5:
            examples.append(dict(sample))

    actual = {
        "decide_duration": list(latency.decide_duration.samples),
        "prepare_duration": list(latency.prepare_duration.samples),
        "reconcile_duration": list(latency.reconcile_duration.samples),
        "receive_to_reconcile": list(latency.receive_to_reconcile.samples),
    }
    agreement = {name: expected[name] == actual[name] for name in expected}
    return {
        "cycles_compared": projected,
        "counts": {name: len(values) for name, values in actual.items()},
        "agreement": agreement,
        "every_figure_agrees": all(agreement.values()),
        "examples": examples,
        "first_mismatch": _first_mismatch(expected, actual),
    }


def _first_mismatch(expected: dict[str, list[int]], actual: dict[str, list[int]]) -> Any:
    for name, values in expected.items():
        other = actual[name]
        if values == other:
            continue
        for index, (a, b) in enumerate(zip(values, other, strict=False)):
            if a != b:
                return {"metric": name, "index": index, "ui": a, "analyzer": b}
        return {"metric": name, "ui_count": len(values), "analyzer_count": len(other)}
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=40_000)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    observations, definition = replay(args.journal, args.limit)
    result = compare(observations)
    evidence = {
        "kind": "P12D_LATENCY_DEFINITION_REPLAY",
        "provenance": "REPLAY_OF_REAL_CAPTURE",
        "journal": args.journal.name,
        "slug": definition.slug,
        "events_replayed": args.limit,
        "observations_captured": len(observations),
        "definitions": {
            "decide_ns": "OBS_DECIDE_STAGE_NS - OBS_REDUCE_STAGE_NS (P8 decide_duration)",
            "prepare_ns": "OBS_PREPARE_DONE_NS - OBS_DECIDE_DONE_NS",
            "reconcile_ns": "OBS_RECONCILE_DONE_NS - OBS_PREPARE_DONE_NS",
            "receive_to_reconcile_ns": "OBS_RECONCILE_DONE_NS - OBS_RAW_RECEIVE_NS",
            "receive_to_decide_ns": "OBS_DECIDE_DONE_NS - OBS_RAW_RECEIVE_NS (P8 by_kind)",
        },
        "note": (
            "Every cycle P8's analyzer measured, compared figure by figure against what the UI "
            "would publish for the same observation. P12C published receive-to-decide as "
            "decide_ns and would disagree on every one of them."
        ),
        **result,
    }
    if args.out is not None:
        args.out.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({k: v for k, v in evidence.items() if k != "examples"}, indent=2))
    if args.out is not None:
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
