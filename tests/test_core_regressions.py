#!/usr/bin/env python3
"""Regression checks for square performance and SQL rendering."""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---


import sqlite3

import core


def test_matches_player_uses_the_same_compiled_constraints_as_solver():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE players (player_id INTEGER, player TEXT, obscurity REAL)")
    con.execute("CREATE TABLE games (player_id INTEGER, club_now TEXT, season INTEGER)")
    con.executemany("INSERT INTO players VALUES (?, ?, ?)",
                    [(1, "Right Player", 50.0), (2, "Wrong Era", 40.0)])
    con.executemany("INSERT INTO games VALUES (?, ?, ?)",
                    [(1, "A", 2020), (2, "A", 1990)])
    schema = core.Schema(required_games_cols=(), required_player_cols=())
    constraints = [
        ("SELECT player_id FROM games WHERE club_now=?", ["A"]),
        ("SELECT player_id FROM games WHERE season>=?", [2000]),
    ]

    assert core.matches_player(con, 1, constraints, schema)
    assert not core.matches_player(con, 2, constraints, schema)


def run():
    con = sqlite3.connect(":memory:")
    con.execute("""
        CREATE TABLE players (
          player_id INTEGER, player TEXT, debut_season INTEGER,
          final_season INTEGER, career_games INTEGER, career_goals INTEGER,
          finals_played INTEGER, birth_year INTEGER, n_clubs INTEGER,
          clubs_hist TEXT, obscurity REAL, name_key TEXT
        )
    """)
    con.executemany(
        "INSERT INTO players VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "Rare One", 1900, 1901, 2, 0, 0, 1880, 1, "A", 99.0, "rare one"),
            (2, "Known Two", 2000, 2010, 200, 50, 10, 1980, 1, "A", 10.0, "known two"),
        ],
    )
    schema = core.Schema(required_games_cols=(), required_player_cols=())
    selects = []
    con.set_trace_callback(lambda sql: selects.append(sql) if sql.lstrip().upper().startswith("SELECT") else None)
    square = core.square(
        con, [("SELECT player_id FROM players WHERE career_games >= ?", [1])], schema
    )
    con.set_trace_callback(None)
    assert len(selects) == 1, selects
    assert square.eligible == 2
    assert square.best_name == "Rare One"
    assert square.obscurity == 99.0

    rendered = core.to_standalone_sql(
        [("SELECT player_id FROM games WHERE venue = ?", ["O'Brien Oval"])],
        schema,
    )
    assert "O''Brien Oval" in rendered
    print("core regression tests: passed")


if __name__ == "__main__":
    run()


# ------------------------------------------------- the team+stat row rule
#
# core.ROW_MARKER exists to fix a specific false positive: intersecting
# "played for Cleveland" and "100+ RBI in a season" as two independent
# player_id subqueries accepts anyone who ever did each, in any season --
# Bobby Bonds drove in 102 for the Giants in 1971 and played for Cleveland
# in 1979, and an unmerged intersection wrongly took him. No test exercised
# this engine at all before now.

def _bonds_db():
    con = sqlite3.connect(":memory:")
    con.execute("""
        CREATE TABLE players (player_id TEXT, player TEXT, obscurity REAL)
    """)
    con.execute("""
        CREATE TABLE games (player_id TEXT, club_now TEXT, season INTEGER,
                            rbis INTEGER)
    """)
    con.executemany("INSERT INTO players VALUES (?, ?, ?)", [
        ("bonds", "Bobby Bonds", 50.0),
        ("onecard", "Same Season Slugger", 50.0),
    ])
    con.executemany(
        "INSERT INTO games (player_id, club_now, season, rbis) VALUES "
        "(?, ?, ?, ?)", [
        # Bobby Bonds: 102 RBI for the Giants in 1971, Cleveland in 1979 --
        # the team and the RBI total are never true in the same row.
        ("bonds", "Giants", 1971, 102),
        ("bonds", "Cleveland", 1979, 85),
        # A player for whom both really did happen in the same season/row.
        ("onecard", "Cleveland", 1971, 102),
    ])
    schema = core.Schema(required_games_cols=(), required_player_cols=())
    return con, schema


def test_team_and_season_stat_axes_require_the_same_row():
    con, schema = _bonds_db()
    cleveland = (f"{core.ROW_MARKER}team@club_now = ?", ["Cleveland"])
    rbi_100 = (f"{core.ROW_MARKER}stat@rbis >= ?", [100])

    assert not core.matches_player(con, "bonds", [cleveland, rbi_100], schema), (
        "Cleveland and the 100-RBI season happened four years apart for "
        "Bobby Bonds -- merging them independently readmits the bug "
        "ROW_MARKER exists to fix")
    assert core.matches_player(con, "onecard", [cleveland, rbi_100], schema), (
        "a player whose 100-RBI season really was with Cleveland must "
        "still match")


def test_two_team_axes_are_never_merged_into_one_row():
    """"Played for both" is two different rows; forcing them into one
    row_exists would make every such square empty by construction."""
    con, schema = _bonds_db()
    giants = (f"{core.ROW_MARKER}team@club_now = ?", ["Giants"])
    cleveland = (f"{core.ROW_MARKER}team@club_now = ?", ["Cleveland"])

    assert core.matches_player(con, "bonds", [giants, cleveland], schema)
