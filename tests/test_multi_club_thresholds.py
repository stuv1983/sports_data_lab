#!/usr/bin/env python3
"""'N+ GAMES TWO DIFF CLUBS' and 'N+ GOALS TWO DIFF CLUBS' are not the same
square.

Gridley #1112 asked "50+ GAMES TWO DIFF CLUBS". A board built by hand from
the axis dropdown was set to the goals builder instead -- one word away,
and the two questions overlap enough that the mistake survives a couple of
correct answers before it costs a guess. Les Allen (Carlton 29 games / 87
goals, North Melbourne 41 / 103) qualifies on goals and fails on games; the
real square has 16 answers, the goals square 14.

These tests pin both halves: the builders select different players, and the
parser routes each wording to the builder its words name.
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

import core
from afl import historic_grids as HG
from afl import parse_criteria as P


def fixture():
    """Careers shaped so the goals and games readings disagree."""
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE players (
          player_id INTEGER, player TEXT, debut_season INTEGER,
          final_season INTEGER, career_games INTEGER, career_goals INTEGER,
          finals_played INTEGER, clubs_hist TEXT, obscurity REAL,
          n_clubs INTEGER
        );
        CREATE TABLE games (
          player_id INTEGER, season INTEGER, club_now TEXT, club_hist TEXT,
          is_final INTEGER, goals REAL
        );
        INSERT INTO players VALUES
          -- The Les Allen shape: prolific at two clubs, long at neither.
          (1,'Allen',1930,1934,8,80,0,'Carlton|North Melbourne',50.0,2),
          -- The Mick Grace shape: qualifies on both readings.
          (2,'Grace',1897,1908,12,60,0,'Fitzroy|Carlton',50.0,2),
          -- Long at two clubs but barely scores: games only.
          (3,'Blocker',1980,1990,12,4,0,'Essendon|Geelong',50.0,2),
          -- Everything at one club: neither reading.
          (4,'Loyal',2000,2010,12,80,0,'Hawthorn',50.0,1);
    """)
    rows = []
    # Allen: 4 games at each club, 10 goals a game.
    rows += [(1, 1930, "Carlton", "Carlton", 0, 10) for _ in range(4)]
    rows += [(1, 1932, "North Melbourne", "North Melbourne", 0, 10)
             for _ in range(4)]
    # Grace: 6 games at each club, 5 goals a game.
    rows += [(2, 1897, "Fitzroy", "Fitzroy", 0, 5) for _ in range(6)]
    rows += [(2, 1903, "Carlton", "Carlton", 0, 5) for _ in range(6)]
    # Blocker: 6 games at each club, almost no goals.
    rows += [(3, 1980, "Essendon", "Essendon", 0, 0) for _ in range(6)]
    rows += [(3, 1985, "Geelong", "Geelong", 0, 0) for _ in range(5)]
    rows += [(3, 1985, "Geelong", "Geelong", 0, 4)]
    # Loyal: 12 games and 80 goals, all at one club.
    rows += [(4, 2000, "Hawthorn", "Hawthorn", 0, 8) for _ in range(10)]
    rows += [(4, 2005, "Hawthorn", "Hawthorn", 0, 0) for _ in range(2)]
    con.executemany("INSERT INTO games VALUES (?,?,?,?,?,?)", rows)
    return con


def G():
    return core.Generic(core.Schema(
        stats=["goals"], required_games_cols=(), required_player_cols=()))


def ids(con, built):
    sql, params = built
    return {r[0] for r in con.execute(sql, params)}


# ------------------------------------------------- the builders disagree

def test_goals_and_games_at_two_clubs_select_different_players():
    con, g = fixture(), G()
    # Allen kicked 40 at each club but managed only 4 games at each;
    # Blocker is the exact inverse. Grace clears both floors.
    assert ids(con, g.score_at_multiple_clubs(30, 2)) == {1, 2}
    assert ids(con, g.score_at_multiple_clubs(40, 2)) == {1}
    assert ids(con, g.games_at_multiple_clubs(5, 2)) == {2, 3}


def test_one_club_totals_never_qualify():
    con, g = fixture(), G()
    # Loyal has 80 goals and 12 games, all at Hawthorn. A player must clear
    # the threshold at each of two clubs, not across a career.
    assert 4 not in ids(con, g.score_at_multiple_clubs(40, 2))
    assert 4 not in ids(con, g.games_at_multiple_clubs(5, 2))


def test_threshold_applies_per_club_not_to_the_total():
    con, g = fixture(), G()
    # Allen's 8 career games are spread 4 and 4, so a 5-game floor at two
    # clubs excludes him even though 8 > 5.
    assert 1 not in ids(con, g.games_at_multiple_clubs(5, 2))
    assert 1 in ids(con, g.games_at_multiple_clubs(4, 2))


# --------------------------------------------------- the parser routes both

def test_parser_distinguishes_games_from_goals():
    games_sql, games_label = P.parse("50+ GAMES TWO DIFF CLUBS")
    goals_sql, goals_label = P.parse("50+ GOALS TWO DIFF CLUBS")
    assert games_label == "50+ games at 2 clubs"
    assert goals_label == "50+ goals at 2 clubs"
    assert games_sql[0] != goals_sql[0]


def test_parser_keeps_the_club_count():
    for text, expected in (
            ("30+ GOALS TWO DIFF CLUBS", "30+ goals at 2 clubs"),
            ("30+ GOALS THREE DIFF CLUBS", "30+ goals at 3 clubs"),
            ("100+ GAMES 3 DIFFERENT CLUBS", "100+ games at 3 clubs"),
    ):
        assert P.parse(text)[1] == expected


def test_captured_grid_1112_parses_to_the_games_question():
    grid = next(g for g in HG.GRIDS if g.number == 1112)
    report = HG.analyse(grid, check_squares=False)
    labels = [c.label for c in report.rows]
    assert "50+ games at 2 clubs" in labels
    assert "50+ goals at 2 clubs" not in labels


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
