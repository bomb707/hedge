"""The whole of the ingress owner's UI work, as one function, so it can be tested as one.

This exists because the last defect was not in any module it could plausibly have been in. Every
file under `maker5m.ui` was clean; the runner called those clean functions from inside `on_tick`,
which is the single ingress consumer. A source scan of the UI package passed while the bot was
doing `listdir` — and later, while it was doing `print(..., flush=True)` — on the trading path.

So the production hot-side function lives here and the test drives *this*, with the filesystem,
`print` and `logging` all replaced by functions that raise. If it does any synchronous I/O, the
test fails with the induced error rather than with an assertion.

The rule is not "no filesystem call". It is **no synchronous I/O on the ingress owner**: a
`print` to a pipe nobody is draining blocks exactly as thoroughly as a stalled `stat`.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from typing import Any

from maker5m.market.timebase import TimestampNs

__all__ = ["ControlEvent", "drain_operator_commands"]


class ControlEvent(tuple[Any, ...]):
    """One thing worth reporting, as an immutable tuple. Formatting happens in Plane 3.

    A tuple rather than a dataclass for the reason P8 measured three times: this is built on the
    ingress owner's thread and the cheap shape is the plain one.
    """

    __slots__ = ()

    @staticmethod
    def of(kind: str, payload: Any) -> ControlEvent:
        return ControlEvent((kind, payload))


def drain_operator_commands(
    channel: Any,
    ingress: Any,
    *,
    ingress_ordinal: int,
    now_ns: TimestampNs,
    report: Callable[[ControlEvent], None] | None = None,
) -> int:
    """Apply whatever operator commands are waiting. The complete hot-side UI path.

    Every operation here is in-memory: a `popleft`, an attribute read, a risk signal, and an
    `append` to a bounded deque. No syscall, no serialization, no lock, no `print`.

    ``report`` receives immutable facts for Plane 3 to render later. It is expected to be a
    bounded, non-blocking publish; if it raises, the exception is absorbed, because a debug
    channel must not be able to interrupt a market.
    """
    applied = 0
    for command in channel.pop_all():
        outcome = ingress.apply(command, ingress_ordinal=ingress_ordinal, now_ns=now_ns)
        applied += 1
        if report is None:
            continue
        with suppress(Exception):
            # Reporting must never reach the trading loop; a debug channel is not worth a market.
            report(ControlEvent.of("command", outcome))
    return applied
