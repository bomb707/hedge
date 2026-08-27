"""One market, composed. Every plane, one identity, and nothing shared with another market.

This is the wiring P12 proved, moved out of the tool that proved it and given a market identity
to belong to. It composes accepted components and adds no strategy, no accounting and no second
opinion about anything they decide:

* **Plane 1** — P6's pipeline and feeds, P7's shadow executor, the ingress owner's tick.
* **Plane 2** — P3/P4's engine through P6's merger, P9's risk overlay.
* **Plane 3** — P8's analyzer, P11's persistence worker and store, P12's snapshot publisher and
  control ingress, P13's L3 aggregation and corpus entry.

**Identity, not "the current market".** Every object here belongs to one slug. Two sessions
overlap by design — market N is still finishing while N+1 is warming its book — so a global
"current market" would be a bug waiting for the five seconds where both are past their T0. The
supervisor decides which session is active for operator commands; nothing else is shared.

**Shadow means shadow.** The executor's transport records rather than sends. Every order here is
a `SHADOW_ORDER`, every queue figure a `SHADOW_ESTIMATE`, and no `OwnFill` exists, because none
has happened. `LIVE_TRADING_ENABLED` is `False` and this module has no path that could care if
it were not.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from maker5m.bot.config import PaperConfig
from maker5m.bot.latency import LatencyArtifact, write_latency
from maker5m.bot.quality import QualityAggregate
from maker5m.bot.resources import LIVE_SESSIONS, ResourceSample, sample_resources, tiers
from maker5m.execution import Executor, RecordingTransport, VenueAdapter
from maker5m.feeds import MarketDataPipeline
from maker5m.feeds.capture import capture_market
from maker5m.feeds.discovery import DiscoveredMarket
from maker5m.market.events import HealthStatus
from maker5m.market.timebase import NANOS_PER_SECOND, TimestampNs
from maker5m.persistence import (
    MANIFEST_SCHEMA_VERSION,
    BoundedChannel,
    Manifest,
    MarketIdentity,
    MetricsAccumulator,
    PersistenceWorker,
    TelemetryProvenance,
    TelemetryStore,
    database_digest,
    settlement_row,
)
from maker5m.replay import encode_journal
from maker5m.risk import RiskConfig, RiskEngine, RiskProvenance
from maker5m.risk.engine import RiskDecision
from maker5m.risk.overlay import risk_adjust
from maker5m.risk.trace import HealthFrame, RiskController
from maker5m.safety import LIVE_TRADING_ENABLED
from maker5m.settlement import REDEMPTION_ENABLED
from maker5m.strategy import StrategyEngine
from maker5m.telemetry import InstrumentedRun, SamplingPolicy, TelemetryAnalyzer, perf_now_ns
from maker5m.telemetry.analyzer import ClassificationMode
from maker5m.telemetry.observation import ObservationBuffer
from maker5m.ui import ControlIngress, SnapshotPublisher

__all__ = ["MarketSession", "PrearmRecord", "SessionResult"]


def _lead(t0_ns: int, when_ns: Any) -> float | None:
    """Seconds before T0. ``None`` when it never happened — never a zero standing in for that."""
    if not isinstance(when_ns, int):
        return None
    return round((t0_ns - when_ns) / NANOS_PER_SECOND, 3)


DEFAULT_RISK_CAPACITY = 400_000
STEP_RELEASE_CHUNK = 4_096
"""How many recorded steps to free before yielding to the loop. Bounds the pause, not the work."""


@dataclass(frozen=True, slots=True)
class PrearmRecord:
    """When this market's **metadata** was resolved. Discovery only.

    Deliberately narrow. P13's first corpus called this "prearm ready" and reported a 74.9 s
    lead, which was true of `discover_market` returning and said nothing about market data: the
    CLOB and Binance producers do not start until T0-30, so a market could be "prearmed" by this
    measure with no book and no BTC price. Feed readiness is a separate set of facts, recorded
    by the session from P6's own warm milestones.
    """

    slug: str
    t0_ns: int
    started_ns: int
    ready_ns: int
    ok: bool
    error: str | None = None

    @property
    def lead_seconds(self) -> float:
        return (self.t0_ns - self.ready_ns) / NANOS_PER_SECOND

    @property
    def duration_seconds(self) -> float:
        return (self.ready_ns - self.started_ns) / NANOS_PER_SECOND

    def summary(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "t0_ns": self.t0_ns,
            "discovery_started_ns": self.started_ns,
            "discovery_ready_ns": self.ready_ns,
            "discovery_lead_seconds": round(self.lead_seconds, 3),
            "discovery_duration_seconds": round(self.duration_seconds, 3),
            "discovery_ready_before_t0": self.ok and self.ready_ns < self.t0_ns,
            "ok": self.ok,
            "error": self.error,
        }


@dataclass(slots=True)
class SessionResult:
    """What one market produced. The corpus entry is built from this and the cold result."""

    slug: str
    entry: dict[str, Any]
    database: Path
    journal: Path
    database_sha256: str


class MarketSession:
    """One market's full lifecycle. Constructed prearmed, run once, finalized once."""

    def __init__(
        self,
        *,
        market: DiscoveredMarket,
        config: PaperConfig,
        prearm: PrearmRecord,
        ui: Any,
        publisher_identity: MarketIdentity | None = None,
    ) -> None:
        if LIVE_TRADING_ENABLED:  # pragma: no cover - the flag is a constant False
            raise RuntimeError("refusing to compose a session while live trading is enabled")
        definition = market.definition
        self.market = market
        self.config = config
        self.prearm = prearm
        self.ui = ui
        self.slug = definition.slug
        self.t0_ns = int(definition.t0)
        self.strategy_config = config.strategy()
        self.identity = publisher_identity or MarketIdentity(
            market_id=definition.market_id,
            slug=self.slug,
            condition_id=market.condition_id,
            provenance=TelemetryProvenance.REAL_PUBLIC_MARKET_DATA.value,
        )

        self.database = config.evidence_dir / f"{self.slug}.p11.sqlite3"
        self.journal_path = config.evidence_dir / f"{self.slug}.journal.ndjson"

        self.controller = RiskController(
            engine=RiskEngine(config=RiskConfig()),
            provenance=RiskProvenance.REAL_PUBLIC_MARKET_DATA,
        )
        self.analyzer = TelemetryAnalyzer(
            sampling=SamplingPolicy(sample_every=config.sample_every),
            # Canonical §34-L3 asks about every quote opportunity, so P13 classifies every
            # decision. Latency stays sampled exactly as P8 accepted it: reading a clock is the
            # expensive part and one cycle in ten characterises a distribution, whereas a
            # classification fraction over "every acting cycle plus one in ten of the rest" has
            # a denominator nobody can interpret — and acting cycles are exactly the ones where
            # an order was being placed or replaced, so it over-weights the worst queue moments.
            classification_mode=ClassificationMode.EVERY_DECISION,
        )
        self.quality = QualityAggregate(t0_ns=self.t0_ns)
        self.analyzer.on_quote = self.quality.observe
        self.metrics = MetricsAccumulator(
            market_id=self.identity.market_id,
            slug=self.slug,
            provenance=self.identity.provenance,
        )
        self.buffer = ObservationBuffer(capacity=config.buffer_capacity)
        self.risk_channel = BoundedChannel(
            capacity=min(DEFAULT_RISK_CAPACITY, max(config.buffer_capacity, 1))
        )
        self.audit_channel = BoundedChannel(capacity=256)
        self.fill_channel = BoundedChannel(capacity=8_192)
        self.control_events = BoundedChannel(capacity=512)
        self.worker = PersistenceWorker(
            buffer=self.buffer,
            store=TelemetryStore(path=self.database),
            identity=self.identity,
            analyzer=self.analyzer,
            metrics=self.metrics,
            risk=self.risk_channel,
            fills=self.fill_channel,
            control_audit=self.audit_channel,
        )
        self.publisher = SnapshotPublisher(
            identity=self.identity,
            config=self.strategy_config,
            bridge=ui.bridge,
            t0_ns=self.t0_ns,
        )
        # The closure captures the *channel*, not the session. Capturing `self` here put the
        # session in a reference cycle — session holds the ingress, the ingress holds the lambda,
        # the lambda holds the session — so releasing it depended on a full garbage collection
        # rather than on reference counting. With full collections deliberately made rare, that
        # meant closed markets stayed resident: the pilot that paced the collector recorded three
        # live sessions where two is the steady state.
        audit_channel = self.audit_channel
        self.control_ingress = ControlIngress(
            controller=self.controller,
            publish=self.risk_channel.publish,
            audit=lambda command, outcome: audit_channel.publish((command, outcome)),
        )
        self.worker.on_decision_record = self._on_persisted
        self.worker.on_risk_record = self.publisher.observe_risk
        self.worker.on_control_record = self._on_control_persisted

        self.runs: list[InstrumentedRun] = []
        self.hot_path_ns: list[int] = []
        self.places_by_state: dict[str, int] = {}
        self.risk_states: dict[str, int] = {}
        self.commands: list[dict[str, Any]] = []
        self.phase_first_seen: dict[str, dict[str, Any]] = {}
        self.incidents: list[str] = []
        self._last_evaluated = -1
        self.started_resources: ResourceSample | None = None
        self.finished_resources: ResourceSample | None = None
        self.released_resources: ResourceSample | None = None
        self.hot_path_tiers: dict[str, int | None] = {}
        LIVE_SESSIONS.add(self)
        self.capture: Any = None
        self.settlement: Any = None
        self.manifest: Manifest | None = None
        self.database_sha256 = ""
        self.journal_sha256 = ""
        self.journal_bytes = 0
        self.closing_sink_errors = 0
        self.source_revision = ""
        self.attempt_id: str | None = None
        self.feed_counters: dict[str, Any] = {}
        self.warm: dict[str, int | None] = {
            "clob_first_ns": None,
            "clob_book_ready_ns": None,
            "spot_first_valid_ns": None,
        }
        self.feed_warm_started_ns: int | None = None
        self.at_t0: dict[str, Any] = {}
        self.latency_path = config.evidence_dir / f"{self.slug}.latency.json.xz"
        self.latency: LatencyArtifact | None = None

    # -- Plane 3 observers -----------------------------------------------------------------

    def _on_persisted(self, record: Any, observation: Any) -> None:
        """A committed decision row. Worker thread; reads nothing another thread can change."""
        self.publisher.observe_decision(record, observation)
        self.publisher.deliver(
            "counters",
            {
                "decisions": self.worker.stats.decisions_written,
                "risk": self.worker.stats.risk_written,
                "dropped": self.buffer.dropped,
                "sink_errors": self.worker.store.sink_errors,
            },
        )
        self.publisher.deliver(
            "audit_counts",
            {
                "accepted": self.audit_channel.accepted,
                "persisted": self.worker.stats.control_records_written,
                "dropped": self.audit_channel.dropped,
            },
        )
        if self.ui.active_slug == self.slug:
            self.publisher.maybe_publish(time.time())

    def _on_control_persisted(self, row: Any) -> None:
        """Durable command history, and which market accepted it."""
        entry = {
            "command_id": row.command_id,
            "kind": row.kind,
            "accepted": row.accepted,
            "market_id": row.market_id,
            "slug": self.slug,
            "ingress_ordinal": row.ingress_ordinal,
            "risk_sequence": row.risk_sequence,
            "risk_state": row.risk_state,
            "allows_place": row.allows_place,
            "detail": row.detail,
        }
        self.commands.append(entry)
        self.publisher.deliver("control_persisted", entry)

    # -- Plane 1 -----------------------------------------------------------------------------

    def _attach(self, pipeline: MarketDataPipeline) -> None:
        sampling = SamplingPolicy(sample_every=self.config.sample_every)
        pipeline.merger.perf_clock = perf_now_ns
        pipeline.stage_selector = lambda ordinal, kind: sampling.selects(ordinal, kind)
        self.runs.append(
            InstrumentedRun(
                pipeline=pipeline,
                engine=StrategyEngine(self.strategy_config),
                rules=self.market.venue_rules,
                executor=Executor(adapter=VenueAdapter(RecordingTransport())),
                buffer=self.buffer,
                sampling=sampling,
                risk=self.controller,
            )
        )

    def _frame(self, pipeline: MarketDataPipeline) -> HealthFrame:
        """P6's verdict, read rather than recomputed. P6 is the sole staleness authority."""
        return HealthFrame(
            clob_status=pipeline.clob_health.status,
            clob_awaiting_snapshot=pipeline.clob_health.awaiting_snapshot,
            spot_status=pipeline.spot_health.status,
            order_stream_status=HealthStatus.UNKNOWN,
            order_stream_required=False,
        )

    def _evaluate(self, pipeline: MarketDataPipeline, now_ns: TimestampNs) -> RiskDecision:
        record = self.controller.evaluate(
            self._frame(pipeline),
            as_of_ingress_ordinal=pipeline.merger.ordinal,
            now_ns=now_ns,
        )
        self.risk_channel.publish(record)
        state = record.state.value
        self.risk_states[state] = self.risk_states.get(state, 0) + 1
        self._last_evaluated = pipeline.merger.ordinal
        return RiskDecision(
            state=record.state,
            active=record.active,
            latched=record.latched,
            snapshot=self.controller.engine.snapshot,
        )

    def _observe(self, kind: str, raw_ns: int, decision: Any) -> None:
        """One shadow cycle, with the verdict taken as of this event and applied to it."""
        run = self.runs[0]
        pipeline = run.pipeline
        verdict = self._evaluate(pipeline, pipeline.merger.state.last_event_timestamp)
        state = verdict.state.value
        started = perf_now_ns()
        before = run.executor.orders.open_count
        run.observe(
            kind,
            raw_ns,
            risk_adjust(decision, verdict),
            # The venue publishes no clock inside the event contract — P6 consumes
            # `source_timestamp_ms` for clock health without it entering the stream — so this is
            # genuinely absent rather than the ingress clock wearing the venue's name.
            None,
            strategy_intent=decision.orders,
        )
        self.hot_path_ns.append(perf_now_ns() - started)
        if run.executor.orders.open_count > before:
            self.places_by_state[state] = self.places_by_state.get(state, 0) + 1
        self._note_phase(decision, pipeline)

    def _note_phase(self, decision: Any, pipeline: MarketDataPipeline) -> None:
        """When each phase was first seen, in market time. Thresholds are P2's, not measured."""
        telemetry = getattr(decision, "telemetry", None)
        phase = getattr(getattr(telemetry, "phase", None), "value", None)
        if phase is None or phase in self.phase_first_seen:
            return
        event_ns = int(pipeline.merger.state.last_event_timestamp)
        self.phase_first_seen[phase] = {
            "ingress_ordinal": pipeline.merger.ordinal,
            "event_timestamp_ns": event_ns,
            "offset_seconds": round((event_ns - self.t0_ns) / NANOS_PER_SECOND, 3),
        }

    def _on_tick(self, now_ns: TimestampNs, pipeline: MarketDataPipeline) -> None:
        """The ingress owner's whole Plane-3 obligation: a bounded pop and an in-memory append."""
        if pipeline.merger.ordinal != self._last_evaluated:
            self._evaluate(pipeline, now_ns)
        self.ui.drain_commands(
            self.slug,
            self.control_ingress,
            ingress_ordinal=pipeline.merger.ordinal,
            now_ns=pipeline.merger.state.last_event_timestamp,
            report=self.control_events.publish,
        )

    def _note_warm(self, key: str, when_ns: TimestampNs) -> None:
        """P6 saw the market data become usable. Recorded, never inferred."""
        if self.warm.get(key) is None:
            self.warm[key] = int(when_ns)

    def _note_prearm(self, at_t0: dict[str, Any]) -> None:
        """P6's warm state at the T0 boundary. Recorded once, by the capture that saw it."""
        self.at_t0 = dict(at_t0)

    @property
    def feed_ready_ns(self) -> int | None:
        """When this market became warm and **stayed** warm up to its own T0.

        Both feeds, currently valid at the boundary, or nothing. A book with no spot cannot
        price a centre and a spot with no book has nothing to quote against; and a feed that was
        ready once and disconnected before T0 was not ready when it mattered. If either lost
        continuity and recovered, this is measured from the recovery, because that is when the
        market actually became warm.

        `None` means exactly that, never "assume it was fine".
        """
        if not self.at_t0:
            return None
        if not (self.at_t0.get("clob_ready") and self.at_t0.get("spot_ready")):
            return None
        book = self.at_t0.get("clob_ready_since_ns")
        spot = self.at_t0.get("spot_ready_since_ns")
        if not isinstance(book, int) or not isinstance(spot, int):
            return None
        return max(book, spot)

    def prearm_summary(self) -> dict[str, Any]:
        """Discovery and feed readiness, kept apart, against this market's own T0."""
        ready = self.feed_ready_ns
        book = self.warm.get("clob_book_ready_ns")
        spot = self.warm.get("spot_first_valid_ns")
        return {
            **self.prearm.summary(),
            "feed_warm_started_ns": self.feed_warm_started_ns,
            "clob_first_ns": self.warm.get("clob_first_ns"),
            "clob_book_ready_ns": book,
            "spot_first_valid_ns": spot,
            "feed_ready_ns": ready,
            "first_clob_lead_seconds": _lead(self.t0_ns, book),
            "first_spot_lead_seconds": _lead(self.t0_ns, spot),
            # The state at the boundary. `clob_lead_seconds` and `spot_lead_seconds` are
            # measured from when each feed last *became* valid, so a disconnect and recovery
            # shortens the lead instead of leaving the original figure standing.
            "at_t0": dict(self.at_t0),
            "clob_ready_at_t0": bool(self.at_t0.get("clob_ready")),
            "spot_ready_at_t0": bool(self.at_t0.get("spot_ready")),
            "clob_lead_seconds": _lead(self.t0_ns, self.at_t0.get("clob_ready_since_ns")),
            "spot_lead_seconds": _lead(self.t0_ns, self.at_t0.get("spot_ready_since_ns")),
            "feed_ready_lead_seconds": _lead(self.t0_ns, ready),
            "feed_ready_before_t0": ready is not None and ready < self.t0_ns,
        }

    # -- lifecycle ---------------------------------------------------------------------------

    async def run(self) -> None:
        """Trade one market in shadow. Returns when the capture window closes."""
        self.started_resources = sample_resources()
        self.feed_warm_started_ns = time.time_ns()
        self.worker.start()
        try:
            self.capture = await capture_market(
                self.market,
                self.strategy_config,
                prearm_ready_ns=TimestampNs(self.prearm.ready_ns),
                description=f"P13 live paper run on {self.slug}",
                on_pipeline=self._attach,
                observer=lambda kind, raw, decision: self._observe(kind, raw, decision),
                on_tick=self._on_tick,
                on_warm=self._note_warm,
                on_prearm=self._note_prearm,
            )
        except Exception as error:
            self.incidents.append(f"capture failed: {type(error).__name__}: {error}")
        finally:
            deadline = time.time() + 30
            while len(self.buffer) and time.time() < deadline:
                await asyncio.sleep(0.05)
            self.worker.stop(timeout=30)
            self.finished_resources = sample_resources()

    async def write_journal(self) -> None:
        """Encode and hash the journal. Off the loop, and only after the market has closed."""
        if self.capture is None:
            self.incidents.append("no journal: the capture did not complete")
            return
        try:
            raw = await asyncio.to_thread(encode_journal, self.capture.journal)
            await asyncio.to_thread(self._write_bytes, self.journal_path, raw)
            self.journal_bytes = len(raw)
        except Exception as error:
            self.incidents.append(f"journal write failed: {type(error).__name__}: {error}")
        finally:
            await self._drop_recorded_steps()

    async def _drop_recorded_steps(self) -> None:
        """Let go of the recorded event stream as soon as it is on disk.

        `IngressMerger.steps` holds every step with its complete `DecisionResult` — two hundred
        megabytes for a busy market — and the settlement watch that follows can run for minutes.
        Holding it until the whole cold path finishes made two markets' streams coexist for no
        reason: the journal file is written, and everything downstream reads that. The pipeline
        itself stays, because the closing metrics still need its ledger. What the capture result
        still had to say — the feed's own message counts — is taken first: the first version of
        this dropped it whole and would have recorded zero CLOB and zero BTC messages for every
        market in the corpus.
        """
        if self.capture is not None and hasattr(self.capture.counters, "summary"):
            self.feed_counters = dict(self.capture.counters.summary())
        self.capture = None
        for run in self.runs:
            steps = run.pipeline.merger.steps
            while steps:
                # In chunks, yielding between them. Freeing 150,000 step graphs is one C-level
                # traversal with no bytecode boundary in it, so the loop cannot service the
                # market that is *currently trading* until it finishes — the corrected pilot
                # measured a single 480 ms `observe` against a 25 microsecond median, and a
                # 2,535-observation buffer high-water on the market that was live at the time.
                # Nothing may stall the ingress owner, including this.
                del steps[-STEP_RELEASE_CHUNK:]
                await asyncio.sleep(0)

    @staticmethod
    def _write_bytes(path: Path, raw: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    async def settle(self, settle_fn: Any) -> None:
        """Watch the chain for this market's resolution. Never blocks another market.

        The watch duration and poll interval come from this run's configuration, which is what
        the corpus identity claims they are.
        """
        try:
            self.settlement = await asyncio.to_thread(
                settle_fn,
                self.market,
                self.slug,
                timeout_s=self.config.settle_timeout_s,
                poll_s=self.config.settle_poll_s,
            )
        except Exception as error:
            self.incidents.append(f"settlement watch failed: {type(error).__name__}: {error}")

    async def write_latency_artifact(self, identity: dict[str, Any]) -> None:
        """Keep the live latency before the market is released. Cold path, off the loop.

        Written before eligibility is decided, because a market whose latency did not survive
        cannot be P13 evidence — measuring live latency is most of what this phase is for.
        """
        try:
            self.latency = await asyncio.to_thread(
                write_latency,
                self.latency_path,
                self.analyzer,
                identity={
                    "slug": self.slug,
                    "market_id": self.identity.market_id,
                    "condition_id": self.identity.condition_id,
                    "t0_ns": self.t0_ns,
                    **identity,
                },
                hot_path_ns=list(self.hot_path_ns),
            )
        except Exception as error:
            self.incidents.append(f"latency artifact failed: {type(error).__name__}: {error}")

    def close_store(self) -> None:
        """Write the settlement row, the metrics and the manifest, then digest the file.

        On the thread that opens the connection, which is the rule the store enforces. The
        counts come from the worker's committed totals — a manifest that reported accepted rows
        would describe a file that does not exist.
        """
        run = self.runs[0] if self.runs else None
        ledger = None if run is None else run.pipeline.merger.state.ledger
        winner = None if self.settlement is None else self.settlement.decision.winning_outcome
        market_metrics = self.metrics.build(ledger, winner=winner) if ledger is not None else None

        closing = TelemetryStore(path=self.database)
        try:
            closing.open()
        except Exception as error:
            self.incidents.append(f"closing store failed: {type(error).__name__}: {error}")
            return
        sequence = self.worker.persistence_sequence
        if self.settlement is not None:
            sequence += 1
            closing.write_settlement(
                settlement_row(
                    self.settlement,
                    market_id=self.identity.market_id,
                    persistence_sequence=sequence,
                )
            )
        if market_metrics is not None:
            closing.write_metrics(market_metrics)

        stats = self.worker.stats
        self.manifest = Manifest(
            schema_version=MANIFEST_SCHEMA_VERSION,
            slug=self.slug,
            market_id=self.identity.market_id,
            condition_id=self.identity.condition_id,
            capture_start_ns=TimestampNs(self.t0_ns),
            capture_end_ns=TimestampNs(self.t0_ns + 300 * NANOS_PER_SECOND),
            source_revision=self.source_revision,
            decision_count=stats.decisions_written,
            fill_count=stats.fills_written,
            risk_count=stats.risk_written,
            settlement_count=0 if self.settlement is None else 1,
            first_ingress_ordinal=self.worker.first_ingress_ordinal,
            last_ingress_ordinal=self.worker.last_ingress_ordinal,
            first_persistence_sequence=1 if sequence else None,
            last_persistence_sequence=sequence or None,
            accepted_records=self.buffer.accepted,
            persisted_records=stats.decisions_written,
            dropped_records=self.buffer.dropped,
            sequence_gaps=stats.sequence_gaps,
            lost_observations=stats.lost_observations,
            sink_errors=self.worker.store.sink_errors + closing.sink_errors,
            first_gap_at=stats.first_gap_at,
            last_gap_at=stats.last_gap_at,
            buffer_capacity=self.buffer.capacity,
            buffer_high_water=stats.buffer_high_water,
            risk_records_accepted=self.risk_channel.accepted,
            risk_records_persisted=stats.risk_written,
            risk_records_dropped=self.risk_channel.dropped,
            fill_captures_accepted=self.fill_channel.accepted,
            fill_captures_persisted=stats.fills_written,
            fill_captures_dropped=self.fill_channel.dropped,
            database_bytes=None,
            database_sha256=None,
            provenance=self.identity.provenance,
            live_trading_enabled=LIVE_TRADING_ENABLED,
            redemption_enabled=REDEMPTION_ENABLED,
            closed=True,
        )
        closing.write_manifest(self.manifest)
        closing.close()
        self.closing_sink_errors = closing.sink_errors
        try:
            _, self.database_sha256 = database_digest(self.database)
        except OSError as error:
            self.incidents.append(f"digest failed: {type(error).__name__}: {error}")

    def publish_close(self, cold: dict[str, Any]) -> None:
        """The last frame an operator sees for this market: the manifest's own figures."""
        manifest = self.manifest
        if manifest is None:
            return
        if self.settlement is not None:
            decision = self.settlement.decision
            self.publisher.deliver(
                "settlement",
                {
                    "state": decision.state.value,
                    "winning_outcome": (
                        None if decision.winning_outcome is None else decision.winning_outcome.value
                    ),
                    "authoritative_block": decision.authoritative_block,
                    "payout_numerators": list(
                        () if decision.payout is None else decision.payout.numerators
                    ),
                    "note": "redemption is disabled in this build; nothing was redeemed",
                },
            )
        status = str(cold.get("verification_status", "UNKNOWN"))
        self.publisher.deliver(
            "closed",
            {
                "decision_count": manifest.decision_count,
                "risk_count": manifest.risk_count,
                "dropped_records": manifest.dropped_records,
                "sink_errors": manifest.sink_errors,
                "telemetry_complete": manifest.telemetry_complete,
                "verification_status": status,
            },
        )
        if self.ui.active_slug in (self.slug, None):
            self.publisher.publish_now(time.time())

    def finish(self) -> None:
        """Take what the entry still needs, let the market go, then measure what is left.

        The order matters and is the point: the previous pilot sampled "end" resources while the
        recorded event stream was still held, and then reported 1.25 GB as though it described a
        released market. A post-release number is the only one that can say whether release
        works, and it has to be taken after the references are actually gone.
        """
        self.hot_path_tiers = tiers(self.hot_path_ns)
        self.release()
        self.released_resources = sample_resources()

    def release(self) -> None:
        """Drop everything this market was holding. Two hundred of these must not accumulate."""
        self.worker.on_decision_record = None
        self.worker.on_risk_record = None
        self.worker.on_control_record = None
        self.analyzer.on_quote = None
        self.runs.clear()
        self.capture = None
        self.buffer.records.clear()
        self.risk_channel.records.clear()
        self.audit_channel.records.clear()
        self.fill_channel.records.clear()
        self.control_events.records.clear()
        self.controller.trace.records.clear()
        self.hot_path_ns.clear()
        # Nothing here may need a full collection to be freed. Reference counting handles a
        # graph with no cycles in it, and these are the two that would otherwise keep a closed
        # market resident until the next gen-2 pass.
        self.publisher.bridge = None
        self.publisher.verdicts.clear()
