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
import zlib

import pytest

import query_builder as QB


def _token(payload_bytes: bytes) -> str:
    return base64.urlsafe_b64encode(zlib.compress(payload_bytes)).decode()


# ------------------------------------------------------------ round trips

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
                              "surprise": True})
    with pytest.raises(ValueError):
        QB.validate_envelope({"v": 1, "mode": "filters", "query": {},
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
