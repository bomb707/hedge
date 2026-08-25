"""Applying a risk verdict to strategy intent, without touching the strategy.

Canonical §28 is explicit that these are "safety controls, not changes to the economic
strategy", so ``StrategyEngine.decide`` is left exactly as it is. It keeps producing what the
strategy economically wants, in every feed condition, and the verdict is applied afterwards:

```text
event -> reduce -> decide -> RiskEngine.evaluate -> risk_adjust -> prepare -> reconcile
```

The original ``DecisionResult`` is preserved for telemetry and the P5 journal. A replayed
journal therefore still shows what the strategy wanted at each step, which is the thing worth
recording — a journal that only showed the post-risk intent could not distinguish "the strategy
declined to quote" from "the strategy wanted to quote and safety refused".

Withdrawal, not reversal
------------------------
A halt turns desired intent into *nothing*. It does not turn it into a sell, a hedge, or a
flattening trade — Canonical §28 rejects conventional stop-loss behaviour and invariant I15
forbids flattening before settlement. Empty intent is exactly what P7's reconciler needs to
plan ``CANCEL`` for anything resting, so withdrawing quotes falls out of the existing minimal-
action rule rather than needing a second code path that knows how to retreat.
"""

from maker5m.risk.engine import RiskDecision
from maker5m.strategy.decision import DecisionResult, DesiredOrders

__all__ = ["EMPTY_INTENT", "risk_adjust"]

EMPTY_INTENT = DesiredOrders()
"""What a halt leaves behind. Holding balances is the whole of the retreat."""


def risk_adjust(decision: DecisionResult, risk: RiskDecision) -> DecisionResult:
    """The execution intent, after safety.

    Returns the **same object** when the verdict is SAFE, not an equal copy. Identity rather
    than equality is the testable form of "risk does not touch a healthy strategy": an
    ``is`` check cannot be satisfied by a reconstruction that happens to compare equal.
    """
    if risk.allows_place:
        return decision
    if decision.orders.is_empty:
        return decision
    return DecisionResult(orders=EMPTY_INTENT, telemetry=decision.telemetry)
