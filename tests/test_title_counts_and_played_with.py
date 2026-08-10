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


# ------------------------------------------- teammate lookup, by name

def _named_games():
    """Two matches, so a teammate is a shared club on a shared day."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE players (player_id INTEGER, player TEXT)")
    con.execute("""CREATE TABLE games (
        player_id INTEGER, player TEXT, season INTEGER, date TEXT,
        club_hist TEXT, opponent TEXT)""")
    people = [(1, "Mason Wood"), (2, "Jack Smith"), (3, "Bob Smith"),
              (4, "Other Guy"), (5, "Away Player")]
    con.executemany("INSERT INTO players VALUES (?,?)", people)
    con.executemany(
        "INSERT INTO games VALUES (?,?,?,?,?,?)",
        [(1, "Mason Wood", 2015, "2015-04-01", "Alpha", "Beta"),
         (2, "Jack Smith", 2015, "2015-04-01", "Alpha", "Beta"),
         (5, "Away Player", 2015, "2015-04-01", "Beta", "Alpha"),
         (3, "Bob Smith", 2016, "2016-04-01", "Alpha", "Beta"),
         (4, "Other Guy", 2016, "2016-04-01", "Alpha", "Beta")])
    return con


def test_a_named_teammate_is_the_same_club_in_the_same_match():
    con = _named_games()
    generic = core.Generic(core.Schema())
    assert _ids(con, generic.teammate_of("Mason Wood")) == {2}
    assert 5 not in _ids(con, generic.teammate_of("Mason Wood"))  # opponent


def test_a_surname_unions_every_namesake_but_only_as_a_fallback():
    """Gridley labels sometimes carry the surname alone."""
    con = _named_games()
    generic = core.Generic(core.Schema())
    # No player is called plain "Smith", so both Smiths' matches count.
    assert _ids(con, generic.teammate_of("Smith")) == {1, 4}
    # An exact full name never falls back to the surname sweep.
    assert _ids(con, generic.teammate_of("Jack Smith")) == {1}


def test_the_teammate_name_is_resolved_over_players_not_over_every_game():
    """This is the whole cost of a teammate square.

    `LOWER(player) LIKE '% wood'` cannot use an index, so matching the name
    in `games` swept 694k AFL player-games -- about two and a half seconds
    per square, on a page asking for six criteria and nine intersections at
    once. Both tables carry the name; the name predicate belongs to the one
    with 13k rows, and the games side must join on the id.
    """
    sql, _params = core.Generic(core.Schema()).teammate_of("Mason Wood")
    before_the_player_lookup, _, _rest = sql.partition("FROM players")
    assert "LIKE" not in before_the_player_lookup
