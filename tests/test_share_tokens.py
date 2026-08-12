#!/usr/bin/env python3
"""Share tokens and URL payloads are attacker-shaped input.

The decoder must refuse malformed base64, malformed and concatenated zlib
streams, decompression bombs, non-finite numbers, wrong top-level types,
unversioned garbage and unknown fields -- and the encoder must never emit
a token its own decoder rejects (the old ``default=str`` silently
stringified anything, and NaN rode straight through json.dumps).
"""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---

import base64
import json
import random
import string
import zlib

import pytest

import query_builder as QB


def _token(payload_bytes: bytes) -> str:
    return base64.urlsafe_b64encode(zlib.compress(payload_bytes)).decode()


def _incompressible_payload(size: int) -> dict:
    """A payload zlib cannot shrink: deterministic pseudo-random ASCII,
    chunked under MAX_SCALAR_CHARS so validate_tree accepts it. A
    repetitive filler would deflate to a few hundred bytes and never
    probe the encoded-size boundary at all."""
    rng = random.Random(7)
    alphabet = string.ascii_letters + string.digits
    chunk = QB.MAX_SCALAR_CHARS // 2
    blobs = ["".join(rng.choices(alphabet, k=min(chunk, size - done)))
             for done in range(0, size, chunk)]
    return {"v": 1, "blob": blobs}


# ------------------------------------------------------------ round trips

def test_every_serialized_token_round_trips():
    """The encoder's contract: a token it returns is one the decoder
    accepts. 40 KB of incompressible state stays under the token ceiling
    and must come back exactly."""
    payload = _incompressible_payload(40_000)
    token = QB.serialize_state(payload)
    assert len(token) <= QB.MAX_TOKEN_CHARS
    assert QB.deserialize_state(token) == payload


def test_serializer_refuses_a_token_larger_than_the_decoder_limit():
    """240 KB of incompressible state passes the decompressed-size bound
    but encodes past MAX_TOKEN_CHARS: the old encoder returned that token
    'successfully' and every restore then refused it. Failure must move
    to the serialize call, immediately and with the reason."""
    payload = _incompressible_payload(240_000)
    with pytest.raises(ValueError, match="too large to share"):
        QB.serialize_state(payload)

def test_a_versioned_envelope_round_trips():
    payload = QB.build_share_envelope(
        type("S", (), {"key": "afl"}), "filters",
        {"type": "group", "op": "AND", "children": [
            {"column": "goals", "kind": "integer", "op": "≥",
             "value": 30}]},
        table="players",
        display={"columns": ["player"], "sort": "goals",
                 "descending": True, "limit": 100, "group_by": []})
    decoded = QB.deserialize_state(QB.serialize_state(payload))
    assert decoded == payload
    assert QB.validate_envelope(decoded) == payload


def test_legacy_tokens_migrate_with_the_fold_made_explicit():
    """Old tokens carried a joiner fold; migration nests it exactly:
    ((g1 AND g2) OR g3)."""
    legacy = {"table": "players", "groups": [
        {"joiner": "AND", "match": "AND", "conditions": [
            {"column": "a", "kind": "integer", "op": "≥", "value": 1}]},
        {"joiner": "AND", "match": "OR", "conditions": [
            {"column": "b", "kind": "integer", "op": "≥", "value": 2}]},
        {"joiner": "OR", "match": "AND", "conditions": [
            {"column": "c", "kind": "integer", "op": "≥", "value": 3}]},
    ]}
    envelope = QB.validate_envelope(
        QB.deserialize_state(QB.serialize_state(legacy)))
    assert envelope["v"] == QB.TOKEN_VERSION
    assert envelope["mode"] == "filters"
    query = envelope["query"]
    assert query["op"] == "OR"
    inner = query["children"][0]
    assert inner["op"] == "AND"
    assert inner["children"][0]["children"][0]["column"] == "a"
    assert inner["children"][1]["op"] == "OR"
    assert query["children"][1]["children"][0]["column"] == "c"


@pytest.mark.parametrize("legacy", [
    {"table": "players", "groups": 1},
    {"table": "players", "groups": []},
    {"table": "players", "groups": [{"conditions": 1}]},
    {"table": "players", "groups": [{"match": "NAND", "conditions": [{}]}]},
    {"table": "players", "groups": [
        {"match": "AND", "conditions": ["not-a-dict"]}]},
    {"table": "players", "groups": [
        {"match": "AND", "conditions": [{}]},
        {"joiner": "XOR", "match": "AND", "conditions": [{}]}]},
])
def test_malformed_legacy_shapes_are_refused_not_crashed(legacy):
    """{"groups": 1} used to sail through deserialize_state and die later
    as an uncaught TypeError while widget keys were already written."""
    with pytest.raises(ValueError):
        QB.validate_envelope(legacy)


# --------------------------------------------------------------- decoding

def test_malformed_base64_and_zlib_are_value_errors():
    with pytest.raises(ValueError):
        QB.deserialize_state("!!!not-base64!!!")
    with pytest.raises(ValueError):
        QB.deserialize_state(
            base64.urlsafe_b64encode(b"not zlib at all").decode())
    truncated = _token(json.dumps({"v": 1}).encode())[:-6]
    with pytest.raises(ValueError):
        QB.deserialize_state(truncated)


def test_concatenated_zlib_streams_are_refused():
    """Trailing data after the deflate stream is a doctored token, not
    padding to shrug at."""
    packed = zlib.compress(b'{"v":1,"mode":"grid","query":{}}') + b"EXTRA"
    with pytest.raises(ValueError):
        QB.deserialize_state(base64.urlsafe_b64encode(packed).decode())


def test_non_finite_numbers_are_refused_both_ways():
    for literal in (b'{"value": Infinity}', b'{"value": -Infinity}',
                    b'{"value": NaN}'):
        with pytest.raises(ValueError):
            QB.deserialize_state(_token(b'{"a":' + literal[10:-1] + b'}'))
        with pytest.raises(ValueError):
            QB.deserialize_state(_token(literal))
    with pytest.raises(ValueError):
        QB.serialize_state({"value": float("inf")})
    with pytest.raises(ValueError):
        QB.serialize_state({"value": float("nan")})


def test_unsupported_python_objects_no_longer_serialize_silently():
    """default=str used to turn dates, sets, anything, into strings the
    restore then misread. Serialisation now fails instead."""
    import datetime as dt

    with pytest.raises(Exception):
        QB.serialize_state({"when": dt.date(2020, 1, 1)})
    with pytest.raises(Exception):
        QB.serialize_state({"values": {1, 2}})


def test_wrong_top_level_types_are_refused():
    for payload in (b'[1,2,3]', b'"just a string"', b'42', b'null'):
        with pytest.raises(ValueError):
            QB.deserialize_state(_token(payload))


# -------------------------------------------------------------- envelopes

def test_unknown_versions_and_modes_are_refused():
    with pytest.raises(ValueError):
        QB.validate_envelope({"v": 99, "mode": "filters", "query": {}})
    with pytest.raises(ValueError):
        QB.validate_envelope({"v": 1, "mode": "spreadsheets", "query": {}})
    with pytest.raises(ValueError):
        QB.validate_envelope({"random": "payload"})


def test_unknown_fields_are_refused_not_dropped():
    """A token carrying fields this build does not understand would
    restore to *some* query while meaning another."""
    with pytest.raises(ValueError):
        QB.validate_envelope({"v": 1, "mode": "filters", "query": {},
                              "table": "players", "surprise": True})
    with pytest.raises(ValueError):
        QB.validate_envelope({"v": 1, "mode": "filters", "query": {},
                              "table": "players",
                              "display": {"columns": [], "rowsz": 5}})


def test_the_ui_envelope_builders_validate_cleanly():
    sport = type("S", (), {"key": "nba"})
    grid = QB.build_share_envelope(
        sport, "grid",
        {"type": "group", "op": "OR", "children": [
            {"type": "criterion", "kind": "Played for club",
             "args": ["Boston Celtics"]}]},
        display={"order": "Most obscure", "limit": 50})
    assert QB.validate_envelope(grid) == grid


# ------------------------------------------------- mode-specific envelopes

def _grid_envelope(**extra):
    payload = {"v": 1, "sport": "afl", "mode": "grid",
               "query": {"type": "group", "op": "AND", "children": []}}
    payload.update(extra)
    return payload


def _filters_envelope(**extra):
    payload = {"v": 1, "sport": "afl", "mode": "filters",
               "table": "players",
               "query": {"type": "group", "op": "AND", "children": []}}
    payload.update(extra)
    return payload


@pytest.mark.parametrize("extra", [
    {"table": "secret_staging"},
    {"display": {"columns": ["password"]}},
    {"display": {"sort": "password"}},
    {"display": {"descending": True}},
    {"display": {"group_by": ["password"]}},
])
def test_grid_tokens_refuse_table_and_table_display_fields(extra):
    """Grid restore has no table, columns or sort: a grid token carrying
    them used to validate and then be silently ignored -- the restored
    query was not the token, it was a reinterpretation of it."""
    with pytest.raises(ValueError, match="grid"):
        QB.validate_envelope(_grid_envelope(**extra))


@pytest.mark.parametrize("mode", ["filters", "tree"])
def test_table_tokens_refuse_the_grid_ranking_field(mode):
    with pytest.raises(ValueError, match="display"):
        QB.validate_envelope(
            _filters_envelope(mode=mode, display={"order": "Most games"}))


@pytest.mark.parametrize("missing", ["table", "query"])
@pytest.mark.parametrize("mode", ["filters", "tree"])
def test_table_tokens_demand_their_required_fields(mode, missing):
    payload = _filters_envelope(mode=mode)
    del payload[missing]
    with pytest.raises(ValueError, match="Missing"):
        QB.validate_envelope(payload)


def test_grid_tokens_demand_a_query():
    payload = _grid_envelope()
    del payload["query"]
    with pytest.raises(ValueError, match="Missing"):
        QB.validate_envelope(payload)


def test_each_mode_still_accepts_its_own_exact_shape():
    grid = _grid_envelope(display={"order": "Most obscure", "limit": 25})
    assert QB.validate_envelope(grid) == grid
    filters = _filters_envelope(display={
        "columns": ["player"], "sort": "player", "descending": False,
        "limit": 100, "group_by": []})
    assert QB.validate_envelope(filters) == filters
    tree = _filters_envelope(mode="tree")
    assert QB.validate_envelope(tree) == tree


def test_restore_entrypoints_assert_their_own_mode():
    """Defense in depth under the validator: handing the wrong mode's
    envelope to a restore function is refused before any staging."""
    with pytest.raises(ValueError):
        QB._apply_grid_restore(
            type("S", (), {"key": "afl",
                           "k": staticmethod(lambda *a: ":".join(
                               map(str, a)))}),
            _filters_envelope())


def test_migrated_legacy_tokens_pass_the_common_v1_validator():
    """Migration feeds the same validator as native v1 tokens: the
    lifted envelope carries exactly the filters vocabulary."""
    legacy = {"table": "players", "groups": [
        {"match": "AND", "conditions": [
            {"column": "a", "kind": "integer", "op": "≥", "value": 1}]}]}
    envelope = QB.validate_envelope(legacy)
    assert envelope["mode"] == "filters"
    assert set(envelope) <= QB._ENVELOPE_KEYS_BY_MODE["filters"]
    assert QB._REQUIRED_KEYS_BY_MODE["filters"] <= set(envelope)
