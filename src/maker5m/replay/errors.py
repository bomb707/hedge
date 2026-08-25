"""Replay errors. Every one of them fails closed.

A journal is evidence. A decoder that guessed at a missing field, tolerated an unknown event
tag, or silently skipped a diverging step would turn evidence into fiction — and every OPEN
strategy item is meant to be closed by exactly this machinery, so a permissive decoder would
undermine the whole point of building it.
"""

__all__ = [
    "JournalDecodeError",
    "JournalEncodeError",
    "ReplayDivergenceError",
    "ReplayError",
    "UnsupportedComponentError",
    "UnsupportedSchemaError",
]


class ReplayError(Exception):
    """Base class for every replay failure."""


class JournalEncodeError(ReplayError):
    """A value could not be canonically encoded."""


class JournalDecodeError(ReplayError):
    """A journal was malformed, incomplete, or carried an unknown tag or value."""


class UnsupportedSchemaError(JournalDecodeError):
    """The journal declares a schema version this build does not implement."""


class UnsupportedComponentError(JournalDecodeError):
    """The journal names a strategy component this build cannot reconstruct."""


class ReplayDivergenceError(ReplayError):
    """A replayed decision did not match the recorded one.

    Carries the step index, event identity, and ingress ordinal so the mismatch can be
    located immediately rather than inferred from a final-state difference.
    """

    def __init__(
        self,
        *,
        step_index: int,
        event_id: str,
        ingress_ordinal: int,
        detail: str,
    ) -> None:
        self.step_index = step_index
        self.event_id = event_id
        self.ingress_ordinal = ingress_ordinal
        self.detail = detail
        super().__init__(
            f"replay diverged at step {step_index} "
            f"(event_id={event_id!r}, ingress_ordinal={ingress_ordinal}): {detail}"
        )
