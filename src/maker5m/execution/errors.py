"""Execution errors. All fail closed.

An execution layer that recovered creatively from a bad state would be worse than one that
stops: the edge is ~0.255 cents per share against a 1.000 cent tick (Canonical §27), so a
single unintended taker fill or duplicated order costs more than many correct ones earn.
"""

__all__ = [
    "ExecutionError",
    "LiveTradingDisabledError",
    "OrderIdentityError",
    "PreparationError",
]


class ExecutionError(Exception):
    """Base class for every execution failure."""


class PreparationError(ExecutionError):
    """A desired order could not be turned into a legal venue submission."""


class OrderIdentityError(ExecutionError):
    """An order identity was reused, unknown, or inconsistent."""


class LiveTradingDisabledError(ExecutionError):
    """A real write executor was requested while live trading is disabled.

    Raised **before** any network write is attempted. Unlocking is a P14 decision gated on
    the Canonical §35 checklist plus explicit human authorisation; there is deliberately no
    flag, environment variable, or config file that can bypass it.
    """
