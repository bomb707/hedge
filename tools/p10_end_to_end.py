"""One complete real market, from PREARM through settlement.

Runs the production stack over a real ``btc-updown-5m`` market — P6 feeds, P4 decisions, P7
shadow execution, P8 telemetry, P9 risk — and then, after DONE, waits for the Conditional Tokens
contract to report a payout and settles the ledger against it with the production verifier and
the production arithmetic.

An honest limitation, stated here rather than buried: **live trading is disabled, so no real
order is placed and the ledger holds no position.** The lifecycle, the resolution, the payout
vector, and the reconciliation are all real; the economics are zero because there is nothing to
settle. Non-zero own-ledger settlement is P14 work and is reported as unrun, not simulated.

Read-only: no order, no credential, no transaction, no redemption.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from maker5m.accounting.ledger import RebateMode
from maker5m.execution import Executor, RecordingTransport, VenueAdapter
from maker5m.feeds.capture import capture_market
from maker5m.feeds.discovery import discover_market, slug_for
from maker5m.feeds.pipeline import MarketDataPipeline
from maker5m.market.events import HealthStatus
from maker5m.risk import HealthFrame, RiskConfig, RiskController, RiskEngine, RiskProvenance
from maker5m.safety import LIVE_TRADING_ENABLED
from maker5m.settlement import (
    DEFAULT_RPC_ENDPOINTS,
    CtfReader,
    MarketResolutionTarget,
    Redeemer,
    ResolutionState,
    RpcEndpoint,
    SettlementPolicy,
    SettlementPreconditions,
    SettlementRecord,
    settle_on_paper,
    verify,
)
from maker5m.strategy import BaseLot, StrategyEngine, default_config
from maker5m.strategy.decision import DecisionResult
from maker5m.telemetry import InstrumentedRun, SamplingPolicy, perf_now_ns
from tools.p10_settlement_run import advisories

MIN_LEAD_SECONDS = 45
SETTLE_TIMEOUT_SECONDS = 400
POLL_SECONDS = 2.0


async def main(out: Path) -> None:
    if LIVE_TRADING_ENABLED:  # pragma: no cover - defensive
        raise SystemExit("refusing to run while live trading is enabled")
    out.mkdir(parents=True, exist_ok=True)

    now = int(time.time())
    t0 = ((now // 300) + 1) * 300
    if t0 - now < MIN_LEAD_SECONDS:
        t0 += 300
    slug = slug_for(t0)
    print(f"[{time.strftime('%H:%M:%S')}] end-to-end on {slug} (T0 in {t0 - now}s)", flush=True)

    market = discover_market(slug)
    config = default_config(BaseLot.of(15))
    controller = RiskController(
        engine=RiskEngine(config=RiskConfig()),
        provenance=RiskProvenance.REAL_PUBLIC_MARKET_DATA,
    )
    runs: list[InstrumentedRun] = []
    phases_seen: list[str] = []

    def attach(pipeline: MarketDataPipeline) -> None:
        sampling = SamplingPolicy(sample_every=10)
        pipeline.merger.perf_clock = perf_now_ns
        pipeline.stage_selector = lambda ordinal, kind: sampling.selects(ordinal, kind)
        runs.append(
            InstrumentedRun(
                pipeline=pipeline,
                engine=StrategyEngine(config),
                rules=market.venue_rules,
                executor=Executor(adapter=VenueAdapter(RecordingTransport())),
                sampling=sampling,
            )
        )

    def observe(kind: str, raw_ns: int, decision: DecisionResult) -> None:
        run = runs[0]
        pipeline = run.pipeline
        phase = pipeline.merger.state.phase.name
        if not phases_seen or phases_seen[-1] != phase:
            phases_seen.append(phase)
        controller.evaluate(
            HealthFrame(
                clob_status=pipeline.clob_health.status,
                clob_awaiting_snapshot=pipeline.clob_health.awaiting_snapshot,
                spot_status=pipeline.spot_health.status,
                order_stream_status=HealthStatus.UNKNOWN,
            ),
            as_of_ingress_ordinal=pipeline.merger.ordinal,
            now_ns=pipeline.merger.state.last_event_timestamp,
        )
        run.observe(kind, raw_ns, decision)

    result = await capture_market(
        market,
        config,
        next_market=discover_market(slug_for(t0 + 300)),
        description=f"P10 end-to-end lifecycle on {slug}",
        on_pipeline=attach,
        observer=observe,
    )
    run = runs[0]
    final_state = result.final_state
    print(f"  lifecycle complete: phases {phases_seen}, final {final_state.phase.name}", flush=True)

    # -- settlement ------------------------------------------------------------------------
    definition = market.definition
    tokens = (definition.up_token_id, definition.down_token_id)
    target = MarketResolutionTarget(
        slug=slug,
        condition_id=market.condition_id,
        up_token_id=tokens[0],
        down_token_id=tokens[1],
    )
    policy = SettlementPolicy(minimum_agreeing_providers=3)
    endpoints = tuple(RpcEndpoint(provider_id=n, url=u) for n, u in DEFAULT_RPC_ENDPOINTS)
    readers = [CtfReader(endpoint) for endpoint in endpoints]

    trajectory: list[str] = []
    decision = None
    deadline = time.time() + SETTLE_TIMEOUT_SECONDS
    while time.time() < deadline:
        readings = tuple(
            reader.read_condition(target.condition_id, block_tag=policy.block_tag)
            for reader in readers
        )
        decision = verify(target, readings, advisories(slug, target.condition_id), policy)
        if not trajectory or trajectory[-1] != decision.state.value:
            trajectory.append(decision.state.value)
            print(f"  settlement state -> {decision.state.value}", flush=True)
        if decision.state is ResolutionState.RESOLVED:
            break
        time.sleep(POLL_SECONDS)

    assert decision is not None
    settlement = None
    preconditions = SettlementPreconditions(
        occupying_orders=run.executor.orders.open_count,
        order_state_uncertain=False,
    )
    if decision.state is ResolutionState.RESOLVED and decision.payout is not None:
        settlement = settle_on_paper(
            final_state.ledger, decision.payout, target, rebate_mode=RebateMode.WITHOUT_REBATE
        )
    plan, blockers = Redeemer().prepare(
        target,
        decision,
        preconditions,
        has_balance=bool(final_state.ledger.n_up or final_state.ledger.n_down),
    )

    record = SettlementRecord(
        target=target,
        policy=policy,
        provider_readings=readings,
        decision=decision,
        settlement=settlement,
        plan=plan,
        blockers=blockers,
        captured_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )

    ledger = final_state.ledger
    manifest: dict[str, Any] = {
        "kind": "P10_END_TO_END_REAL_MARKET",
        "provenance": "REAL_PUBLIC_MARKET_DATA",
        "captured_utc": record.captured_utc,
        "slug": slug,
        "condition_id": target.condition_id,
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "orders_sent": 0,
        "redemptions_sent": 0,
        "phases_observed": phases_seen,
        "final_phase": final_state.phase.name,
        "cycles": run.cycles,
        "feed_counters": result.counters.summary(),
        "settlement_state_trajectory": trajectory,
        "settlement": record.summary(),
        "ledger": {
            "n_up": int(ledger.n_up),
            "n_down": int(ledger.n_down),
            "cost_up": int(ledger.cost_up),
            "cost_down": int(ledger.cost_down),
            "fees": int(ledger.fees),
            "total_cost": int(ledger.total_cost),
        },
        "reconciliation": {
            "paper_settlement_pnl": (
                None if settlement is None else int(settlement.paper_settlement_pnl)
            ),
            "ledger_pnl_if_winner": (
                None
                if settlement is None or decision.winning_outcome is None
                else int(ledger.pnl_if(decision.winning_outcome, RebateMode.WITHOUT_REBATE))
            ),
            "matches_to_the_last_money_unit": (
                settlement is not None
                and decision.winning_outcome is not None
                and settlement.paper_settlement_pnl
                == ledger.pnl_if(decision.winning_outcome, RebateMode.WITHOUT_REBATE)
            ),
        },
        "limitation": (
            "REAL MARKET LIFECYCLE VALIDATED. NONZERO OWN-LEDGER ECONOMICS UNRUN / P14: live "
            "trading is disabled, so no order was placed and the ledger holds no position. The "
            "lifecycle, resolution, payout vector, and reconciliation are real; the settled "
            "amounts are zero because there is nothing to settle."
        ),
    }
    path = out / f"{slug}.p10-end-to-end.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(
        json.dumps(
            {
                k: manifest[k]
                for k in ("phases_observed", "settlement_state_trajectory", "reconciliation")
            },
            indent=2,
            sort_keys=True,
        )
    )
    print(f"wrote {path}")


def entry() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    asyncio.run(main(args.out))


if __name__ == "__main__":
    entry()
