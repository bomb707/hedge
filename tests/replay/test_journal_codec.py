"""Canonical codec: byte identity, exhaustive coverage, and fail-closed decoding."""

from __future__ import annotations

import json

import pytest

from maker5m.market import (
    BookUpdate,
    HealthEvent,
    OrderStateEvent,
    OwnFill,
    PhaseEvent,
    SpotTick,
)
from maker5m.replay import (
    SCHEMA_VERSION,
    JournalDecodeError,
    JournalEncodeError,
    JournalProvenance,
    UnsupportedComponentError,
    UnsupportedSchemaError,
    decode_journal,
    encode_journal,
)
from maker5m.replay.codec import encode_line
from tests.replay.corpus import synthetic_run

RUN = synthetic_run()
JOURNAL = RUN.journal
BYTES = encode_journal(JOURNAL)


# -- the byte contract ----------------------------------------------------------------------


def test_decode_of_encode_is_the_same_journal() -> None:
    assert decode_journal(BYTES) == JOURNAL


def test_encode_of_decode_is_byte_identical() -> None:
    """The P5 acceptance property."""
    assert encode_journal(decode_journal(BYTES)) == BYTES


def test_encoding_is_stable_across_repeated_calls() -> None:
    assert all(encode_journal(JOURNAL) == BYTES for _ in range(10))


def test_a_second_recording_produces_identical_bytes() -> None:
    assert encode_journal(synthetic_run().journal) == BYTES


# -- canonical form -------------------------------------------------------------------------


def test_journal_is_utf8_ndjson_with_newline_endings() -> None:
    text = BYTES.decode("utf-8")
    assert text.endswith("\n")
    assert "\r" not in text
    lines = text.split("\n")[:-1]
    assert len(lines) == JOURNAL.step_count + 1
    assert all(json.loads(line) for line in lines)


def test_keys_are_sorted_and_separators_are_compact() -> None:
    for line in BYTES.decode("utf-8").split("\n")[:-1]:
        record = json.loads(line)
        assert line == json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def test_the_encoding_is_pure_ascii() -> None:
    """Removes any dependence on encoding subtleties for byte identity."""
    BYTES.decode("ascii")


def test_no_float_appears_anywhere_in_the_journal() -> None:
    def walk(value: object, path: str) -> None:
        assert not isinstance(value, float), f"float at {path}"
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    for number, line in enumerate(BYTES.decode("utf-8").split("\n")[:-1]):
        walk(json.loads(line), f"line{number}")


def test_the_encoder_refuses_a_float() -> None:
    with pytest.raises(JournalEncodeError):
        encode_line({"a": 1.0})
    with pytest.raises(JournalEncodeError):
        encode_line({"a": {"b": [1, 2.5]}})


def test_no_repr_or_class_paths_leak_into_the_journal() -> None:
    text = BYTES.decode("utf-8")
    assert "maker5m." not in text
    assert "object at 0x" not in text
    assert "<" not in text


# -- coverage --------------------------------------------------------------------------------


def test_every_event_type_appears_in_the_corpus() -> None:
    kinds = {type(step.event) for step in JOURNAL.steps}
    assert kinds == {SpotTick, BookUpdate, OwnFill, OrderStateEvent, PhaseEvent, HealthEvent}


def test_every_event_round_trips_exactly() -> None:
    decoded = decode_journal(BYTES)
    for original, restored in zip(JOURNAL.steps, decoded.steps, strict=True):
        assert restored.event == original.event
        assert restored.event.meta.event_id == original.event.meta.event_id
        assert restored.event.meta.ingress_ordinal == original.event.meta.ingress_ordinal
        assert restored.event.meta.timestamp == original.event.meta.timestamp
        assert restored.event.meta.market_id == original.event.meta.market_id


def test_every_decision_round_trips_exactly() -> None:
    decoded = decode_journal(BYTES)
    for original, restored in zip(JOURNAL.steps, decoded.steps, strict=True):
        assert restored.decision == original.decision
        assert restored.decision.telemetry == original.decision.telemetry
        assert restored.decision.telemetry.economics == original.decision.telemetry.economics


def test_header_round_trips_including_the_full_config() -> None:
    header = decode_journal(BYTES).header
    assert header == JOURNAL.header
    assert header.schema_version == SCHEMA_VERSION
    assert header.provenance is JournalProvenance.SYNTHETIC
    assert header.market == JOURNAL.header.market
    assert header.config == JOURNAL.header.config
    assert header.config.quote_centre == JOURNAL.header.config.quote_centre
    assert header.config.base_lot_selector == JOURNAL.header.config.base_lot_selector


def test_optional_values_are_present_and_explicitly_null_never_omitted() -> None:
    """An absent key and a null value must not be the same thing on the wire.

    Every optional field is written explicitly, so a decoder can tell "recorded as nothing"
    from "this build forgot to write it" - and the strict decoder rejects a missing key.
    """
    records = [json.loads(line) for line in BYTES.decode("utf-8").split("\n")[:-1]]
    assert "strike" in records[0]["market"]

    telemetry_keys = {key for record in records[1:] for key in record["decision"]["telemetry"]}
    for record in records[1:]:
        assert set(record["decision"]["telemetry"]) == telemetry_keys

    # A QUOTE step has no endgame block, and it is written as an explicit null.
    quote_steps = [r for r in records[1:] if r["decision"]["telemetry"]["phase"] == "QUOTE"]
    assert quote_steps
    assert quote_steps[0]["decision"]["telemetry"]["endgame"] is None

    # A one-sided or absent book level is written as an explicit null too.
    books = [r["event"] for r in records[1:] if r["event"]["tag"] == "BookUpdate"]
    assert books
    assert all({"up_bid", "up_ask", "down_bid", "down_ask", "sequence"} <= set(b) for b in books)


def test_endgame_and_non_endgame_steps_both_round_trip() -> None:
    decoded = decode_journal(BYTES)
    with_endgame = [s for s in decoded.steps if s.decision.telemetry.endgame is not None]
    without = [s for s in decoded.steps if s.decision.telemetry.endgame is None]
    assert with_endgame and without


def test_steps_with_and_without_orders_both_round_trip() -> None:
    decoded = decode_journal(BYTES)
    assert any(not s.decision.orders.is_empty for s in decoded.steps)
    assert any(s.decision.orders.is_empty for s in decoded.steps)


# -- fail closed -------------------------------------------------------------------------------


def lines() -> list[str]:
    return BYTES.decode("utf-8").split("\n")[:-1]


def rebuild(records: list[str]) -> bytes:
    return "".join(line + "\n" for line in records).encode("utf-8")


def mutate(index: int, mutator: object) -> bytes:
    records = lines()
    record = json.loads(records[index])
    mutator(record)  # type: ignore[operator]
    records[index] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return rebuild(records)


def test_unknown_schema_version_fails_closed() -> None:
    with pytest.raises(UnsupportedSchemaError):
        decode_journal(mutate(0, lambda r: r.__setitem__("schema_version", 999)))


def test_unknown_event_tag_fails_closed() -> None:
    with pytest.raises(JournalDecodeError, match="unknown event tag"):
        decode_journal(mutate(1, lambda r: r["event"].__setitem__("tag", "TeleportEvent")))


def test_unknown_enum_value_fails_closed() -> None:
    with pytest.raises(JournalDecodeError, match="unknown Phase value"):
        decode_journal(
            mutate(1, lambda r: r["decision"]["telemetry"].__setitem__("phase", "LUNCH"))
        )


def test_missing_required_field_fails_closed() -> None:
    with pytest.raises(JournalDecodeError, match="missing required fields"):
        decode_journal(mutate(1, lambda r: r["event"]["meta"].pop("ingress_ordinal")))


def test_unexpected_extra_field_fails_closed() -> None:
    with pytest.raises(JournalDecodeError, match="unexpected fields"):
        decode_journal(mutate(1, lambda r: r["event"]["meta"].__setitem__("extra", 1)))


def test_unsupported_config_component_fails_closed() -> None:
    with pytest.raises(UnsupportedComponentError):
        decode_journal(
            mutate(0, lambda r: r["config"]["quote_centre"].__setitem__("kind", "ORACLE"))
        )


def test_unsupported_base_lot_selector_fails_closed() -> None:
    with pytest.raises(UnsupportedComponentError):
        decode_journal(
            mutate(0, lambda r: r["config"]["base_lot_selector"].__setitem__("kind", "MAGIC"))
        )


def test_a_boolean_where_an_integer_belongs_fails_closed() -> None:
    with pytest.raises(JournalDecodeError, match="expected an integer"):
        decode_journal(mutate(1, lambda r: r["event"]["meta"].__setitem__("ingress_ordinal", True)))


def test_a_string_where_an_integer_belongs_fails_closed() -> None:
    with pytest.raises(JournalDecodeError, match="expected an integer"):
        decode_journal(mutate(1, lambda r: r["event"]["meta"].__setitem__("timestamp", "5")))


def test_a_misplaced_step_index_fails_closed() -> None:
    with pytest.raises(JournalDecodeError, match="does not match its position"):
        decode_journal(mutate(2, lambda r: r.__setitem__("index", 99)))


def test_a_header_that_is_not_a_header_fails_closed() -> None:
    with pytest.raises(JournalDecodeError, match="not a header"):
        decode_journal(mutate(0, lambda r: r.__setitem__("record_type", "step")))


def test_invalid_json_fails_closed() -> None:
    records = lines()
    records[1] = "{not json"
    with pytest.raises(JournalDecodeError, match="not valid JSON"):
        decode_journal(rebuild(records))


def test_missing_trailing_newline_fails_closed() -> None:
    with pytest.raises(JournalDecodeError, match="must end with a newline"):
        decode_journal(BYTES.rstrip(b"\n"))


def test_empty_input_fails_closed() -> None:
    with pytest.raises(JournalDecodeError, match="empty"):
        decode_journal(b"")


def test_non_bytes_input_fails_closed() -> None:
    with pytest.raises(JournalDecodeError, match="expected bytes"):
        decode_journal("not bytes")  # type: ignore[arg-type]


def test_invalid_utf8_fails_closed() -> None:
    with pytest.raises(JournalDecodeError, match="UTF-8"):
        decode_journal(b"\xff\xfe\n")


def test_a_logically_invalid_value_fails_closed() -> None:
    """Domain validation still applies after decoding: a negative fill size is impossible."""
    step_index = next(
        index
        for index, line in enumerate(lines())
        if index > 0 and json.loads(line)["event"]["tag"] == "OwnFill"
    )
    from maker5m.numeric import DomainError

    with pytest.raises((JournalDecodeError, DomainError)):
        decode_journal(mutate(step_index, lambda r: r["event"]["fill"].__setitem__("shares", -1)))
