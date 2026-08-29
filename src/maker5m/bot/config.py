"""What one paper run is, and how it identifies itself.

Everything the corpus needs in order to say *which* software and *which* strategy produced a
market lives here: the frozen strategy configuration, the source revision, and the operational
knobs that are not strategy at all. A two-hundred-market corpus that silently mixed
configurations would be two corpora wearing one name, so the identity is hashed and every market
records it.

**Operational is not canonical.** The thresholds below detect a *broken collector* — a market
that produced no decisions, a feed that was stale for its whole life. They are engineering
tripwires, fixed before the corpus run and never tuned afterwards, and they decide nothing about
strategy. The empirical distributions are the result; a threshold is only how the run notices it
has stopped collecting anything worth having.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from maker5m.bot.resources import GEN2_EVERY
from maker5m.safety import LIVE_TRADING_ENABLED
from maker5m.settlement import REDEMPTION_ENABLED
from maker5m.strategy import BaseLot, StrategyConfig, default_config

__all__ = [
    "CORPUS_SCHEMA_VERSION",
    "OperationalThresholds",
    "PaperConfig",
    "config_identity",
    "source_identity",
    "source_revision",
]

CORPUS_SCHEMA_VERSION: Final[int] = 1


@dataclass(frozen=True, slots=True)
class OperationalThresholds:
    """Tripwires for a broken collector. **OPERATIONAL**, never a strategy claim.

    None of these is a canonical rule and none may become one. They exist so a run that has
    stopped collecting anything useful — a dead feed, a wedged state machine — is noticed while
    it is happening rather than two hundred markets later.
    """

    min_decisions: int = 1_000
    """A real five-minute market produces tens of thousands. A market under this had a fault."""

    min_clob_messages: int = 100
    min_spot_messages: int = 100
    max_stale_fraction: float = 0.95
    """Not a quality bar. A market classified STALE essentially throughout means the feed or the
    health machinery was broken, not that the strategy quoted badly."""

    max_prearm_lead_shortfall_s: float = 0.0
    """Prearm must be ready before T0. Zero shortfall tolerated, hence a recorded incident."""


@dataclass(frozen=True, slots=True)
class PaperConfig:
    """One paper run's composition. Paths, sizes, and the frozen strategy identity."""

    evidence_dir: Path
    """Where journals, stores and archives live. Outside git — they are gigabytes."""

    corpus_path: Path
    """The append-only corpus index. Small, and in git."""

    ui_dir: Path
    base_lot: int = 15
    sample_every: int = 10
    buffer_capacity: int = 320_000
    settle_timeout_s: int = 400
    settle_poll_s: float = 5.0
    keep_raw_store: bool = False
    """Whether to keep the ~650 MB SQLite file after its archive verifies. Off: 200 markets is
    130 GB raw and 2.4 GB archived, and the archive is proved to restore before the raw file is
    even considered removable."""

    allocator_maintenance: bool = False
    """Whether one `malloc_trim(0)` runs per rollover, in the non-quoting window. OPERATIONAL.

    **Off, because the experiment that turned it on failed.** `p13-resource-2` ran 57 markets with
    it enabled: 59 trims, every one inside the window, none with any market in QUOTE or ENDGAME,
    1,082.6 MB returned to the kernel. The process still finished at 534.2 MB against
    `p13-resource-1`'s 489.8 — with an after-warm-up slope of +2.7434 MB/market against +1.3607,
    and 11 % *less* work done (6,140 MB of journals against 6,905, 4.17 M decisions against
    4.71 M). Giving the pages back left the process resident-larger than keeping them.

    The machinery and its contract stay, because the next allocator experiment will want both and
    because the negative result is worth being able to reproduce. The default does not, because a
    runtime should not ship an action that measurement says makes the thing it targets worse."""

    maintenance_margin_s: float = 10.0
    """Seconds of the rollover gap left untouched before the next market may quote. OPERATIONAL."""

    epoch: str = "p13-corpus-1"
    """Which corpus epoch these markets belong to. A configuration change starts a new one."""

    thresholds: OperationalThresholds = field(default_factory=OperationalThresholds)

    def strategy(self) -> StrategyConfig:
        """The frozen strategy configuration. P13 collects data; it does not tune."""
        return default_config(BaseLot.of(self.base_lot))

    def with_paths(self, root: Path) -> PaperConfig:
        return replace(
            self,
            evidence_dir=root / "markets",
            corpus_path=root / "corpus.jsonl",
            ui_dir=root / "ui",
        )


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - defensive
        return None


def source_revision() -> str:
    return _git("rev-parse", "HEAD") or "unknown"


def source_identity() -> dict[str, Any]:
    """Which software this is, and whether that statement is true.

    `git rev-parse HEAD` names a commit; it says nothing about whether the files that were
    imported match it. A corpus frozen against a revision while the working tree carried
    uncommitted edits would be frozen against nothing, so the tree hash and the cleanliness of
    tracked files are recorded beside it.

    Untracked files are ignored on purpose: evidence directories, scratch output and editor
    droppings are not the software. What matters is whether a *tracked* source file differs from
    the revision this run claims to be.
    """
    revision = source_revision()
    tree = _git("rev-parse", "HEAD^{tree}")
    modified = _git("status", "--porcelain", "--untracked-files=no")
    return {
        "source_revision": revision,
        "source_tree_sha": tree,
        "working_tree_clean": modified == "",
        "modified_tracked_files": None if not modified else modified.splitlines(),
    }


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return {f: _plain(getattr(value, f)) for f in sorted(value.__dataclass_fields__)}
    return str(value)


def config_identity(config: PaperConfig) -> dict[str, Any]:
    """The complete running configuration, plus a hash of it.

    Recorded on every market. Two markets with different hashes are not the same experiment,
    and the corpus says so rather than letting them be averaged together.
    """
    # Everything that can change what gets collected or how it is judged. Paths are excluded —
    # where the evidence lives is not what the experiment is — but a collection knob that alters
    # behaviour is part of the identity, because two runs that differ in one are not the same
    # collection whatever their strategy config says.
    snapshot = {
        "corpus_schema_version": CORPUS_SCHEMA_VERSION,
        "epoch": config.epoch,
        "strategy": _plain(config.strategy()),
        "base_lot": config.base_lot,
        "sample_every": config.sample_every,
        "classification_mode": "EVERY_DECISION",
        "buffer_capacity": config.buffer_capacity,
        "settle_timeout_s": config.settle_timeout_s,
        "settle_poll_s": _plain(config.settle_poll_s),
        "keep_raw_store": config.keep_raw_store,
        "gc_full_collection_every": GEN2_EVERY,
        # Which allocator-maintenance policy produced this run. A corpus collected with the trim
        # and one collected without it are not the same experiment, and the identity says so.
        "allocator_maintenance": {
            "enabled": config.allocator_maintenance,
            "action": "malloc_trim(0)",
            "per_rollover": 1,
            "margin_s": _plain(config.maintenance_margin_s),
            "adaptive": False,
        },
        "thresholds": {"OPERATIONAL": _plain(asdict(config.thresholds))},
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "redemption_enabled": REDEMPTION_ENABLED,
    }
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        **snapshot,
        **source_identity(),
        "config_sha256": hashlib.sha256(encoded).hexdigest(),
    }
