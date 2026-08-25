"""Deterministic replay of the risk stream.

P5 proves the strategy's decisions follow from the market events. This proves the *permissions*
follow from the risk signals — that every ``allows_place`` in a recorded run was the necessary
consequence of the signals that preceded it, and not of something invisible.

The verifier re-applies the recorded signals to a fresh controller and compares each resulting
record against the one that was written. It **fails closed at the first divergence**, naming the
risk sequence, the ingress ordinal, and both verdicts. Continuing past a mismatch would produce
a report whose later agreements meant nothing.

Tampering is caught by the same mechanism: a recorded state, active set, latched set, or
``allows_place`` that does not follow from the signals is a divergence, because the replay
derives them rather than reading them.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from maker5m.risk.config import RiskConfig
from maker5m.risk.engine import RiskEngine, RiskSnapshot
from maker5m.risk.reasons import RiskReason, RiskState
from maker5m.risk.trace import (
    RISK_SCHEMA_VERSION,
    OperationalState,
    RiskController,
    RiskRecord,
    RiskTrace,
)

__all__ = ["RiskDivergenceError", "RiskReplayOutcome", "verify_risk_replay"]


class RiskDivergenceError(AssertionError):
    """A recorded risk verdict does not follow from the signals that preceded it."""

    def __init__(
        self,
        *,
        risk_sequence: int,
        as_of_ingress_ordinal: int,
        field_name: str,
        expected: object,
        actual: object,
    ) -> None:
        self.risk_sequence = risk_sequence
        self.as_of_ingress_ordinal = as_of_ingress_ordinal
        self.field_name = field_name
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"risk sequence {risk_sequence} (as of ingress ordinal {as_of_ingress_ordinal}): "
            f"{field_name} differs — recorded {actual!r}, replay produced {expected!r}"
        )


@dataclass(frozen=True, slots=True)
class RiskReplayOutcome:
    """What the replay reproduced."""

    verified: bool
    record_count: int
    states: tuple[RiskState, ...] = ()
    final_state: RiskState = RiskState.RECOVERING
    sequence_gaps: tuple[int, ...] = ()
    active_by_sequence: tuple[frozenset[RiskReason], ...] = field(default=())
    latched_by_sequence: tuple[frozenset[RiskReason], ...] = field(default=())

    def summary(self) -> dict[str, object]:
        return {
            "verified": self.verified,
            "records": self.record_count,
            "final_state": self.final_state.value,
            "sequence_gaps": list(self.sequence_gaps),
            "states_seen": sorted({state.value for state in self.states}),
        }


def _sequence_gaps(records: Sequence[RiskRecord]) -> tuple[int, ...]:
    """Sequences missing from a stream that must be contiguous and strictly increasing."""
    gaps: list[int] = []
    expected = records[0].risk_sequence if records else 0
    for record in records:
        while record.risk_sequence > expected:
            gaps.append(expected)
            expected += 1
        expected = record.risk_sequence + 1
    return tuple(gaps)


def verify_risk_replay(
    records: Iterable[RiskRecord],
    *,
    config: RiskConfig | None = None,
    initial: RiskSnapshot | None = None,
) -> RiskReplayOutcome:
    """Re-derive every verdict from the recorded signals and compare, failing at the first miss.

    A gap in the recorded sequence fails immediately: a permission audit missing records cannot
    answer "why was this PLACE permitted?" for the cycles it lost, and replaying around the hole
    would produce agreements that mean nothing.

    The health frame is taken from each record because P6 owns feed health and the risk stream
    only observes it. Everything else — the operational conditions, the latches, the recovery
    confirmations — is rebuilt from the signals alone, so a recorded verdict that does not
    follow from them is caught rather than trusted.
    """
    ordered = list(records)
    gaps = _sequence_gaps(ordered)
    if gaps:
        # A gap in the permission audit is itself the failure, and it is caught here rather
        # than left to surface later as a puzzling state divergence. A stream missing records
        # cannot answer "why was this PLACE permitted?" for the cycles it lost.
        first = next(r for r in ordered if r.risk_sequence > gaps[0])
        raise RiskDivergenceError(
            risk_sequence=gaps[0],
            as_of_ingress_ordinal=first.as_of_ingress_ordinal,
            field_name="risk_sequence",
            expected=gaps[0],
            actual=first.risk_sequence,
        )

    controller = RiskController(
        engine=RiskEngine(config=config or RiskConfig(), snapshot=initial or RiskSnapshot()),
        operational=OperationalState(),
        trace=RiskTrace(capacity=max(1, len(ordered))),
    )

    states: list[RiskState] = []
    actives: list[frozenset[RiskReason]] = []
    latches: list[frozenset[RiskReason]] = []

    for recorded in ordered:
        if recorded.schema_version != RISK_SCHEMA_VERSION:
            raise RiskDivergenceError(
                risk_sequence=recorded.risk_sequence,
                as_of_ingress_ordinal=recorded.as_of_ingress_ordinal,
                field_name="schema_version",
                expected=RISK_SCHEMA_VERSION,
                actual=recorded.schema_version,
            )
        controller.provenance = recorded.signal.provenance
        produced = controller.apply(recorded.signal, recorded.health)

        for name, expected, actual in (
            ("state", produced.state, recorded.state),
            ("active", produced.active, recorded.active),
            ("latched", produced.latched, recorded.latched),
            ("allows_place", produced.allows_place, recorded.allows_place),
            ("allows_cancel", produced.allows_cancel, recorded.allows_cancel),
        ):
            if expected != actual:
                raise RiskDivergenceError(
                    risk_sequence=recorded.risk_sequence,
                    as_of_ingress_ordinal=recorded.as_of_ingress_ordinal,
                    field_name=name,
                    expected=expected,
                    actual=actual,
                )
        states.append(produced.state)
        actives.append(produced.active)
        latches.append(produced.latched)

    return RiskReplayOutcome(
        verified=True,
        record_count=len(ordered),
        states=tuple(states),
        final_state=states[-1] if states else RiskState.RECOVERING,
        sequence_gaps=(),
        active_by_sequence=tuple(actives),
        latched_by_sequence=tuple(latches),
    )
