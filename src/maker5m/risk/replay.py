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
    """Always empty on success: a gap raises rather than being reported. Retained so evidence
    manifests can state the fact explicitly."""
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


def _require_complete_sequence(records: Sequence[RiskRecord]) -> None:
    """The risk sequence must be exactly ``0, 1, 2, …`` with no exceptions.

    One rule, checked positionally, catches every way a permission audit can be incomplete: a
    lost prefix, a missing record, a duplicate, a value that goes backwards, and a whole trace
    shifted off zero. The earlier version derived its expectation from ``records[0]``, which
    made a truncated stream look internally contiguous — ``3, 4, 5`` verified happily — and its
    forward-gap scan accepted ``0, 1, 1, 2`` and ``0, 1, 2, 1`` as well.

    The expectation is **not** inferred from the data. A trace that starts at 5 is a trace whose
    first five permission decisions are unaccounted for, and letting the file tell us where it
    ought to begin is how that becomes invisible.

    Partial replay is deliberately unsupported. If it is ever needed it must arrive with an
    explicit initial sequence *and* an explicit initial :class:`RiskSnapshot`, because replaying
    a tail without the state it inherited proves nothing.
    """
    for index, record in enumerate(records):
        if record.risk_sequence != index:
            raise RiskDivergenceError(
                risk_sequence=index,
                as_of_ingress_ordinal=record.as_of_ingress_ordinal,
                field_name="risk_sequence",
                expected=index,
                actual=record.risk_sequence,
            )


def verify_risk_replay(
    records: Iterable[RiskRecord],
    *,
    config: RiskConfig | None = None,
    initial: RiskSnapshot | None = None,
) -> RiskReplayOutcome:
    """Re-derive every verdict from the recorded signals and compare, failing at the first miss.

    The recorded sequence must be exactly ``0, 1, 2, …``. That is checked before anything is
    replayed, and then each produced sequence is compared to its recorded one, so the number the
    whole audit is indexed by is verified rather than assumed. A permission audit missing records
    cannot answer "why was this PLACE permitted?" for the cycles it lost, and replaying around
    the hole would produce agreements that mean nothing.

    A bounded :class:`~maker5m.risk.trace.RiskTrace` that has dropped records therefore cannot
    verify: its first retained sequence is greater than zero. That is the correct outcome —
    trading may continue under the existing safety policy, but the evidence may not claim
    deterministic full-risk replay. Nothing here renumbers the tail or invents the state it
    inherited.

    The health frame is taken from each record because P6 owns feed health and the risk stream
    only observes it. Everything else — the operational conditions, the latches, the recovery
    confirmations — is rebuilt from the signals alone, so a recorded verdict that does not
    follow from them is caught rather than trusted.
    """
    ordered = list(records)
    # Checked before anything is replayed. An incomplete audit is the failure itself, not
    # something to discover later as a puzzling state divergence.
    _require_complete_sequence(ordered)

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
            # The sequence is *proved*, not merely used to address the record. Replaying a
            # trace without checking it would leave the one number the whole audit is indexed
            # by unverified.
            ("risk_sequence", produced.risk_sequence, recorded.risk_sequence),
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
