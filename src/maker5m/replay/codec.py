"""Canonical journal codec.

One encoder, one decoder, and a byte contract:

```text
decode(encode(journal)) == journal
encode(decode(bytes))   == bytes        byte for byte
```

Canonical form
--------------
UTF-8 NDJSON, one record per line, ``\\n`` endings including after the last line.
``json.dumps`` with ``sort_keys=True``, ``separators=(",", ":")``, and ``ensure_ascii=True``:

* ``sort_keys`` removes any dependence on dict insertion order;
* the compact separators remove whitespace variation;
* ``ensure_ascii`` keeps the output pure ASCII, so the bytes cannot vary with any encoding
  subtlety at all.

**No floats anywhere.** Every quantity in this project is already an exact integer, and the
encoder asserts that on the way out — a float in a journal would silently destroy the exactness
the whole numeric kernel exists to provide. Enums are encoded by their explicit stable values,
never by name, index, or ``repr``. No ``repr``, no ``pickle``, no class-qualified paths, no
object identities.

Strictness
----------
The decoder rejects missing fields, unknown fields, unknown tags, unknown enum values,
unsupported schema versions, and unsupported strategy components. It never guesses and never
falls back to a default — a journal that decoded into *something* other than what was recorded
would be worse than one that failed to decode.
"""

import json
from collections.abc import Iterator, Mapping
from enum import Enum
from typing import Any, Final

from maker5m.accounting.ledger import Fill
from maker5m.domain import Outcome, ParameterStatus
from maker5m.market.btc_price import BtcPrice
from maker5m.market.events import (
    BookLevel,
    BookUpdate,
    Event,
    EventMeta,
    HealthComponent,
    HealthEvent,
    HealthStatus,
    Liquidity,
    OrderStateEvent,
    OrderStatus,
    OwnFill,
    PhaseEvent,
    SpotTick,
)
from maker5m.market.phase import Phase, PhaseConfig
from maker5m.market.state import MarketDefinition
from maker5m.market.timebase import DurationNs, TimestampNs
from maker5m.numeric.units import MoneyUnits, PriceUnits, ShareUnits
from maker5m.replay.errors import (
    JournalDecodeError,
    JournalEncodeError,
    UnsupportedComponentError,
    UnsupportedSchemaError,
)
from maker5m.replay.journal import Journal, JournalHeader, ReplayStep
from maker5m.replay.schema import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    JournalProvenance,
    RecordType,
)
from maker5m.strategy.baselot import BaseLot, BaseLotSelector, ConfiguredBaseLotSelector
from maker5m.strategy.centre import (
    CentreSource,
    CentreUnavailable,
    ClobMidCentre,
    QuoteCentre,
    RawCentre,
)
from maker5m.strategy.config import StrategyConfig
from maker5m.strategy.decision import (
    DecisionEconomics,
    DecisionResult,
    DecisionTelemetry,
    DesiredOrder,
    DesiredOrders,
    EndgameTelemetry,
)
from maker5m.strategy.eligibility import EligibilityReason, EligibilityResult
from maker5m.strategy.grid import GridPolicy, GridRounding
from maker5m.strategy.quantization import TickRounding

__all__ = ["decode_journal", "encode_journal", "encode_line", "iter_encoded_journal"]

_JSON: Final[dict[str, Any]] = {
    "sort_keys": True,
    "separators": (",", ":"),
    "ensure_ascii": True,
    "allow_nan": False,
}

_CENTRE_CLOB_MID: Final = "CLOB_MID_CENTRE"
_SELECTOR_CONFIGURED: Final = "CONFIGURED_BASE_LOT_SELECTOR"

Json = Any


# -- primitives ----------------------------------------------------------------------------


def _no_floats(value: Json, path: str = "$") -> None:
    """Reject any float before it can reach a journal."""
    if isinstance(value, float):
        raise JournalEncodeError(f"{path}: floats are not encodable in a journal")
    if isinstance(value, dict):
        for key, item in value.items():
            _no_floats(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _no_floats(item, f"{path}[{index}]")


def _fields(data: Json, *names: str) -> tuple[Json, ...]:
    """Extract exactly ``names`` from a mapping, rejecting missing and unknown keys."""
    if not isinstance(data, dict):
        raise JournalDecodeError(f"expected an object, got {type(data).__name__}")
    missing = [name for name in names if name not in data]
    if missing:
        raise JournalDecodeError(f"missing required fields: {sorted(missing)}")
    unknown = sorted(set(data) - set(names))
    if unknown:
        raise JournalDecodeError(f"unexpected fields: {unknown}")
    return tuple(data[name] for name in names)


def _int(value: Json, field: str) -> int:
    # bool is a subclass of int; accepting it would let ``true`` decode as ``1``.
    if isinstance(value, bool) or not isinstance(value, int):
        raise JournalDecodeError(f"{field}: expected an integer, got {value!r}")
    return value


def _str(value: Json, field: str) -> str:
    if not isinstance(value, str):
        raise JournalDecodeError(f"{field}: expected a string, got {value!r}")
    return value


def _bool(value: Json, field: str) -> bool:
    if not isinstance(value, bool):
        raise JournalDecodeError(f"{field}: expected a boolean, got {value!r}")
    return value


def _opt_int(value: Json, field: str) -> int | None:
    return None if value is None else _int(value, field)


def _opt_str(value: Json, field: str) -> str | None:
    return None if value is None else _str(value, field)


def _enum[E: Enum](cls: type[E], value: Json, field: str) -> E:
    if not isinstance(value, str):
        raise JournalDecodeError(f"{field}: expected a string enum value, got {value!r}")
    try:
        return cls(value)
    except ValueError as exc:
        raise JournalDecodeError(f"{field}: unknown {cls.__name__} value {value!r}") from exc


def _opt_enum[E: Enum](cls: type[E], value: Json, field: str) -> E | None:
    return None if value is None else _enum(cls, value, field)


# -- shared value objects -------------------------------------------------------------------


def _enc_meta(meta: EventMeta) -> Json:
    return {
        "market_id": meta.market_id,
        "event_id": meta.event_id,
        "ingress_ordinal": meta.ingress_ordinal,
        "timestamp": meta.timestamp,
    }


def _dec_meta(data: Json) -> EventMeta:
    market_id, event_id, ordinal, timestamp = _fields(
        data, "market_id", "event_id", "ingress_ordinal", "timestamp"
    )
    return EventMeta(
        market_id=_str(market_id, "meta.market_id"),
        event_id=_str(event_id, "meta.event_id"),
        ingress_ordinal=_int(ordinal, "meta.ingress_ordinal"),
        timestamp=TimestampNs(_int(timestamp, "meta.timestamp")),
    )


def _enc_btc(price: BtcPrice) -> Json:
    return {"units": price.units, "scale_decimals": price.scale_decimals}


def _dec_btc(data: Json) -> BtcPrice:
    units, scale = _fields(data, "units", "scale_decimals")
    return BtcPrice(_int(units, "btc.units"), _int(scale, "btc.scale_decimals"))


def _enc_level(level: BookLevel | None) -> Json:
    return None if level is None else {"price": level.price, "size": level.size}


def _dec_level(data: Json, field: str) -> BookLevel | None:
    if data is None:
        return None
    price, size = _fields(data, "price", "size")
    return BookLevel(
        PriceUnits(_int(price, f"{field}.price")), ShareUnits(_int(size, f"{field}.size"))
    )


def _enc_fill(fill: Fill) -> Json:
    return {
        "outcome": fill.outcome.value,
        "shares": fill.shares,
        "cost": fill.cost,
        "fee": fill.fee,
        "price": fill.price,
    }


def _dec_fill(data: Json) -> Fill:
    outcome, shares, cost, fee, price = _fields(data, "outcome", "shares", "cost", "fee", "price")
    decoded_price = _opt_int(price, "fill.price")
    return Fill(
        outcome=_enum(Outcome, outcome, "fill.outcome"),
        shares=ShareUnits(_int(shares, "fill.shares")),
        cost=MoneyUnits(_int(cost, "fill.cost")),
        fee=MoneyUnits(_int(fee, "fill.fee")),
        price=None if decoded_price is None else PriceUnits(decoded_price),
    )


def _enc_raw_centre(centre: RawCentre | None) -> Json:
    if centre is None:
        return None
    return {"numerator": centre.numerator, "denominator": centre.denominator}


def _dec_raw_centre(data: Json) -> RawCentre | None:
    if data is None:
        return None
    numerator, denominator = _fields(data, "numerator", "denominator")
    return RawCentre(
        _int(numerator, "raw_centre.numerator"), _int(denominator, "raw_centre.denominator")
    )


def _enc_base_lot(lot: BaseLot | None) -> Json:
    return None if lot is None else {"shares": lot.shares}


def _dec_base_lot(data: Json, field: str) -> BaseLot | None:
    if data is None:
        return None
    (shares,) = _fields(data, "shares")
    return BaseLot(ShareUnits(_int(shares, f"{field}.shares")))


# -- events ---------------------------------------------------------------------------------


def _enc_event(event: Event) -> Json:
    meta = _enc_meta(event.meta)
    if isinstance(event, SpotTick):
        return {
            "tag": "SpotTick",
            "meta": meta,
            "price": _enc_btc(event.price),
            "source_sequence": event.source_sequence,
        }
    if isinstance(event, BookUpdate):
        return {
            "tag": "BookUpdate",
            "meta": meta,
            "up_bid": _enc_level(event.up_bid),
            "up_ask": _enc_level(event.up_ask),
            "down_bid": _enc_level(event.down_bid),
            "down_ask": _enc_level(event.down_ask),
            "sequence": event.sequence,
        }
    if isinstance(event, OwnFill):
        return {
            "tag": "OwnFill",
            "meta": meta,
            "fill": _enc_fill(event.fill),
            "client_order_id": event.client_order_id,
            "venue_order_id": event.venue_order_id,
            "liquidity": event.liquidity.value,
        }
    if isinstance(event, OrderStateEvent):
        return {
            "tag": "OrderStateEvent",
            "meta": meta,
            "client_order_id": event.client_order_id,
            "status": event.status.value,
            "outcome": None if event.outcome is None else event.outcome.value,
            "price": event.price,
            "remaining": event.remaining,
            "venue_order_id": event.venue_order_id,
            "reason": event.reason,
        }
    if isinstance(event, PhaseEvent):
        return {"tag": "PhaseEvent", "meta": meta, "phase": event.phase.value}
    return {
        "tag": "HealthEvent",
        "meta": meta,
        "component": event.component.value,
        "status": event.status.value,
        "detail": event.detail,
    }


def _dec_spot(data: Json) -> SpotTick:
    _, meta, price, sequence = _fields(data, "tag", "meta", "price", "source_sequence")
    return SpotTick(
        meta=_dec_meta(meta),
        price=_dec_btc(price),
        source_sequence=_opt_int(sequence, "SpotTick.source_sequence"),
    )


def _dec_book(data: Json) -> BookUpdate:
    _, meta, up_bid, up_ask, down_bid, down_ask, sequence = _fields(
        data, "tag", "meta", "up_bid", "up_ask", "down_bid", "down_ask", "sequence"
    )
    return BookUpdate(
        meta=_dec_meta(meta),
        up_bid=_dec_level(up_bid, "BookUpdate.up_bid"),
        up_ask=_dec_level(up_ask, "BookUpdate.up_ask"),
        down_bid=_dec_level(down_bid, "BookUpdate.down_bid"),
        down_ask=_dec_level(down_ask, "BookUpdate.down_ask"),
        sequence=_opt_int(sequence, "BookUpdate.sequence"),
    )


def _dec_own_fill(data: Json) -> OwnFill:
    _, meta, fill, client_id, venue_id, liquidity = _fields(
        data, "tag", "meta", "fill", "client_order_id", "venue_order_id", "liquidity"
    )
    return OwnFill(
        meta=_dec_meta(meta),
        fill=_dec_fill(fill),
        client_order_id=_opt_str(client_id, "OwnFill.client_order_id"),
        venue_order_id=_opt_str(venue_id, "OwnFill.venue_order_id"),
        liquidity=_enum(Liquidity, liquidity, "OwnFill.liquidity"),
    )


def _dec_order_state(data: Json) -> OrderStateEvent:
    _, meta, client_id, status, outcome, price, remaining, venue_id, reason = _fields(
        data,
        "tag",
        "meta",
        "client_order_id",
        "status",
        "outcome",
        "price",
        "remaining",
        "venue_order_id",
        "reason",
    )
    decoded_price = _opt_int(price, "OrderStateEvent.price")
    decoded_remaining = _opt_int(remaining, "OrderStateEvent.remaining")
    return OrderStateEvent(
        meta=_dec_meta(meta),
        client_order_id=_str(client_id, "OrderStateEvent.client_order_id"),
        status=_enum(OrderStatus, status, "OrderStateEvent.status"),
        outcome=_opt_enum(Outcome, outcome, "OrderStateEvent.outcome"),
        price=None if decoded_price is None else PriceUnits(decoded_price),
        remaining=None if decoded_remaining is None else ShareUnits(decoded_remaining),
        venue_order_id=_opt_str(venue_id, "OrderStateEvent.venue_order_id"),
        reason=_opt_str(reason, "OrderStateEvent.reason"),
    )


def _dec_phase_event(data: Json) -> PhaseEvent:
    _, meta, phase = _fields(data, "tag", "meta", "phase")
    return PhaseEvent(meta=_dec_meta(meta), phase=_enum(Phase, phase, "PhaseEvent.phase"))


def _dec_health(data: Json) -> HealthEvent:
    _, meta, component, status, detail = _fields(
        data, "tag", "meta", "component", "status", "detail"
    )
    return HealthEvent(
        meta=_dec_meta(meta),
        component=_enum(HealthComponent, component, "HealthEvent.component"),
        status=_enum(HealthStatus, status, "HealthEvent.status"),
        detail=_opt_str(detail, "HealthEvent.detail"),
    )


_EVENT_DECODERS: Final[Mapping[str, Any]] = {
    "SpotTick": _dec_spot,
    "BookUpdate": _dec_book,
    "OwnFill": _dec_own_fill,
    "OrderStateEvent": _dec_order_state,
    "PhaseEvent": _dec_phase_event,
    "HealthEvent": _dec_health,
}


def _dec_event(data: Json) -> Event:
    if not isinstance(data, dict) or "tag" not in data:
        raise JournalDecodeError("event record has no tag")
    tag = _str(data["tag"], "event.tag")
    decoder = _EVENT_DECODERS.get(tag)
    if decoder is None:
        raise JournalDecodeError(f"unknown event tag {tag!r}")
    event: Event = decoder(data)
    return event


# -- decision --------------------------------------------------------------------------------


def _enc_order(order: DesiredOrder | None) -> Json:
    if order is None:
        return None
    return {"outcome": order.outcome.value, "price": order.price, "size": order.size}


def _dec_order(data: Json, field: str) -> DesiredOrder | None:
    if data is None:
        return None
    outcome, price, size = _fields(data, "outcome", "price", "size")
    return DesiredOrder(
        outcome=_enum(Outcome, outcome, f"{field}.outcome"),
        price=PriceUnits(_int(price, f"{field}.price")),
        size=ShareUnits(_int(size, f"{field}.size")),
    )


_ECONOMICS_FIELDS: Final[tuple[str, ...]] = (
    "inventory",
    "n_up",
    "n_down",
    "cost_up",
    "cost_down",
    "total_cost",
    "fees",
    "estimated_rebates",
    "realised_rebates",
    "pnl_if_up_without_rebate",
    "pnl_if_down_without_rebate",
    "pnl_if_up_estimated_rebate",
    "pnl_if_down_estimated_rebate",
)


def _enc_economics(economics: DecisionEconomics) -> Json:
    return {name: getattr(economics, name) for name in _ECONOMICS_FIELDS}


def _dec_economics(data: Json) -> DecisionEconomics:
    values = _fields(data, *_ECONOMICS_FIELDS)
    ints = [_int(v, f"economics.{n}") for v, n in zip(values, _ECONOMICS_FIELDS, strict=True)]
    return DecisionEconomics(*ints)  # type: ignore[arg-type]


def _enc_eligibility(result: EligibilityResult) -> Json:
    return {
        "up_allowed": result.up_allowed,
        "down_allowed": result.down_allowed,
        "up_reasons": [reason.value for reason in result.up_reasons],
        "down_reasons": [reason.value for reason in result.down_reasons],
    }


def _dec_reasons(data: Json, field: str) -> tuple[EligibilityReason, ...]:
    if not isinstance(data, list):
        raise JournalDecodeError(f"{field}: expected a list, got {data!r}")
    return tuple(_enum(EligibilityReason, item, field) for item in data)


def _dec_eligibility(data: Json) -> EligibilityResult:
    up_allowed, down_allowed, up_reasons, down_reasons = _fields(
        data, "up_allowed", "down_allowed", "up_reasons", "down_reasons"
    )
    return EligibilityResult(
        up_allowed=_bool(up_allowed, "eligibility.up_allowed"),
        down_allowed=_bool(down_allowed, "eligibility.down_allowed"),
        up_reasons=_dec_reasons(up_reasons, "eligibility.up_reasons"),
        down_reasons=_dec_reasons(down_reasons, "eligibility.down_reasons"),
    )


def _enc_endgame(endgame: EndgameTelemetry | None) -> Json:
    if endgame is None:
        return None
    return {
        "favourite": endgame.favourite.value,
        "target_inventory": endgame.target_inventory,
        "distance_to_target": endgame.distance_to_target,
        "tilt": endgame.tilt,
        "tilt_status": endgame.tilt_status.value,
        "band": endgame.band,
        "band_status": endgame.band_status.value,
        "gate_up_allowed": endgame.gate_up_allowed,
        "gate_down_allowed": endgame.gate_down_allowed,
        "settlement_edge_favourite": endgame.settlement_edge_favourite,
        "settlement_edge_underdog": endgame.settlement_edge_underdog,
    }


def _dec_endgame(data: Json) -> EndgameTelemetry | None:
    if data is None:
        return None
    (
        favourite,
        target,
        distance,
        tilt,
        tilt_status,
        band,
        band_status,
        gate_up,
        gate_down,
        edge_fav,
        edge_dog,
    ) = _fields(
        data,
        "favourite",
        "target_inventory",
        "distance_to_target",
        "tilt",
        "tilt_status",
        "band",
        "band_status",
        "gate_up_allowed",
        "gate_down_allowed",
        "settlement_edge_favourite",
        "settlement_edge_underdog",
    )
    return EndgameTelemetry(
        favourite=_enum(Outcome, favourite, "endgame.favourite"),
        target_inventory=ShareUnits(_int(target, "endgame.target_inventory")),
        distance_to_target=ShareUnits(_int(distance, "endgame.distance_to_target")),
        tilt=ShareUnits(_int(tilt, "endgame.tilt")),
        tilt_status=_enum(ParameterStatus, tilt_status, "endgame.tilt_status"),
        band=ShareUnits(_int(band, "endgame.band")),
        band_status=_enum(ParameterStatus, band_status, "endgame.band_status"),
        gate_up_allowed=_bool(gate_up, "endgame.gate_up_allowed"),
        gate_down_allowed=_bool(gate_down, "endgame.gate_down_allowed"),
        settlement_edge_favourite=MoneyUnits(_int(edge_fav, "endgame.settlement_edge_favourite")),
        settlement_edge_underdog=MoneyUnits(_int(edge_dog, "endgame.settlement_edge_underdog")),
    )


_TELEMETRY_FIELDS: Final[tuple[str, ...]] = (
    "phase",
    "centre_source",
    "centre_status",
    "raw_centre",
    "centre_unavailable",
    "tick_rounding",
    "tick_rounding_status",
    "quantized_centre",
    "tick",
    "grid_policy",
    "grid_policy_status",
    "grid_rounding",
    "base_lot",
    "base_lot_status",
    "candidate_up_price",
    "candidate_up_size",
    "candidate_down_price",
    "candidate_down_size",
    "eligibility",
    "band_hard",
    "band_hard_status",
    "endgame",
    "economics",
)


def _enc_telemetry(telemetry: DecisionTelemetry) -> Json:
    return {
        "phase": telemetry.phase.value,
        "centre_source": telemetry.centre_source.value,
        "centre_status": telemetry.centre_status.value,
        "raw_centre": _enc_raw_centre(telemetry.raw_centre),
        "centre_unavailable": (
            None if telemetry.centre_unavailable is None else telemetry.centre_unavailable.value
        ),
        "tick_rounding": telemetry.tick_rounding.value,
        "tick_rounding_status": telemetry.tick_rounding_status.value,
        "quantized_centre": telemetry.quantized_centre,
        "tick": telemetry.tick,
        "grid_policy": telemetry.grid_policy.value,
        "grid_policy_status": telemetry.grid_policy_status.value,
        "grid_rounding": telemetry.grid_rounding.value,
        "base_lot": _enc_base_lot(telemetry.base_lot),
        "base_lot_status": telemetry.base_lot_status.value,
        "candidate_up_price": telemetry.candidate_up_price,
        "candidate_up_size": telemetry.candidate_up_size,
        "candidate_down_price": telemetry.candidate_down_price,
        "candidate_down_size": telemetry.candidate_down_size,
        "eligibility": _enc_eligibility(telemetry.eligibility),
        "band_hard": telemetry.band_hard,
        "band_hard_status": telemetry.band_hard_status.value,
        "endgame": _enc_endgame(telemetry.endgame),
        "economics": _enc_economics(telemetry.economics),
    }


def _dec_telemetry(data: Json) -> DecisionTelemetry:
    values = dict(zip(_TELEMETRY_FIELDS, _fields(data, *_TELEMETRY_FIELDS), strict=True))
    quantized = _opt_int(values["quantized_centre"], "telemetry.quantized_centre")
    up_price = _opt_int(values["candidate_up_price"], "telemetry.candidate_up_price")
    up_size = _opt_int(values["candidate_up_size"], "telemetry.candidate_up_size")
    down_price = _opt_int(values["candidate_down_price"], "telemetry.candidate_down_price")
    down_size = _opt_int(values["candidate_down_size"], "telemetry.candidate_down_size")
    return DecisionTelemetry(
        phase=_enum(Phase, values["phase"], "telemetry.phase"),
        centre_source=_enum(CentreSource, values["centre_source"], "telemetry.centre_source"),
        centre_status=_enum(ParameterStatus, values["centre_status"], "telemetry.centre_status"),
        raw_centre=_dec_raw_centre(values["raw_centre"]),
        centre_unavailable=_opt_enum(
            CentreUnavailable, values["centre_unavailable"], "telemetry.centre_unavailable"
        ),
        tick_rounding=_enum(TickRounding, values["tick_rounding"], "telemetry.tick_rounding"),
        tick_rounding_status=_enum(
            ParameterStatus, values["tick_rounding_status"], "telemetry.tick_rounding_status"
        ),
        quantized_centre=None if quantized is None else PriceUnits(quantized),
        tick=PriceUnits(_int(values["tick"], "telemetry.tick")),
        grid_policy=_enum(GridPolicy, values["grid_policy"], "telemetry.grid_policy"),
        grid_policy_status=_enum(
            ParameterStatus, values["grid_policy_status"], "telemetry.grid_policy_status"
        ),
        grid_rounding=_enum(GridRounding, values["grid_rounding"], "telemetry.grid_rounding"),
        base_lot=_dec_base_lot(values["base_lot"], "telemetry.base_lot"),
        base_lot_status=_enum(
            ParameterStatus, values["base_lot_status"], "telemetry.base_lot_status"
        ),
        candidate_up_price=None if up_price is None else PriceUnits(up_price),
        candidate_up_size=None if up_size is None else ShareUnits(up_size),
        candidate_down_price=None if down_price is None else PriceUnits(down_price),
        candidate_down_size=None if down_size is None else ShareUnits(down_size),
        eligibility=_dec_eligibility(values["eligibility"]),
        band_hard=ShareUnits(_int(values["band_hard"], "telemetry.band_hard")),
        band_hard_status=_enum(
            ParameterStatus, values["band_hard_status"], "telemetry.band_hard_status"
        ),
        endgame=_dec_endgame(values["endgame"]),
        economics=_dec_economics(values["economics"]),
    )


def _enc_decision(decision: DecisionResult) -> Json:
    return {
        "orders": {
            "up": _enc_order(decision.orders.up),
            "down": _enc_order(decision.orders.down),
        },
        "telemetry": _enc_telemetry(decision.telemetry),
    }


def _dec_decision(data: Json) -> DecisionResult:
    orders, telemetry = _fields(data, "orders", "telemetry")
    up, down = _fields(orders, "up", "down")
    return DecisionResult(
        orders=DesiredOrders(up=_dec_order(up, "orders.up"), down=_dec_order(down, "orders.down")),
        telemetry=_dec_telemetry(telemetry),
    )


# -- header ------------------------------------------------------------------------------------


def _enc_phase_config(config: PhaseConfig) -> Json:
    return {
        "quote_start_offset": config.quote_start_offset,
        "endgame_offset": config.endgame_offset,
        "stop_quoting_offset": config.stop_quoting_offset,
        "duration": config.duration,
        "version": config.version,
    }


def _dec_phase_config(data: Json) -> PhaseConfig:
    quote, endgame, stop, duration, version = _fields(
        data, "quote_start_offset", "endgame_offset", "stop_quoting_offset", "duration", "version"
    )
    return PhaseConfig(
        quote_start_offset=DurationNs(_int(quote, "phase_config.quote_start_offset")),
        endgame_offset=DurationNs(_int(endgame, "phase_config.endgame_offset")),
        stop_quoting_offset=DurationNs(_int(stop, "phase_config.stop_quoting_offset")),
        duration=DurationNs(_int(duration, "phase_config.duration")),
        version=_str(version, "phase_config.version"),
    )


def _enc_market(market: MarketDefinition) -> Json:
    return {
        "market_id": market.market_id,
        "slug": market.slug,
        "up_token_id": market.up_token_id,
        "down_token_id": market.down_token_id,
        "t0": market.t0,
        "phase_config": _enc_phase_config(market.phase_config),
        "tick": market.tick,
        "strike": None if market.strike is None else _enc_btc(market.strike),
    }


def _dec_market(data: Json) -> MarketDefinition:
    market_id, slug, up_token, down_token, t0, phase_config, tick, strike = _fields(
        data,
        "market_id",
        "slug",
        "up_token_id",
        "down_token_id",
        "t0",
        "phase_config",
        "tick",
        "strike",
    )
    return MarketDefinition(
        market_id=_str(market_id, "market.market_id"),
        slug=_str(slug, "market.slug"),
        up_token_id=_str(up_token, "market.up_token_id"),
        down_token_id=_str(down_token, "market.down_token_id"),
        t0=TimestampNs(_int(t0, "market.t0")),
        phase_config=_dec_phase_config(phase_config),
        tick=PriceUnits(_int(tick, "market.tick")),
        strike=None if strike is None else _dec_btc(strike),
    )


def _enc_centre_component(centre: QuoteCentre) -> Json:
    """Encode a centre component by stable identity, never by class path or ``repr``."""
    if isinstance(centre, ClobMidCentre):
        return {
            "kind": _CENTRE_CLOB_MID,
            "source": centre.source.value,
            "status": centre.status.value,
        }
    raise JournalEncodeError(
        f"quote centre component {type(centre).__name__} has no journal representation; "
        "extend the versioned codec before recording with it"
    )


def _dec_centre_component(data: Json) -> QuoteCentre:
    if not isinstance(data, dict) or "kind" not in data:
        raise JournalDecodeError("quote_centre has no kind")
    kind = _str(data["kind"], "quote_centre.kind")
    if kind != _CENTRE_CLOB_MID:
        raise UnsupportedComponentError(f"unsupported quote centre component {kind!r}")
    _, source, status = _fields(data, "kind", "source", "status")
    decoded_source = _enum(CentreSource, source, "quote_centre.source")
    if decoded_source is not CentreSource.CLOB_MID:
        raise UnsupportedComponentError(
            f"{_CENTRE_CLOB_MID} cannot carry source {decoded_source.value}"
        )
    return ClobMidCentre(
        source=decoded_source,
        status=_enum(ParameterStatus, status, "quote_centre.status"),
    )


def _enc_selector_component(selector: BaseLotSelector) -> Json:
    if isinstance(selector, ConfiguredBaseLotSelector):
        return {
            "kind": _SELECTOR_CONFIGURED,
            "base_lot": _enc_base_lot(selector.base_lot),
            "status": selector.status.value,
        }
    raise JournalEncodeError(
        f"base lot selector {type(selector).__name__} has no journal representation; "
        "extend the versioned codec before recording with it"
    )


def _dec_selector_component(data: Json) -> BaseLotSelector:
    if not isinstance(data, dict) or "kind" not in data:
        raise JournalDecodeError("base_lot_selector has no kind")
    kind = _str(data["kind"], "base_lot_selector.kind")
    if kind != _SELECTOR_CONFIGURED:
        raise UnsupportedComponentError(f"unsupported base lot selector {kind!r}")
    _, base_lot, status = _fields(data, "kind", "base_lot", "status")
    lot = _dec_base_lot(base_lot, "base_lot_selector.base_lot")
    if lot is None:
        raise JournalDecodeError("base_lot_selector.base_lot must not be null")
    return ConfiguredBaseLotSelector(
        base_lot=lot, status=_enum(ParameterStatus, status, "base_lot_selector.status")
    )


def _enc_strategy_config(config: StrategyConfig) -> Json:
    """Capture every behaviour-affecting choice explicitly.

    A replay must never depend on what this build's defaults happen to be: a journal recorded
    under ``OBSERVED_ADJACENT`` must still replay under it after the default changes.
    """
    return {
        "quote_centre": _enc_centre_component(config.quote_centre),
        "base_lot_selector": _enc_selector_component(config.base_lot_selector),
        "grid_policy": config.grid_policy.value,
        "grid_rounding": config.grid_rounding.value,
        "tick_rounding": config.tick_rounding.value,
        "endgame_tilt": config.endgame_tilt,
        "endgame_band": config.endgame_band,
        "band_hard": config.band_hard,
    }


def _dec_strategy_config(data: Json) -> StrategyConfig:
    centre, selector, policy, grid_rounding, tick_rounding, tilt, band, hard = _fields(
        data,
        "quote_centre",
        "base_lot_selector",
        "grid_policy",
        "grid_rounding",
        "tick_rounding",
        "endgame_tilt",
        "endgame_band",
        "band_hard",
    )
    return StrategyConfig(
        quote_centre=_dec_centre_component(centre),
        base_lot_selector=_dec_selector_component(selector),
        grid_policy=_enum(GridPolicy, policy, "config.grid_policy"),
        grid_rounding=_enum(GridRounding, grid_rounding, "config.grid_rounding"),
        tick_rounding=_enum(TickRounding, tick_rounding, "config.tick_rounding"),
        endgame_tilt=ShareUnits(_int(tilt, "config.endgame_tilt")),
        endgame_band=ShareUnits(_int(band, "config.endgame_band")),
        band_hard=ShareUnits(_int(hard, "config.band_hard")),
    )


def _enc_header(header: JournalHeader) -> Json:
    return {
        "record_type": RecordType.HEADER.value,
        "schema_version": header.schema_version,
        "provenance": header.provenance.value,
        "description": header.description,
        "market": _enc_market(header.market),
        "config": _enc_strategy_config(header.config),
    }


def _dec_header(data: Json) -> JournalHeader:
    record_type, schema_version, provenance, description, market, config = _fields(
        data, "record_type", "schema_version", "provenance", "description", "market", "config"
    )
    if _str(record_type, "header.record_type") != RecordType.HEADER.value:
        raise JournalDecodeError("first record is not a header")
    version = _int(schema_version, "header.schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise UnsupportedSchemaError(
            f"journal schema version {version} is not supported by this build "
            f"(supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )
    return JournalHeader(
        schema_version=version,
        provenance=_enum(JournalProvenance, provenance, "header.provenance"),
        description=_str(description, "header.description"),
        market=_dec_market(market),
        config=_dec_strategy_config(config),
    )


# -- journal -------------------------------------------------------------------------------------


def encode_line(record: Json) -> bytes:
    """Encode one record canonically, without its newline."""
    _no_floats(record)
    return json.dumps(record, **_JSON).encode("utf-8")


def iter_encoded_journal(journal: Journal) -> Iterator[bytes]:
    """The canonical journal, one complete line at a time, in order. Nothing accumulates.

    Each yielded value is exactly ``encode_line(record) + b"\\n"`` — the same records, in the
    same order, with the same JSON options — so a consumer that concatenates the whole iterator
    gets precisely what `encode_journal` returns. That equality is a test, not a comment.

    It exists because the alternative is measurable. A busy P13 market journal is 187 MB and a
    large one 443 MB; building a list of every line and then joining it holds the line bytes, the
    joined result and the recorded object graph at the same time. A writer that consumes this
    holds one line.
    """
    yield encode_line(_enc_header(journal.header)) + b"\n"
    for index, step in enumerate(journal.steps):
        yield (
            encode_line(
                {
                    "record_type": RecordType.STEP.value,
                    "index": index,
                    "event": _enc_event(step.event),
                    "decision": _enc_decision(step.decision),
                }
            )
            + b"\n"
        )


def encode_journal(journal: Journal) -> bytes:
    """Canonical NDJSON bytes for a journal. Deterministic and byte-stable.

    Unchanged contract, and deliberately still here: fixtures, replay round-trips and the cold
    verifier all compare against these exact bytes. It is now the concatenation of
    `iter_encoded_journal`, which is what makes the streaming writer's output provably identical.
    """
    return b"".join(iter_encoded_journal(journal))


def _dec_step(data: Json, expected_index: int) -> ReplayStep:
    record_type, index, event, decision = _fields(data, "record_type", "index", "event", "decision")
    if _str(record_type, "step.record_type") != RecordType.STEP.value:
        raise JournalDecodeError(f"record {expected_index} is not a step")
    if _int(index, "step.index") != expected_index:
        raise JournalDecodeError(f"step index {index} does not match its position {expected_index}")
    return ReplayStep(event=_dec_event(event), decision=_dec_decision(decision))


def decode_journal(data: bytes) -> Journal:
    """Decode canonical NDJSON bytes. Fails closed on anything unexpected."""
    if not isinstance(data, bytes):
        raise JournalDecodeError(f"expected bytes, got {type(data).__name__}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JournalDecodeError("journal is not valid UTF-8") from exc
    if text and not text.endswith("\n"):
        raise JournalDecodeError("journal must end with a newline")

    lines = text.split("\n")[:-1] if text else []
    if not lines:
        raise JournalDecodeError("journal is empty; a header record is required")

    records: list[Json] = []
    for number, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise JournalDecodeError(f"line {number} is not valid JSON: {exc.msg}") from exc

    header = _dec_header(records[0])
    steps = tuple(_dec_step(record, index) for index, record in enumerate(records[1:]))
    return Journal(header=header, steps=steps)


def current_schema_version() -> int:
    """The schema version this build writes."""
    return SCHEMA_VERSION
