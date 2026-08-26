"""The one path from a resolution decision to execution permission.

A settlement decision is not allowed to change what the bot may do by returning a value that
somebody remembers to check. P9 established that every permission change is an ordered,
recorded signal owned by ``RiskController``; ambiguity at settlement is such a change, so it
travels the same way and lands in the same audit trace as a stale feed or a failed
reconciliation.

The direction is one-way on purpose. Ambiguity sets the condition; settlement never clears it.

``RESOLUTION_AMBIGUOUS`` is in P9's ``REQUIRES_RECONCILIATION``, so clearing the condition is not
enough to restore SAFE: the reason stays latched until an explicit ``RECONCILIATION_CONFIRMED``
says somebody established what actually happened. A clean-looking second reading of the same
chain is not that. Both signals are ordered and recorded, so lifting a settlement halt leaves as
much of a trail as raising one.

(Through P10's first round this module's restraint *was* the safety property — P9 did not latch
the reason, and nothing but this file's refusal to emit ``flag=False`` kept the halt up. That is
what O16 asked about, and it is now answered in the risk contract rather than here.)
"""

from __future__ import annotations

from maker5m.market.timebase import TimestampNs
from maker5m.risk import RiskProvenance, RiskReason, RiskRecord, RiskSignal, RiskSignalKind
from maker5m.risk.trace import RiskController
from maker5m.settlement.resolution import ResolutionDecision, ResolutionState

__all__ = ["report_resolution", "resolution_safety_signal"]

_UNSAFE: frozenset[ResolutionState] = frozenset(
    {ResolutionState.AMBIGUOUS, ResolutionState.INSUFFICIENT_EVIDENCE}
)
"""Both are "we do not know what this market paid".

``INSUFFICIENT_EVIDENCE`` is included deliberately. It means quorum was never reached — no
provider disagreed, because too few answered at all. Treating that as merely "keep waiting"
would let a settlement window pass with the bot quoting into a market it cannot price.
``UNRESOLVED`` is not here: a market that has not resolved yet is the normal state of every
market before its end, and halting on it would halt always.
"""


def resolution_safety_signal(
    decision: ResolutionDecision,
    *,
    as_of_ingress_ordinal: int,
    now_ns: TimestampNs,
    provenance: RiskProvenance = RiskProvenance.REAL_PUBLIC_MARKET_DATA,
) -> RiskSignal | None:
    """The signal a decision warrants, or ``None`` when it warrants none.

    Pure. Returning ``None`` for a resolved or still-unresolved market means the caller emits
    nothing rather than emitting ``flag=False``, which would clear a latch that only
    reconciliation may clear.
    """
    if decision.state not in _UNSAFE:
        return None
    return RiskSignal(
        kind=RiskSignalKind.RESOLUTION_SAFETY_UPDATE,
        as_of_ingress_ordinal=as_of_ingress_ordinal,
        timestamp=now_ns,
        provenance=provenance,
        reason=RiskReason.RESOLUTION_AMBIGUOUS,
        flag=True,
    )


def report_resolution(
    controller: RiskController,
    decision: ResolutionDecision,
    *,
    as_of_ingress_ordinal: int,
    now_ns: TimestampNs,
) -> RiskRecord | None:
    """Apply the warranted signal through the controller, returning the record it produced.

    ``None`` means nothing was applied, and therefore nothing entered the trace — an ordinary
    resolution costs the audit stream no record at all.
    """
    signal = resolution_safety_signal(
        decision,
        as_of_ingress_ordinal=as_of_ingress_ordinal,
        now_ns=now_ns,
        provenance=controller.provenance,
    )
    if signal is None:
        return None
    return controller.apply(signal)
