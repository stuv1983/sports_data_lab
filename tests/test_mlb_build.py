#!/usr/bin/env python3
"""What mlb/build_mlb_db.py must produce, and what MLB must never claim.

The bug this suite exists for: the first MLB build wrote Lahman's own
column names straight through, so `players` had `playerID` where every
page in the repository reads `player_id`. Nothing caught it until the app
opened the Player Search page and SQLite said "no such column: player_id",
because no test had ever opened an MLB database.

The second half is about honesty rather than plumbing. Lahman's finest
grain is a player's season with one team, so a row of `games` is a season.
constraints_mlb.py must therefore *not* offer the per-game squares, and
these tests fail if someone adds one back.
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

import core
import obscurity
import sports
from mlb.obscurity_model import MODEL as MLB_MODEL
from mlb import build_mlb_db, constraints_mlb


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    """A built MLB database from the synthetic Lahman fixture.

    Written under a temporary root so it can never overwrite data/mlb/, and
    in particular so a four-player fixture never replaces the reference
    file sports.py reads at import.
    """
    import mlb_fixture

    root = tmp_path_factory.mktemp("mlb")
    mlb_fixture.write(root / "csv")
    path = root / "mlb.db"
    # retrosheet=False keeps the suite offline. The rivalry step downloads
    # 34MB of game logs and has nothing to say about a four-player fixture;
    # write() still declares the empty table, which is what the builders
    # below are checked against.
    build_mlb_db.build(db=path, raw=root / "csv", verbose=False,
                       retrosheet=False)
    return path


@pytest.fixture(scope="module")
def con(db):
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    yield connection
    connection.close()


def one(con, sql, *params):
    return con.execute(sql, params).fetchone()


# ------------------------------------------------------------- the schema

def test_the_database_satisfies_the_schema_the_app_requires(con):
    """The whole point: core.require_schema is what app.py runs at startup."""
    core.require_schema(con, sports.MLB_SCHEMA)


def test_players_is_keyed_by_player_id_not_lahmans_playerID(con):
    columns = {r[1] for r in con.execute("PRAGMA table_info(players)")}
    assert "player_id" in columns
    assert "playerID" not in columns


def test_the_query_the_player_page_runs(con):
    """app.player_options composes exactly this. It used to throw."""
    s = sports.MLB_SCHEMA
    rows = con.execute(
        f"SELECT {s.player_id}, {s.player}, {s.debut_season}, "
        f"{s.final_season}, {s.career_games}, {s.clubs_hist} "
        f"FROM {s.players} ORDER BY {s.player}").fetchall()
    assert len(rows) == 4
    assert all(row[0] and row[1] for row in rows)


def test_every_stat_the_schema_names_is_a_column_of_games(con):
    columns = {r[1] for r in con.execute("PRAGMA table_info(games)")}
    assert set(sports.MLB_STATS) <= columns


# -------------------------------------------------------------- the data

def test_games_from_appearances_not_from_the_batting_line(con):
    """A relief pitcher's 60 games are 60 games; his 8 at-bats are not.

    Counting career_games from Batting.G was the old build's approach and
    it undercounts every pitcher in the file.
    """
    games, at_bats = one(
        con, "SELECT career_games, (SELECT SUM(at_bats) FROM games "
             "WHERE player_id='pitchr01' AND is_postseason=0) "
             "FROM players WHERE player_id='pitchr01'")
    assert games == 60
    assert at_bats == 8


def test_a_mid_season_trade_is_two_team_seasons(con):
    rows = con.execute(
        "SELECT club_now, games FROM games WHERE player_id='brookj01' "
        "AND season=1955 AND is_postseason=0 ORDER BY club_now").fetchall()
    assert rows == [("Los Angeles Dodgers", 100), ("New York Yankees", 40)]


def test_club_hist_is_the_season_name_and_club_now_the_franchise(con):
    hist, now = one(
        con, "SELECT club_hist, club_now FROM games "
             "WHERE player_id='dodgem01' AND season=1955 AND is_postseason=0")
    assert hist == "Brooklyn Dodgers"
    assert now == "Los Angeles Dodgers"


def test_the_lineage_is_one_directional(con):
    """Asking for the Dodgers includes Brooklyn; asking for Brooklyn does
    not include Los Angeles. core.Schema.club_identities defines this."""
    lineage = build_mlb_db.lineage(_teams_frame(), _franchises_frame())
    assert "Brooklyn Dodgers" in lineage["Los Angeles Dodgers"]
    assert "Brooklyn Dodgers" not in lineage
    assert lineage["Los Angeles Dodgers"][0] == "Los Angeles Dodgers"


def _teams_frame():
    import pandas as pd

    import mlb_fixture
    return pd.DataFrame(mlb_fixture.TEAMS, columns=[
        "yearID", "lgID", "teamID", "franchID", "name", "park"])


def _franchises_frame():
    import pandas as pd

    import mlb_fixture
    return pd.DataFrame(mlb_fixture.FRANCHISES,
                        columns=["franchID", "franchName", "active"])


def test_pitching_era_is_weighted_by_outs_not_averaged(con):
    era, = one(con, "SELECT era FROM games WHERE player_id='pitchr01' "
                    "AND season=1955 AND is_postseason=0")
    assert era == pytest.approx(2.70)


def test_career_game_no_numbers_seasons_so_debut_club_works(con):
    first, = one(con, "SELECT club_now FROM games WHERE player_id='brookj01' "
                      "AND career_game_no=1")
    assert first == "Los Angeles Dodgers"        # Brooklyn, 1955


# --------------------------------------------------------- the postseason

def test_a_world_series_row_carries_the_round_and_the_result(con):
    round_, result = one(
        con, "SELECT round, result FROM games WHERE player_id='dodgem01' "
             "AND is_postseason=1")
    assert round_ == "WS"
    assert result == "W"


def test_postseason_played_counts_postseason_games(con):
    played, = one(con, "SELECT postseason_played FROM players "
                       "WHERE player_id='dodgem01'")
    assert played == 7


def test_postseason_is_null_when_no_postseason_was_played_at_all(con):
    """1871 had no series. "Never reached October" is a different claim
    from "there was no October", and MLB_MODEL drops the term."""
    played, = one(con, "SELECT postseason_played FROM players "
                       "WHERE player_id='earlyt01'")
    assert played is None


def test_won_the_world_series_finds_the_winner_and_not_the_loser(con):
    sql, params = constraints_mlb.won_the_world_series()
    winners = {row[0] for row in con.execute(sql, params)}
    assert "dodgem01" in winners
    assert "brookj01" in winners            # also a Dodger in the 1955 WS


# ------------------------------------------------------------- obscurity

def test_obscurity_is_scored_and_recorded_against_the_mlb_model(con):
    score, model = one(con, "SELECT obscurity, obscurity_model FROM players "
                            "WHERE player_id='dodgem01'")
    assert 0 <= score <= 100
    assert model == MLB_MODEL.version


def test_the_component_columns_the_model_declares_are_all_written(con):
    columns = {r[1] for r in con.execute("PRAGMA table_info(players)")}
    for term in MLB_MODEL.terms:
        assert f"{term.name}_component" in columns


def test_a_career_with_no_postseason_at_all_carries_lower_confidence(con):
    early, = one(con, "SELECT obscurity_confidence FROM players "
                      "WHERE player_id='earlyt01'")
    full, = one(con, "SELECT obscurity_confidence FROM players "
                     "WHERE player_id='dodgem01'")
    assert early < full == pytest.approx(1.0)


# ---------------------------------------------------- honesty about grain

#: Squares core.Generic can build but this data cannot honestly answer.
#: A per-season total behind a per-game label is worse than a gap. These
#: ask about a single game, which a season row genuinely cannot answer.
PER_GAME_SQUARES = (
    "X+ of a stat in one game",
    "Two stats in the same game",
    "X+ games with Y+ of a stat",
    "Teammate of…",
)


@pytest.mark.parametrize("name", PER_GAME_SQUARES)
def test_mlb_does_not_offer_a_square_a_season_row_cannot_answer(name):
    assert name not in constraints_mlb.BUILDERS


def test_per_game_averages_divide_by_games_not_by_rows():
    """The averages are offered, but only because they use SUM(games).

    core.Generic's average builders divide by COUNT(*), which for MLB is a
    count of seasons -- that is the reason these were absent, and using the
    generic builder here would put a per-season rate under a per-game name.
    """
    for label in ("Career average of a stat", "Season average of a stat"):
        assert label in constraints_mlb.BUILDERS
        builder = constraints_mlb.BUILDERS[label][0]
        sql, _ = builder("home_runs", 0.2)
        assert "SUM(games)" in sql
        assert "COUNT(*)" not in sql
        assert "AVG(" not in sql


def test_a_per_game_average_is_a_rate_not_a_season_total(con):
    """0.2 home runs a game is routine; 0.2 a season is nobody."""
    builder = constraints_mlb.BUILDERS["Career average of a stat"][0]
    sql, params = builder("home_runs", 0.2, 1)
    rate = con.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
    total = con.execute(
        "SELECT COUNT(DISTINCT player_id) FROM games "
        "WHERE home_runs IS NOT NULL AND games > 0").fetchone()[0]
    assert 0 < rate <= total


def test_a_fixture_build_writes_its_reference_beside_itself(db):
    """Not over data/mlb/reference/mlb_reference.json.

    write_reference used to write to a fixed path, so running this suite
    replaced the real 30-franchise reference with the fixture's two -- and
    because sports.py freezes the franchise list into a frozen dataclass at
    import, the next test run then failed on a club that had silently
    stopped existing.
    """
    from mlb import mlb_reference

    beside = db.parent / "reference" / "mlb_reference.json"
    assert beside.exists()
    assert beside != mlb_reference.PATH
    assert "Boston Red Sox" in sports.MLB_SCHEMA.clubs


def test_the_status_panel_does_not_call_a_season_a_game():
    assert sports.MLB.games_row_label == "Player-seasons"


def test_every_builder_actually_runs(con):
    """A registry entry that throws is a square that breaks the board.

    Executed through core.count rather than by running the fragment
    directly. Since the Immaculate Grid pairing rule landed, a builder may
    return a *row-scoped predicate* ("@row:team@g.club_now IN (?)") that is
    only valid once core._where has wrapped it in an EXISTS -- running it
    raw fails with 'near "@row": syntax error', which says nothing about
    whether the square works.

    Builders gated on an optional layer are skipped when the layer is
    absent, exactly as app.py hides them, so this asserts what a solver
    could actually click.
    """
    arguments = {"club": "Los Angeles Dodgers", "games": 10, "goals": 1,
                 "clubs": 2, "stat": "home_runs", "x": 1, "from": 1950,
                 "to": 1960, "venue": "Ebbets Field",
                 "rivalry": "yankees_redsox", "award_axis": "Gold Glove",
                 "position": "Left Field", "average": 0.300,
                 "min_plate_appearances": 1, "stat_a": "home_runs", "x_a": 1,
                 "stat_b": "hits", "x_b": 1, "avg": 0.2, "min_games": 1,
                 "player_id": "dodgem01", "times": 1,
                 "state": "New York", "war": 1.0}
    gates = getattr(constraints_mlb, "LAYER_BUILDERS", {})
    for name, (builder, argnames) in constraints_mlb.BUILDERS.items():
        probe = gates.get(name)
        if probe and not getattr(constraints_mlb, probe)(con):
            continue
        built = builder(*[arguments[a] for a in argnames])
        core.count(con, [built], constraints_mlb.SCHEMA)


def test_a_team_square_pairs_with_a_season_stat_on_one_row(con):
    """The Immaculate Grid rule: 100 RBI must be *with that team*.

    Intersecting two independent player_id sets answered "played here at
    some point AND had such a season for anyone", which on the real
    database accepted 113 players for a square with 42 answers.
    """
    club = "Los Angeles Dodgers"
    paired = core.count(
        con, [constraints_mlb.played_for(club),
              constraints_mlb.season_stat_total_min("home_runs", 1)],
        constraints_mlb.SCHEMA)
    same_row = con.execute(
        "SELECT COUNT(DISTINCT player_id) FROM games "
        "WHERE club_now = ? AND home_runs >= 1", (club,)).fetchone()[0]
    assert paired == same_row

    # Two teams must NOT merge: one row cannot be two clubs, so merging
    # would empty every "played for both" square on the board.
    both = core.count(
        con, [constraints_mlb.played_for(club),
              constraints_mlb.played_for("Boston Red Sox")],
        constraints_mlb.SCHEMA)
    assert both == len(
        {r[0] for r in con.execute(
            "SELECT player_id FROM games WHERE club_now = ?", (club,))}
        & {r[0] for r in con.execute(
            "SELECT player_id FROM games WHERE club_now = ?",
            ("Boston Red Sox",))})


def test_team_constraints_use_stable_franchise_names_not_ambiguous_lineage():
    """A historic Yankees name must not turn modern Orioles into Yankees."""
    sql, params = constraints_mlb.played_for("New York Yankees")
    assert sql == "@row:team@g.club_now = ?"
    assert params == ["New York Yankees"]

    sql, params = constraints_mlb.debut_club("New York Yankees")
    assert "club_now = ?" in sql
    assert params == ["New York Yankees"]


def main():
    import subprocess
    return subprocess.call([_sys.executable, "-m", "pytest", __file__, "-q"])


if __name__ == "__main__":
    _sys.exit(main())
