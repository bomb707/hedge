"""Observability. Plane 3. Built in P8 and P11.

See ``docs/INVARIANTS.md`` I19 and Canonical §25, §26.

P8 delivers measurement of the two properties the edge depends on: **critical-path latency**
and **queue position**.

Two rules shape everything here:

* **Instrumentation is never strategy state.** No latency value, queue estimate, or
  performance-counter reading enters ``MarketState``, ``DecisionResult``, ``LedgerState``, or a
  P5 journal. A measurement describes *this run on this machine*; putting one into a replayed
  decision would make replay depend on the machine that recorded it (I20).
* **Instrumentation never blocks trading.** The sink is a bounded in-memory ring that drops
  oldest and counts drops. A lost observation is an observability incident; a stalled hot loop
  is a trading incident (I19).

Queue position is always an **estimate**. Polymarket publishes no per-order queue index, so
every value carries a confidence and nothing is named as though the venue's true index were
known.
"""

from maker5m.telemetry.classifier import (
    ExecutionQuality,
    QualityReason,
    QuoteClassification,
    classify,
)
from maker5m.telemetry.instrumented import InstrumentedRun, LatencyBook
from maker5m.telemetry.latency import (
    LatencyClock,
    Stage,
    TraceBuilder,
    perf_now_ns,
)
from maker5m.telemetry.metrics import ActionCounters, Distribution, quantile
from maker5m.telemetry.queue_estimate import QueueConfidence, QueueEstimate, QueueSlot
from maker5m.telemetry.sampling import (
    ALWAYS_TRACED_KINDS,
    SAMPLING_STATUS,
    SamplingPolicy,
)
from maker5m.telemetry.shadow import SHADOW_LABEL, ShadowQueueTracker
from maker5m.telemetry.sink import DEFAULT_CAPACITY, TelemetrySink

__all__ = [
    "ALWAYS_TRACED_KINDS",
    "DEFAULT_CAPACITY",
    "SAMPLING_STATUS",
    "SHADOW_LABEL",
    "ActionCounters",
    "Distribution",
    "ExecutionQuality",
    "InstrumentedRun",
    "LatencyBook",
    "LatencyClock",
    "QualityReason",
    "QueueConfidence",
    "QueueEstimate",
    "QueueSlot",
    "QuoteClassification",
    "SamplingPolicy",
    "ShadowQueueTracker",
    "Stage",
    "TelemetrySink",
    "TraceBuilder",
    "classify",
    "perf_now_ns",
    "quantile",
]
