"""Deterministic execution records for P8 and P11.

Lightweight and value-only. No high-resolution latency fields — P8 owns latency measurement,
and adding half-built timing here would give the next phase something to unpick rather than
build on. Nothing is persisted synchronously (I19); these are values handed onward.
"""

from dataclasses import dataclass

from maker5m.domain import Outcome
from maker5m.execution.prepare import PreparationOutcome
from maker5m.execution.rate_limit import RateDecision
from maker5m.execution.reconciler import ReconcileAction, SideReason
from maker5m.numeric.units import PriceUnits, ShareUnits

__all__ = ["ExecutionRecord"]


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    """One side's execution outcome for one decision cycle."""

    outcome: Outcome
    action: ReconcileAction
    reason: SideReason
    client_order_id: str | None = None
    venue_order_id: str | None = None
    strategy_price: PriceUnits | None = None
    strategy_size: ShareUnits | None = None
    submitted_price: PriceUnits | None = None
    submitted_size: ShareUnits | None = None
    size_quantization_delta: ShareUnits | None = None
    live_price: PriceUnits | None = None
    live_remaining: ShareUnits | None = None
    post_only_guard: PreparationOutcome | None = None
    supersedes_client_order_id: str | None = None
    rate_decision: RateDecision | None = None
