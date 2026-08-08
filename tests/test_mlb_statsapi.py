#!/usr/bin/env python3
"""The current MLB season, without reopening the Lahman export.

Lahman publishes a season only once it has finished, so from April to
November the MLB database sat a season behind and the update job could not
close the gap -- it re-ran a rebuild from CSVs that had nothing new in
them. The season in progress now comes from MLB's own Stats API and is
appended to the database that already exists.

The grain is what these tests mostly guard. `games` holds one row per
player per season per club, with `games` carrying the appearance count,
and `career_games` is a SUM over that column. The Stats API will happily
serve a row per game, and mixing those in would make every career total
count a season's games as a single season -- silently, and for everyone.
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
from contextlib import closing

import pytest

from mlb import statsapi_source
from utils.mlb import load_statsapi


# ------------------------------------------------------------ a fake API

def _split(player_id, name, stat):
    return {"player": {"id": player_id, "fullName": name}, "stat": stat}


class _FakeApi:
    """Only the three calls the source makes."""

    def __init__(self, teams, splits, people=None):
        self._teams = teams
        self._splits = splits
        self._people = people or {}
        self.calls = []

    def get(self, endpoint, params):
        self.calls.append((endpoint, params))
        if endpoint == "teams":
            return {"teams": self._teams}
        if endpoint == "stats":
            key = (params["teamId"], params["group"])
            return {"stats": [{"splits": self._splits.get(key, [])}]}
        if endpoint == "people":
            wanted = str(params["personIds"]).split(",")
            return {"people": [self._people[i] for i in wanted
                               if i in self._people]}
        raise AssertionError(f"unexpected endpoint {endpoint}")


def _source(api, crosswalk=None, tmp_path=None):
    source = statsapi_source.StatsApiSource(
        cache=tmp_path, refresh=True, verbose=False)
    source._api = lambda: api
    source._crosswalk = crosswalk if crosswalk is not None else {}
    return source


ATHLETICS = {"id": 133, "name": "Athletics", "venue": {"name": "Sutter Health"}}
BRAVES = {"id": 144, "name": "Atlanta Braves", "venue": {"name": "Truist Park"}}


# ----------------------------------------------------------------- grain

def test_a_season_row_carries_every_games_column(tmp_path):
    api = _FakeApi([BRAVES], {
        (144, "hitting"): [_split(1, "Some Batter", {
            "gamesPlayed": 100, "atBats": 380, "hits": 104, "homeRuns": 21,
            "runs": 55, "doubles": 20, "triples": 1, "rbi": 60,
            "stolenBases": 4, "baseOnBalls": 40, "strikeOuts": 90})],
        (144, "pitching"): [],
    })
    row, = _source(api, {"1": "batteso01"}, tmp_path).season_rows(2026)

    for column in statsapi_source.GAME_COLUMNS:
        assert column in row, column
    assert row["player_id"] == "batteso01"
    assert row["season"] == 2026
    assert row["games"] == 100
    assert row["hits"] == 104
    assert row["home_runs"] == 21
    assert row["club_now"] == "Atlanta Braves"
    assert row["venue"] == "Truist Park"


def test_the_row_is_a_season_not_a_game(tmp_path):
    """`games` is the appearance count the season is worth, and
    career_games is a SUM over it."""
    api = _FakeApi([BRAVES], {
        (144, "hitting"): [_split(1, "Some Batter", {"gamesPlayed": 100})],
        (144, "pitching"): [],
    })
    row, = _source(api, {}, tmp_path).season_rows(2026)
    assert row["games"] == 100
    assert row["date"] == "2026-04-01", "the season-grain placeholder date"
    assert row["round"] == "R"
    assert row["is_postseason"] == 0


def test_a_traded_player_keeps_one_row_per_club(tmp_path):
    """Asking without a team returns one collapsed row per player. Jonah
    Heim's 57 games for the Athletics and 12 for Atlanta are two rows in
    the table and have to stay two."""
    api = _FakeApi([ATHLETICS, BRAVES], {
        (133, "hitting"): [_split(7, "Jonah Heim", {"gamesPlayed": 57})],
        (144, "hitting"): [_split(7, "Jonah Heim", {"gamesPlayed": 12})],
        (133, "pitching"): [], (144, "pitching"): [],
    })
    rows = _source(api, {"7": "heimjo01"}, tmp_path).season_rows(2026)

    assert len(rows) == 2
    assert {(r["club_now"], r["games"]) for r in rows} == {
        ("Oakland Athletics", 57), ("Atlanta Braves", 12)}
    assert {r["player_id"] for r in rows} == {"heimjo01"}


def test_a_two_way_player_is_not_credited_with_two_seasons(tmp_path):
    """A hitting line and a pitching line for one club is one season."""
    api = _FakeApi([BRAVES], {
        (144, "hitting"): [_split(9, "Two Way", {"gamesPlayed": 120})],
        (144, "pitching"): [_split(9, "Two Way", {"gamesPlayed": 20,
                                                  "wins": 5, "era": "3.10"})],
    })
    row, = _source(api, {}, tmp_path).season_rows(2026)
    assert row["games"] == 120
    assert row["wins"] == 5
    assert row["era"] == 3.10


def test_pitching_allowed_does_not_land_in_the_batting_columns(tmp_path):
    """The pitching group carries hits, runs, strikeOuts and baseOnBalls
    too, but those are what the pitcher gave up. Adding them to a batting
    column invents a season at the plate that never happened."""
    api = _FakeApi([BRAVES], {
        (144, "hitting"): [],
        (144, "pitching"): [_split(5, "Some Pitcher", {
            "gamesPlayed": 30, "wins": 12, "losses": 4, "saves": 0,
            "era": "2.85", "hits": 150, "runs": 60, "strikeOuts": 200,
            "baseOnBalls": 45})],
    })
    row, = _source(api, {}, tmp_path).season_rows(2026)

    assert row["wins"] == 12
    assert row["era"] == 2.85
    assert row["hits"] is None, "150 hits allowed is not 150 hits made"
    assert row["strikeouts"] is None
    assert row["walks"] is None


def test_a_blank_statistic_is_null_and_never_zero(tmp_path):
    api = _FakeApi([BRAVES], {
        (144, "hitting"): [_split(1, "Some Batter",
                                  {"gamesPlayed": 5, "atBats": "", "hits": None})],
        (144, "pitching"): [],
    })
    row, = _source(api, {}, tmp_path).season_rows(2026)
    assert row["at_bats"] is None
    assert row["hits"] is None
    assert row["era"] is None


# ----------------------------------------------------------- identifiers

def test_a_known_player_resolves_to_the_database_key(tmp_path):
    api = _FakeApi([BRAVES], {
        (144, "hitting"): [_split(691023, "Jordan Walker", {"gamesPlayed": 9})],
        (144, "pitching"): [],
    })
    row, = _source(api, {"691023": "walkejo05"}, tmp_path).season_rows(2026)
    assert row["player_id"] == "walkejo05"


def test_an_unregistered_debut_is_minted_not_guessed(tmp_path):
    """Attaching their games to whichever existing player looked closest
    would be worse than admitting they are new."""
    api = _FakeApi([BRAVES], {
        (144, "hitting"): [_split(999999, "Brand New", {"gamesPlayed": 3})],
        (144, "pitching"): [],
    })
    row, = _source(api, {}, tmp_path).season_rows(2026).__iter__()
    assert row["player_id"] == "mlbam-999999"


# ------------------------------------------------------------ franchises

def test_mlbs_current_club_names_map_onto_the_ones_already_stored(tmp_path):
    """club_now came from Lahman's franchise table, which lags MLB's
    renames, and the schema's 30 club names are frozen at import.
    Emitting 'Cleveland Guardians' would not add a club, it would split a
    franchise in half."""
    import sports

    stored = set(sports.get("mlb").schema.clubs)
    for mlb_name, database_name in statsapi_source.FRANCHISE_ALIASES.items():
        assert mlb_name not in stored, mlb_name
        assert database_name in stored, database_name


def test_a_renamed_club_is_emitted_under_the_stored_name(tmp_path):
    api = _FakeApi([ATHLETICS], {
        (133, "hitting"): [_split(1, "Some Batter", {"gamesPlayed": 10})],
        (133, "pitching"): [],
    })
    row, = _source(api, {}, tmp_path).season_rows(2026)
    assert row["club_now"] == "Oakland Athletics"
    assert row["club_hist"] == "Oakland Athletics"


# ------------------------------------------------------- writing it down

def _database(tmp_path):
    """A database shaped like the real one, with one settled season."""
    path = tmp_path / "mlb.db"
    with closing(sqlite3.connect(path)) as con:
        con.execute(f"CREATE TABLE games ({', '.join(statsapi_source.GAME_COLUMNS)})")
        con.execute(
            "CREATE TABLE players (player_id TEXT PRIMARY KEY, player TEXT, "
            "name_key TEXT, birth_year INT, debut_season INT, "
            "final_season INT, career_games INT, career_hits INT, "
            "career_home_runs INT, postseason_played INT, clubs_hist TEXT, "
            "n_clubs INT, birth_country TEXT, birth_state TEXT)")
        con.execute(
            "INSERT INTO players (player_id, player) VALUES ('oldguy01', 'Old Guy')")
        con.execute(
            "INSERT INTO games (player_id, player, season, club_now, games, "
            "hits, home_runs, is_postseason) "
            "VALUES ('oldguy01', 'Old Guy', 2025, 'Atlanta Braves', 150, 40, 5, 0)")
        con.execute(
            "INSERT INTO games (player_id, player, season, club_now, games, "
            "hits, home_runs, is_postseason, round) "
            "VALUES ('oldguy01', 'Old Guy', 2025, 'Atlanta Braves', 6, 2, 1, 1, 'WS')")
        con.commit()
    return path


def _rows(games=10, hits=3, player="oldguy01"):
    base = {column: None for column in statsapi_source.GAME_COLUMNS}
    base.update({
        "player_id": player, "player": "Old Guy", "season": 2026,
        "date": "2026-04-01", "round": "R", "club_hist": "Atlanta Braves",
        "club_now": "Atlanta Braves", "games": games, "hits": hits,
        "home_runs": 1, "is_postseason": 0,
    })
    return [base]


def test_reloading_a_season_replaces_it_rather_than_doubling_it(tmp_path):
    """A mid-season re-run has to supersede yesterday's totals. Appending
    would give every player two 2026 rows and double their career games."""
    path = _database(tmp_path)
    with closing(sqlite3.connect(path)) as con:
        load_statsapi.replace_season(con, 2026, _rows(games=10))
        load_statsapi.replace_season(con, 2026, _rows(games=14))
        con.commit()
        rows = con.execute(
            "SELECT games FROM games WHERE season=2026").fetchall()
    assert rows == [(14,)]


def test_reloading_a_season_leaves_its_postseason_rows_alone(tmp_path):
    """This loader does not produce postseason rows and must not delete
    the ones an earlier Lahman build did."""
    path = _database(tmp_path)
    with closing(sqlite3.connect(path)) as con:
        load_statsapi.replace_season(con, 2025, _rows(games=1))
        con.commit()
        kept = con.execute(
            "SELECT round, games FROM games "
            "WHERE season=2025 AND is_postseason=1").fetchall()
    assert kept == [("WS", 6)]


def test_career_totals_are_recomputed_from_games_not_from_lahman(tmp_path):
    path = _database(tmp_path)
    with closing(sqlite3.connect(path)) as con:
        load_statsapi.replace_season(con, 2026, _rows(games=10, hits=3))
        load_statsapi.recompute_careers(con)
        con.commit()
        row = con.execute(
            "SELECT debut_season, final_season, career_games, career_hits, "
            "postseason_played, clubs_hist, n_clubs FROM players "
            "WHERE player_id='oldguy01'").fetchone()

    debut, final, career_games, career_hits, postseason, clubs, n_clubs = row
    assert (debut, final) == (2025, 2026)
    assert career_games == 160, "150 in 2025 plus 10 in 2026, postseason excluded"
    assert career_hits == 43
    assert postseason == 6
    assert clubs == "Atlanta Braves"
    assert n_clubs == 1


def test_career_game_no_numbers_a_players_seasons(tmp_path):
    path = _database(tmp_path)
    with closing(sqlite3.connect(path)) as con:
        load_statsapi.replace_season(con, 2026, _rows())
        load_statsapi.recompute_careers(con)
        con.commit()
        numbering = con.execute(
            "SELECT season, career_game_no FROM games "
            "WHERE is_postseason=0 ORDER BY season").fetchall()
    assert numbering == [(2025, 1), (2026, 2)]


def test_a_new_player_gets_a_row_before_their_games_need_one(tmp_path):
    path = _database(tmp_path)
    api = _FakeApi([], {}, people={"999999": {
        "id": 999999, "fullName": "Brand New", "birthDate": "2003-04-05",
        "birthCountry": "Canada", "birthStateProvince": "ON"}})
    source = _source(api, {}, tmp_path)
    rows = _rows(player="mlbam-999999")
    rows[0]["player"] = "Brand New"
    rows[0]["mlbam_id"] = "999999"

    with closing(sqlite3.connect(path)) as con:
        added = load_statsapi.add_missing_players(con, rows, source, verbose=False)
        con.commit()
        stored = con.execute(
            "SELECT player, name_key, birth_year, birth_country, birth_state "
            "FROM players WHERE player_id='mlbam-999999'").fetchone()

    assert added == 1
    assert stored == ("Brand New", "brand new", 2003, "Canada", "ON")


def test_a_player_without_a_biography_still_gets_their_games(tmp_path):
    path = _database(tmp_path)
    source = _source(_FakeApi([], {}, people={}), {}, tmp_path)
    rows = _rows(player="mlbam-424242")
    rows[0]["player"] = "No Biography"
    rows[0]["mlbam_id"] = "424242"

    with closing(sqlite3.connect(path)) as con:
        assert load_statsapi.add_missing_players(
            con, rows, source, verbose=False) == 1
        con.commit()
        stored = con.execute(
            "SELECT player, birth_year FROM players "
            "WHERE player_id='mlbam-424242'").fetchone()
    assert stored == ("No Biography", None)


def test_biographies_are_asked_for_in_batches(tmp_path):
    """One request per newcomer is hundreds of requests in the first
    season after an export."""
    people = {str(i): {"id": i, "fullName": f"P{i}", "birthDate": "2000-01-01"}
              for i in range(120)}
    api = _FakeApi([], {}, people=people)
    source = _source(api, {}, tmp_path)

    found = load_statsapi.biographies(source, list(people), verbose=False)

    assert len(found) == 120
    requests = [c for c in api.calls if c[0] == "people"]
    assert len(requests) == 3, f"120 ids in batches of 50, got {len(requests)}"


# -------------------------------------------------------------- plumbing

def test_the_update_plan_loads_mlb_instead_of_rebuilding_it():
    import database_updates as updates

    labels = [step.label for sport, step in updates.plan("regular", ["mlb"])]
    assert "Load the current MLB season from the Stats API" in labels
    assert not any("rebuild" in label.lower() for label in labels)


def test_the_loader_never_reads_a_lahman_csv():
    source = (_ROOT and open(
        _os.path.join(_ROOT, "utils", "mlb", "load_statsapi.py"),
        encoding="utf-8").read())
    for forbidden in ("People.csv", "Batting.csv", "find_source", "lahman"):
        assert forbidden not in source, forbidden


@pytest.mark.parametrize("today,expected", [
    ("2026-08-08", 2026),
    ("2026-04-01", 2026),
    ("2026-01-15", 2025),
])
def test_the_season_in_progress_rolls_over_in_march(today, expected):
    import datetime as dt

    assert load_statsapi.current_season(
        dt.date.fromisoformat(today)) == expected
