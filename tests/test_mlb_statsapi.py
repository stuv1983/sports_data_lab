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
        if endpoint == "schedule":
            return self._splits.get("schedule", {"dates": []})
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

# ------------------------------------------------------------- schedules

def test_season_schedule_maps_home_and_away_rows(tmp_path):
    api = _FakeApi([ATHLETICS, BRAVES], {
        "schedule": {"dates": [{"games": [{
            "gamePk": 123, "gameDate": "2026-04-01T15:00:00Z",
            "status": {"statusCode": "F"}, "venue": {"name": "Truist Park"},
            "teams": {
                "away": {"team": {"id": 133}, "score": 2},
                "home": {"team": {"id": 144}, "score": 5}
            }
        }]}]}
    })
    matches = _source(api, {}, tmp_path).season_schedule(2026)
    assert len(matches) == 2
    
    away, home = matches
    assert away["source_game_key"] == "statsapi-123"
    assert away["source_club_id"] == "Oakland Athletics"
    assert away["team_position"] == "A"
    assert away["points_for"] == 2
    assert away["points_against"] == 5
    assert away["margin"] == 3
    assert away["result"] == "L"
    
    assert home["source_club_id"] == "Atlanta Braves"
    assert home["team_position"] == "H"
    assert home["result"] == "W"
    assert home["points_for"] == 5

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


# ------------------------------------------------ the season being played

def _date(text):
    import datetime as dt
    return dt.date.fromisoformat(text)


def _live_source(tmp_path, api, seasons, today="2026-08-08"):
    source = statsapi_source.StatsApiSource(
        cache=tmp_path,
        refresh=load_statsapi.live_seasons(seasons, _date(today)),
        verbose=False)
    source._api = lambda: api
    source._crosswalk = {}
    return source


def _one_batter(team_id=144):
    return {
        (team_id, "hitting"): [_split(1, "Some Batter", {"gamesPlayed": 10})],
        (team_id, "pitching"): [],
    }


def test_the_season_being_played_is_never_served_from_cache(tmp_path):
    """The whole point of this loader is currency.

    Responses are cached to disk, which is right for a season that has
    ended and fatal for the one in progress: a nightly job reading
    yesterday's file would insert yesterday's numbers and report success,
    leaving the database frozen on the day it first ran.
    """
    api = _FakeApi([BRAVES], _one_batter())
    source = _live_source(tmp_path, api, [2026])

    source.season_rows(2026)
    first = len(api.calls)
    source.season_rows(2026)

    assert first, "nothing was requested at all"
    assert len(api.calls) == 2 * first, "the second run reused the cache"


def test_a_settled_season_still_comes_off_disk(tmp_path):
    """The other half of it: 150 finished seasons cannot change, and
    re-requesting them nightly is the surest way to be rate-limited."""
    api = _FakeApi([BRAVES], _one_batter())
    source = _live_source(tmp_path, api, [2019])

    source.season_rows(2019)
    first = len(api.calls)
    source.season_rows(2019)

    assert len(api.calls) == first


@pytest.mark.parametrize("seasons,today,expected", [
    ([2026], "2026-08-08", {2026}),
    ([2025, 2026], "2026-08-08", {2026}),
    ([2024, 2025], "2026-08-08", set()),
    ([2025], "2026-01-15", {2025}),
])
def test_only_an_unfinished_season_counts_as_live(seasons, today, expected):
    assert load_statsapi.live_seasons(seasons, _date(today)) == expected


def test_naming_a_season_to_refresh_does_not_refetch_the_crosswalk(tmp_path):
    """The register is sixteen files and 65 MB, and it changes only when
    somebody debuts -- a live season is no reason to fetch it again."""
    source = statsapi_source.StatsApiSource(
        cache=tmp_path, refresh={2026}, verbose=False)
    asked = []

    original = statsapi_source.chadwick_crosswalk
    statsapi_source.chadwick_crosswalk = (
        lambda cache, refresh=False, verbose=True: asked.append(refresh) or {})
    try:
        source.crosswalk()
    finally:
        statsapi_source.chadwick_crosswalk = original

    assert asked == [False]


# ---------------------------------------------------- club_match_sources

CMS_COLUMNS = ("source_game_key", "source_club_id", "season", "round",
               "is_final", "match_date", "venue_raw", "team_position",
               "result", "points_for", "points_against", "margin",
               "attendance", "match_id", "match_status")


def _with_match_sources(path, rows=()):
    with closing(sqlite3.connect(path)) as con:
        # The real table keys on (source_game_key, source_club_id) with no
        # season in it, so a row the loader fails to clear is not a
        # harmless duplicate but an IntegrityError.
        con.execute(
            f"CREATE TABLE club_match_sources ({', '.join(CMS_COLUMNS)}, "
            "PRIMARY KEY (source_game_key, source_club_id))")
        con.executemany(
            f"INSERT INTO club_match_sources ({', '.join(CMS_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(CMS_COLUMNS))})", rows)
        con.commit()
    return path


def _match(key, club="Atlanta Braves", season=2026):
    return {"source_game_key": key, "source_club_id": club, "season": season,
            "round": None, "is_final": 1, "match_date": f"{season}-04-01",
            "venue_raw": "Truist Park", "team_position": "H", "result": "W",
            "points_for": 5, "points_against": 3, "margin": 2,
            "attendance": None, "match_id": None, "match_status": "unique"}


def test_october_survives_a_reload_of_the_regular_season(tmp_path):
    """The delete must not take rows the loader cannot put back.

    Only completed regular-season games come back from the schedule
    endpoint, so deleting the season outright would drop the postseason
    rows mlb/constraints_mlb.py reads for the World Series square.
    """
    path = _with_match_sources(_database(tmp_path), [
        ("statsapi-1", "Atlanta Braves", 2026, None, 1, "2026-04-01",
         "Truist Park", "H", "W", 5, 3, 2, None, None, "unique"),
        ("20261030-0-ATL-NYA", "Atlanta Braves", 2026, "WS", 1, "2026-10-30",
         "Truist Park", "H", "W", 4, 1, 3, None, None, "unique"),
    ])
    with closing(sqlite3.connect(path)) as con:
        load_statsapi.replace_match_sources(
            con, 2026, [_match("statsapi-1")], verbose=False)
        con.commit()
        kept = con.execute(
            "SELECT source_game_key FROM club_match_sources "
            "WHERE round='WS'").fetchall()
        total = con.execute(
            "SELECT COUNT(*) FROM club_match_sources").fetchone()[0]

    assert kept == [("20261030-0-ATL-NYA",)], "the postseason row was deleted"
    assert total == 2, "the regular-season row was added, not replaced"


def test_a_row_written_under_the_old_round_convention_is_still_replaced(
        tmp_path):
    """Ownership is the key prefix, not the round value.

    Rows loaded before the convention was settled read 'R'. Skipping them
    on a reload does not leave a stale duplicate -- (source_game_key,
    source_club_id) is the primary key, so the insert fails outright.
    """
    path = _with_match_sources(_database(tmp_path), [
        ("statsapi-1", "Atlanta Braves", 2026, "R", 1, "2026-04-01",
         "Truist Park", "H", "L", 1, 2, 1, None, None, "unique"),
    ])
    with closing(sqlite3.connect(path)) as con:
        written = load_statsapi.replace_match_sources(
            con, 2026, [_match("statsapi-1")], verbose=False)
        con.commit()
        rows = con.execute(
            "SELECT round, result, points_for FROM club_match_sources"
        ).fetchall()

    assert written == 1
    assert rows == [(None, "W", 5)], "the stale row was not superseded"


def test_a_season_a_fuller_source_already_covers_is_left_alone(tmp_path):
    """Retrosheet's rows carry attendance and real game keys. Overlaying
    a thinner second copy of the same fixtures would double every game."""
    path = _with_match_sources(_database(tmp_path), [
        ("20250401-0-ATL-NYN", "Atlanta Braves", 2025, None, 1, "2025-04-01",
         "Truist Park", "H", "W", 5, 3, 2, 41000, None, "unique"),
    ])
    with closing(sqlite3.connect(path)) as con:
        written = load_statsapi.replace_match_sources(
            con, 2025, [_match("statsapi-9", season=2025)], verbose=False)
        con.commit()
        rows = con.execute(
            "SELECT source_game_key, attendance FROM club_match_sources"
        ).fetchall()

    assert written == 0
    assert rows == [("20250401-0-ATL-NYN", 41000)]


def test_war_survives_a_season_being_replaced(tmp_path):
    """The Stats API does not serve WAR and the CSVs it came from are not
    read any more, so the value in the database is the only one there is.
    Replacing the season blindly would blank it for everyone in it."""
    path = _database(tmp_path)
    with closing(sqlite3.connect(path)) as con:
        con.execute("UPDATE games SET war=4.5 WHERE season=2025 "
                    "AND is_postseason=0")
        con.commit()
        fresh, = _rows(games=151, hits=41)
        fresh["season"] = 2025
        fresh["war"] = None
        outcome = load_statsapi.replace_season(con, 2025, [fresh])
        con.commit()
        war, games = con.execute(
            "SELECT war, games FROM games WHERE season=2025 "
            "AND is_postseason=0").fetchone()

    assert (war, games) == (4.5, 151), "WAR was lost when the row refreshed"
    assert outcome["carried"] == 1


def test_a_new_row_without_a_predecessor_simply_has_no_war(tmp_path):
    path = _database(tmp_path)
    with closing(sqlite3.connect(path)) as con:
        con.execute("UPDATE games SET war=4.5 WHERE season=2025")
        con.commit()
        outcome = load_statsapi.replace_season(con, 2026, _rows())
        con.commit()
        assert con.execute(
            "SELECT war FROM games WHERE season=2026").fetchone() == (None,)
    assert outcome["carried"] == 0


def test_a_database_without_the_table_is_not_an_error(tmp_path):
    with closing(sqlite3.connect(_database(tmp_path))) as con:
        assert load_statsapi.replace_match_sources(
            con, 2026, [_match("statsapi-1")], verbose=False) == 0


def _schedule(status="F"):
    return {"dates": [{"games": [{
        "gamePk": 77, "gameDate": "2026-04-01T17:00:00Z",
        "status": {"statusCode": status}, "venue": {"name": "Truist Park"},
        "teams": {"away": {"team": {"id": 133}, "score": 3},
                  "home": {"team": {"id": 144}, "score": 5}},
    }]}]}


def test_a_match_row_follows_the_convention_of_the_table_it_lands_in(tmp_path):
    """`games` writes 'R' for the regular season. club_match_sources does
    not -- the Retrosheet build left it NULL and used the column only for
    October, and a season loaded here has to sort alongside those."""
    api = _FakeApi([ATHLETICS, BRAVES], {"schedule": _schedule()})
    matches = _source(api, {}, tmp_path).season_schedule(2026)

    assert len(matches) == 2, "one row per club per game"
    assert {m["round"] for m in matches} == {None}
    assert {m["source_club_id"] for m in matches} == {
        "Oakland Athletics", "Atlanta Braves"}
    winner, = [m for m in matches if m["result"] == "W"]
    assert (winner["source_club_id"], winner["points_for"],
            winner["team_position"]) == ("Atlanta Braves", 5, "H")


def test_a_game_still_being_played_is_not_written_down(tmp_path):
    api = _FakeApi([ATHLETICS, BRAVES], {"schedule": _schedule("I")})
    assert _source(api, {}, tmp_path).season_schedule(2026) == []
