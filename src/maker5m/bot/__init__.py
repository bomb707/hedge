"""Composition root. Wiring, configuration, and process entry points only.

No strategy logic, no accounting logic, and no god object: this package composes the accepted
components of P6 through P12 and adds nothing they do not already do
(``docs/ARCHITECTURE_SSOT.md`` §8).

P13 runs it in **paper mode** — real Polymarket markets, real Binance spot, real Polygon
settlement reads, and shadow execution throughout. `LIVE_TRADING_ENABLED` and
`REDEMPTION_ENABLED` are both `False`, there is no argument or environment variable that can
change either, and no order, cancellation or redemption transaction is ever sent.
"""

from maker5m.bot.config import OperationalThresholds, PaperConfig, config_identity
from maker5m.bot.corpus import CorpusIndex, CorpusStats
from maker5m.bot.latency import LATENCY_SCHEMA_VERSION, LatencyArtifact, read_latency, write_latency
from maker5m.bot.quality import QUALITY_LABELS, QUEUE_PROVENANCE, QualityAggregate
from maker5m.bot.resources import ResourceSample, sample_resources
from maker5m.bot.session import MarketSession, PrearmRecord
from maker5m.bot.settle import settle_market
from maker5m.bot.supervisor import Supervisor, UiPlane

__all__ = [
    "LATENCY_SCHEMA_VERSION",
    "QUALITY_LABELS",
    "QUEUE_PROVENANCE",
    "CorpusIndex",
    "CorpusStats",
    "LatencyArtifact",
    "MarketSession",
    "OperationalThresholds",
    "PaperConfig",
    "PrearmRecord",
    "QualityAggregate",
    "ResourceSample",
    "Supervisor",
    "UiPlane",
    "config_identity",
    "read_latency",
    "sample_resources",
    "settle_market",
    "write_latency",
]
