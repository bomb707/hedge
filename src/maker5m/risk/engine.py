"""The risk verdict: pure, deterministic, and separate from the strategy.

Canonical §28 and Detailed §38 give the governing rule:

```text
IF THE BOT CANNOT TRUST ITS OWN STATE:
    stop new quoting
    reconcile state
    resume only when safe
```

Three things that rule is *not*, and which this module deliberately cannot do:

* It is not a stop-loss. Canonical §28 opens by saying the target strategy does not use one.
  There is no SELL, no hedge, no flatten, no merge, no split, no convert, and no directional
  rescue. A halt withdraws quotes and holds the balances exactly as they are (invariant I15).
* It is not ``band_hard``. P4 already implements the one-sided inventory wall, and it stays a
  wall: at ``I >= +band_hard`` the UP side is blocked while DOWN remains a legitimate order
  (invariant I17). Turning the wall into a global halt would convert a safety bound into the
  mean-reversion control Canonical §28 explicitly refuses.
* It is not part of the strategy. ``StrategyEngine.decide`` keeps producing what the strategy
  economically wants, and risk is an overlay applied afterwards. A healthy verdict leaves the
  intent byte-for-byte unchanged — the same object, not an equal copy.

Purity
------
``evaluate`` is a pure function of the previous snapshot and the inputs. No clock, no network,
no filesystem, no randomness, no logging side effect. ``now_ns`` and every threshold arrive as
arguments, so a replay produces the same halts as the run that recorded it (invariant I20).
"""

from dataclasses import dataclass, field
from typing import Final, NamedTuple

from maker5m.market.events import HealthStatus
from maker5m.market.timebase import TimestampNs
from maker5m.risk.config import RiskConfig
from maker5m.risk.monitors import clock_drift_exceeded
from maker5m.risk.reasons import REQUIRES_RECONCILIATION, RiskReason, RiskState

__all__ = ["RiskDecision", "RiskEngine", "RiskInputs", "RiskSnapshot", "evaluate"]

_UNTRUSTED_CLOB: Final[frozenset[HealthStatus]] = frozenset(
    {
        HealthStatus.DISCONNECTED,
        HealthStatus.SEQUENCE_GAP,
        HealthStatus.UNKNOWN,
    }
)
"""CLOB statuses that mean continuity is broken, not merely quiet."""

_UNTRUSTED_SPOT: Final[frozenset[HealthStatus]] = frozenset(
    {
        HealthStatus.DISCONNECTED,
        HealthStatus.SEQUENCE_GAP,
        HealthStatus.UNKNOWN,
    }
)

_UNTRUSTED_ORDER_STREAM: Final[frozenset[HealthStatus]] = frozenset(
    {
        HealthStatus.DISCONNECTED,
        HealthStatus.SEQUENCE_GAP,
        HealthStatus.STALE,
        HealthStatus.UNKNOWN,
    }
)
"""Any order-stream doubt blocks placement: an order that may or may not exist is not a
question to answer by placing another one."""


class RiskInputs(NamedTuple):
    """Everything the verdict depends on, supplied rather than fetched.

    Deliberately flat and primitive. A monitor that could reach out and read something would be
    a monitor whose answer depends on when it was asked.

    A ``NamedTuple`` rather than a frozen dataclass, measured rather than assumed: the dataclass
    form cost 1,907 ns to construct against roughly 350 ns here, and this is built on every
    evaluation of every cycle. The contract is identical — keyword construction, immutability,
    fields mypy checks — and it is the third time this codebase has paid for the dataclass form
    on a hot path.
    """

    now_ns: TimestampNs

    clob_status: HealthStatus = HealthStatus.UNKNOWN
    clob_awaiting_snapshot: bool = True
    spot_status: HealthStatus = HealthStatus.UNKNOWN

    order_stream_status: HealthStatus = HealthStatus.UNKNOWN
    order_stream_required: bool = False
    """Whether an authenticated order stream is expected to be up.

    ``False`` until P14: no credential exists, so no authenticated socket is opened and its
    ``UNKNOWN`` status is an accurate description of a stream we never asked for rather than
    evidence that anything is wrong. Reporting a permanent halt for an absent stream would make
    every other condition unobservable.
    """

    clock_drift_ns: int = 0
    order_state_uncertain: bool = False
    maker_only_uncertain: bool = False
    position_mismatch: bool = False
    cost_ledger_mismatch: bool = False
    api_errors_exceeded: bool = False
    rate_limit_uncertain: bool = False
    resolution_ambiguous: bool = False
    taker_fill_seen: bool = False

    reconciled: frozenset[RiskReason] = frozenset()
    """Reconciliation results that have been established since the halt began.

    Only reasons in ``REQUIRES_RECONCILIATION`` consult this. Nothing here can be set by a
    condition going quiet; it has to be recorded deliberately by whoever established the truth.
    """


class RiskSnapshot(NamedTuple):
    """The latched part of the verdict, carried between evaluations."""

    state: RiskState = RiskState.RECOVERING
    """Start in RECOVERING, not SAFE.

    A freshly started bot has an unknown book, an unknown spot feed, and no snapshot. Beginning
    at SAFE would mean the first evaluation had to *discover* that, and until it ran the system
    would look permitted.
    """

    latched: frozenset[RiskReason] = frozenset()
    """Reasons that halted us and still need reconciliation, even if the condition has passed."""

    clear_evaluations: int = 0
    """Consecutive evaluations with no active condition, for the recovery confirmation count."""


class RiskDecision(NamedTuple):
    """One verdict. Immutable, and carrying its own justification."""

    state: RiskState
    active: frozenset[RiskReason]
    """Conditions true right now."""

    latched: frozenset[RiskReason]
    """Reasons still awaiting reconciliation, whether or not their condition persists."""

    snapshot: RiskSnapshot
    """The state to carry into the next evaluation."""

    @property
    def allows_place(self) -> bool:
        """Only SAFE may create new risk. RECOVERING is not "nearly safe"."""
        return self.state is RiskState.SAFE

    @property
    def allows_cancel(self) -> bool:
        """Always. Withdrawing a quote reduces risk, and a halt must never trap us in one."""
        return True

    @property
    def halted(self) -> bool:
        return self.state is not RiskState.SAFE

    @property
    def reasons(self) -> frozenset[RiskReason]:
        return self.active | self.latched

    def summary(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "active": sorted(reason.value for reason in self.active),
            "latched": sorted(reason.value for reason in self.latched),
            "allows_place": self.allows_place,
            "allows_cancel": self.allows_cancel,
        }


def active_reasons(inputs: RiskInputs, config: RiskConfig) -> frozenset[RiskReason]:
    """Every Canonical §28.1 condition that is true right now. Pure.

    Feed staleness is **read, never computed**. P6 owns the question "has this stream been quiet
    too long?" — it holds the ``StalenessMonitor``, the ``OPERATIONAL`` thresholds, and the
    ``mark_stale`` transition, and its ``check_staleness`` runs on the capture loop's idle path
    so silence is detected without waiting for a market event. P9 owns only "what do we do when
    P6 says it is stale?".

    An earlier version of this function carried its own ``last_message_at`` comparison against
    its own copy of the threshold. Two authorities for one question is one too many: they can
    disagree, and the one that is wrong is invisible until it matters.
    """
    reasons: set[RiskReason] = set()

    if inputs.clob_status in _UNTRUSTED_CLOB or inputs.clob_awaiting_snapshot:
        reasons.add(RiskReason.CLOB_CONTINUITY_UNCERTAIN)
    if inputs.clob_status is HealthStatus.STALE:
        reasons.add(RiskReason.CLOB_STALE)

    # The spot feed has no snapshot concept, so every way of not being trustworthy — quiet,
    # disconnected, or never yet seen — is the same condition.
    if inputs.spot_status is HealthStatus.STALE or inputs.spot_status in _UNTRUSTED_SPOT:
        reasons.add(RiskReason.SPOT_STALE)

    if inputs.order_stream_required and inputs.order_stream_status in _UNTRUSTED_ORDER_STREAM:
        reasons.add(RiskReason.ORDER_STATE_UNCERTAIN)
    if inputs.order_state_uncertain:
        reasons.add(RiskReason.ORDER_STATE_UNCERTAIN)

    if clock_drift_exceeded(inputs.clock_drift_ns, config.clock_drift_limit_ns):
        reasons.add(RiskReason.CLOCK_DRIFT)
    if inputs.maker_only_uncertain:
        reasons.add(RiskReason.MAKER_ONLY_UNCERTAIN)
    if inputs.position_mismatch:
        reasons.add(RiskReason.POSITION_MISMATCH)
    if inputs.cost_ledger_mismatch:
        reasons.add(RiskReason.COST_LEDGER_MISMATCH)
    if inputs.api_errors_exceeded:
        reasons.add(RiskReason.API_ERROR_RATE)
    if inputs.rate_limit_uncertain:
        reasons.add(RiskReason.RATE_LIMIT_UNCERTAIN)
    if inputs.resolution_ambiguous:
        reasons.add(RiskReason.RESOLUTION_AMBIGUOUS)
    if inputs.taker_fill_seen:
        reasons.add(RiskReason.TAKER_FILL)

    return frozenset(reasons)


def evaluate(previous: RiskSnapshot, inputs: RiskInputs, config: RiskConfig) -> RiskDecision:
    """The whole verdict, as a pure function of the previous snapshot and the inputs.

    The transition rules, stated once:

    * Any active condition means HALTED, and every reason in ``REQUIRES_RECONCILIATION`` that
      goes active is latched — it will outlive its own condition.
    * With no active condition but something latched or unconfirmed, the state is RECOVERING.
      That is where a reconnected feed sits while its snapshot is still missing, and where an
      unknown order sits until someone has actually asked the venue.
    * SAFE requires all of: no active condition, nothing latched, and
      ``recovery_confirmations`` consecutive clear evaluations. One healthy message clears
      nothing on its own, which is the point.
    """
    active = active_reasons(inputs, config)

    # Latch first, so a reason that appears and vanishes inside one evaluation still requires
    # its reconciliation. Reconciliation results only ever *remove* from the latch.
    latched = (previous.latched | (active & REQUIRES_RECONCILIATION)) - inputs.reconciled
    # A reason whose condition is still active cannot be reconciled away.
    latched |= active & REQUIRES_RECONCILIATION

    if active:
        snapshot = RiskSnapshot(state=RiskState.HALTED, latched=latched, clear_evaluations=0)
        return RiskDecision(RiskState.HALTED, active, latched, snapshot)

    confirmations = previous.clear_evaluations + 1
    if latched or confirmations < config.recovery_confirmations:
        snapshot = RiskSnapshot(
            state=RiskState.RECOVERING, latched=latched, clear_evaluations=confirmations
        )
        return RiskDecision(RiskState.RECOVERING, active, latched, snapshot)

    snapshot = RiskSnapshot(
        state=RiskState.SAFE, latched=frozenset(), clear_evaluations=confirmations
    )
    return RiskDecision(RiskState.SAFE, active, frozenset(), snapshot)


@dataclass(slots=True)
class RiskEngine:
    """A thin stateful holder around the pure :func:`evaluate`.

    The engine owns only the latched snapshot. Every decision is still a pure function of that
    snapshot and the inputs, so a test can drive the transition function directly and a replay
    can reconstruct the same sequence of verdicts from the same ordered inputs.
    """

    config: RiskConfig = field(default_factory=RiskConfig)
    snapshot: RiskSnapshot = field(default_factory=RiskSnapshot)

    def evaluate(self, inputs: RiskInputs) -> RiskDecision:
        decision = evaluate(self.snapshot, inputs, self.config)
        self.snapshot = decision.snapshot
        return decision

    def reconciled(self, *reasons: RiskReason) -> None:
        """Record that a reconciliation has established the truth for these reasons.

        Deliberately explicit and deliberately separate from evaluation: nothing observable on
        a feed can call this. Someone has to have actually compared our state against an
        authoritative source and found it consistent.
        """
        unknown = set(reasons) - REQUIRES_RECONCILIATION
        if unknown:
            names = ", ".join(sorted(reason.value for reason in unknown))
            raise ValueError(f"these reasons do not require reconciliation: {names}")
        self.snapshot = self.snapshot._replace(latched=self.snapshot.latched - set(reasons))

    @property
    def state(self) -> RiskState:
        return self.snapshot.state
