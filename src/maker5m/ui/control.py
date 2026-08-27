"""Turning an operator's request into an ordered fact. The only way in.

A command is inert until this module accepts it. Accepting it means one thing: emitting a
``RiskSignal`` through the ``RiskController`` that owns every other permission change, so an
operator halt arrives in the same stream, with the same ordering, and the same audit record as a
stale feed or a failed reconciliation. There is no second path — nothing here reaches into
``MarketState``, the ledger, the order table, or the risk engine's internals.

Ordering comes from the market, not the browser. The command carries the UI's wall clock for the
record, and it is used for nothing: causality is the ingress ordinal the bot assigns when it
accepts the command, because a click has no position in an event stream until the event stream
gives it one. That is also what makes the control replayable — the same ordered signals produce
the same permission transitions, which is the whole point of P9's contract.

Release is narrow on purpose. ``RELEASE_OPERATOR_HALT`` clears the operator's own condition and
nothing else; if the feed is stale or a position is unreconciled, execution stays non-SAFE and
the operator finds out by reading the risk state rather than by discovering that a button
promised more than it could deliver.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from maker5m.market.timebase import TimestampNs
from maker5m.risk import RiskReason, RiskSignal, RiskSignalKind
from maker5m.risk.trace import RiskController
from maker5m.ui.model import CommandKind, OperatorCommand

__all__ = ["CommandOutcome", "ControlIngress"]


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """What the bot did with one command, and where it landed in the ordered stream."""

    command_id: str
    kind: str
    accepted: bool
    ingress_ordinal: int | None = None
    risk_sequence: int | None = None
    risk_state: str | None = None
    allows_place: bool | None = None
    detail: str = ""

    def summary(self) -> dict[str, object]:
        return {
            "command_id": self.command_id,
            "kind": self.kind,
            "accepted": self.accepted,
            "ingress_ordinal": self.ingress_ordinal,
            "risk_sequence": self.risk_sequence,
            "risk_state": self.risk_state,
            "allows_place": self.allows_place,
            "detail": self.detail,
        }


@dataclass(slots=True)
class ControlIngress:
    """Applies operator commands to the ordered risk stream. Bot-side, single owner."""

    controller: RiskController
    publish: Callable[[Any], None] | None = None
    """Where the resulting RiskRecord goes to be persisted.

    Required in production, and the first real market found out why it is not optional: without
    it the operator's two commands were applied to the controller, appeared in its in-memory
    trace, and were the *only* two records of 107,252 missing from the durable stream. The
    verifier refused to call that market complete, which is the correct answer — a control
    action that changed what the bot was allowed to do, and left no durable record, is precisely
    what an audit exists to make impossible.
    """

    accepted: int = 0
    refused: int = 0
    outcomes: list[CommandOutcome] = field(default_factory=list)

    def apply(
        self, command: OperatorCommand, *, ingress_ordinal: int, now_ns: TimestampNs
    ) -> CommandOutcome:
        """Accept one command, or refuse it, and record which. Never raises into the caller."""
        flag = command.kind == CommandKind.OPERATOR_HALT.value
        if command.kind not in {
            CommandKind.OPERATOR_HALT.value,
            CommandKind.RELEASE_OPERATOR_HALT.value,
        }:
            return self._refuse(command, "not a command this build accepts")

        try:
            record = self.controller.apply(
                RiskSignal(
                    kind=RiskSignalKind.OPERATOR_CONTROL,
                    as_of_ingress_ordinal=ingress_ordinal,
                    timestamp=now_ns,
                    provenance=self.controller.provenance,
                    reason=RiskReason.OPERATOR_HALT,
                    flag=flag,
                )
            )
        except Exception as error:
            return self._refuse(command, f"{type(error).__name__}: {error}")

        if self.publish is not None:
            # Same channel as every other risk record, so the operator's action lands in the
            # durable stream in its own sequence position rather than only in memory.
            self.publish(record)

        outcome = CommandOutcome(
            command_id=command.command_id,
            kind=command.kind,
            accepted=True,
            ingress_ordinal=ingress_ordinal,
            risk_sequence=record.risk_sequence,
            risk_state=record.state.value,
            allows_place=record.allows_place,
            detail=(
                "operator halt raised"
                if flag
                else "operator halt released; every other risk condition still applies"
            ),
        )
        self.accepted += 1
        self._remember(outcome)
        return outcome

    def _refuse(self, command: OperatorCommand, detail: str) -> CommandOutcome:
        outcome = CommandOutcome(
            command_id=command.command_id, kind=command.kind, accepted=False, detail=detail
        )
        self.refused += 1
        self._remember(outcome)
        return outcome

    def _remember(self, outcome: CommandOutcome) -> None:
        self.outcomes.append(outcome)
        del self.outcomes[:-32]

    def summary(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "refused": self.refused,
            "outcomes": [outcome.summary() for outcome in self.outcomes],
        }
