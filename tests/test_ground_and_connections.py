"""Ground-grid and exact player-connection regressions."""

import sqlite3

import core
from afl import constraints


def _games_db():
    con = sqlite3.connect(":memory:")
    con.execute("""CREATE TABLE games (
        player_id INTEGER, player TEXT, season INTEGER, date TEXT,
        club_hist TEXT, club_now TEXT, opponent TEXT, venue TEXT,
        result TEXT, is_final INTEGER, goals INTEGER, behinds INTEGER,
        marks INTEGER, disposals INTEGER, kicks INTEGER, handballs INTEGER,
        tackles INTEGER, brownlow INTEGER
    )""")
    return con


def test_teammate_means_the_same_match_not_merely_same_club_season():
    con = _games_db()
    con.executemany(
        "INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "Target", 2020, "2020-04-01", "A", "A", "B", "MCG",
             "W", 0, 1, 0, 2, 3, 2, 1, 0, 0),
            (2, "Actual Mate", 2020, "2020-04-01", "A", "A", "B", "MCG",
             "W", 0, 0, 0, 1, 2, 1, 1, 0, 0),
            (3, "Same Season Only", 2020, "2020-05-01", "A", "A", "C", "MCG",
             "L", 0, 0, 0, 1, 2, 1, 1, 0, 0),
        ],
    )
    sql, params = core.Generic(core.Schema()).teammate_of_id(1)
    assert [row[0] for row in con.execute(sql, params)] == [2]


def test_ground_performance_combines_ground_status_metric_and_threshold():
    con = _games_db()
    con.executemany(
        "INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "One", 2020, "2020-04-01", "A", "A", "B", "M.C.G.",
             "W", 0, 2, 1, 6, 10, 5, 5, 2, 1),
            (1, "One", 2020, "2020-05-01", "A", "A", "C", "M.C.G.",
             "L", 0, 1, 0, 20, 30, 15, 15, 3, 0),
            (2, "Two", 2020, "2020-04-01", "A", "A", "B", "M.C.G.",
             "W", 0, 0, 0, 4, 5, 3, 2, 1, 0),
        ],
    )
    sql, params = constraints.ground_performance("MCG", "wins", "marks", 5)
    assert [row[0] for row in con.execute(sql, params)] == [1]
    sql, params = constraints.ground_performance("MCG", "all", "score", 18)
    assert [row[0] for row in con.execute(sql, params)] == [1]


def test_ground_performance_builder_is_available_to_the_grid():
    fn, args = constraints.BUILDERS["Ground performance"]
    assert fn is constraints.ground_performance
    assert args == ["venue", "ground_status", "ground_metric", "x"]
