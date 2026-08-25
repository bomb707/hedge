"""The minimal-action reconciler. Pure function, and the most load-bearing code in P7.

Canonical §20 and §33 state the rule this exists to enforce:

```text
UNCHANGED ORDER -> KEEP QUEUE POSITION
```

Every cancellation destroys a queue timestamp, and fill rate collapses as depth-ahead grows —
38 fills at zero ahead, 0 at 30 ahead in the reconstruction (Canonical §10.1). So the default
is KEEP and the burden of proof is on any action that gives up a queue slot.

There is deliberately **no** age-based, timer-based, or event-count-based replacement. A
market-data event arriving is not a reason to touch an order (I09, I10).

The one subtlety worth stating: a partially-filled order whose *remaining* size equals the
newly desired size is a KEEP. Naively comparing the original size would cancel a good order
after every partial fill and hand its queue position away for nothing.

Pure: no clock, no network, no sleep, no logging. Time enters only as an explicit argument to
the rate limiter, which is a separate concern applied after this decision.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from maker5m.domain import Outcome
from maker5m.execution.live_orders import LiveOrder, OrderLifecycle
from maker5m.execution.prepare import PreparationOutcome, PreparedOrder

__all__ = ["ReconcileAction", "ReconcilePlan", "SideAction", "reconcile"]


class ReconcileAction(Enum):
    """The complete set of things execution may do about one side."""

    NOTHING = "NOTHING"
    """No desired order and nothing live. The common case in PREARM and DONE."""

    KEEP = "KEEP"
    """A live order already expresses the desired state. Queue position preserved."""

    PLACE = "PLACE"
    CANCEL = "CANCEL"
    REPLACE = "REPLACE"

    WAIT = "WAIT"
    """A request is in flight or state is unknown. Acting now risks a duplicate."""

    BLOCKED = "BLOCKED"
    """The desired order exists but cannot legally or safely be submitted."""


class SideReason(Enum):
    """Why the action was chosen. Typed so telemetry can classify, never free text."""

    NO_DESIRED_NO_LIVE = "NO_DESIRED_NO_LIVE"
    DESIRED_WITHDRAWN = "DESIRED_WITHDRAWN"
    UNCHANGED = "UNCHANGED"
    NO_LIVE_ORDER = "NO_LIVE_ORDER"
    PRICE_CHANGED = "PRICE_CHANGED"
    SIZE_CHANGED = "SIZE_CHANGED"
    IN_FLIGHT = "IN_FLIGHT"
    UNKNOWN_STATE = "UNKNOWN_STATE"
    NOT_SUBMITTABLE = "NOT_SUBMITTABLE"
    UNSAFE_REPLACEMENT = "UNSAFE_REPLACEMENT"


@dataclass(frozen=True, slots=True)
class SideAction:
    """What to do about one outcome, and why."""

    outcome: Outcome
    action: ReconcileAction
    reason: SideReason
    prepared: PreparedOrder | None = None
    live: LiveOrder | None = None
    preparation: PreparationOutcome | None = None

    @property
    def requires_request(self) -> bool:
        """Whether this action consumes network capacity."""
        return self.action in (
            ReconcileAction.PLACE,
            ReconcileAction.CANCEL,
            ReconcileAction.REPLACE,
        )


@dataclass(frozen=True, slots=True)
class ReconcilePlan:
    """The minimal action set for both sides."""

    up: SideAction
    down: SideAction

    @property
    def sides(self) -> tuple[SideAction, SideAction]:
        return (self.up, self.down)

    @property
    def request_count(self) -> int:
        return sum(1 for side in self.sides if side.requires_request)

    def action_for(self, outcome: Outcome) -> SideAction:
        return self.up if outcome is Outcome.UP else self.down


def _reconcile_side(
    outcome: Outcome, prepared: PreparedOrder | None, live: LiveOrder | None
) -> SideAction:
    # An in-flight or unknown order is resolved before anything else is attempted. Placing a
    # second order because the first acknowledgement is slow is how duplicates happen.
    if live is not None and live.status.in_flight:
        reason = (
            SideReason.UNKNOWN_STATE
            if live.status is OrderLifecycle.UNKNOWN
            else SideReason.IN_FLIGHT
        )
        return SideAction(outcome, ReconcileAction.WAIT, reason, prepared, live)

    if prepared is None:
        if live is None:
            return SideAction(outcome, ReconcileAction.NOTHING, SideReason.NO_DESIRED_NO_LIVE)
        return SideAction(outcome, ReconcileAction.CANCEL, SideReason.DESIRED_WITHDRAWN, None, live)

    if live is not None:
        # The load-bearing comparison. Remaining size, not original: a partially filled order
        # whose remainder matches the new desired size is exactly what we want resting.
        if (
            prepared.submittable
            and live.price == prepared.submission_price
            and live.remaining_size == prepared.submission_size
        ):
            return SideAction(
                outcome,
                ReconcileAction.KEEP,
                SideReason.UNCHANGED,
                prepared,
                live,
                prepared.outcome_status,
            )
        if not prepared.submittable:
            # Retire the stale order, but never replace it with something unsafe.
            return SideAction(
                outcome,
                ReconcileAction.CANCEL,
                SideReason.UNSAFE_REPLACEMENT,
                prepared,
                live,
                prepared.outcome_status,
            )
        reason = (
            SideReason.PRICE_CHANGED
            if live.price != prepared.submission_price
            else SideReason.SIZE_CHANGED
        )
        return SideAction(
            outcome, ReconcileAction.REPLACE, reason, prepared, live, prepared.outcome_status
        )

    if not prepared.submittable:
        return SideAction(
            outcome,
            ReconcileAction.BLOCKED,
            SideReason.NOT_SUBMITTABLE,
            prepared,
            None,
            prepared.outcome_status,
        )
    return SideAction(
        outcome,
        ReconcileAction.PLACE,
        SideReason.NO_LIVE_ORDER,
        prepared,
        None,
        prepared.outcome_status,
    )


def reconcile(
    prepared: Mapping[Outcome, PreparedOrder | None],
    live: Mapping[Outcome, LiveOrder | None],
) -> ReconcilePlan:
    """Compute the minimal action set. Pure, total, and independent of dispatch order.

    Both sides are always evaluated. A fill on one side changes ``total_cost`` and therefore
    both desired sizes (I08), so reconciling only the filled side would leave the other
    stale.
    """
    return ReconcilePlan(
        up=_reconcile_side(Outcome.UP, prepared.get(Outcome.UP), live.get(Outcome.UP)),
        down=_reconcile_side(Outcome.DOWN, prepared.get(Outcome.DOWN), live.get(Outcome.DOWN)),
    )
