"""An ordered, replayable record of why execution was or was not permitted.

P5's journal answers *what the strategy wanted*. It does not answer *why the order was allowed
out*, and until now nothing did: reconciliation results mutated a latched snapshot directly, and
operational conditions like clock drift or an API error rate could flip ``allows_place`` without
appearing anywhere. A run could be replayed for its economics and not for its permissions, which
for a trading system is half an audit trail.

So risk gets its own stream, deliberately **beside** the strategy journal rather than inside it:

* Nothing here enters ``MarketState``, ``LedgerState``, ``DecisionResult``, or
  ``StrategyConfig``, and ``StrategyEngine`` is untouched. Historical P5 journals still decode
  and re-encode byte-identically, which is asserted by test.
* Feed health is **not** duplicated into it. That already exists as ordered ``HealthEvent``s in
  the market stream; a risk record references the ingress ordinal it was observed at and
  records the verdict that followed.

Ordering
--------
``risk_sequence`` is a strict total order within the risk stream. ``as_of_ingress_ordinal`` ties
each record to a position in the market stream, with exact semantics: *the signal was applied
after every market event through that ordinal had been consumed, and before the next execution
permission decision that references this risk sequence*.

Nothing here depends on coroutine scheduling or wall-clock tie-breaking. :class:`RiskController`
is the single owner of risk state and the only path that may change it, so two tasks cannot
mutate the engine independently — if concurrency is ever introduced, it must route through this
one boundary.

Representation
--------------
``NamedTuple``, measured rather than assumed. P8 benchmarked frozen slotted dataclass
construction at 1,791 ns for sixteen fields against 362 ns for a ``NamedTuple`` and 76 ns for a
plain tuple. A ``NamedTuple`` is a genuinely typed contract that mypy checks — not the
"arbitrary dictionary" this must not be — at roughly a fifth of the dataclass cost, which is the
right trade for something constructed on every evaluation.
"""

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, NamedTuple

from maker5m.market.events import HealthStatus
from maker5m.market.timebase import TimestampNs
from maker5m.risk.config import RiskConfig
from maker5m.risk.engine import RiskDecision, RiskEngine, RiskInputs
from maker5m.risk.reasons import REQUIRES_RECONCILIATION, RiskReason, RiskState

__all__ = [
    "DEFAULT_RISK_TRACE_CAPACITY",
    "RISK_SCHEMA_VERSION",
    "HealthFrame",
    "OperationalState",
    "RiskController",
    "RiskOrderError",
    "RiskProvenance",
    "RiskRecord",
    "RiskSignal",
    "RiskSignalKind",
    "RiskTrace",
]

RISK_SCHEMA_VERSION: Final[int] = 1
"""Version of the risk record contract. Carried on every record.

Separate from P5's journal version on purpose: this stream can evolve without touching the
canonical strategy journal, and a P5 journal recorded before this existed is unaffected.
"""

DEFAULT_RISK_TRACE_CAPACITY: Final[int] = 400_000
"""Bounded, from evidence. Real markets have produced 113k-204k cycles, and the risk stream
carries roughly one record per cycle plus a handful of operational signals."""


class RiskOrderError(RuntimeError):
    """A risk signal arrived out of order, or repeated one that cannot be repeated.

    Fail closed. A permission stream whose order is unknown has unknown provenance, and quietly
    sorting it into shape would manufacture confidence about the one thing this exists to prove.
    """


class RiskProvenance(Enum):
    """Where a risk stream came from. Stated on every record, never inferred."""

    REAL_PUBLIC_MARKET_DATA = "REAL_PUBLIC_MARKET_DATA"
    CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET = "CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET"
    SUPPORTING_UNIT_TEST = "SUPPORTING_UNIT_TEST"
    """Constructed. Never satisfies an empirical gate (ARCHITECTURE_SSOT §4.4)."""


class RiskSignalKind(Enum):
    """What kind of thing changed, or was checked."""

    RISK_EVALUATION = "RISK_EVALUATION"
    """A verdict taken against current feed health. The ordinary per-cycle record."""

    RECONCILIATION_CONFIRMED = "RECONCILIATION_CONFIRMED"
    """Someone established the truth for a latched reason. The only way a latch clears."""

    CLOCK_HEALTH_UPDATE = "CLOCK_HEALTH_UPDATE"
    API_ERROR_STATE_UPDATE = "API_ERROR_STATE_UPDATE"
    RATE_LIMIT_STATE_UPDATE = "RATE_LIMIT_STATE_UPDATE"
    ORDER_RECONCILIATION_RESULT = "ORDER_RECONCILIATION_RESULT"
    POSITION_RECONCILIATION_RESULT = "POSITION_RECONCILIATION_RESULT"
    COST_RECONCILIATION_RESULT = "COST_RECONCILIATION_RESULT"
    OPERATOR_CONTROL = "OPERATOR_CONTROL"
    """A command from a person, entering the ordered stream like any other signal.

    The UI never mutates anything; it produces an immutable command, and this is where that
    command becomes a fact with a sequence number attached to it."""

    RESOLUTION_SAFETY_UPDATE = "RESOLUTION_SAFETY_UPDATE"
    MAKER_ONLY_STATE_UPDATE = "MAKER_ONLY_STATE_UPDATE"
    TAKER_FILL_OBSERVED = "TAKER_FILL_OBSERVED"


class HealthFrame(NamedTuple):
    """P6's verdict, as read at one ingress position.

    Read, never computed: P6 owns staleness, its thresholds, and its monitor. This carries the
    result so a replay sees exactly the health the live run saw.
    """

    clob_status: HealthStatus = HealthStatus.UNKNOWN
    clob_awaiting_snapshot: bool = True
    spot_status: HealthStatus = HealthStatus.UNKNOWN
    order_stream_status: HealthStatus = HealthStatus.UNKNOWN
    order_stream_required: bool = False


class RiskSignal(NamedTuple):
    """One thing that happened which can change execution permission."""

    kind: RiskSignalKind
    as_of_ingress_ordinal: int
    timestamp: TimestampNs
    provenance: RiskProvenance = RiskProvenance.REAL_PUBLIC_MARKET_DATA
    reason: RiskReason | None = None
    """Which condition this signal concerns, where one applies."""

    flag: bool = False
    """The new value for a boolean operational condition."""

    value_ns: int = 0
    """The new value for a numeric operational condition, currently clock drift."""

    detail: str = ""
    """Short provenance note. Never load-bearing; never parsed."""


class RiskRecord(NamedTuple):
    """One signal, its position, and the verdict that resulted."""

    schema_version: int
    risk_sequence: int
    signal: RiskSignal
    health: HealthFrame
    state: RiskState
    active: frozenset[RiskReason]
    latched: frozenset[RiskReason]
    allows_place: bool
    allows_cancel: bool

    @property
    def as_of_ingress_ordinal(self) -> int:
        return self.signal.as_of_ingress_ordinal

    def summary(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "risk_sequence": self.risk_sequence,
            "kind": self.signal.kind.value,
            "as_of_ingress_ordinal": self.signal.as_of_ingress_ordinal,
            "timestamp": int(self.signal.timestamp),
            "provenance": self.signal.provenance.value,
            "reason": None if self.signal.reason is None else self.signal.reason.value,
            "state": self.state.value,
            "active": sorted(reason.value for reason in self.active),
            "latched": sorted(reason.value for reason in self.latched),
            "allows_place": self.allows_place,
            "allows_cancel": self.allows_cancel,
            "detail": self.signal.detail,
        }


@dataclass(slots=True)
class OperationalState:
    """Conditions that persist between evaluations and are not readable from a feed.

    Every field here changes only through an ordered signal. Nothing else may write to it —
    that is the whole point of the correction this module exists for.
    """

    clock_drift_ns: int = 0
    order_state_uncertain: bool = False
    maker_only_uncertain: bool = False
    position_mismatch: bool = False
    cost_ledger_mismatch: bool = False
    api_errors_exceeded: bool = False
    rate_limit_uncertain: bool = False
    resolution_ambiguous: bool = False
    operator_halt: bool = False
    taker_fill_seen: bool = False

    def snapshot(self) -> tuple[object, ...]:
        return (
            self.clock_drift_ns,
            self.order_state_uncertain,
            self.maker_only_uncertain,
            self.position_mismatch,
            self.cost_ledger_mismatch,
            self.api_errors_exceeded,
            self.rate_limit_uncertain,
            self.resolution_ambiguous,
            self.operator_halt,
            self.taker_fill_seen,
        )


_FLAG_FOR_KIND: Final[dict[RiskSignalKind, str]] = {
    RiskSignalKind.ORDER_RECONCILIATION_RESULT: "order_state_uncertain",
    RiskSignalKind.POSITION_RECONCILIATION_RESULT: "position_mismatch",
    RiskSignalKind.COST_RECONCILIATION_RESULT: "cost_ledger_mismatch",
    RiskSignalKind.API_ERROR_STATE_UPDATE: "api_errors_exceeded",
    RiskSignalKind.RATE_LIMIT_STATE_UPDATE: "rate_limit_uncertain",
    RiskSignalKind.RESOLUTION_SAFETY_UPDATE: "resolution_ambiguous",
    RiskSignalKind.OPERATOR_CONTROL: "operator_halt",
    RiskSignalKind.MAKER_ONLY_STATE_UPDATE: "maker_only_uncertain",
    RiskSignalKind.TAKER_FILL_OBSERVED: "taker_fill_seen",
}
"""Which operational field each signal kind sets. One mapping, so a new kind cannot silently
land in an untracked place."""


@dataclass(slots=True)
class RiskTrace:
    """Bounded, non-blocking record of the risk stream.

    Nothing here encodes JSON, writes a file, touches a database, or formats a log line. P11
    owns durable persistence; this is an in-memory ring so the audit contract cannot become the
    latency defect P8 exists to catch.
    """

    capacity: int = DEFAULT_RISK_TRACE_CAPACITY
    records: deque[RiskRecord] = field(default_factory=deque, repr=False)
    accepted: int = 0

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {self.capacity}")
        if self.records.maxlen != self.capacity:
            self.records = deque(self.records, maxlen=self.capacity)

    def append(self, record: RiskRecord) -> None:
        self.accepted += 1
        self.records.append(record)

    @property
    def dropped(self) -> int:
        return self.accepted - len(self.records)

    def __iter__(self) -> Iterator[RiskRecord]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)


@dataclass(slots=True)
class RiskController:
    """The single owner of risk state, and the only path that may change it.

    Every mutation is an ordered, recorded signal. There is deliberately no way to reach in and
    flip a condition: an earlier version let production code call ``engine.reconciled(...)``
    directly, which meant execution permission could change with no trace of when, why, or
    relative to which market events.
    """

    engine: RiskEngine = field(default_factory=RiskEngine)
    operational: OperationalState = field(default_factory=OperationalState)
    trace: RiskTrace = field(default_factory=RiskTrace)
    provenance: RiskProvenance = RiskProvenance.REAL_PUBLIC_MARKET_DATA
    health: HealthFrame = field(default_factory=HealthFrame)
    """The most recent health frame. An operational signal re-evaluates against it, so the
    verdict it records is the one that actually applied at that moment."""

    sequence: int = -1
    _last_ordinal: int = -1

    @property
    def config(self) -> RiskConfig:
        return self.engine.config

    @property
    def state(self) -> RiskState:
        return self.engine.state

    def evaluate(
        self, health: HealthFrame, *, as_of_ingress_ordinal: int, now_ns: TimestampNs
    ) -> RiskRecord:
        """Take a verdict against current feed health. The ordinary per-cycle path."""
        signal = RiskSignal(
            kind=RiskSignalKind.RISK_EVALUATION,
            as_of_ingress_ordinal=as_of_ingress_ordinal,
            timestamp=now_ns,
            provenance=self.provenance,
        )
        return self.apply(signal, health)

    def apply(self, signal: RiskSignal, health: HealthFrame | None = None) -> RiskRecord:
        """Record one signal, update state, and record the verdict that follows.

        Order is enforced rather than assumed: a signal whose ingress ordinal precedes the last
        one applied is rejected. A ``RECONCILIATION_CONFIRMED`` for a reason that is not
        currently latched is also rejected — it is either a duplicate or a claim about
        something that was never in doubt, and both should be noticed rather than absorbed.
        """
        if signal.as_of_ingress_ordinal < self._last_ordinal:
            raise RiskOrderError(
                f"risk signal at ingress ordinal {signal.as_of_ingress_ordinal} arrived after "
                f"{self._last_ordinal}; the risk stream is out of order and will not be "
                "silently reordered"
            )
        if health is not None:
            self.health = health

        if signal.kind is RiskSignalKind.RECONCILIATION_CONFIRMED:
            self._confirm(signal)
        elif signal.kind is RiskSignalKind.CLOCK_HEALTH_UPDATE:
            self.operational.clock_drift_ns = signal.value_ns
        else:
            field_name = _FLAG_FOR_KIND.get(signal.kind)
            if field_name is not None:
                setattr(self.operational, field_name, signal.flag)

        decision = self.engine.evaluate(self._inputs(signal.timestamp))
        self._last_ordinal = signal.as_of_ingress_ordinal
        self.sequence += 1
        record = RiskRecord(
            schema_version=RISK_SCHEMA_VERSION,
            risk_sequence=self.sequence,
            signal=signal,
            health=self.health,
            state=decision.state,
            active=decision.active,
            latched=decision.latched,
            allows_place=decision.allows_place,
            allows_cancel=decision.allows_cancel,
        )
        self.trace.append(record)
        return record

    def _confirm(self, signal: RiskSignal) -> None:
        reason = signal.reason
        if reason is None:
            raise RiskOrderError("a reconciliation signal must name the reason it resolves")
        if reason not in REQUIRES_RECONCILIATION:
            raise RiskOrderError(
                f"{reason.value} does not require reconciliation; it clears when its condition "
                "does, and confirming it would claim evidence that was never needed"
            )
        if reason not in self.engine.snapshot.latched:
            raise RiskOrderError(
                f"{reason.value} is not latched, so there is nothing to reconcile; this is "
                "either a duplicate signal or a claim about something never in doubt"
            )
        self.engine.reconciled(reason)

    def _inputs(self, now_ns: TimestampNs) -> RiskInputs:
        operational = self.operational
        health = self.health
        return RiskInputs(
            now_ns=now_ns,
            clob_status=health.clob_status,
            clob_awaiting_snapshot=health.clob_awaiting_snapshot,
            spot_status=health.spot_status,
            order_stream_status=health.order_stream_status,
            order_stream_required=health.order_stream_required,
            clock_drift_ns=operational.clock_drift_ns,
            order_state_uncertain=operational.order_state_uncertain,
            maker_only_uncertain=operational.maker_only_uncertain,
            position_mismatch=operational.position_mismatch,
            cost_ledger_mismatch=operational.cost_ledger_mismatch,
            api_errors_exceeded=operational.api_errors_exceeded,
            rate_limit_uncertain=operational.rate_limit_uncertain,
            resolution_ambiguous=operational.resolution_ambiguous,
            operator_halt=operational.operator_halt,
            taker_fill_seen=operational.taker_fill_seen,
        )

    def last_decision(self) -> RiskDecision | None:
        if not self.trace.records:
            return None
        record = self.trace.records[-1]
        return RiskDecision(
            state=record.state,
            active=record.active,
            latched=record.latched,
            snapshot=self.engine.snapshot,
        )

    def summary(self) -> dict[str, object]:
        return {
            "schema_version": RISK_SCHEMA_VERSION,
            "provenance": self.provenance.value,
            "risk_records": self.trace.accepted,
            "risk_records_retained": len(self.trace),
            "risk_records_dropped": self.trace.dropped,
            "last_risk_sequence": self.sequence,
            "state": self.engine.state.value,
        }
