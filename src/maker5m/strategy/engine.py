"""``StrategyEngine.decide`` — the assembled deterministic decision layer.

This is the single place the strategy exists (``ARCHITECTURE_SSOT`` §4). It composes the P3
primitives and the P4 regime rules and returns *intent* plus the record explaining it. It
knows nothing about a venue: no submission, no post-only validation, no reconciliation, no
cancel. Execution begins at P7.

Pure by construction — no clock, no I/O, no logging, no randomness, no global state — so
P5 replay drives this exact code object and reproduces production decisions bit for bit
(I20).

Shape of a decision
-------------------
```text
phase not QUOTE/ENDGAME  ->  no orders, PHASE_NOT_QUOTING          (fast path)
centre unavailable       ->  no orders, CENTRE_UNAVAILABLE
otherwise                ->  candidate quote, then eligibility
```

The candidate quote is built **identically** in QUOTE and ENDGAME: same centre, same tick
rounding, same zero-spread prices, same base lot, same grid plan. ENDGAME then applies its
gate on top. That is invariant A5 (Detailed §29) expressed structurally — there is no branch
in which the endgame could resize or reprice anything, because the sizing happens before the
regime is consulted.
"""

from dataclasses import dataclass

from maker5m.accounting.ledger import RebateMode
from maker5m.domain import Outcome
from maker5m.market.phase import Phase
from maker5m.market.state import MarketState
from maker5m.numeric.units import PriceUnits
from maker5m.strategy.centre import CentreResult
from maker5m.strategy.config import (
    BAND_HARD_STATUS,
    ENDGAME_BAND_STATUS,
    ENDGAME_TILT_STATUS,
    StrategyConfig,
)
from maker5m.strategy.decision import (
    DecisionEconomics,
    DecisionResult,
    DecisionTelemetry,
    DesiredOrder,
    DesiredOrders,
    EndgameTelemetry,
    economics_of,
)
from maker5m.strategy.eligibility import EligibilityResult, evaluate_eligibility
from maker5m.strategy.endgame import EndgameGate, evaluate_endgame, favourite_from_centre
from maker5m.strategy.grid import GRID_POLICY_STATUS, plan_grid
from maker5m.strategy.prices import build_quote_prices
from maker5m.strategy.quantization import TICK_ROUNDING_STATUS, quantize_centre

__all__ = ["QUOTING_PHASES", "StrategyEngine"]

QUOTING_PHASES = frozenset({Phase.QUOTE, Phase.ENDGAME})
"""The only phases in which new quotes exist. PREARM, SETTLING, and DONE emit nothing."""


@dataclass(frozen=True, slots=True)
class StrategyEngine:
    """Turns authoritative market state into desired orders. Stateless and pure."""

    config: StrategyConfig

    def decide(self, state: MarketState) -> DecisionResult:
        """Produce strategy intent for the current state.

        Never mutates ``state``, never reads a clock, and depends on nothing outside its
        arguments and its own configuration.
        """
        config = self.config
        economics = economics_of(state.ledger)
        phase = state.phase
        tick = state.definition.tick

        # Fast path: PREARM, SETTLING, DONE cannot create quotes, so no centre is computed,
        # no base lot selected, and no grid planned. Economics are still recorded, because
        # the settlement position matters in every phase.
        if phase not in QUOTING_PHASES:
            return self._empty(
                phase=phase,
                tick=tick,
                centre=None,
                eligibility=evaluate_eligibility(
                    quoting_phase=False,
                    centre_available=False,
                    inventory=economics.inventory,
                    band_hard=config.band_hard,
                ),
                economics=economics,
            )

        centre_result = config.quote_centre.compute(state)
        if centre_result.centre is None:
            # A normal NOT_QUOTING case, not an exception. No price is invented: no previous
            # centre, no DOWN-book fallback, no 0.50 default. The precise reason is kept.
            return self._empty(
                phase=phase,
                tick=tick,
                centre=centre_result,
                eligibility=evaluate_eligibility(
                    quoting_phase=True,
                    centre_available=False,
                    inventory=economics.inventory,
                    band_hard=config.band_hard,
                ),
                economics=economics,
            )

        raw_centre = centre_result.centre
        quantized = quantize_centre(raw_centre, tick, config.tick_rounding)
        prices = build_quote_prices(quantized, tick)
        base_lot = config.base_lot_selector.select(state)
        plan = plan_grid(economics.inventory, base_lot, config.grid_policy, config.grid_rounding)

        # Favourite direction comes from the RAW centre, before quantization: Canonical §32
        # evaluates ``centre > 0.5`` on the unrounded value, and a rounding artefact must not
        # decide a 30-share terminal residual.
        gate: EndgameGate | None = None
        endgame_telemetry: EndgameTelemetry | None = None
        if phase is Phase.ENDGAME:
            favourite = favourite_from_centre(raw_centre)
            gate = evaluate_endgame(
                economics.inventory, favourite, config.endgame_tilt, config.endgame_band
            )
            endgame_telemetry = EndgameTelemetry(
                favourite=favourite,
                target_inventory=gate.target,
                distance_to_target=gate.distance,
                tilt=config.endgame_tilt,
                tilt_status=ENDGAME_TILT_STATUS,
                band=config.endgame_band,
                band_status=ENDGAME_BAND_STATUS,
                gate_up_allowed=gate.up_allowed,
                gate_down_allowed=gate.down_allowed,
                settlement_edge_favourite=state.ledger.pnl_if(
                    favourite, RebateMode.ESTIMATED_REBATE
                ),
                settlement_edge_underdog=state.ledger.pnl_if(
                    favourite.other, RebateMode.ESTIMATED_REBATE
                ),
            )

        eligibility = evaluate_eligibility(
            quoting_phase=True,
            centre_available=True,
            inventory=economics.inventory,
            band_hard=config.band_hard,
            endgame_up_allowed=None if gate is None else gate.up_allowed,
            endgame_down_allowed=None if gate is None else gate.down_allowed,
        )

        orders = DesiredOrders(
            up=(
                DesiredOrder(Outcome.UP, prices.up_buy_price, plan.up_size)
                if eligibility.up_allowed
                else None
            ),
            down=(
                DesiredOrder(Outcome.DOWN, prices.down_buy_price, plan.down_size)
                if eligibility.down_allowed
                else None
            ),
        )

        return DecisionResult(
            orders=orders,
            telemetry=DecisionTelemetry(
                phase=phase,
                centre_source=centre_result.source,
                centre_status=config.quote_centre.status,
                raw_centre=raw_centre,
                centre_unavailable=None,
                tick_rounding=config.tick_rounding,
                tick_rounding_status=TICK_ROUNDING_STATUS,
                quantized_centre=quantized,
                tick=tick,
                grid_policy=config.grid_policy,
                grid_policy_status=GRID_POLICY_STATUS,
                grid_rounding=config.grid_rounding,
                base_lot=base_lot,
                base_lot_status=config.base_lot_selector.status,
                # Recorded regardless of eligibility, so A5 is checkable from the record.
                candidate_up_price=prices.up_buy_price,
                candidate_up_size=plan.up_size,
                candidate_down_price=prices.down_buy_price,
                candidate_down_size=plan.down_size,
                eligibility=eligibility,
                band_hard=config.band_hard,
                band_hard_status=BAND_HARD_STATUS,
                endgame=endgame_telemetry,
                economics=economics,
            ),
        )

    def _empty(
        self,
        *,
        phase: Phase,
        tick: PriceUnits,
        centre: CentreResult | None,
        eligibility: EligibilityResult,
        economics: DecisionEconomics,
    ) -> DecisionResult:
        """A decision with no intent. Economics are always present."""
        config = self.config
        return DecisionResult(
            orders=DesiredOrders(),
            telemetry=DecisionTelemetry(
                phase=phase,
                centre_source=config.quote_centre.source,
                centre_status=config.quote_centre.status,
                raw_centre=None,
                centre_unavailable=None if centre is None else centre.unavailable,
                tick_rounding=config.tick_rounding,
                tick_rounding_status=TICK_ROUNDING_STATUS,
                quantized_centre=None,
                tick=tick,
                grid_policy=config.grid_policy,
                grid_policy_status=GRID_POLICY_STATUS,
                grid_rounding=config.grid_rounding,
                base_lot=None,
                base_lot_status=config.base_lot_selector.status,
                candidate_up_price=None,
                candidate_up_size=None,
                candidate_down_price=None,
                candidate_down_size=None,
                eligibility=eligibility,
                band_hard=config.band_hard,
                band_hard_status=BAND_HARD_STATUS,
                endgame=None,
                economics=economics,
            ),
        )
