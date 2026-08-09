#!/usr/bin/env python3
"""
The Basketball-Reference scrape as a source: shape, and the traps in it.

Three things this file exists to hold the line on.

`game_date` in the scrape's games.csv currently repeats the game key, so
every date in the database would be unparseable if the column were trusted.
The key's first eight digits are the date and that is what is read.

A box score that has not been scraped yet is not an error. The index lists
every game the moment the season page is read; the JSON arrives over the
following hours. A build mid-scrape has to produce a fixture with no
player rows, not a crash.

And the 1946 box score has no steals column. Those cells must arrive as
None -- a 0 there is a claim about the players rather than about the
records, and it ranks the entire early league as maximally obscure.

`test_the_real_sample_loads` runs against data/nba/sample, so the shape
being tested is the scraper's actual output and not a fixture that agrees
with a stale reading of it.
"""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---


import csv
import json
import math
from pathlib import Path

import pytest

from nba import nba_source
from nba import nba_source_bbr

SAMPLE = Path("data/nba/sample")

GAME_COLUMNS = ["bbr_game_key", "league", "season", "season_label",
                "bbr_season_year", "phase", "game_date", "game_time",
                "visitor_team_name", "visitor_team_key", "visitor_points",
                "home_team_name", "home_team_key", "home_points", "overtime",
                "attendance", "arena", "boxscore_url", "game_path",
                "source_url"]

PLAYER_COLUMNS = ["bbr_player_key", "player_name", "player_url",
                  "surname_letter", "career_from", "career_to", "position",
                  "height_text", "weight_lb", "birth_date", "college",
                  "is_active", "is_hall_of_fame", "profile_path",
                  "image_path", "source_url"]


def _game_row(key, season, label, phase, home, home_name, away, away_name,
              **over):
    row = dict.fromkeys(GAME_COLUMNS, "")
    row.update({
        "bbr_game_key": key, "league": "NBA", "season": season,
        "season_label": label, "bbr_season_year": season + 1, "phase": phase,
        # The scraper's bug, reproduced exactly: the key, not a date.
        "game_date": key,
        "home_team_key": home, "home_team_name": home_name,
        "visitor_team_key": away, "visitor_team_name": away_name,
        "home_points": 100, "visitor_points": 98, "arena": "Test Arena",
        "attendance": 15000,
        "game_path": f"seasons/{label}/{phase}/{key}.json"})
    row.update(over)
    return row


def _player_row(key, name, **over):
    row = dict.fromkeys(PLAYER_COLUMNS, "")
    row.update({"bbr_player_key": key, "player_name": name,
                "career_from": 1946, "career_to": 1950, "position": "G",
                "height_text": "75.0", "weight_lb": 180,
                "birth_date": "19200304"})
    row.update(over)
    return row


def _line(key, name, team, **stats):
    """One box-score player line. Absent keys stay absent, as 1946's do."""
    line = {"player_key": key, "player_name": name, "team_key": team,
            "starter": 1, "minutes": "30:30", "fg": 5, "fga": 10, "ft": 2,
            "fta": 2, "pf": 3, "pts": 12, "ast": 4, "trb": 6}
    line.update(stats)
    return line


def _write_csv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def root(tmp_path):
    """A scrape root: two seasons, one game each, one box score missing."""
    base = tmp_path / "scrape"
    _write_csv(base / "games.csv", GAME_COLUMNS, [
        _game_row("194611010TRH", 1946, "1946-47", "regular",
                  "TRH", "Toronto Huskies", "NYK", "New York Knicks"),
        _game_row("201610250CLE", 2016, "2016-17", "regular",
                  "CLE", "Cleveland Cavaliers", "NYK", "New York Knicks"),
        _game_row("201704150CLE", 2016, "2016-17", "playoff",
                  "CLE", "Cleveland Cavaliers", "IND", "Indiana Pacers"),
        # An ABA game, which the default league filter drops.
        _game_row("197204010NYA", 1971, "1971-72", "regular",
                  "NYA", "New York Nets", "IND", "Indiana Pacers",
                  league="ABA"),
    ])
    _write_csv(base / "players.csv", PLAYER_COLUMNS, [
        _player_row("earlyra01", "Ray Early"),
        _player_row("jamesle01", "LeBron James", height_text="81.0",
                    weight_lb=250, birth_date="19841230", position="F"),
    ])

    def box(key, label, phase, payload):
        path = base / "seasons" / label / phase / f"{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    box("194611010TRH", "1946-47", "regular", {
        "bbr_game_key": "194611010TRH", "league": "BAA", "season": 1946,
        "source_sha256": "a" * 64,
        # No stl/blk/tov/orb/drb/three_p: they were not recorded in 1946.
        "players": [_line("earlyra01", "Ray Early", "TRH", pts=9)]})
    box("201610250CLE", "2016-17", "regular", {
        "bbr_game_key": "201610250CLE", "league": "NBA", "season": 2016,
        "source_sha256": "b" * 64,
        "players": [_line("jamesle01", "LeBron James", "CLE", pts=19,
                          stl=1, blk=0, tov=3, orb=1, drb=10,
                          three_p=1, three_pa=3)]})
    # 201704150CLE is deliberately absent: indexed, not yet scraped.
    return base


@pytest.fixture
def source(root):
    return nba_source_bbr.BbrNbaSource(root, verbose=False)


# ------------------------------------------------------------- the shape

def test_it_satisfies_the_source_contract(source):
    assert source.key == "bbr"
    assert list(source.teams().columns) == list(nba_source.TEAM_COLUMNS)
    assert list(source.players().columns) == list(nba_source.PLAYER_COLUMNS)
    assert list(source.matches(2016).columns) == list(nba_source.MATCH_COLUMNS)
    games = source.player_games(2016, "regular")
    assert list(games.columns) == list(nba_source.PLAYER_GAME_COLUMNS)


def test_get_source_reaches_it_by_name(root):
    found = nba_source.get_source("bbr", root=root, verbose=False)
    assert isinstance(found, nba_source_bbr.BbrNbaSource)


# -------------------------------------------------------------- the date

def test_the_date_is_read_from_the_game_key_not_the_broken_column(source):
    """games.csv repeats the key in game_date. The key carries the date."""
    row = source.matches(1946).iloc[0]
    assert row["date"] == "1946-11-01"


def test_a_real_date_column_is_used_when_the_scraper_starts_writing_one():
    assert nba_source_bbr.game_date("201610250CLE", "2016-10-25") == "2016-10-25"
    assert nba_source_bbr.game_date("201610250CLE", "") == "2016-10-25"
    assert nba_source_bbr.game_date("nonsense", "") is None


# ---------------------------------------------------- null is not zero

def test_a_1946_box_score_has_null_steals_not_zero_steals(source):
    row = source.player_games(1946, "regular").iloc[0]
    for column in ("steals", "blocks", "turnovers", "oreb", "dreb",
                   "fg3m", "fg3a"):
        assert row[column] is None or math.isnan(row[column]), column
    assert row["points"] == 9


def test_plus_minus_is_null_because_the_box_score_does_not_carry_it(source):
    row = source.player_games(2016, "regular").iloc[0]
    assert row["plus_minus"] is None or math.isnan(row["plus_minus"])
    assert row["steals"] == 1
    # A recorded zero is a zero. Only an absent field is None.
    assert row["blocks"] == 0


def test_minutes_are_parsed_from_the_clock(source):
    row = source.player_games(2016, "regular").iloc[0]
    assert row["minutes"] == pytest.approx(30.5)


# ------------------------------------------------- an unfinished scrape

def test_an_unscraped_box_score_is_not_an_error(source):
    """The playoff game is indexed and has no JSON. It builds as a fixture."""
    assert source.player_games(2016, "playoff") is None
    ids = list(source.matches(2016)["match_id"])
    assert "201704150CLE" in ids


def test_complete_only_holds_back_games_with_no_box_score(root):
    source = nba_source_bbr.BbrNbaSource(root, complete_only=True,
                                         verbose=False)
    ids = list(source.matches(2016)["match_id"])
    assert ids == ["201610250CLE"]


def test_coverage_reports_how_far_the_scrape_has_got(source):
    found = source.coverage()
    assert found["games"] == 3            # the ABA game is filtered out
    assert found["boxscores"] == 2
    assert found["missing"] == 1
    assert found["seasons"] == [1946, 2016]


def test_a_half_written_box_score_is_skipped_not_half_parsed(root):
    path = root / "seasons" / "2016-17" / "regular" / "201610250CLE.json"
    path.write_text('{"players": [{"player_key": "jam', encoding="utf-8")
    source = nba_source_bbr.BbrNbaSource(root, verbose=False)
    assert source.player_games(2016, "regular") is None
    assert any("could not be read" in note for note in source.notes())


# ------------------------------------------------------------- the teams

def test_teams_are_discovered_from_the_schedule_with_franchise_lineage(source):
    teams = source.teams().set_index("team_id")
    assert teams.loc["CLE", "name"] == "Cleveland Cavaliers"
    assert teams.loc["CLE", "is_current"] == 1
    # Defunct: its own franchise, and not current, so the build reports it
    # rather than this module deciding the Huskies still exist.
    assert teams.loc["TRH", "is_current"] == 0
    assert teams.loc["TRH", "franchise_id"] == "bbr-trh"
    assert teams.loc["NYK", "first_season"] == 1946


def test_the_aba_is_left_out_unless_asked_for(root):
    default = nba_source_bbr.BbrNbaSource(root, verbose=False)
    assert 1971 not in default.seasons()
    everything = nba_source_bbr.BbrNbaSource(root, leagues=None, verbose=False)
    assert 1971 in everything.seasons()


# ----------------------------------------------------------- the players

def test_player_biography_is_converted_not_copied(source):
    people = source.players().set_index("source_player_id")
    assert people.loc["jamesle01", "birth_year"] == 1984
    assert people.loc["jamesle01", "height_cm"] == pytest.approx(205.7)
    assert people.loc["jamesle01", "weight_kg"] == pytest.approx(113.4, abs=0.1)


def test_height_reads_both_the_inches_and_the_feet_inches_forms():
    assert nba_source_bbr.height_cm("81.0") == pytest.approx(205.7)
    assert nba_source_bbr.height_cm("6-9") == pytest.approx(205.7)
    assert nba_source_bbr.height_cm("") is None
    assert nba_source_bbr.height_cm("0") is None


# ------------------------------------------------------------ the manifest

def test_every_read_is_recorded_for_the_manifest(source):
    source.players()
    source.matches(2016)
    source.player_games(2016, "regular")
    endpoints = {f.endpoint for f in source.fetches()}
    assert {"games.csv", "players.csv", "boxscores"} <= endpoints
    assert all(f.digest for f in source.fetches())


# -------------------------------------------------------------- the chain

def test_a_scrape_root_builds_a_database_end_to_end(tmp_path):
    """
    The part no unit test reaches: the adapter's output through the build.

    Team keys have to resolve to franchises, a float score has to survive
    into a W/L result, and a playoff match with no round of its own has to
    pick one up from reference/playoff_series.csv -- without which the
    Finals and championship squares answer nobody. Built strict, so any
    error issue fails this test rather than being reported and ignored.
    """
    from nba import build_nba_db

    base = tmp_path / "scrape"
    _write_csv(base / "games.csv", GAME_COLUMNS, [
        _game_row("201610250CLE", 2016, "2016-17", "regular", "CLE",
                  "Cleveland Cavaliers", "NYK", "New York Knicks",
                  home_points=117, visitor_points=88),
        _game_row("201610260GSW", 2016, "2016-17", "regular", "GSW",
                  "Golden State Warriors", "NYK", "New York Knicks",
                  home_points=110, visitor_points=100),
        _game_row("201706010GSW", 2016, "2016-17", "playoff", "GSW",
                  "Golden State Warriors", "CLE", "Cleveland Cavaliers",
                  home_points=113, visitor_points=91),
    ])
    _write_csv(base / "players.csv", PLAYER_COLUMNS, [
        _player_row("jamesle01", "LeBron James"),
        _player_row("curryst01", "Stephen Curry"),
        _player_row("porzikr01", "Kristaps Porzingis"),
    ])
    for key, phase, lines in (
            ("201610250CLE", "regular",
             [_line("jamesle01", "LeBron James", "CLE", pts=19),
              _line("porzikr01", "Kristaps Porzingis", "NYK", pts=16)]),
            ("201610260GSW", "regular",
             [_line("curryst01", "Stephen Curry", "GSW", pts=30),
              _line("porzikr01", "Kristaps Porzingis", "NYK", pts=12)]),
            ("201706010GSW", "playoff",
             [_line("curryst01", "Stephen Curry", "GSW", pts=28),
              _line("jamesle01", "LeBron James", "CLE", pts=28)])):
        path = base / "seasons" / "2016-17" / phase / f"{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"bbr_game_key": key, "league": "NBA", "season": 2016,
             "phase": phase, "source_sha256": "x" * 64, "players": lines}),
            encoding="utf-8")

    db = tmp_path / "nba.db"
    _write_csv(tmp_path / "reference" / "playoff_series.csv",
               ["season", "league", "round", "series_name", "winner",
                "loser", "wins_winner", "wins_loser"],
               [{"season": 2016, "league": "NBA", "round": "F",
                 "series_name": "2017 NBA Finals",
                 "winner": "Golden State Warriors",
                 "loser": "Cleveland Cavaliers",
                 "wins_winner": 1, "wins_loser": 0}])

    summary = build_nba_db.build(
        db, nba_source_bbr.BbrNbaSource(base, verbose=False),
        strict=True, write_reference=False, verbose=False)
    assert summary["players"] == 3
    assert summary["games"] == 6
    assert summary["matches"] == 3

    import sqlite3
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert con.execute(
            "SELECT COUNT(*) FROM source_issues WHERE severity='error'"
        ).fetchone()[0] == 0
        # The reference supplied the round, so a champion exists.
        assert con.execute(
            "SELECT club_now FROM team_seasons WHERE champion=1"
        ).fetchall() == [("Golden State Warriors",)]
        # Team keys resolved to franchises, and the score decided the result.
        assert con.execute(
            "SELECT club_now, result FROM games WHERE player='LeBron James' "
            "AND is_playoff=0").fetchone() == ("Cleveland Cavaliers", "W")
    finally:
        con.close()


# --------------------------------------------------------- the real thing

@pytest.mark.skipif(not (SAMPLE / "sample_games.csv").exists(),
                    reason="no scrape sample checked out")
def test_the_real_sample_loads():
    """data/nba/sample, unmodified, as a source root."""
    source = nba_source_bbr.BbrNbaSource(SAMPLE, verbose=False)
    seasons = source.seasons()
    assert seasons, "the sample index produced no seasons"

    teams = source.teams()
    assert list(teams.columns) == list(nba_source.TEAM_COLUMNS)
    assert teams["team_id"].is_unique

    people = source.players()
    assert list(people.columns) == list(nba_source.PLAYER_COLUMNS)
    assert people["source_player_id"].is_unique
    assert people["player"].str.len().gt(0).all()

    matches = source.matches(seasons[0])
    assert matches["date"].str.match(r"^\d{4}-\d{2}-\d{2}$").all()
    assert matches["match_id"].is_unique


@pytest.mark.skipif(not (SAMPLE / "sample_regular_games").exists(),
                    reason="no scrape sample checked out")
def test_the_real_box_scores_parse():
    """Every stat column in a real 2016-17 box score, straight off disk."""
    path = next((SAMPLE / "sample_regular_games").glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))

    rows = []
    for line in payload["players"]:
        row = {"minutes": nba_source.parse_minutes(line.get("minutes"))}
        for column, field in nba_source_bbr.STAT_FIELDS.items():
            row[column] = nba_source.numeric(line.get(field))
        rows.append(row)

    assert rows, f"{path.name} carried no player lines"
    assert all(r["points"] is not None for r in rows)
    assert all(r["minutes"] is not None for r in rows)
    total = sum(r["points"] for r in rows)
    scores = payload["home_team"]["points"] + payload["away_team"]["points"]
    assert total == scores, "player points do not add up to the game score"
