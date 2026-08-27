"""What the operator sees, and what the operator may ask for. Both immutable, both versioned.

Two contracts live here and they point in opposite directions:

* :class:`UiSnapshot` travels *out* of the bot. It is a projection — complete enough to render a
  market without touching a single trading object, and holding no reference to any of them. The
  UI cannot mutate what it does not have.
* :class:`OperatorCommand` travels *in*. It is a request, not an action: producing one changes
  nothing, and only the bot's own ordered control path decides whether it becomes a fact.

Neither recomputes anything. A snapshot carries numbers the authoritative records already
established, and the UI's job is to format them — `1_230_000` may be rendered as `1.23`, and that
is the whole of the licence. A second PnL implementation living in a dashboard would be a second
thing to be wrong, and it would be wrong in the place people look when they want the truth.

Absence is carried as absence. Every optional field means "not known", never "zero": an operator
reading a blank inventory and an operator reading `0` are being told different things, and only
one of them is safe to act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final

__all__ = [
    "COMMAND_SCHEMA_VERSION",
    "SNAPSHOT_SCHEMA_VERSION",
    "CommandKind",
    "OperatorCommand",
    "ParameterView",
    "SideView",
    "UiSnapshot",
]

SNAPSHOT_SCHEMA_VERSION: Final[int] = 1
COMMAND_SCHEMA_VERSION: Final[int] = 1


class CommandKind(Enum):
    """Everything an operator may ask for. Deliberately almost nothing.

    P12 is visibility and a safety brake, not a remote trading console. There is no command to
    place an order, change a strategy parameter, clear risk wholesale, bypass post-only, force a
    settlement, or enable live trading — not disabled ones, none. A command that does not exist
    cannot be issued by accident, misconfigured, or found later by somebody reading the code and
    wondering what it would do.
    """

    OPERATOR_HALT = "OPERATOR_HALT"
    """Stop placing. Existing quotes reconcile toward CANCEL through the normal minimal-action
    rule; nothing is sold, hedged or flattened."""

    RELEASE_OPERATOR_HALT = "RELEASE_OPERATOR_HALT"
    """Withdraw *that* halt, and nothing else.

    Named for what it does. "Resume" would imply the bot resumes, which is not in an operator's
    gift: a stale feed, an unreconciled position or an ambiguous settlement all still forbid
    placement, and this command cannot see them, let alone clear them.
    """


@dataclass(frozen=True, slots=True)
class OperatorCommand:
    """One request from a person. Immutable, identified, and inert until the bot accepts it."""

    schema_version: int
    command_id: str
    """Unique per request. The bot deduplicates on it, so a retried submission or a re-posted
    form is the same command rather than a second one."""

    kind: str
    issued_at_ns: int
    """The UI's wall clock, recorded for the audit and used for **nothing** else.

    It never reaches the strategy, the phase, the risk ordering or an event timestamp. Causality
    comes from the ingress ordinal the bot assigns when it accepts the command; a browser's idea
    of when it clicked is not evidence about the market."""

    source: str = "operator-ui"
    detail: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != COMMAND_SCHEMA_VERSION:
            raise ValueError(f"unsupported command schema {self.schema_version}")
        if not self.command_id:
            raise ValueError("a command needs an id so it can be deduplicated")
        if self.kind not in {member.value for member in CommandKind}:
            raise ValueError(f"{self.kind!r} is not a command this build accepts")

    def summary(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "command_id": self.command_id,
            "kind": self.kind,
            "issued_at_ns": self.issued_at_ns,
            "source": self.source,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ParameterView:
    """One strategy parameter, and how well established it is (I18).

    The status is not decoration. An operator looking at a base lot has to be able to tell a
    value the frozen sources state from one this build guessed at, and the dashboard is exactly
    where that difference stops being visible if nobody carries it.
    """

    name: str
    value: str
    status: str
    """CONFIRMED / FITTED / OPEN / OPERATIONAL."""

    open_item: str | None = None
    """The OPEN item this parameter belongs to, where one applies."""

    note: str = ""


@dataclass(frozen=True, slots=True)
class SideView:
    """One outcome, from strategy intent through to what execution actually did."""

    outcome: str
    strategy_price: int | None = None
    strategy_size: int | None = None
    """What the *strategy* wanted, before risk. Kept apart from the executable intent so a halt
    reads as "wanted to quote, was refused" rather than as "had nothing to say"."""

    executable_price: int | None = None
    executable_size: int | None = None
    action: str | None = None
    reason: str | None = None
    live_client_order_id: str | None = None
    live_price: int | None = None
    live_remaining_size: int | None = None
    live_status: str | None = None
    queue_ahead: int | None = None
    queue_confidence: str | None = None
    preparation_outcome: str | None = None
    """SAFE / WOULD_CROSS / NO_BOOK / OFF_VENUE_TICK — the post-only verdict for this side."""


@dataclass(frozen=True, slots=True)
class UiSnapshot:
    """One market as of one decision, complete enough to render without asking anything else."""

    schema_version: int
    published_at_ns: int
    """Wall clock at publication, so a viewer can say how old this is. Rendering only."""

    market_id: str
    slug: str
    condition_id: str | None
    phase: str
    ingress_ordinal: int
    event_timestamp_ns: int
    elapsed_seconds: float | None
    remaining_seconds: float | None

    clob_status: str
    clob_awaiting_snapshot: bool
    spot_status: str

    risk_state: str | None
    risk_sequence: int | None
    risk_active: tuple[str, ...]
    risk_latched: tuple[str, ...]
    allows_place: bool | None
    allows_cancel: bool | None

    n_up: int
    n_down: int
    inventory: int
    cost_up: int
    cost_down: int
    total_cost: int
    fees: int
    estimated_rebates: int
    realised_rebates: int
    pnl_if_up_without_rebate: int
    pnl_if_down_without_rebate: int
    pnl_if_up_estimated_rebate: int
    pnl_if_down_estimated_rebate: int

    raw_centre_numerator: int | None
    raw_centre_denominator: int | None
    quantized_centre: int | None
    centre_source: str
    centre_status: str

    favourite: str | None
    target_inventory: int | None

    up: SideView
    down: SideView

    decide_ns: int | None
    """P8's ``decide_duration``: ``decide_stage - reduce_stage``. Not receive-to-decide."""

    prepare_ns: int | None
    reconcile_ns: int | None
    receive_to_reconcile_ns: int | None

    resolution_state: str | None
    winning_outcome: str | None
    authoritative_block: int | None
    payout_numerators: tuple[int, ...]

    decisions_persisted: int
    risk_records_persisted: int
    dropped_records: int
    sink_errors: int
    telemetry_complete: bool | None
    """``None`` while the market is still running — completeness is only decidable at close."""

    live_trading_enabled: bool
    redemption_enabled: bool

    order_stream_status: str = "UNKNOWN"

    receive_to_decide_ns: int | None = None
    """Ingress, reduction, dispatch and decide together — P8's per-kind figure.

    A different measurement from `decide_ns` and named as one. P12C published this under the
    other name, which made the strategy look four times slower than it is."""

    latency_sample_ordinal: int | None = None
    """Which cycle the latency figures came from.

    P8 samples; most cycles carry no timing at all. Rather than show zeros for an unsampled
    decision, the most recent measured sample is carried with the ordinal it was taken at, so a
    reader can see it is not necessarily this decision's. Absent when nothing has been sampled."""

    observation_points: dict[str, int | None] = field(default_factory=dict)
    """Which ordinal each part of the view describes.

    The decision, the verdict that governed it and the queue estimate are all decision-specific
    and should agree; the telemetry counters are latest-known aggregates. Saying so explicitly is
    cheaper than a dashboard that silently mixes observation points."""

    settlement_note: str = ""
    verification_status: str | None = None
    """P11's verdict once the market has closed and been verified. ``None`` before that."""

    control_channel_available: bool | None = None
    """Whether the Plane-3 command bridge is alive. ``None`` if there is no bridge.

    Shown because an operator whose halt button silently does nothing is worse off than one who
    is told the channel is down."""

    control_audit_complete: bool | None = None
    """Whether every accepted command reached durable storage."""

    parameters: tuple[ParameterView, ...] = field(default_factory=tuple)
    accepted_commands: tuple[dict[str, object], ...] = field(default_factory=tuple)
    """Recent operator commands and what the risk stream did with each. The audit an operator
    needs in front of them, not only in a database."""

    def __post_init__(self) -> None:
        if self.schema_version != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(f"unsupported snapshot schema {self.schema_version}")
