"""Deterministic replay. Offline. Built in P5.

See ``docs/ARCHITECTURE_SSOT.md`` §7 and invariant I20.

Journal format, canonical codec, recorder, verifier, and the parameter-sweep runner used to
close OPEN items once empirical journals exist.

Replay drives the **same** Plane 2 code objects as production — ``reduce_event`` from P2 and
``StrategyEngine.decide`` from P4, unchanged. There is no replay-specific branch anywhere in
``market/``, ``accounting/``, ``strategy/``, or ``numeric/``, and a static test enforces that
those packages cannot import this one. A second implementation of any strategy rule here, or
a branch keyed on "am I replaying", would be a defect.

**Synthetic journals prove the machinery, not the strategy.** Every journal declares its
provenance, and a ``SYNTHETIC`` one is evidence about this code only. No OPEN strategy item
can be closed from it.
"""

from maker5m.replay.codec import (
    current_schema_version,
    decode_journal,
    encode_journal,
    encode_line,
    iter_encoded_journal,
)
from maker5m.replay.errors import (
    JournalDecodeError,
    JournalEncodeError,
    ReplayDivergenceError,
    ReplayError,
    UnsupportedComponentError,
    UnsupportedSchemaError,
)
from maker5m.replay.journal import Journal, JournalHeader, ReplayStep
from maker5m.replay.recorder import RecordedRun, record
from maker5m.replay.schema import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    JournalProvenance,
    RecordType,
)
from maker5m.replay.sweep import SweepCandidate, SweepResult, SweepRun, run_sweep
from maker5m.replay.verifier import ReplayOutcome, replay_with_config, verify_replay
from maker5m.replay.writer import JournalWrite, write_journal_stream

__all__ = [
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "Journal",
    "JournalDecodeError",
    "JournalEncodeError",
    "JournalHeader",
    "JournalProvenance",
    "JournalWrite",
    "RecordType",
    "RecordedRun",
    "ReplayDivergenceError",
    "ReplayError",
    "ReplayOutcome",
    "ReplayStep",
    "SweepCandidate",
    "SweepResult",
    "SweepRun",
    "UnsupportedComponentError",
    "UnsupportedSchemaError",
    "current_schema_version",
    "decode_journal",
    "encode_journal",
    "encode_line",
    "iter_encoded_journal",
    "record",
    "replay_with_config",
    "run_sweep",
    "verify_replay",
    "write_journal_stream",
]
