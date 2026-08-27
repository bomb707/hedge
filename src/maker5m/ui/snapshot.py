"""Building the operator's view. Plane 3, derived from records that already exist.

Nothing here runs on the trading path, and nothing here computes an economic quantity. The
persistence worker already builds a `DecisionRecord` for every cycle from P8's captured
observation; this takes the most recent one, adds the risk verdict and settlement the bot already
holds, and projects the lot into a flat immutable snapshot. The cost to Plane 1 is zero, because
Plane 1 is not involved.

Publication is throttled rather than per-cycle. A real market produces ~380 decisions a second
and no operator can read at that rate, so the newest record is kept and written at a fixed
interval — which also means a slow disk delays a *frame*, not a decision.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Final

from maker5m.persistence.records import latency_sample
from maker5m.persistence.schema import DecisionRecord
from maker5m.ui.model import (
    SNAPSHOT_SCHEMA_VERSION,
    ParameterView,
    SideView,
    UiSnapshot,
)

__all__ = ["DEFAULT_PUBLISH_INTERVAL_S", "SnapshotPublisher", "parameter_views"]

VERDICT_HISTORY: Final[int] = 512
"""How many recent RiskRecords to keep for the join.

Bounded because this is a read model, not an archive; P11 holds the durable stream. A few hundred
covers any lag between the risk drain and the decision drain by a wide margin — the measured
worst case is a handful."""

DEFAULT_PUBLISH_INTERVAL_S: Final[float] = 0.25
"""Four frames a second. Fast enough to watch, slow enough that publication is never the work."""


def parameter_views(config: Any) -> tuple[ParameterView, ...]:
    """The running strategy's parameters, each carrying how well established it is (I18).

    Read off the config's own status labels rather than restated here: a second table of which
    values are OPEN would drift from the first, and the drift would show up as a dashboard
    quietly promoting a guess to a fact.
    """
    centre = config.quote_centre
    selector = config.base_lot_selector
    base_lot = getattr(selector, "base_lot", None)
    return (
        ParameterView(
            name="quote centre",
            value=str(getattr(centre, "source", centre)),
            status=str(getattr(getattr(centre, "status", None), "value", "OPEN")),
            open_item="O01",
            note="which price the grid is centred on",
        ),
        ParameterView(
            name="base lot L",
            value="unavailable" if base_lot is None else str(base_lot.shares),
            status=str(getattr(getattr(selector, "status", None), "value", "OPEN")),
            open_item="O03",
            note="order size before endgame tilt",
        ),
        ParameterView(
            name="grid policy",
            value=str(getattr(config.grid_policy, "value", config.grid_policy)),
            status="OPEN",
            open_item="O04",
            note="the two frozen sources disagree; the reference policy is in use",
        ),
        ParameterView(
            name="grid rounding",
            value=str(getattr(config.grid_rounding, "value", config.grid_rounding)),
            status="OPERATIONAL",
            note="tie-breaking only",
        ),
        ParameterView(
            name="tick rounding",
            value=str(getattr(config.tick_rounding, "value", config.tick_rounding)),
            status="OPEN",
            open_item="O13",
            note="quote-centre tick tie-breaking",
        ),
        ParameterView(
            name="endgame tilt",
            value=str(config.endgame_tilt),
            status="FITTED",
            open_item="O05",
            note="magnitude fitted, not stated by the sources",
        ),
        ParameterView(
            name="endgame band",
            value=str(config.endgame_band),
            status="FITTED",
            open_item="O06",
            note="magnitude fitted, not stated by the sources",
        ),
        ParameterView(
            name="band_hard",
            value=str(config.band_hard),
            status="OPEN",
            note="one-sided inventory wall",
        ),
        ParameterView(
            name="rebate",
            value="estimated only",
            status="OPEN",
            open_item="O07",
            note="no realised rebate has ever been observed; PnL is reported both ways",
        ),
    )


@dataclass(slots=True)
class SnapshotPublisher:
    """Builds the operator's view. **Single owner: the persistence worker's thread.**

    Everything that crosses into it does so as an immutable message through a bounded Plane-3
    channel, and the earlier version did not: `on_tick` — the ingress consumer — mutated
    `counters` and appended to `accepted_commands` while the worker thread owned `latest`. A read
    model maintained by two threads by accident is a read model that can be read halfway through
    being written, and it was reachable from Plane 1.

    Publication hands the finished snapshot to the bridge, which writes it. Nothing here touches
    a file.
    """

    identity: Any
    config: Any
    bridge: Any = None
    interval_seconds: float = DEFAULT_PUBLISH_INTERVAL_S

    inbox: deque[tuple[str, Any]] = field(default_factory=lambda: deque(maxlen=256), repr=False)
    """Immutable messages from other Plane-3 owners: command outcomes, settlement, verification.

    Bounded and drop-oldest like every other channel here. Losing an old command outcome costs a
    row in a table on a dashboard; blocking to keep it would cost more than it is worth."""

    latest: DecisionRecord | None = field(default=None, repr=False)
    verdicts: dict[int, Any] = field(default_factory=dict, repr=False)
    """Recent persisted RiskRecords, keyed by `risk_sequence`. Bounded.

    A decision names the verdict that governed it, and this is how that verdict is found. The
    previous version read `controller.trace.records[-1]` from the persistence thread — a
    cross-thread read of the trading side's mutable controller, which also answered the wrong
    question: the newest record is frequently several sequences ahead of the decision being
    persisted, so the dashboard showed one moment's decision beside another moment's verdict."""

    latest_latency: dict[str, int] | None = field(default=None, repr=False)
    latency_ordinal: int | None = None
    counters: dict[str, int] = field(default_factory=dict)
    settlement: dict[str, Any] | None = None
    verification_status: str | None = None
    telemetry_complete: bool | None = None
    accepted_commands: list[dict[str, Any]] = field(default_factory=list)
    audit_failures: int = 0
    audit_accepted: int = 0
    audit_persisted: int = 0
    audit_dropped: int = 0
    closed: bool = False
    t0_ns: int = 0
    duration_ns: int = 300_000_000_000

    _last_published: float = 0.0
    published: int = 0

    def observe(self, record: DecisionRecord, verdict: Any = None) -> None:
        """Take the newest decision. Worker thread only.

        ``verdict`` is accepted for supporting tests that hand one directly; production supplies
        risk records through :meth:`observe_risk` and the join happens by sequence.
        """
        self.latest = record
        if verdict is not None and getattr(verdict, "risk_sequence", None) is not None:
            self.observe_risk(verdict)

    def observe_risk(self, verdict: Any) -> None:
        """Take one persisted RiskRecord, keyed by its sequence. Worker thread only."""
        sequence = getattr(verdict, "risk_sequence", None)
        if sequence is None:
            return
        self.verdicts[int(sequence)] = verdict
        if len(self.verdicts) > VERDICT_HISTORY:
            for stale in sorted(self.verdicts)[: len(self.verdicts) - VERDICT_HISTORY]:
                del self.verdicts[stale]

    def observe_decision(self, record: DecisionRecord, observation: Any) -> None:
        """The production path: one decision and the observation it was built from.

        Latency comes out of that observation, so the figures always belong to this cycle and
        nothing reads a merger afterwards.
        """
        sample = latency_sample(observation)
        if sample is not None:
            self.observe_latency(record.ingress_ordinal, sample)
        self.observe(record)

    def observe_latency(self, ordinal: int, sample: dict[str, int]) -> None:
        """Take one P8 latency sample. Measured by P8; nothing here re-times anything."""
        self.latency_ordinal = ordinal
        self.latest_latency = sample

    def deliver(self, kind: str, payload: Any) -> None:
        """Hand the publisher an immutable message from another Plane-3 owner. Never blocks.

        Named `deliver` rather than the obvious alternative so it cannot be confused with an
        HTTP verb: the read-only venue guard scans for that method name and was right to flag
        the collision, since a guard that has to know which callers it can trust is not a guard.
        """
        self.inbox.append((kind, payload))

    def note_command(self, entry: dict[str, Any]) -> None:
        """Kept for direct Plane-3 callers; goes through the same bounded inbox."""
        self.deliver("command", entry)

    def _drain_inbox(self) -> None:
        while True:
            try:
                kind, payload = self.inbox.popleft()
            except IndexError:
                return
            if kind == "command":
                self.accepted_commands.append(dict(payload))
                del self.accepted_commands[:-10]
            elif kind == "settlement":
                self.settlement = dict(payload)
            elif kind == "verification":
                self.verification_status = str(payload.get("status"))
                self.telemetry_complete = payload.get("complete")
            elif kind == "closed":
                # The closed market's own truth. A live counter is a running estimate; the
                # manifest is what was actually written, and it is the one that wins. The first
                # P12B final snapshot disagreed with its own manifest by one decision, one risk
                # record and one drop, because the read model kept the last figures it happened
                # to have rather than the ones the close established.
                self.counters.update(
                    {
                        "decisions": int(payload["decision_count"]),
                        "risk": int(payload["risk_count"]),
                        "dropped": int(payload["dropped_records"]),
                        "sink_errors": int(payload["sink_errors"]),
                    }
                )
                self.telemetry_complete = bool(payload["telemetry_complete"])
                self.verification_status = str(payload["verification_status"])
                self.closed = True
            elif kind == "counters":
                # A counter that arrives after the close is a straggler from a thread that has
                # not noticed the market ended. The manifest already said what was written.
                if not self.closed:
                    self.counters.update(payload)
            elif kind == "audit_failure":
                self.audit_failures += 1
            elif kind == "audit_counts":
                self.audit_accepted = int(payload.get("accepted", 0))
                self.audit_persisted = int(payload.get("persisted", 0))
                self.audit_dropped = int(payload.get("dropped", 0))
            elif kind == "control_persisted":
                # Durable evidence, not an in-memory note from the ingress thread. A command
                # appears in the operator's history once it has actually been written down.
                self.accepted_commands.append(dict(payload))
                del self.accepted_commands[:-10]

    def _audit_complete(self) -> bool:
        return _audit_complete_from(
            self.audit_failures, self.audit_accepted, self.audit_persisted, self.audit_dropped
        )

    def maybe_publish(self, now: float) -> bool:
        """Publish if the interval has elapsed. Plane 3 only — this must not be called from
        `on_tick`, which is the ingress consumer."""
        if self.latest is None or now - self._last_published < self.interval_seconds:
            return False
        self._last_published = now
        snapshot = self.build(now)
        if self.bridge is not None:
            self.bridge.offer_snapshot(snapshot)
        self.published += 1
        return True

    def publish_now(self, now: float) -> bool:
        """Publish unconditionally — used for the final snapshot after settlement."""
        if self.latest is None:
            return False
        self._last_published = now
        if self.bridge is not None:
            self.bridge.offer_snapshot(self.build(now))
        self.published += 1
        return True

    def build(self, now: float) -> UiSnapshot:
        # Drained here rather than only in `maybe_publish`, so a snapshot is never built from a
        # read model with unread messages sitting behind it. Single-owner thread, so this is a
        # `popleft` loop and nothing more.
        self._drain_inbox()
        record = self.latest
        assert record is not None
        # The verdict this decision *names*, not the newest one. If it has not been drained yet
        # the risk fields read unavailable rather than borrowing a neighbouring moment's answer.
        verdict = (
            None if record.risk_sequence is None else self.verdicts.get(int(record.risk_sequence))
        )
        elapsed = None
        remaining = None
        if self.t0_ns:
            elapsed = (record.event_timestamp_ns - self.t0_ns) / 1e9
            remaining = (self.t0_ns + self.duration_ns - record.event_timestamp_ns) / 1e9
        settlement = self.settlement or {}
        latency = self.latest_latency or {}
        health = _health_of(verdict)
        return UiSnapshot(
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            published_at_ns=int(now * 1e9),
            market_id=record.market_id,
            slug=record.slug,
            condition_id=record.condition_id,
            phase=record.phase,
            ingress_ordinal=record.ingress_ordinal,
            event_timestamp_ns=int(record.event_timestamp_ns),
            elapsed_seconds=elapsed,
            remaining_seconds=remaining,
            # Health is read off the HealthFrame P6 supplied to the governing verdict. It is not
            # inferred from whether data exists: a spot price can be present and the feed STALE,
            # and "we have a number" is not "the feed is healthy".
            clob_status=health[0],
            clob_awaiting_snapshot=health[1],
            spot_status=health[2],
            order_stream_status=health[3],
            risk_state=_verdict_state(verdict, record),
            risk_sequence=_verdict_sequence(verdict, record),
            risk_active=_reasons(verdict, "active"),
            risk_latched=_reasons(verdict, "latched"),
            allows_place=_verdict_flag(verdict, "allows_place", record.risk_allows_place),
            allows_cancel=_verdict_flag(verdict, "allows_cancel", record.risk_allows_cancel),
            n_up=int(record.n_up),
            n_down=int(record.n_down),
            inventory=int(record.inventory),
            cost_up=int(record.cost_up),
            cost_down=int(record.cost_down),
            total_cost=int(record.total_cost),
            fees=int(record.fees),
            estimated_rebates=int(record.estimated_rebates),
            realised_rebates=int(record.realised_rebates),
            pnl_if_up_without_rebate=int(record.pnl_if_up_without_rebate),
            pnl_if_down_without_rebate=int(record.pnl_if_down_without_rebate),
            pnl_if_up_estimated_rebate=int(record.pnl_if_up_estimated_rebate),
            pnl_if_down_estimated_rebate=int(record.pnl_if_down_estimated_rebate),
            raw_centre_numerator=(
                None if record.raw_centre is None else record.raw_centre.numerator
            ),
            raw_centre_denominator=(
                None if record.raw_centre is None else record.raw_centre.denominator
            ),
            quantized_centre=(
                None if record.quantized_centre is None else int(record.quantized_centre)
            ),
            centre_source=record.centre_source,
            centre_status=record.centre_status,
            favourite=record.favourite,
            target_inventory=(
                None if record.target_inventory is None else int(record.target_inventory)
            ),
            up=_side(record, "up"),
            down=_side(record, "down"),
            decide_ns=latency.get("decide_ns"),
            prepare_ns=latency.get("prepare_ns"),
            reconcile_ns=latency.get("reconcile_ns"),
            receive_to_reconcile_ns=latency.get("receive_to_reconcile_ns"),
            latency_sample_ordinal=self.latency_ordinal,
            observation_points={
                "decision": record.ingress_ordinal,
                "risk_verdict": _verdict_ordinal(verdict, record),
                "latency_sample": self.latency_ordinal,
                "counters": None,
            },
            resolution_state=settlement.get("state"),
            winning_outcome=settlement.get("winning_outcome"),
            authoritative_block=settlement.get("authoritative_block"),
            payout_numerators=tuple(settlement.get("payout_numerators") or ()),
            settlement_note=str(settlement.get("note") or ""),
            decisions_persisted=self.counters.get("decisions", 0),
            risk_records_persisted=self.counters.get("risk", 0),
            dropped_records=self.counters.get("dropped", 0),
            sink_errors=self.counters.get("sink_errors", 0),
            telemetry_complete=self.telemetry_complete,
            verification_status=self.verification_status,
            control_channel_available=_bridge_available(self.bridge),
            control_audit_complete=self._audit_complete(),
            live_trading_enabled=_live_trading_enabled(),
            redemption_enabled=_redemption_enabled(),
            parameters=parameter_views(self.config),
            accepted_commands=tuple(dict(entry) for entry in self.accepted_commands),
        )


def _audit_complete_from(failures: int, accepted: int, persisted: int, dropped: int) -> bool:
    """Whether every accepted command actually reached durable storage.

    Not `audit_errors == 0`. `BoundedChannel.publish` does not raise when it drops, so a clean
    error count proves only that nothing threw — it says nothing about whether the record was
    written. Acceptance is compared against persistence, which is the question.
    """
    return failures == 0 and dropped == 0 and accepted == persisted


def _bridge_available(bridge: Any) -> bool | None:
    if bridge is None:
        return None
    stats = getattr(bridge, "stats", None)
    if stats is None:
        return None
    # The bridge's own verdict, which accounts for recorded filesystem failures. A thread that is
    # still running but cannot read the inbox is not a healthy control channel.
    return bool(stats.summary()["available"])


def _health_of(verdict: Any) -> tuple[str, bool, str, str]:
    """P6's own health, as carried by the governing risk record. Never inferred."""
    health = getattr(verdict, "health", None)
    if health is None:
        return ("UNKNOWN", True, "UNKNOWN", "UNKNOWN")
    return (
        str(getattr(health.clob_status, "value", health.clob_status)),
        bool(health.clob_awaiting_snapshot),
        str(getattr(health.spot_status, "value", health.spot_status)),
        str(getattr(health.order_stream_status, "value", health.order_stream_status)),
    )


def _reasons(verdict: Any, field_name: str) -> tuple[str, ...]:
    """The exact reason set P9 recorded. Never derived from the state name."""
    reasons = getattr(verdict, field_name, None)
    if not reasons:
        return ()
    return tuple(sorted(str(getattr(item, "value", item)) for item in reasons))


def _verdict_state(verdict: Any, record: DecisionRecord) -> str | None:
    state = getattr(verdict, "state", None)
    return record.risk_state if state is None else str(getattr(state, "value", state))


def _verdict_sequence(verdict: Any, record: DecisionRecord) -> int | None:
    sequence = getattr(verdict, "risk_sequence", None)
    return record.risk_sequence if sequence is None else int(sequence)


def _verdict_ordinal(verdict: Any, record: DecisionRecord) -> int | None:
    signal = getattr(verdict, "signal", None)
    if signal is None:
        return record.ingress_ordinal
    return int(getattr(signal, "as_of_ingress_ordinal", record.ingress_ordinal))


def _verdict_flag(verdict: Any, field_name: str, fallback: bool | None) -> bool | None:
    value = getattr(verdict, field_name, None)
    return fallback if value is None else bool(value)


def _side(record: DecisionRecord, which: str) -> SideView:
    side = record.up if which == "up" else record.down
    strategy_price = record.strategy_up_price if which == "up" else record.strategy_down_price
    strategy_size = record.strategy_up_size if which == "up" else record.strategy_down_size
    return SideView(
        outcome=side.outcome,
        strategy_price=None if strategy_price is None else int(strategy_price),
        strategy_size=None if strategy_size is None else int(strategy_size),
        executable_price=None if side.desired_price is None else int(side.desired_price),
        executable_size=None if side.desired_size is None else int(side.desired_size),
        action=side.action,
        reason=side.reason,
        live_client_order_id=side.live_client_order_id,
        live_price=None if side.live_price is None else int(side.live_price),
        live_remaining_size=(
            None if side.live_remaining_size is None else int(side.live_remaining_size)
        ),
        live_status=side.live_status,
        queue_ahead=None if side.queue_ahead is None else int(side.queue_ahead),
        queue_confidence=side.queue_confidence,
        preparation_outcome=side.preparation_outcome,
    )


def _live_trading_enabled() -> bool:
    from maker5m.safety import LIVE_TRADING_ENABLED

    return LIVE_TRADING_ENABLED


def _redemption_enabled() -> bool:
    from maker5m.settlement.redeem import REDEMPTION_ENABLED

    return REDEMPTION_ENABLED
