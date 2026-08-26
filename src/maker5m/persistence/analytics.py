"""Canonical §26 per-market metrics, and the flattening of P9/P10 records for storage.

The accounting is **not** restated here. Term1 and Term2 come from
:mod:`maker5m.accounting.decomposition`, which already carries the corrected general form for
the case where the bot ends holding more of the loser — a second copy would be a second thing to
get wrong, and the corrected form exists precisely because the frozen document's literal one is
wrong in that case.

Every metric below is defined, because Canonical §26 lists names and not definitions, and a
metric whose meaning is inferred is a metric two readers will disagree about.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Final

from maker5m.accounting.decomposition import decompose
from maker5m.accounting.ledger import LedgerState
from maker5m.domain import Outcome
from maker5m.numeric.units import MoneyUnits, ShareUnits
from maker5m.persistence.schema import (
    METRICS_SCHEMA_VERSION,
    RISK_ROW_SCHEMA_VERSION,
    SETTLEMENT_ROW_SCHEMA_VERSION,
    ExactRatio,
    MarketMetrics,
    RiskRow,
    SettlementRow,
)

__all__ = ["MetricsAccumulator", "risk_row", "settlement_row"]

ZERO_MONEY: Final[MoneyUnits] = MoneyUnits(0)

QUOTE_INTENT_ACTIONS: Final[frozenset[str]] = frozenset({"PLACE", "KEEP", "REPLACE"})
"""Actions meaning "the strategy wants a resting order on this side".

Canonical §26 says "quote count" without defining it. Both readings are stored — this one and
the bare PLACE count — rather than one being chosen silently, because they answer different
questions and the difference is large: a market that keeps one order all day has ~1 place and
tens of thousands of quote intents."""


@dataclass(slots=True)
class MetricsAccumulator:
    """Folds decision and fill records into one market's §26 metrics.

    Incremental rather than a final pass over stored rows, so a market that is still running has
    live metrics and a market that crashed has whatever was true when it stopped.
    """

    market_id: str
    slug: str
    provenance: str

    decision_count: int = 0
    actions: Counter[str] = field(default_factory=Counter)
    quote_intent_count: int = 0
    stale_quote_count: int = 0

    queue_ahead_sum: int = 0
    queue_ahead_samples: int = 0
    queue_stale_samples: int = 0

    maker_fill_count: int = 0
    taker_fill_count: int = 0
    unknown_liquidity_fill_count: int = 0

    last_pnl_if_up: MoneyUnits = ZERO_MONEY
    last_pnl_if_down: MoneyUnits = ZERO_MONEY

    def observe_decision(self, record: Any) -> None:
        """Fold one `DecisionRecord`. Counts every typed action, never a free-text label."""
        self.decision_count += 1
        for side in (record.up, record.down):
            self.actions[side.action] += 1
            if side.action in QUOTE_INTENT_ACTIONS:
                self.quote_intent_count += 1
            if side.queue_ahead is not None:
                self.queue_ahead_sum += int(side.queue_ahead)
                self.queue_ahead_samples += 1
            if side.queue_confidence == "STALE":
                self.queue_stale_samples += 1
                self.stale_quote_count += 1
        self.last_pnl_if_up = record.pnl_if_up_without_rebate
        self.last_pnl_if_down = record.pnl_if_down_without_rebate

    def observe_fill(self, record: Any) -> None:
        """Fold one `FillRecord`. Liquidity is counted as reported, never repaired."""
        if record.liquidity == "MAKER":
            self.maker_fill_count += 1
        elif record.liquidity == "TAKER":
            self.taker_fill_count += 1
        else:
            self.unknown_liquidity_fill_count += 1

    @property
    def fill_count(self) -> int:
        return self.maker_fill_count + self.taker_fill_count + self.unknown_liquidity_fill_count

    def build(
        self,
        ledger: LedgerState,
        *,
        winner: Outcome | None,
        rebate_mode: str = "WITHOUT_REBATE",
    ) -> MarketMetrics:
        """The finished §26 metric set.

        ``winner`` is ``None`` for a market that never settled — the terms are then genuinely
        undefined rather than zero, and are stored as ``None``. Gross payout is likewise zero
        only because nothing has been paid, which is a different statement from "the payout was
        zero" and is disambiguated by ``settled``.
        """
        rebate = ledger.realised_rebates if rebate_mode == "REALISED_REBATE" else MoneyUnits(0)
        if rebate_mode == "ESTIMATED_REBATE":
            rebate = ledger.estimated_rebates

        if winner is None:
            gross = MoneyUnits(0)
            term1 = term2 = None
            avg_w = avg_l = None
            matched = None
            residual_side = None
            residual_magnitude = ShareUnits(abs(ledger.net_inventory))
            net = MoneyUnits(-ledger.total_cost - ledger.fees + rebate)
        else:
            terms = decompose(ledger, winner)
            gross = MoneyUnits(terms.trading_pnl + ledger.total_cost)
            term1 = ExactRatio.of(terms.term1)
            term2 = ExactRatio.of(terms.term2)
            avg_w = ExactRatio.of(terms.average_price_winner)
            avg_l = ExactRatio.of(terms.average_price_loser)
            matched = terms.matched_shares
            side = terms.residual_side
            residual_side = None if side is None else side.value
            residual_magnitude = ShareUnits(
                terms.winner_residual_shares + terms.loser_residual_shares
            )
            net = MoneyUnits(terms.trading_pnl - ledger.fees + rebate)

        return MarketMetrics(
            schema_version=METRICS_SCHEMA_VERSION,
            market_id=self.market_id,
            slug=self.slug,
            settled=winner is not None,
            winner=None if winner is None else winner.value,
            gross_payout=gross,
            total_cost=ledger.total_cost,
            fees=ledger.fees,
            estimated_rebates=ledger.estimated_rebates,
            realised_rebates=ledger.realised_rebates,
            rebate_mode=rebate_mode,
            net_pnl=net,
            term1=term1,
            term2=term2,
            average_price_winner=avg_w,
            average_price_loser=avg_l,
            matched_shares=matched,
            n_up=ledger.n_up,
            n_down=ledger.n_down,
            terminal_inventory=ledger.net_inventory,
            terminal_residual_side=residual_side,
            terminal_residual_magnitude=residual_magnitude,
            pnl_if_up_before_settlement=self.last_pnl_if_up,
            pnl_if_down_before_settlement=self.last_pnl_if_down,
            maker_fill_count=self.maker_fill_count,
            taker_fill_count=self.taker_fill_count,
            unknown_liquidity_fill_count=self.unknown_liquidity_fill_count,
            fill_count=self.fill_count,
            queue_ahead_sum=self.queue_ahead_sum,
            queue_ahead_samples=self.queue_ahead_samples,
            queue_stale_samples=self.queue_stale_samples,
            place_count=self.actions["PLACE"],
            keep_count=self.actions["KEEP"],
            cancel_count=self.actions["CANCEL"],
            replace_count=self.actions["REPLACE"],
            wait_count=self.actions["WAIT"],
            blocked_count=self.actions["BLOCKED"],
            nothing_count=self.actions["NOTHING"],
            quote_intent_count=self.quote_intent_count,
            stale_quote_count=self.stale_quote_count,
            decision_count=self.decision_count,
            provenance=self.provenance,
        )


def risk_row(record: Any, *, market_id: str, persistence_sequence: int) -> RiskRow:
    """Flatten one P9 ``RiskRecord`` without interpreting any of it.

    Every field is copied as recorded. P11 does not decide what a risk state means, does not
    recompute `allows_place`, and does not reorder anything: `risk_sequence` stays the ordering
    key so a persisted trace remains a valid input to P9's own replay verifier.
    """
    signal = record.signal
    return RiskRow(
        schema_version=RISK_ROW_SCHEMA_VERSION,
        persistence_sequence=persistence_sequence,
        market_id=market_id,
        risk_sequence=record.risk_sequence,
        as_of_ingress_ordinal=signal.as_of_ingress_ordinal,
        signal_kind=signal.kind.value,
        signal_reason=None if signal.reason is None else signal.reason.value,
        signal_flag=bool(signal.flag),
        signal_timestamp_ns=signal.timestamp,
        signal_value_ns=getattr(signal, "value_ns", None),
        state=record.state.value,
        active=tuple(sorted(reason.value for reason in record.active)),
        latched=tuple(sorted(reason.value for reason in record.latched)),
        allows_place=record.allows_place,
        allows_cancel=record.allows_cancel,
        provenance=signal.provenance.value,
        risk_schema_version=record.schema_version,
    )


def settlement_row(record: Any, *, market_id: str, persistence_sequence: int) -> SettlementRow:
    """Flatten one P10 ``SettlementRecord``. No key, no signature, no credential."""
    decision = record.decision
    payout = decision.payout
    plan = getattr(record, "plan", None)
    paper = getattr(record, "settlement", None)
    return SettlementRow(
        schema_version=SETTLEMENT_ROW_SCHEMA_VERSION,
        persistence_sequence=persistence_sequence,
        market_id=market_id,
        slug=record.target.slug,
        condition_id=record.target.condition_id,
        resolution_state=decision.state.value,
        winning_outcome=(
            None if decision.winning_outcome is None else decision.winning_outcome.value
        ),
        payout_denominator=None if payout is None else payout.denominator,
        payout_numerators=() if payout is None else tuple(payout.numerators),
        outcome_slot_count=None if payout is None else payout.outcome_slot_count,
        authoritative_block=decision.authoritative_block,
        block_tag=decision.block_tag,
        agreeing_providers=tuple(decision.agreeing_providers),
        answering_providers=tuple(decision.answering_providers),
        minimum_agreeing_providers=record.policy.minimum_agreeing_providers,
        reasons=tuple(reason.value for reason in decision.reasons),
        advisory=tuple((item.source, item.winning_slot) for item in decision.advisory),
        expected_redeem_value=None if paper is None else paper.expected_redeem_value,
        paper_settlement_pnl=None if paper is None else paper.paper_settlement_pnl,
        rebate_mode=None if paper is None else paper.rebate_mode.value,
        redeem_plan_condition_id=None if plan is None else plan.condition_id,
        redeem_plan_index_sets=() if plan is None else tuple(plan.index_sets),
        redeem_blockers=tuple(blocker.value for blocker in getattr(record, "blockers", ())),
        redemption_enabled=False,
    )
