"""Settlement arithmetic, redemption planning, and the boundary that cannot be crossed.

**SUPPORTING UNIT TEST ONLY.** Constructed ledgers and payout vectors.

The load-bearing assertion here is the P1 equality: for a binary payout, the settlement value
computed from the CTF vector must equal `LedgerState.pnl_if(winner, mode)` to the last
`MoneyUnit`. There is exactly one settlement equation in this codebase (Detailed §33), and this
proves P10 did not quietly introduce a second one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import maker5m
from maker5m.accounting.ledger import LedgerState, RebateMode
from maker5m.domain import Outcome
from maker5m.numeric import MoneyUnits, ShareUnits, parse_money, parse_share
from maker5m.settlement import (
    CTF_ADDRESS,
    PARENT_COLLECTION_ID,
    PUSD_ADDRESS,
    AmbiguityReason,
    MarketResolutionTarget,
    PayoutVector,
    RedeemBlocker,
    Redeemer,
    RedemptionDisabledError,
    ResolutionDecision,
    ResolutionState,
    SettlementPreconditions,
    binary_index_sets,
    encode_redeem_positions,
    outcome_payout,
    plan_redemption,
    settle_on_paper,
)

CONDITION = "0x38cd5ae6f1edc9b5256aec2104570c7b65edcf523a923c427cf7174f5c81ad8a"
SRC = Path(maker5m.__file__).parent


def target(**overrides: object) -> MarketResolutionTarget:
    base = MarketResolutionTarget(
        slug="btc-updown-5m-1787678100",
        condition_id=CONDITION,
        up_token_id="1108124849990432312390859405610024220096750055559039808496404328810196773281",
        down_token_id="1026460359682193161830826734616592566861294327338545869450442233082753432089",
    )
    return base._replace(**overrides)  # type: ignore[arg-type]


def ledger(up: str = "120", down: str = "100", cost: str = "122") -> LedgerState:
    """The mandatory P1 example: 120 UP at $72 and 100 DOWN at $50."""
    return (
        LedgerState(
            n_up=parse_share(up),
            n_down=parse_share(down),
            cost_up=parse_money("72"),
            cost_down=parse_money("50"),
        )
        if cost == "122"
        else LedgerState(n_up=parse_share(up), n_down=parse_share(down))
    )


def resolved(numerators: tuple[int, ...], outcome: Outcome | None) -> ResolutionDecision:
    return ResolutionDecision(
        state=ResolutionState.RESOLVED,
        payout=PayoutVector(1, numerators, 2),
        winning_outcome=outcome,
        agreeing_providers=("a", "b", "c"),
        answering_providers=("a", "b", "c"),
        authoritative_block=92_665_372,
    )


# -- exact CTF arithmetic ------------------------------------------------------------------


def test_a_winning_slot_pays_par() -> None:
    assert outcome_payout(parse_share("120"), 1, 1) == parse_money("120")


def test_a_losing_slot_pays_nothing() -> None:
    assert outcome_payout(parse_share("100"), 0, 1) == MoneyUnits(0)


def test_fractional_payout_truncates_the_way_solidity_does() -> None:
    """`balance * numerator / denominator` with integer division, floor for non-negatives."""
    # 1 share unit is 1/1_000_000 of a share; at par that is 1 money unit. Thirds truncate.
    assert outcome_payout(ShareUnits(10), 1, 3) == MoneyUnits(3)
    assert outcome_payout(ShareUnits(1), 1, 2) == MoneyUnits(0)
    assert outcome_payout(parse_share("1"), 1, 2) == parse_money("0.5")
    assert outcome_payout(parse_share("1"), 1, 3) == MoneyUnits(333_333)


def test_negative_inputs_are_refused_rather_than_wrapped() -> None:
    for args in (
        (ShareUnits(-1), 1, 1),
        (ShareUnits(1), -1, 1),
        (ShareUnits(1), 1, 0),
    ):
        with pytest.raises(ValueError):
            outcome_payout(*args)


# -- the P1 equality invariant ----------------------------------------------------------------


@pytest.mark.parametrize(("numerators", "outcome"), [((1, 0), Outcome.UP), ((0, 1), Outcome.DOWN)])
@pytest.mark.parametrize("mode", list(RebateMode))
def test_binary_paper_settlement_equals_the_p1_equation_exactly(
    numerators: tuple[int, int], outcome: Outcome, mode: RebateMode
) -> None:
    state = ledger()
    settlement = settle_on_paper(state, PayoutVector(1, numerators, 2), target(), rebate_mode=mode)
    assert settlement.paper_settlement_pnl == state.pnl_if(outcome, mode)
    assert settlement.winning_outcome is outcome


def test_the_mandatory_accounting_example_settles_to_the_documented_figures() -> None:
    """120 UP at $72 and 100 DOWN at $50 -> -$2 if UP, -$22 if DOWN."""
    state = ledger()
    up = settle_on_paper(
        state, PayoutVector(1, (1, 0), 2), target(), rebate_mode=RebateMode.WITHOUT_REBATE
    )
    down = settle_on_paper(
        state, PayoutVector(1, (0, 1), 2), target(), rebate_mode=RebateMode.WITHOUT_REBATE
    )
    assert up.expected_redeem_value == parse_money("120")
    assert up.paper_settlement_pnl == parse_money("-2")
    assert down.expected_redeem_value == parse_money("100")
    assert down.paper_settlement_pnl == parse_money("-22")


def test_a_fractional_payout_still_settles_without_a_winner() -> None:
    """Summing per-slot payouts works for any vector; only the winner label is absent."""
    state = ledger()
    settlement = settle_on_paper(
        state, PayoutVector(2, (1, 1), 2), target(), rebate_mode=RebateMode.WITHOUT_REBATE
    )
    assert settlement.up_payout == parse_money("60")
    assert settlement.down_payout == parse_money("50")
    assert settlement.expected_redeem_value == parse_money("110")
    assert settlement.winning_outcome is None


def test_settling_an_unresolved_vector_is_refused() -> None:
    with pytest.raises(ValueError, match="unresolved"):
        settle_on_paper(
            ledger(), PayoutVector(0, (0, 0), 2), target(), rebate_mode=RebateMode.WITHOUT_REBATE
        )


# -- redeem plan --------------------------------------------------------------------------------


def test_a_resolved_market_plans_a_redemption() -> None:
    plan, blockers = plan_redemption(
        target(), resolved((1, 0), Outcome.UP), SettlementPreconditions()
    )
    assert blockers == ()
    assert plan is not None
    assert plan.to == CTF_ADDRESS
    assert plan.collateral_token == PUSD_ADDRESS
    assert plan.parent_collection_id == PARENT_COLLECTION_ID
    assert plan.parent_collection_id == "0x" + "00" * 32
    assert plan.condition_id == CONDITION
    assert plan.value == 0


def test_index_sets_are_bit_masks_not_slot_indices() -> None:
    """`(1, 2)`, not `(0, 1)`. The empty set is rejected on chain and slot 1 would be missed."""
    assert binary_index_sets() == (1, 2)
    plan, _ = plan_redemption(target(), resolved((0, 1), Outcome.DOWN), SettlementPreconditions())
    assert plan is not None
    assert plan.index_sets == (1, 2)
    assert 0 not in plan.index_sets


def test_the_calldata_matches_the_abi_layout_word_by_word() -> None:
    calldata = encode_redeem_positions(
        collateral_token=PUSD_ADDRESS,
        parent_collection_id=PARENT_COLLECTION_ID,
        condition_id=CONDITION,
        index_sets=(1, 2),
    )
    assert calldata.startswith("0x01b7037c")
    body = calldata[10:]
    words = [body[i : i + 64] for i in range(0, len(body), 64)]
    # Four head words, then the tail: length followed by its two elements.
    assert len(words) == 7
    assert words[0] == PUSD_ADDRESS.removeprefix("0x").lower().rjust(64, "0")
    assert words[1] == "00" * 32
    assert words[2] == CONDITION.removeprefix("0x")
    assert int(words[3], 16) == 4 * 32, "offset to the dynamic array"
    assert int(words[4], 16) == 2, "array length"
    assert [int(words[5], 16), int(words[6], 16)] == [1, 2]


def test_an_empty_or_zero_index_set_is_refused() -> None:
    for bad in ((), (0,), (1, 0)):
        with pytest.raises(ValueError):
            encode_redeem_positions(
                collateral_token=PUSD_ADDRESS,
                parent_collection_id=PARENT_COLLECTION_ID,
                condition_id=CONDITION,
                index_sets=bad,
            )


# -- every blocker ------------------------------------------------------------------------------


def test_an_unresolved_market_plans_nothing() -> None:
    decision = ResolutionDecision(state=ResolutionState.UNRESOLVED)
    plan, blockers = plan_redemption(target(), decision, SettlementPreconditions())
    assert plan is None
    assert RedeemBlocker.NOT_RESOLVED in blockers


def test_an_ambiguous_market_plans_nothing() -> None:
    decision = ResolutionDecision(
        state=ResolutionState.AMBIGUOUS, reasons=(AmbiguityReason.PROVIDER_DISAGREEMENT,)
    )
    plan, blockers = plan_redemption(target(), decision, SettlementPreconditions())
    assert plan is None
    assert RedeemBlocker.RESOLUTION_AMBIGUOUS in blockers


@pytest.mark.parametrize(
    ("field", "blocker"),
    [
        ("order_state_uncertain", RedeemBlocker.ORDER_STATE_UNCERTAIN),
        ("position_mismatch", RedeemBlocker.POSITION_MISMATCH),
        ("ledger_inconsistent", RedeemBlocker.LEDGER_INCONSISTENT),
    ],
)
def test_each_precondition_blocks_redemption(field: str, blocker: RedeemBlocker) -> None:
    preconditions = SettlementPreconditions(**{field: True})
    plan, blockers = plan_redemption(target(), resolved((1, 0), Outcome.UP), preconditions)
    assert plan is None
    assert blocker in blockers


def test_a_live_order_blocks_redemption() -> None:
    """DONE is not proof that every cancellation reached the venue."""
    plan, blockers = plan_redemption(
        target(), resolved((1, 0), Outcome.UP), SettlementPreconditions(occupying_orders=1)
    )
    assert plan is None
    assert RedeemBlocker.LIVE_ORDERS_PRESENT in blockers


def test_nothing_to_redeem_blocks_redemption() -> None:
    plan, blockers = plan_redemption(
        target(), resolved((1, 0), Outcome.UP), SettlementPreconditions(), has_balance=False
    )
    assert plan is None
    assert RedeemBlocker.NOTHING_TO_REDEEM in blockers


def test_there_is_no_optimistic_branch() -> None:
    """Every failure combination yields no plan, never a partial or best-effort one."""
    decision = ResolutionDecision(state=ResolutionState.INSUFFICIENT_EVIDENCE)
    preconditions = SettlementPreconditions(
        occupying_orders=2, order_state_uncertain=True, position_mismatch=True
    )
    plan, blockers = plan_redemption(target(), decision, preconditions, has_balance=False)
    assert plan is None
    assert len(blockers) >= 4


# -- the write boundary --------------------------------------------------------------------------


def test_submitting_a_redemption_always_raises() -> None:
    plan, _ = plan_redemption(target(), resolved((1, 0), Outcome.UP), SettlementPreconditions())
    assert plan is not None
    with pytest.raises(RedemptionDisabledError, match="disabled"):
        Redeemer().submit(plan)


def test_preparing_is_safe_and_produces_only_a_value() -> None:
    plan, blockers = Redeemer().prepare(
        target(), resolved((0, 1), Outcome.DOWN), SettlementPreconditions()
    )
    assert blockers == ()
    assert plan is not None and plan.winning_outcome is Outcome.DOWN


def test_there_is_no_flag_that_enables_redemption() -> None:
    """A flag, an environment variable, or a config key would each be a bypass."""
    code = (SRC / "settlement" / "redeem.py").read_text("utf-8")
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"environ", "getenv"}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"getenv", "input"}
    lowered = ast.unparse(tree).lower()
    for marker in ("--redeem-live", "redeem_live", "redeem=true", "os.environ"):
        assert marker not in lowered


def test_the_settlement_package_has_no_trading_verb() -> None:
    """Canonical §18 forbids them; redeemPositions is the only token-destroying operation."""
    forbidden = ("sell", "hedge", "flatten", "merge", "split", "convert", "liquidate")
    for path in (SRC / "settlement").rglob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                for verb in forbidden:
                    assert verb not in node.name.lower(), f"{path.name}:{node.name}"
