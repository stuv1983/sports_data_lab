import sqlite3

import core
from afl import constraints as afl
from mlb import constraints_mlb as mlb
from nba import constraints_nba as nba
from nfl import constraints_nfl as nfl


def _ids(con, constraint):
    sql, params = constraint
    return {row[0] for row in con.execute(sql, params)}


def _games():
    con = sqlite3.connect(":memory:")
    con.execute("""CREATE TABLE games (
        player_id INTEGER, season INTEGER, club_now TEXT,
        round TEXT, result TEXT
    )""")
    con.executemany(
        "INSERT INTO games VALUES (?,?,?,?,?)",
        [
            (1, 2020, "Alpha", "GF", "W"),
            (1, 2020, "Alpha", "GF", "W"),  # same title, two game rows
            (1, 2021, "Alpha", "GF", "W"),
            (2, 2020, "Alpha", "GF", "L"),
            (3, 2021, "Alpha", "R1", "W"),
            (4, 2020, "Beta", "GF", "L"),
        ],
    )
    return con


def test_title_counts_count_seasons_not_rows():
    con = _games()
    generic = core.Generic(core.Schema())
    assert _ids(con, generic.played_in_round_min("GF", 2)) == {1}
    assert _ids(con, generic.round_outcome_min("GF", "W", 2)) == {1}
    assert _ids(con, generic.round_outcome_min("GF", "L", 1)) == {2, 4}


def test_played_with_means_same_team_and_season_for_every_data_grain():
    con = _games()
    found = _ids(con, core.Generic(core.Schema()).played_with_id(2))
    assert found == {1}
    assert 4 not in found  # same season, different team
    assert 2 not in found  # never return the selected player


def test_every_sport_offers_played_with_and_title_counts():
    expected = {
        afl: ("Played in X+ Grand Finals", "Won X+ premierships",
              "Lost X+ Grand Finals"),
        nfl: ("Played in X+ Super Bowls", "Won X+ Super Bowls",
              "Lost X+ Super Bowls"),
        mlb: ("Played in X+ World Series", "Won X+ World Series",
              "Lost X+ World Series"),
        nba: ("Played in X+ NBA Finals", "Won X+ championships",
              "Lost X+ NBA Finals"),
    }
    for module, title_builders in expected.items():
        assert module.BUILDERS["Played with…"][1] == ["player_id"]
        for name in title_builders:
            assert module.BUILDERS[name][1] == ["times"]
