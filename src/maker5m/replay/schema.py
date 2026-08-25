"""Journal schema constants.

The schema version is explicit and checked on decode. A journal recorded under one version is
not silently reinterpreted under another: replay exists to make old runs reproducible, and a
format change that quietly altered their meaning would defeat that.
"""

from enum import Enum
from typing import Final

__all__ = ["SCHEMA_VERSION", "SUPPORTED_SCHEMA_VERSIONS", "JournalProvenance", "RecordType"]

SCHEMA_VERSION: Final[int] = 1
"""Version written by this build."""

SUPPORTED_SCHEMA_VERSIONS: Final[frozenset[int]] = frozenset({1})
"""Versions this build can decode. Anything else fails closed."""


class RecordType(Enum):
    """The two line kinds in a journal."""

    HEADER = "header"
    STEP = "step"


class JournalProvenance(Enum):
    """Where a journal's events came from.

    Required in every header, because the difference matters enormously and is invisible in
    the data itself. A synthetic journal proves the replay machinery works; it proves nothing
    about the target wallet, and mislabelling one as reconstructed evidence would corrupt
    every experiment built on it.
    """

    SYNTHETIC = "SYNTHETIC"
    """Constructed by this project for testing. Not evidence about any real wallet."""

    RECONSTRUCTED = "RECONSTRUCTED"
    """Rebuilt from observed target-wallet activity. None exist yet (L1 is blocked)."""

    LIVE_PAPER = "LIVE_PAPER"
    """Recorded from real market data with no real orders (P13)."""

    LIVE = "LIVE"
    """Recorded from a real trading session (P14)."""
