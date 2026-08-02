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
