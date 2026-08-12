#!/usr/bin/env python3
"""Per-sport query-builder metadata is a schema contract, held by tests.

query_column_kinds tells the builder which TEXT columns are really dates
and which INTEGERs are really flags; query_low_cardinality_columns tells
the visual tree which text columns deserve a distinct-values scan. Both
are server-owned declarations, so every entry must resolve against the
live database it describes -- a typo'd table, a dropped column, or a
column whose stored format contradicts its declared kind (the MLB
rivalry table stores compact YYYYMMDD dates the ISO compiler must never
touch) fails here, not in front of a reader.
"""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---

import datetime as dt
import os
import sqlite3

import pytest

import query_builder as QB
import sports

SPORT_KEYS = ("afl", "nba", "mlb", "nfl")

VALID_KINDS = {"integer", "float", "boolean", "date", "datetime", "text"}


def _sport_with_db(key):
    sport = sports.get(key)
    if not os.path.exists(sport.db):
        pytest.skip(f"{key} database not present")
    return sport


def _ro(sport):
    return sqlite3.connect(f"file:{sport.db}?mode=ro", uri=True)


def _live_columns(con, table):
    return {name: declared for name, declared in con.execute(
        "SELECT name, type FROM pragma_table_info(?)", (table,))}


# ------------------------------------------------------ kind declarations

@pytest.mark.parametrize("key", SPORT_KEYS)
def test_kind_override_entries_resolve_against_the_live_schema(key):
    sport = _sport_with_db(key)
    allowed = QB._allowed_tables(sport)
    with _ro(sport) as con:
        for entry, kind in sport.query_column_kinds.items():
            table, _, column = entry.partition(".")
            assert kind in VALID_KINDS, entry
            assert table in allowed, \
                f"{entry} declares a kind for a table the builder hides"
            live = _live_columns(con, table)
            assert live, f"{entry}: table missing from the database"
            assert column in live, f"{entry}: column missing"


@pytest.mark.parametrize("key", SPORT_KEYS)
def test_declared_kinds_reach_discovery(key):
    """The same merge the page performs: discovery plus the sport's
    overrides, each declared field receiving its intended kind."""
    from sqlalchemy import create_engine

    sport = _sport_with_db(key)
    conn = type("C", (), {"engine": create_engine(
        "sqlite:///" + sport.db.replace("\\", "/"))})()
    schema = QB.discover_schema(
        conn, sport.db, ("metadata-test", 0),
        tuple(sorted(sport.query_column_kinds.items())))
    for entry, kind in sport.query_column_kinds.items():
        table, _, column = entry.partition(".")
        by_name = {c.name: c for c in schema[table]}
        assert by_name[column].kind == kind, entry


@pytest.mark.parametrize("key", SPORT_KEYS)
def test_declared_booleans_store_zero_or_one(key):
    sport = _sport_with_db(key)
    with _ro(sport) as con:
        for entry, kind in sport.query_column_kinds.items():
            if kind != "boolean":
                continue
            table, _, column = entry.partition(".")
            stored = {row[0] for row in con.execute(
                f'SELECT DISTINCT "{column}" FROM "{table}" '
                f'WHERE "{column}" IS NOT NULL')}
            assert stored <= {0, 1}, \
                f"{entry} declared boolean but stores {sorted(stored)[:5]}"


@pytest.mark.parametrize("key", SPORT_KEYS)
def test_declared_dates_store_iso_text(key):
    """A date/datetime declaration promises the ISO text the compiler's
    string comparisons and pickers assume. The MLB rivalry table's
    compact YYYYMMDD game_date is exactly what this catches if someone
    declares it without normalising the loader first."""
    sport = _sport_with_db(key)
    with _ro(sport) as con:
        for entry, kind in sport.query_column_kinds.items():
            if kind not in ("date", "datetime"):
                continue
            table, _, column = entry.partition(".")
            samples = [row[0] for row in con.execute(
                f'SELECT DISTINCT "{column}" FROM "{table}" '
                f'WHERE "{column}" IS NOT NULL LIMIT 50')]
            for value in samples:
                text = str(value)
                try:
                    if kind == "date":
                        dt.date.fromisoformat(text[:10])
                    else:
                        dt.datetime.fromisoformat(
                            text.replace("Z", "+00:00"))
                except ValueError:
                    pytest.fail(
                        f"{entry} declared {kind} but stores {text!r}")


def test_declared_kind_predicates_compile_with_the_bare_column():
    """Representative predicates for the newly declared kinds: the raw
    column stays on the left (no DATE() wrapper, no arithmetic on a
    flag), values bound."""
    bag = QB.ParamBag()
    clause = QB.compile_condition(
        {"column": "scheduled_datetime", "kind": "datetime", "op": "after",
         "value": "2020-01-01", "day_ceiling": True},
        {"scheduled_datetime"}, bag)
    assert clause.startswith('"scheduled_datetime"')
    assert "DATE(" not in clause
    assert bag.values == {"p0": "2020-01-02"}   # strict after, half-open

    bag = QB.ParamBag()
    clause = QB.compile_condition(
        {"column": "is_captain", "kind": "boolean", "op": "is true"},
        {"is_captain"}, bag)
    assert clause == '"is_captain" = :p0'
    assert bag.values == {"p0": 1}


# ------------------------------------------------- tree profiling budget

class _Cols:
    def __init__(self, *pairs):
        self.cols = [QB.Column(name, "TEXT" if kind == "text" else "INT",
                               kind) for name, kind in pairs]


def test_tree_profiles_only_declared_low_cardinality_columns():
    sport = type("S", (), {"query_low_cardinality_columns": (
        "games.club_now", "games.result", "games.goals")})()
    cols = _Cols(("club_now", "text"), ("result", "text"),
                 ("player", "text"), ("date", "text"),
                 ("goals", "integer")).cols
    picked = QB._tree_profile_columns(sport, "games", cols)
    # player and date are text but undeclared; goals is declared but not
    # text, and a kind gate beats a config typo.
    assert {c.name for c in picked} == {"club_now", "result"}
    # Another table's identically named columns are not covered.
    assert QB._tree_profile_columns(sport, "matches", cols) == []


def test_a_sport_declaring_nothing_profiles_nothing_in_tree_mode():
    sport = type("S", (), {})()
    cols = _Cols(("player", "text"), ("club", "text")).cols
    assert QB._tree_profile_columns(sport, "games", cols) == []


def test_unprofiled_text_columns_stay_free_text_fields():
    cols = _Cols(("player", "text"), ("club_now", "text")).cols
    config = QB.condition_tree_config(
        cols, {"club_now": {"kind": "text", "values": ["A", "B"]}})
    assert config["fields"]["player"]["type"] == "text"
    assert config["fields"]["club_now"]["type"] == "select"


@pytest.mark.parametrize("key", SPORT_KEYS)
def test_low_cardinality_entries_are_live_text_columns(key):
    sport = _sport_with_db(key)
    allowed = QB._allowed_tables(sport)
    declared = getattr(sport, "query_low_cardinality_columns", ())
    assert declared, f"{key} declares no tree select columns"
    overrides = sport.query_column_kinds
    with _ro(sport) as con:
        for entry in declared:
            table, _, column = entry.partition(".")
            assert table in allowed, entry
            live = _live_columns(con, table)
            assert column in live, f"{entry}: column missing"
            kind = overrides.get(entry, QB.type_kind(live[column]))
            assert kind == "text", \
                f"{entry} would profile a {kind} column for a select"
