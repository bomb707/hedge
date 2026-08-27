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

from dataclasses import dataclass, field
from typing import Any, Final

from maker5m.persistence.schema import DecisionRecord
from maker5m.ui.channel import SnapshotChannel
from maker5m.ui.model import (
    SNAPSHOT_SCHEMA_VERSION,
    ParameterView,
    SideView,
    UiSnapshot,
)

__all__ = ["DEFAULT_PUBLISH_INTERVAL_S", "SnapshotPublisher", "parameter_views"]

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
    """Keeps the newest decision and publishes a snapshot on a timer."""

    channel: SnapshotChannel
    identity: Any
    config: Any
    interval_seconds: float = DEFAULT_PUBLISH_INTERVAL_S

    latest: DecisionRecord | None = field(default=None, repr=False)
    counters: dict[str, int] = field(default_factory=dict)
    settlement: dict[str, Any] | None = None
    accepted_commands: list[dict[str, Any]] = field(default_factory=list)
    t0_ns: int = 0
    duration_ns: int = 300_000_000_000

    _last_published: float = 0.0
    published: int = 0

    def observe(self, record: DecisionRecord) -> None:
        """Take the newest decision. Called by the persistence worker, on its own thread."""
        self.latest = record

    def note_command(self, entry: dict[str, Any]) -> None:
        """Record what happened to one operator command, for the audit strip in the view."""
        self.accepted_commands.append(entry)
        del self.accepted_commands[:-10]

    def maybe_publish(self, now: float) -> bool:
        """Publish if the interval has elapsed. Cheap and safe to call on every control tick."""
        if self.latest is None or now - self._last_published < self.interval_seconds:
            return False
        self._last_published = now
        self.channel.publish(self.build(now))
        self.published += 1
        return True

    def build(self, now: float) -> UiSnapshot:
        record = self.latest
        assert record is not None
        elapsed = None
        remaining = None
        if self.t0_ns:
            elapsed = (record.event_timestamp_ns - self.t0_ns) / 1e9
            remaining = (self.t0_ns + self.duration_ns - record.event_timestamp_ns) / 1e9
        settlement = self.settlement or {}
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
            clob_status="HEALTHY" if record.clob_healthy else "NOT HEALTHY",
            clob_awaiting_snapshot=not record.clob_healthy,
            spot_status="HEALTHY" if record.spot_age_ns is not None else "UNKNOWN",
            risk_state=record.risk_state,
            risk_sequence=record.risk_sequence,
            risk_active=(),
            risk_latched=(),
            allows_place=record.risk_allows_place,
            allows_cancel=record.risk_allows_cancel,
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
            raw_centre_numerator=None if record.raw_centre is None else record.raw_centre.numerator,
            raw_centre_denominator=(
                None if record.raw_centre is None else record.raw_centre.denominator
            ),
            quantized_centre=None
            if record.quantized_centre is None
            else int(record.quantized_centre),
            centre_source=record.centre_source,
            centre_status=record.centre_status,
            favourite=record.favourite,
            target_inventory=(
                None if record.target_inventory is None else int(record.target_inventory)
            ),
            up=_side(record, "up"),
            down=_side(record, "down"),
            decide_ns=None,
            prepare_ns=None,
            reconcile_ns=None,
            receive_to_reconcile_ns=None,
            resolution_state=settlement.get("state"),
            winning_outcome=settlement.get("winning_outcome"),
            authoritative_block=settlement.get("authoritative_block"),
            payout_numerators=tuple(settlement.get("payout_numerators") or ()),
            decisions_persisted=self.counters.get("decisions", 0),
            risk_records_persisted=self.counters.get("risk", 0),
            dropped_records=self.counters.get("dropped", 0),
            sink_errors=self.counters.get("sink_errors", 0),
            telemetry_complete=None,
            live_trading_enabled=_live_trading_enabled(),
            redemption_enabled=_redemption_enabled(),
            parameters=parameter_views(self.config),
            accepted_commands=tuple(dict(entry) for entry in self.accepted_commands),
        )


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
