#!/usr/bin/env python3
"""Regression tests for the safe Advanced Search compiler."""

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

import core
import query_filters as Q


def fixture():
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE players (
          player_id INTEGER, player TEXT, debut_season INTEGER,
          final_season INTEGER, career_games INTEGER, career_goals INTEGER,
          finals_played INTEGER, clubs_hist TEXT, obscurity REAL
        );
        CREATE TABLE games (
          player_id INTEGER, player TEXT, season INTEGER, round TEXT,
          opponent TEXT, club_now TEXT, club_hist TEXT, venue TEXT,
          career_game_no INTEGER, goals REAL, disposals REAL,
          is_final INTEGER, result TEXT
        );
        CREATE TABLE captaincies (
          player_id INTEGER, season INTEGER, club TEXT, match_status TEXT
        );
        INSERT INTO players VALUES
          (1,'Alpha One',1990,2000,200,300,10,'A|B',80),
          (2,'Beta Two',2001,2005,50,20,0,'B',40);
        INSERT INTO games VALUES
          (1,'Alpha One',1995,'1','B','A','A','MCG',1,3,31,0,'W'),
          (1,'Alpha One',1996,'2','C','B','B','MCG',2,2,20,1,'L'),
          (2,'Beta Two',2002,'1','A','B','B','SCG',1,1,10,0,'W');
        INSERT INTO captaincies VALUES (1,1996,'B','unique');
    """)
    schema = core.Schema(
        stats=("goals", "disposals"), clubs=("A", "B"),
        required_games_cols=(), required_player_cols=(),
    )
    return con, schema


def run():
    con, schema = fixture()
    sql, params, _ = Q.compile_query(
        schema,
        'club:A club:B game.disposals>=30 game.goals>=3 sort:obscurity',
        con=con,
    )
    rows = con.execute(sql, params).fetchall()
    assert [row[0] for row in rows] == ["Alpha One"]

    sql, params, _ = Q.compile_query(
        schema, 'captain:true captain_club:B captain_year:1995..1997', con=con
    )
    assert con.execute(sql, params).fetchone()[0] == "Alpha One"

    query = Q.query_from_params({
        "club": ["A", "B"], "games_min": ["100"],
        "game_disposals_min": ["30"],
    })
    assert query.count("club:") == 2
    assert "games>=100" in query
    assert "game.disposals>=30" in query

    try:
        Q.compile_query(schema, "game.hacks>=1", con=con)
    except Q.QuerySyntaxError:
        pass
    else:
        raise AssertionError("unknown stat should be rejected")
    print("query filter tests: passed")


@pytest.mark.parametrize("query", ["limit:1.5", "limit:nan", "games>=inf"])
def test_non_integral_or_non_finite_numbers_are_rejected(query):
    con, schema = fixture()
    with pytest.raises(Q.QuerySyntaxError):
        Q.compile_query(schema, query, con=con)


if __name__ == "__main__":
    run()
