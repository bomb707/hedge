"""Versioned durable telemetry contracts. Plane 3.

Everything here is a *record*: a flat, immutable projection of something that already happened,
shaped for storage and for being read back years later by code that no longer exists yet. None
of it is computed on the trading path, and none of it is authoritative for anything — the
authority stays with the ledger, the risk controller, and the settlement verifier.

Three rules the shapes follow, and the reasons they are not negotiable:

**Exactness survives the round trip.** Every authoritative economic quantity is a fixed-point
integer and is stored as an integer. Nothing here converts a `MoneyUnits` to a float on its way
to disk, because a value that is exact in memory and approximate on disk is worse than one that
was never exact: it looks trustworthy. Rational quantities — Term1, Term2, average prices — are
stored as an explicit numerator and denominator rather than reduced to a decimal.

**Identifiers that do not fit are text.** Polymarket token ids are 77-digit decimal integers and
condition ids are 32-byte hex. Both overflow SQLite's signed 64-bit INTEGER, so both are stored
as TEXT in their canonical form. Storing them as numbers would silently corrupt them.

**Absence is recorded, never imputed.** A missing exchange timestamp is ``None``. A book age
that cannot be computed because no book has arrived is ``None``. Zero is a measurement; ``None``
is the absence of one, and the difference matters when the record is the only surviving evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from typing import Final

from maker5m.market.timebase import TimestampNs
from maker5m.numeric.units import MoneyUnits, PriceUnits, ShareUnits

__all__ = [
    "DECISION_SCHEMA_VERSION",
    "FILL_SCHEMA_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "METRICS_SCHEMA_VERSION",
    "RISK_ROW_SCHEMA_VERSION",
    "SETTLEMENT_ROW_SCHEMA_VERSION",
    "STORE_SCHEMA_VERSION",
    "SUPPORTED_DECISION_SCHEMA_VERSIONS",
    "DecisionRecord",
    "ExactRatio",
    "FillProvenance",
    "FillRecord",
    "Manifest",
    "MarketMetrics",
    "RiskRow",
    "SettlementRow",
    "SideRecord",
    "TelemetryProvenance",
]

STORE_SCHEMA_VERSION: Final[int] = 2
"""The database's own version, checked on open. A newer one fails the read closed."""

DECISION_SCHEMA_VERSION: Final[int] = 2
"""V2 separates what the strategy wanted from what risk allowed execution to do.

V1 stored one intent and could not distinguish 'the strategy declined to quote' from 'the
strategy wanted to quote and safety refused'. Bumped rather than reinterpreted: a V1 row still
means what it meant when it was written."""
SUPPORTED_DECISION_SCHEMA_VERSIONS: Final[frozenset[int]] = frozenset({1, DECISION_SCHEMA_VERSION})
"""Every DecisionRecord contract this build defines. Nothing else is a contract.

Enumerated rather than expressed as a range, because "older than current" is not the same as
"a version we know how to read". A record stamped 0, -1 or 3 is internally consistent about a
schema that has never existed here, and reading it under V1's rules — or under V2's — would be
guessing at a layout nobody wrote down.
"""

FILL_SCHEMA_VERSION: Final[int] = 1
RISK_ROW_SCHEMA_VERSION: Final[int] = 1
SETTLEMENT_ROW_SCHEMA_VERSION: Final[int] = 1
METRICS_SCHEMA_VERSION: Final[int] = 1
MANIFEST_SCHEMA_VERSION: Final[int] = 1


class TelemetryProvenance(Enum):
    """Where the underlying market data came from. Never inferred from context."""

    REAL_PUBLIC_MARKET_DATA = "REAL_PUBLIC_MARKET_DATA"
    CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET = "CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET"
    REPLAY_OF_REAL_CAPTURE = "REPLAY_OF_REAL_CAPTURE"
    SYNTHETIC_SUPPORTING_TEST_ONLY = "SYNTHETIC_SUPPORTING_TEST_ONLY"
    """Present so a synthetic record is *labelled* as one, not so synthetic data can pass a
    gate. A market carrying this provenance is not empirical evidence about anything."""


class FillProvenance(Enum):
    """Whether a fill actually happened at a venue.

    Kept separate from everything else because conflating the two would be the single most
    misleading thing this package could do: a modelled fill and a real one look identical once
    they are rows in a table.
    """

    REAL_VENUE = "REAL_VENUE"
    """An authenticated venue fill. **None exist yet** — P14 owns the first one."""

    SHADOW_MODEL = "SHADOW_MODEL"
    """Our own model of what would have filled. Evidence about the model, not about money."""


@dataclass(frozen=True, slots=True)
class ExactRatio:
    """A rational number, stored as it is rather than as the nearest float.

    Term1 and Term2 are genuinely rational — they come from average acquisition prices — and
    the identity ``term1 + term2 == gross_payout - total_cost`` holds exactly over Fractions
    and only approximately over floats. Since the whole point of persisting them is to check
    that identity later, they are stored as a pair of integers.
    """

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if self.denominator == 0:
            raise ValueError("an exact ratio needs a non-zero denominator")

    @classmethod
    def of(cls, value: Fraction) -> ExactRatio:
        return cls(numerator=value.numerator, denominator=value.denominator)

    @property
    def value(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    def summary(self) -> dict[str, object]:
        return {"numerator": self.numerator, "denominator": self.denominator}


@dataclass(frozen=True, slots=True)
class SideRecord:
    """One outcome's whole story for one decision.

    Deliberately not collapsed into a single action string. "REPLACE" alone cannot answer why
    the queue slot was given up, what was resting before, what was wanted instead, or which
    strategy gate allowed the side at all — and those are exactly the questions the edge budget
    in Canonical §27 turns on.
    """

    outcome: str
    action: str
    reason: str

    desired_price: PriceUnits | None = None
    desired_size: ShareUnits | None = None
    strategy_price: PriceUnits | None = None
    strategy_size: ShareUnits | None = None
    preparation_outcome: str | None = None
    observed_ask: PriceUnits | None = None

    live_client_order_id: str | None = None
    live_venue_order_id: str | None = None
    live_price: PriceUnits | None = None
    live_original_size: ShareUnits | None = None
    live_remaining_size: ShareUnits | None = None
    live_status: str | None = None

    queue_ahead: ShareUnits | None = None
    queue_confidence: str | None = None
    displayed_depth: ShareUnits | None = None

    strategy_reason: str | None = None
    """Which strategy gate explains a side that is not quoting, where one applies."""


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """One decision cycle, complete enough to explain itself without the process that made it.

    Covers every field Canonical §25 requires of a decision, plus the provenance and ordering
    needed to verify the stream afterwards.
    """

    schema_version: int
    record_type: str
    persistence_sequence: int
    """Storage order. **Not market causality** — see `ingress_ordinal` for that."""

    market_id: str
    slug: str
    condition_id: str | None

    ingress_ordinal: int
    event_id: str
    event_kind: str
    capture_sequence: int

    local_monotonic_ns: int
    """The latency clock. Comparable only with other readings from the same run."""

    event_timestamp_ns: TimestampNs
    exchange_timestamp_ns: TimestampNs | None
    """The venue's own clock, when the feed genuinely supplied one. ``None`` otherwise, and
    never the ingress clock wearing the venue's name."""

    phase: str

    spot_price_units: int | None
    spot_price_scale_decimals: int | None
    """BTC spot in its own self-describing form, exactly as O12 left it.

    Two fields rather than one decimal: the feed's scale is data, not a formatting choice, and
    collapsing ``units`` and ``scale_decimals`` into a single number would either lose precision
    or silently assert a scale the feed never claimed."""

    spot_timestamp_ns: TimestampNs | None
    spot_age_ns: int | None

    up_best_bid: PriceUnits | None
    up_best_ask: PriceUnits | None
    down_best_bid: PriceUnits | None
    down_best_ask: PriceUnits | None
    book_timestamp_ns: TimestampNs | None
    book_age_ns: int | None

    raw_centre: ExactRatio | None
    """The centre *before* tick quantization, kept rational.

    `RawCentre` is a numerator/denominator pair on purpose: the CLOB midpoint of an odd bid+ask
    sum is a genuine half unit, and P3 refuses to round it early so the quantization decision
    stays in one place. Persisting it as an integer price would throw away the very thing it
    exists to preserve, and would make the stored centre disagree with the one the decision was
    actually made from."""

    quantized_centre: PriceUnits | None
    centre_source: str
    centre_status: str
    centre_unavailable: str | None

    inventory: ShareUnits
    n_up: ShareUnits
    n_down: ShareUnits
    cost_up: MoneyUnits
    cost_down: MoneyUnits
    total_cost: MoneyUnits
    fees: MoneyUnits
    estimated_rebates: MoneyUnits
    realised_rebates: MoneyUnits

    pnl_if_up_without_rebate: MoneyUnits
    pnl_if_down_without_rebate: MoneyUnits
    pnl_if_up_estimated_rebate: MoneyUnits
    pnl_if_down_estimated_rebate: MoneyUnits

    favourite: str | None
    target_inventory: ShareUnits | None
    base_lot: ShareUnits | None
    base_lot_status: str
    grid_policy: str
    grid_policy_status: str
    endgame_tilt: ShareUnits | None
    endgame_tilt_status: str | None
    endgame_band: ShareUnits | None
    endgame_band_status: str | None
    band_hard: ShareUnits
    band_hard_status: str

    up: SideRecord
    down: SideRecord

    strategy_up_price: PriceUnits | None
    strategy_up_size: ShareUnits | None
    strategy_down_price: PriceUnits | None
    strategy_down_size: ShareUnits | None
    """What the *economic strategy* wanted, before any risk verdict was applied.

    Distinct from `up.desired_price` and its DOWN counterpart, which describe what execution was
    actually allowed to prepare. When risk halts, these stay populated and those go empty, and a
    reader can tell the two situations apart — which is the entire reason the pair exists."""

    risk_withdrew_intent: bool
    """Whether the executable intent differs from the strategy's because safety withdrew it."""

    eligibility_reasons: tuple[str, ...]
    clob_healthy: bool

    risk_state: str | None
    risk_sequence: int | None
    """The `risk_sequence` of the verdict that governed this cycle. A foreign key into the
    persisted risk stream, and the thing a PLACE has to be able to point at."""

    risk_allows_place: bool | None
    risk_allows_cancel: bool | None

    provenance: str

    def __post_init__(self) -> None:
        if self.schema_version != DECISION_SCHEMA_VERSION:
            raise ValueError(f"unsupported decision schema {self.schema_version}")


@dataclass(frozen=True, slots=True)
class FillRecord:
    """One fill, with the ledger on both sides of it.

    Before-state and after-state are **captured**, never derived by running the fill arithmetic
    backwards. Reversing it would produce a number that agrees with the ledger by construction
    and would therefore never disagree with it, which is the opposite of evidence.
    """

    schema_version: int
    record_type: str
    persistence_sequence: int

    market_id: str
    event_id: str
    ingress_ordinal: int

    outcome: str
    token_id: str
    price: PriceUnits
    size: ShareUnits
    liquidity: str
    """MAKER / TAKER / UNKNOWN, exactly as reported. Never repaired, never reinterpreted."""

    fee: MoneyUnits
    provenance: str

    inventory_before: ShareUnits
    inventory_after: ShareUnits
    n_up_before: ShareUnits
    n_up_after: ShareUnits
    n_down_before: ShareUnits
    n_down_after: ShareUnits
    cost_up_before: MoneyUnits
    cost_up_after: MoneyUnits
    cost_down_before: MoneyUnits
    cost_down_after: MoneyUnits
    total_cost_before: MoneyUnits
    total_cost_after: MoneyUnits
    fees_before: MoneyUnits
    fees_after: MoneyUnits
    estimated_rebates_before: MoneyUnits
    estimated_rebates_after: MoneyUnits
    realised_rebates_before: MoneyUnits
    realised_rebates_after: MoneyUnits

    pnl_if_up_before: MoneyUnits
    pnl_if_up_after: MoneyUnits
    pnl_if_down_before: MoneyUnits
    pnl_if_down_after: MoneyUnits

    queue_ahead_before: ShareUnits | None
    queue_confidence: str | None

    spot_price_units_at_fill: int | None
    spot_price_scale_decimals_at_fill: int | None
    up_best_bid_at_fill: PriceUnits | None
    up_best_ask_at_fill: PriceUnits | None
    down_best_bid_at_fill: PriceUnits | None
    down_best_ask_at_fill: PriceUnits | None

    client_order_id: str | None
    venue_order_id: str | None

    def __post_init__(self) -> None:
        if self.schema_version != FILL_SCHEMA_VERSION:
            raise ValueError(f"unsupported fill schema {self.schema_version}")


@dataclass(frozen=True, slots=True)
class RiskRow:
    """A P9 ``RiskRecord``, flattened for storage and nothing more.

    P11 does not own risk semantics and does not get an opinion about them. The round trip is
    required to reproduce the original record exactly, so that a persisted trace remains a valid
    input to P9's own replay verifier.
    """

    schema_version: int
    persistence_sequence: int
    market_id: str

    risk_sequence: int
    as_of_ingress_ordinal: int
    signal_kind: str
    signal_reason: str | None
    signal_flag: bool
    signal_timestamp_ns: TimestampNs
    signal_value_ns: int | None
    state: str
    active: tuple[str, ...]
    latched: tuple[str, ...]
    allows_place: bool
    allows_cancel: bool
    provenance: str
    risk_schema_version: int


@dataclass(frozen=True, slots=True)
class SettlementRow:
    """A P10 ``SettlementRecord``, flattened. No key, no signature, no credential."""

    schema_version: int
    persistence_sequence: int
    market_id: str
    slug: str
    condition_id: str

    resolution_state: str
    winning_outcome: str | None
    payout_denominator: int | None
    payout_numerators: tuple[int, ...]
    outcome_slot_count: int | None

    authoritative_block: int | None
    block_tag: str
    agreeing_providers: tuple[str, ...]
    answering_providers: tuple[str, ...]
    minimum_agreeing_providers: int
    reasons: tuple[str, ...]
    advisory: tuple[tuple[str, int | None], ...]

    expected_redeem_value: MoneyUnits | None
    paper_settlement_pnl: MoneyUnits | None
    rebate_mode: str | None
    redeem_plan_condition_id: str | None
    redeem_plan_index_sets: tuple[int, ...]
    redeem_blockers: tuple[str, ...]
    redemption_enabled: bool


@dataclass(frozen=True, slots=True)
class MarketMetrics:
    """Canonical §26, per market. Every term defined, none left to interpretation."""

    schema_version: int
    market_id: str
    slug: str

    settled: bool
    winner: str | None

    gross_payout: MoneyUnits
    total_cost: MoneyUnits
    fees: MoneyUnits
    estimated_rebates: MoneyUnits
    realised_rebates: MoneyUnits
    rebate_mode: str
    """Which rebate view produced ``net_pnl``. O07 is open, so this is never implicit."""

    net_pnl: MoneyUnits

    term1: ExactRatio | None
    term2: ExactRatio | None
    average_price_winner: ExactRatio | None
    average_price_loser: ExactRatio | None
    matched_shares: ShareUnits | None

    n_up: ShareUnits
    n_down: ShareUnits
    terminal_inventory: ShareUnits
    terminal_residual_side: str | None
    terminal_residual_magnitude: ShareUnits

    pnl_if_up_before_settlement: MoneyUnits
    pnl_if_down_before_settlement: MoneyUnits

    maker_fill_count: int
    taker_fill_count: int
    unknown_liquidity_fill_count: int
    fill_count: int
    """``maker_fraction`` is deliberately absent as a decimal: it is stored as the numerator and
    denominator above so a reader can compute it without inheriting a rounding choice."""

    queue_ahead_sum: int
    queue_ahead_samples: int
    """Sum and count, so the mean is reproducible exactly rather than stored pre-rounded."""

    queue_stale_samples: int

    place_count: int
    keep_count: int
    cancel_count: int
    replace_count: int
    wait_count: int
    blocked_count: int
    nothing_count: int

    quote_intent_count: int
    """Cycles where the strategy wanted a resting order on a side — PLACE, KEEP or REPLACE.

    Canonical §26 says "quote count" without defining it, so both readings are stored rather
    than one being silently chosen: this is the intent reading, `place_count` is the other."""

    stale_quote_count: int
    decision_count: int
    provenance: str

    def maker_fraction(self) -> ExactRatio | None:
        """Maker fills over all fills, exactly. ``None`` when nothing has filled."""
        if self.fill_count == 0:
            return None
        return ExactRatio(numerator=self.maker_fill_count, denominator=self.fill_count)

    def average_queue_ahead(self) -> ExactRatio | None:
        if self.queue_ahead_samples == 0:
            return None
        return ExactRatio(numerator=self.queue_ahead_sum, denominator=self.queue_ahead_samples)


@dataclass(frozen=True, slots=True)
class Manifest:
    """What a persisted market claims about itself, and whether the claim can be checked.

    ``telemetry_complete`` is the load-bearing field. It is false whenever anything was lost,
    and trading having gone perfectly well is not evidence for it: a market whose telemetry has
    a hole in it is not a market whose telemetry can be used to settle an open question.
    """

    schema_version: int
    slug: str
    market_id: str
    condition_id: str | None

    capture_start_ns: TimestampNs | None
    capture_end_ns: TimestampNs | None
    source_revision: str

    decision_count: int
    fill_count: int
    risk_count: int
    settlement_count: int

    first_ingress_ordinal: int | None
    last_ingress_ordinal: int | None
    first_persistence_sequence: int | None
    last_persistence_sequence: int | None

    accepted_records: int
    persisted_records: int
    dropped_records: int
    sequence_gaps: int
    lost_observations: int
    sink_errors: int
    first_gap_at: int | None
    last_gap_at: int | None

    buffer_capacity: int
    buffer_high_water: int

    database_bytes: int | None
    database_sha256: str | None
    """Left ``None`` inside the store, deliberately.

    A file cannot contain its own hash: writing the digest in would change the file and
    invalidate the digest in the same act. The digest lives in a sidecar manifest written once
    the database is closed, in the same shape P6 already uses for its capture manifests."""

    provenance: str
    live_trading_enabled: bool
    redemption_enabled: bool
    closed: bool
    """False until the market is explicitly finished. A crash therefore leaves it false, and an
    unfinished market can never be mistaken for a complete one."""

    risk_records_accepted: int = 0
    risk_records_persisted: int = 0
    risk_records_dropped: int = 0
    fill_captures_accepted: int = 0
    fill_captures_persisted: int = 0
    fill_captures_dropped: int = 0
    """Per-stream durability accounting.

    A bounded channel that overflows loses its *oldest* entries, so a risk stream can lose its
    prefix and the retained suffix will still look internally contiguous. Counting acceptance
    against persistence is what makes that visible; P9C closed the same class of defect once
    already and it is not being reopened here."""

    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def telemetry_complete(self) -> bool:
        """Complete means nothing was lost, not that trading succeeded.

        Every stream has to have survived, not just the one that happens to be biggest. A market
        whose decisions all landed but whose risk prefix fell off a bounded channel is exactly
        the case this property exists to refuse.
        """
        return (
            self.closed
            and self.dropped_records == 0
            and self.sequence_gaps == 0
            and self.lost_observations == 0
            and self.sink_errors == 0
            and self.risk_records_dropped == 0
            and self.fill_captures_dropped == 0
            and self.accepted_records == self.persisted_records
            and self.risk_records_accepted == self.risk_records_persisted
            and self.fill_captures_accepted == self.fill_captures_persisted
        )
