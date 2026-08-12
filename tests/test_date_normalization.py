#!/usr/bin/env python3
"""Stored dates mean what they say: ISO text, or NULL, never both spellings.

AFL dob columns mixed "30-Jan-1987" with ISO for years, and the MLB
rivalry table stored compact YYYYMMDD -- so no date declaration was
truthful and chronological operators compared garbage lexically. These
hold the canonical forms, the loaders' normalization, the one-off
migrations, and the property the whole exercise exists for: after
normalization, plain string comparison IS chronological comparison.
"""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---

import sqlite3

import pytest

import query_builder as QB
from utils.afl import normalize_dob
from utils.afl.dob import canonical_dob
from utils.mlb import normalize_rivalry_dates
from utils.mlb.load_retrosheet import iso_game_date


# ------------------------------------------------------------ AFL spelling

@pytest.mark.parametrize("raw, expected", [
    ("1987-01-30", "1987-01-30"),       # already ISO: unchanged
    ("30-Jan-1987", "1987-01-30"),      # AFL Tables spelling
    ("9-Jan-2004", "2004-01-09"),       # single-digit day, no zero pad
    ("1-Dec-1899", "1899-12-01"),
])
def test_canonical_dob_reads_both_stored_spellings(raw, expected):
    assert canonical_dob(raw) == expected


@pytest.mark.parametrize("raw", [
    None, "", "   ", "nan", "None", "NaT",       # pandas' stringified gaps
    "30-January-1987",                           # not a stored spelling
    "32-Jan-1987", "1987-13-30", "1987-02-30",   # impossible dates
    "circa 1900", "30/01/1987",
])
def test_canonical_dob_refuses_what_is_not_a_date(raw):
    """Never the input unparsed: text that cannot be read as a date must
    become NULL rather than survive masquerading as one."""
    assert canonical_dob(raw) is None


def test_afl_migration_normalizes_both_tables_and_reports_the_rest(tmp_path):
    path = tmp_path / "afl.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE players (player_id INTEGER, dob TEXT);
        CREATE TABLE games (player_id INTEGER, dob TEXT);
        INSERT INTO players VALUES
          (1, '30-Jan-1987'), (2, '1908-05-27'), (3, '9-Jan-2004'),
          (4, NULL), (5, 'not a date');
        INSERT INTO games VALUES
          (1, '30-Jan-1987'), (1, '30-Jan-1987'), (2, '1908-05-27');
    """)
    con.commit()
    con.close()

    assert normalize_dob.run(str(path)) == 0

    con = sqlite3.connect(path)
    assert con.execute(
        "SELECT dob FROM players ORDER BY player_id").fetchall() == [
        ("1987-01-30",), ("1908-05-27",), ("2004-01-09",), (None,), (None,)]
    # Every games row with the shared spelling converted, none skipped.
    assert con.execute(
        "SELECT DISTINCT dob FROM games ORDER BY dob").fetchall() == [
        ("1908-05-27",), ("1987-01-30",)]
    con.close()


def test_afl_migration_is_idempotent(tmp_path):
    path = tmp_path / "afl.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE players (dob TEXT);
        CREATE TABLE games (dob TEXT);
        INSERT INTO players VALUES ('30-Jan-1987');
    """)
    con.commit()
    con.close()
    assert normalize_dob.run(str(path)) == 0
    assert normalize_dob.run(str(path)) == 0
    con = sqlite3.connect(path)
    assert con.execute("SELECT dob FROM players").fetchall() == \
        [("1987-01-30",)]
    con.close()


# ------------------------------------------------------ MLB compact dates

@pytest.mark.parametrize("raw, expected", [
    ("19110426", "1911-04-26"),
    ("20240930", "2024-09-30"),
])
def test_iso_game_date_reads_retrosheet_compact(raw, expected):
    assert iso_game_date(raw) == expected


@pytest.mark.parametrize("raw", [
    None, "", "1911", "1911042",          # partial
    "191104260",                          # too long
    "19111333", "19110230",               # impossible calendar dates
    "1911-04-26",                         # already ISO is not compact
    "abcdefgh",
])
def test_iso_game_date_refuses_partial_and_impossible(raw):
    assert iso_game_date(raw) is None


def test_mlb_migration_converts_verifies_and_indexes(tmp_path):
    path = tmp_path / "mlb.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE mlb_player_rivalry_games (
            player_id TEXT, game_date TEXT, game_number TEXT,
            season INTEGER, rivalry_key TEXT, team_id TEXT,
            opponent_id TEXT, is_win INTEGER,
            PRIMARY KEY (player_id, game_date, game_number, rivalry_key));
        INSERT INTO mlb_player_rivalry_games VALUES
          ('ruthb101', '19270601', '0', 1927, 'yankees-red-sox',
           'New York Yankees', 'Boston Red Sox', 1),
          ('ruthb101', '19270602', '0', 1927, 'yankees-red-sox',
           'New York Yankees', 'Boston Red Sox', 0),
          ('gehrl101', '19271333', '0', 1927, 'yankees-red-sox',
           'New York Yankees', 'Boston Red Sox', 1);
    """)
    con.commit()
    con.close()

    assert normalize_rivalry_dates.run(str(path)) == 0

    con = sqlite3.connect(path)
    stored = con.execute(
        "SELECT game_date FROM mlb_player_rivalry_games "
        "ORDER BY game_date").fetchall()
    # The impossible 13th month was deleted and reported, not rewritten
    # into a fake date; the real dates converted.
    assert stored == [("1927-06-01",), ("1927-06-02",)]
    assert con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='ix_rivalry_date'").fetchone()
    con.close()

    # Idempotent.
    assert normalize_rivalry_dates.run(str(path)) == 0


# ---------------------------------------------- the point of the exercise

def test_normalized_dates_make_string_comparison_chronological(tmp_path):
    """The compact-vs-ISO trap in one query: before normalization,
    '19270601' < '1911-04-26' lexically, so a date filter lied. After it,
    the compiled predicate's plain string comparison is the timeline."""
    path = tmp_path / "afl.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE players (player TEXT, dob TEXT)")
    con.executemany("INSERT INTO players VALUES (?, ?)", [
        ("older", "30-Jan-1908"), ("mid", "15-Jun-1987"),
        ("younger", "9-Jan-2004"), ("unknown", None)])
    con.commit()
    con.close()

    assert normalize_dob.run(str(path)) == 0

    con = sqlite3.connect(path)
    bag = QB.ParamBag()
    clause = QB.compile_condition(
        {"column": "dob", "kind": "date", "op": "between",
         "lo": "1950-01-01", "hi": "1999-12-31"}, {"dob"}, bag)
    rows = con.execute(
        f"SELECT player FROM players WHERE {clause}",
        bag.values).fetchall()
    assert [r[0] for r in rows] == ["mid"]
    con.close()
