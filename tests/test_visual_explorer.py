#!/usr/bin/env python3
"""The Visual Explorer's promises: measured capabilities, honest denominators.

Three kinds of test live here.

**Synthetic arithmetic.** Three tiny databases with hand-written rows, so
the numbers a chart would draw can be asserted exactly. This is where the
statistical rules are pinned: a rate divides by the games a statistic was
*recorded* in and not by games played, a draw is half a win, a match is
counted once and not once per club, and a season-grain build counts the
games its rows stand for rather than the rows.

**The identifier wall.** Every column name that reaches an f-string has to
have come from the sport's schema or from the measured statistic list.
The test feeds it the shapes an injection would take and expects a
refusal, not a quoted string.

**Live renders.** Marked `live`, because they need the built databases:
every section of the page, for every sport, with no exception raised and
no stack trace shown to a reader.
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

import pandas as pd
import pytest

import charts
import core
import sports
import visual_queries as vq
from registry import Vocab


# ------------------------------------------------------------ fixtures

class FakeSport:
    """A sport built entirely out of a schema and a database path.

    Duck-typed rather than a real `registry.Sport` because the query layer
    only ever asks for `.schema`, `.vocab`, `.key` and `.db` -- and because
    a Sport carries dicts, so it is unhashable and could not be a cached
    function's argument even if one wanted to pass it.
    """

    def __init__(self, key, db, schema, vocab=None, past_games_hint=""):
        self.key = key
        self.db = str(db)
        self.schema = schema
        self.vocab = vocab or Vocab()
        self.label = f"{key} Data Lab"
        self.past_games_hint = past_games_hint
        self.stat_eras = {}

    def k(self, *parts):
        return ":".join((self.key,) + tuple(str(p) for p in parts))

    def stat_available_from(self, stat):
        return self.stat_eras.get(stat)


def _register(monkeypatch, sport):
    """Put a fixture sport in the registry the cached queries resolve through.

    `sports.get` answers an unknown key with the default sport, so without
    this a synthetic database would silently be read with the AFL's schema.
    """
    monkeypatch.setitem(sports.SPORTS, sport.key, sport)
    vq.capabilities.clear()
    return sport


def _revision(path):
    stat = _os.stat(path)
    return (str(path), stat.st_mtime_ns, stat.st_size)


GAME_SCHEMA = core.Schema(
    career_score="career_goals", career_postseason="finals_played",
    game_score="goals", required_player_cols=(), required_games_cols=(),
    stats=("goals", "marks", "tackles"), clubs=("Aces", "Bruisers"))

SEASON_SCHEMA = core.Schema(
    career_score="career_goals", career_postseason="finals_played",
    game_score="goals", required_player_cols=(), required_games_cols=(),
    stats=("goals", "era"), rate_stats=("era",), games_per_row="games",
    matches="", clubs=("Aces", "Bruisers"))


def _players_and_games(con, extra_columns, rows):
    con.executescript(f"""
        CREATE TABLE players (
          player_id INTEGER, player TEXT, debut_season INTEGER,
          final_season INTEGER, career_games INTEGER, career_goals REAL,
          finals_played INTEGER, clubs_hist TEXT, obscurity REAL);
        CREATE TABLE games (
          player_id INTEGER, player TEXT, season INTEGER, date TEXT,
          round TEXT, venue TEXT, club_hist TEXT, club_now TEXT,
          opponent TEXT, career_game_no INTEGER, is_final INTEGER,
          result TEXT{extra_columns});
        INSERT INTO players VALUES
          (1,'Alpha',2000,2001,4,9,0,'Aces',50.0),
          (2,'Beta',2000,2001,4,4,0,'Bruisers',50.0);
    """)
    con.executemany(
        f"INSERT INTO games VALUES ({', '.join('?' * (12 + extra_columns.count(',')))})",
        rows)


@pytest.fixture
def game_grain(tmp_path, monkeypatch):
    """One row per player per game, with a statistic that starts late.

    `tackles` is NULL through 2000 and recorded through 2001 -- the era
    boundary every denominator rule in the module exists for.
    """
    path = tmp_path / "grain.db"
    con = sqlite3.connect(path)
    _players_and_games(con, ", goals REAL, marks REAL, tackles REAL", [
        # player, name, season, date, round, venue, club_hist, club_now,
        # opponent, game_no, is_final, result, goals, marks, tackles
        (1, 'Alpha', 2000, '2000-04-01', '1', 'Ground', 'Aces', 'Aces',
         'Bruisers', 1, 0, 'W', 3.0, 5.0, None),
        (1, 'Alpha', 2000, '2000-04-08', '2', 'Ground', 'Aces', 'Aces',
         'Bruisers', 2, 0, 'L', 1.0, 4.0, None),
        (1, 'Alpha', 2001, '2001-04-01', '1', 'Ground', 'Aces', 'Aces',
         'Bruisers', 3, 0, 'W', 4.0, 6.0, 8.0),
        (1, 'Alpha', 2001, '2001-04-08', '2', 'Ground', 'Aces', 'Aces',
         'Bruisers', 4, 1, 'D', 1.0, None, 2.0),
        (2, 'Beta', 2000, '2000-04-01', '1', 'Ground', 'Bruisers',
         'Bruisers', 'Aces', 1, 0, 'L', 1.0, 2.0, None),
        (2, 'Beta', 2000, '2000-04-08', '2', 'Ground', 'Bruisers',
         'Bruisers', 'Aces', 2, 0, 'W', 2.0, 3.0, None),
        (2, 'Beta', 2001, '2001-04-01', '1', 'Ground', 'Bruisers',
         'Bruisers', 'Aces', 3, 0, 'L', 0.0, 1.0, 4.0),
        (2, 'Beta', 2001, '2001-04-08', '2', 'Ground', 'Bruisers',
         'Bruisers', 'Aces', 4, 1, 'D', 1.0, 2.0, 6.0),
    ])
    # Three matches: two in 2000, one final in 2001. Every match has two
    # club rows, which is the double-count the margin query must avoid.
    con.executescript("""
        CREATE TABLE club_match_sources (
          source_game_key TEXT, source_club_id TEXT, source_club_label TEXT,
          season INTEGER, round TEXT, is_final INTEGER, match_date TEXT,
          venue_raw TEXT, team_position TEXT, result TEXT,
          points_for INTEGER, points_against INTEGER, margin INTEGER,
          attendance INTEGER, match_id INTEGER, match_status TEXT);
        INSERT INTO club_match_sources VALUES
          ('g1','aces','Aces',2000,'1',0,'2000-04-01','Ground','H','W',
           30,20,10,1000,1,'unique'),
          ('g1','bruisers','Bruisers',2000,'1',0,'2000-04-01','Ground','A','L',
           20,30,-10,1000,1,'unique'),
          ('g2','aces','Aces',2000,'2',0,'2000-04-08','Ground','A','L',
           15,25,-10,900,2,'unique'),
          ('g2','bruisers','Bruisers',2000,'2',0,'2000-04-08','Ground','H','W',
           25,15,10,900,2,'unique'),
          ('g3','aces','Aces',2001,'F',1,'2001-04-08','Ground','F','D',
           40,40,0,2000,3,'unique'),
          ('g3','bruisers','Bruisers',2001,'F',1,'2001-04-08','Ground','F','D',
           40,40,0,2000,3,'unique'),
          ('g4','aces','Aces',2001,'1',0,'2001-04-01','Ground','H','W',
           50,10,40,500,4,'suspect'),
          ('g4','bruisers','Bruisers',2001,'1',0,'2001-04-01','Ground','A','L',
           10,50,-40,500,4,'suspect');
        CREATE TABLE team_seasons (
          season INTEGER, phase TEXT, club_now TEXT, played INTEGER,
          wins INTEGER, losses INTEGER, points_for INTEGER,
          points_against INTEGER, ladder_rank INTEGER);
        INSERT INTO team_seasons VALUES
          (2000,'regular','Aces',10,6,4,300,250,2),
          (2000,'playoff','Aces',2,1,1,60,55,NULL),
          (2000,'regular','Bruisers',10,4,6,250,300,5),
          (2001,'regular','Aces',10,10,0,400,200,1),
          (2001,'regular','Bruisers',10,0,10,200,400,10);
        -- The AFL's award shape: a slug, a display name and the winner
        -- written out as text with no id to open a card with.
        CREATE TABLE awards (
          award_slug TEXT, award_name TEXT, season INTEGER, player TEXT);
        INSERT INTO awards VALUES
          ('medal','The Medal',2000,'Alpha'),
          ('medal','The Medal',2001,'Alpha'),
          ('squad','The Squad',2000,'Alpha'),
          ('squad','The Squad',2000,'Beta');
    """)
    con.commit()
    con.close()
    return _register(monkeypatch,
                     FakeSport("grain", path, GAME_SCHEMA))


@pytest.fixture
def season_grain(tmp_path, monkeypatch):
    """One row per player per season, MLB-shaped: `games` says how many."""
    path = tmp_path / "coarse.db"
    con = sqlite3.connect(path)
    _players_and_games(con, ", games INTEGER, goals REAL, era REAL", [
        (1, 'Alpha', 2000, '2000-01-01', 'R', '', 'Aces', 'Aces', '', 1, 0,
         'W', 100, 40.0, 3.5),
        (1, 'Alpha', 2001, '2001-01-01', 'R', '', 'Aces', 'Aces', '', 2, 0,
         'W', 50, 10.0, 4.5),
        (1, 'Alpha', 2001, '2001-01-01', 'R', '', 'Bruisers', 'Bruisers', '',
         3, 0, 'L', 50, 5.0, 2.5),
        (2, 'Beta', 2000, '2000-01-01', 'R', '', 'Bruisers', 'Bruisers', '',
         1, 0, 'L', 20, 2.0, None),
    ])
    con.commit()
    con.close()
    return _register(monkeypatch,
                     FakeSport("coarse", path, SEASON_SCHEMA))


@pytest.fixture
def bare(tmp_path, monkeypatch):
    """players and games and nothing else -- no match rows, no season table."""
    path = tmp_path / "bare.db"
    con = sqlite3.connect(path)
    _players_and_games(con, ", goals REAL, marks REAL, tackles REAL", [
        (1, 'Alpha', 2000, '2000-04-01', '1', 'G', 'Aces', 'Aces',
         'Bruisers', 1, 0, 'W', 3.0, 5.0, 1.0),
    ])
    con.commit()
    con.close()
    return _register(monkeypatch,
                     FakeSport("bare", path, GAME_SCHEMA,
                               past_games_hint="Run the loader."))


def _open(sport):
    return sqlite3.connect(f"file:{sport.db}?mode=ro", uri=True)


def _caps(sport):
    con = _open(sport)
    try:
        return vq.capabilities(sport.key, _revision(sport.db), con), con
    finally:
        pass


# ------------------------------------------------- the capability model

def test_capabilities_are_measured_from_the_file(game_grain):
    caps, con = _caps(game_grain)
    assert caps.player_game_grain is True
    assert caps.games_per_row == ""
    assert caps.stats == ("goals", "marks", "tackles")
    assert caps.season_range == (2000, 2001)
    assert caps.team_match_rows is True
    assert caps.team_label == "source_club_label"
    assert caps.team_season_table is True
    assert caps.team_rank_column == "ladder_rank"
    assert caps.team_phase_column == "phase"
    # The phase accounting for the most games played is the regular one.
    assert caps.team_phase_primary == "regular"
    # This build's team_seasons has no draws column; naming one would make
    # every team query return nothing at all.
    assert caps.team_draws_column == ""
    con.close()


def test_a_season_grain_build_is_not_offered_per_game_charts(season_grain):
    caps, con = _caps(season_grain)
    assert caps.player_game_grain is False
    assert caps.games_per_row == "games"
    assert caps.matches is False              # Lahman has no box scores
    assert caps.rate_stats == frozenset({"era"})
    con.close()


def test_a_bare_database_declines_the_sections_it_cannot_answer(bare):
    caps, con = _caps(bare)
    assert caps.league_activity is True
    assert caps.player_trajectory is True
    assert caps.coverage is True
    # Nothing game-level about the clubs, so no team or match section.
    assert caps.team_match_rows is False
    assert caps.team_trends is False
    assert caps.match_distributions is False
    assert caps.team_postseason_toggle is False
    assert "Game-level team rows" in caps.missing
    con.close()


def test_capabilities_survive_a_database_with_no_tables_at_all(tmp_path,
                                                              monkeypatch):
    """app.py probes before it knows a build succeeded; nothing may raise."""
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    sport = _register(monkeypatch, FakeSport("empty", path, GAME_SCHEMA))
    con = _open(sport)
    caps = vq.capabilities(sport.key, _revision(path), con)
    assert caps.stats == () and caps.season_range == ()
    assert not any(ready for _, ready, _ in caps.summary())
    con.close()


# --------------------------------------------- denominators and grain

def test_appearances_count_games_not_rows_on_a_season_grain_build(
        season_grain):
    """Counting Lahman's player-seasons as appearances would report 2000
    as having had two games played in it rather than 120."""
    con = _open(season_grain)
    frame = vq.league_activity(season_grain.key, _revision(season_grain.db),
                               con)
    by_season = frame.set_index("Season")["Appearances"]
    assert by_season.loc[2000] == 120         # 100 + 20, not 2 rows
    assert by_season.loc[2001] == 100         # 50 + 50, one player, two clubs
    con.close()


def test_a_rate_divides_by_recorded_games_not_games_played(game_grain):
    """Alpha played two 2001 games and had marks recorded in one of them.

    Six marks over one recorded game is six. Over two games played it
    would be three -- an average of a game nobody measured.
    """
    con = _open(game_grain)
    frame = vq.player_seasons(game_grain.key, _revision(game_grain.db), con,
                              1, "marks")
    row = frame.set_index("Season").loc[2001]
    assert row["Played"] == 2 and row["Recorded"] == 1 and row["Total"] == 6
    rated = vq.with_rate(frame, "Rate").set_index("Season")
    assert rated.loc[2001, "Rate"] == 6.0
    con.close()


def test_a_season_with_nothing_recorded_is_absent_not_zero(game_grain):
    """Tackles start in 2001. Alpha's 2000 is a hole in the axis, not a
    pair of zero-tackle seasons."""
    con = _open(game_grain)
    frame = vq.player_seasons(game_grain.key, _revision(game_grain.db), con,
                              1, "tackles")
    assert frame["Season"].tolist() == [2001]
    con.close()


def test_with_rate_leaves_an_unrecorded_rate_missing(game_grain):
    frame = pd.DataFrame({"Season": [1999], "Total": [None], "Recorded": [0]})
    rated = vq.with_rate(frame, "Rate")
    assert pd.isna(rated.loc[0, "Rate"])


def test_a_rate_statistic_is_never_summed_across_clubs(season_grain):
    """Alpha's 2001 was 4.50 with one club and 2.50 with another. It is
    neither a 7.00 nor -- without innings to weight it -- a 3.50."""
    con = _open(season_grain)
    frame = vq.player_seasons(season_grain.key, _revision(season_grain.db),
                              con, 1, "era")
    rows = frame[frame["Season"] == 2001]
    assert sorted(rows["Value"]) == [2.5, 4.5]
    assert sorted(rows["Club"]) == ["Aces", "Bruisers"]
    con.close()


def test_volume_efficiency_refuses_a_rate_statistic(season_grain):
    con = _open(season_grain)
    assert vq.volume_efficiency(season_grain.key,
                                _revision(season_grain.db), con,
                                "era").empty
    con.close()


def test_volume_efficiency_thresholds_on_recorded_games(game_grain):
    """Alpha has four games and tackles recorded in two of them, so a
    floor of three excludes him -- the floor is on the sample the rate is
    computed from, not on the career."""
    con = _open(game_grain)
    revision = _revision(game_grain.db)
    kept = vq.volume_efficiency(game_grain.key, revision, con, "tackles",
                                min_games=2)
    assert set(kept["Player"]) == {"Alpha", "Beta"}
    assert kept.set_index("Player").loc["Alpha", "Tackles per game"] == 5.0
    assert vq.volume_efficiency(game_grain.key, revision, con, "tackles",
                                min_games=3).empty
    con.close()


def test_volume_efficiency_never_exceeds_the_scatter_cap(game_grain):
    con = _open(game_grain)
    frame = vq.volume_efficiency(game_grain.key, _revision(game_grain.db),
                                 con, "goals", min_games=1,
                                 limit=charts.SCATTER_CAP * 10)
    assert len(frame) <= charts.SCATTER_CAP
    con.close()


# ------------------------------------------------------- team records

def test_a_curated_table_is_filtered_to_the_regular_phase(game_grain):
    """The Aces' 2000 is a ten-game season, not a twelve-game one with the
    playoff run silently added on."""
    con = _open(game_grain)
    frame = vq.team_seasons(game_grain.key, _revision(game_grain.db), con,
                            ("Aces",), (), False)
    row = frame[frame["Season"] == 2000].iloc[0]
    assert row["Played"] == 10 and row["Wins"] == 6
    assert row["Rank"] == 2
    con.close()


def test_including_the_postseason_sums_the_phases(game_grain):
    con = _open(game_grain)
    frame = vq.team_seasons(game_grain.key, _revision(game_grain.db), con,
                            ("Aces",), (), True)
    row = frame[frame["Season"] == 2000].iloc[0]
    assert row["Played"] == 12 and row["Wins"] == 7
    # The playoff row leaves the ladder position NULL; MAX keeps the real one.
    assert row["Rank"] == 2
    con.close()


def test_derived_records_count_a_draw_as_half_a_win(bare, game_grain,
                                                    tmp_path, monkeypatch):
    """A 1-1-1 season is 50%, not 33%: a draw is half a result, and
    dropping it from the numerator understates every drawn era."""
    path = tmp_path / "derived.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE players (player_id INTEGER, player TEXT,
          debut_season INTEGER, final_season INTEGER, career_games INTEGER,
          career_goals REAL, finals_played INTEGER, clubs_hist TEXT,
          obscurity REAL);
        CREATE TABLE games (player_id INTEGER, season INTEGER,
          club_hist TEXT, club_now TEXT, goals REAL);
        CREATE TABLE club_match_sources (
          source_game_key TEXT, source_club_id TEXT, season INTEGER,
          is_final INTEGER, team_position TEXT, result TEXT,
          points_for INTEGER, points_against INTEGER, margin INTEGER,
          match_status TEXT);
        INSERT INTO club_match_sources VALUES
          ('a','Aces',2000,0,'H','W',10,5,5,'unique'),
          ('b','Aces',2000,0,'A','L',5,10,-5,'unique'),
          ('c','Aces',2000,0,'H','D',7,7,0,'unique');
    """)
    con.commit()
    con.close()
    sport = _register(monkeypatch, FakeSport("derived", path, GAME_SCHEMA))
    read = _open(sport)
    caps = vq.capabilities(sport.key, _revision(path), read)
    assert caps.team_season_table is False      # derives, does not curate
    frame = vq.team_seasons(sport.key, _revision(path), read, ("Aces",))
    row = frame.iloc[0]
    assert row["Wins"] == 1 and row["Losses"] == 1 and row["Draws"] == 1
    assert row["WinPct"] == 50.0
    read.close()


# ------------------------------------------------ match distributions

def test_a_match_is_binned_once_not_once_per_club(game_grain):
    """Two rows a match is the shape of the table, not two matches. Only
    the untrusted match is excluded, so two remain."""
    con = _open(game_grain)
    frame = vq.margin_distribution(game_grain.key, _revision(game_grain.db),
                                   con, width=10)
    assert frame["Matches"].sum() == 3          # g1, g2 and the g3 draw
    con.close()


def test_an_untrusted_source_row_is_excluded(game_grain):
    """The 40-point margin belongs to a row marked 'suspect' and must not
    appear -- a rescrape can introduce those at any time."""
    con = _open(game_grain)
    frame = vq.margin_distribution(game_grain.key, _revision(game_grain.db),
                                   con, width=10)
    assert frame["Bin"].max() == 10
    con.close()


def test_a_draw_is_a_margin_of_nought(game_grain):
    con = _open(game_grain)
    frame = vq.margin_distribution(game_grain.key, _revision(game_grain.db),
                                   con, width=10).set_index("Bin")
    assert frame.loc[0, "Matches"] == 1
    con.close()


def test_home_and_away_excludes_the_finals_that_have_no_home_side(
        game_grain):
    """The drawn final is marked 'F' for both clubs. Folding it into away
    would move the away curve by a fact about the fixture."""
    con = _open(game_grain)
    frame = vq.home_away_margins(game_grain.key, _revision(game_grain.db),
                                 con, width=10)
    assert frame["Matches"].sum() == 4          # two matches, both sides
    assert set(frame["Side"]) == {"Home", "Away"}
    con.close()


def test_scoring_by_season_counts_each_match_once(game_grain):
    con = _open(game_grain)
    frame = vq.scoring_by_season(game_grain.key, _revision(game_grain.db),
                                 con).set_index("Season")
    assert frame.loc[2000, "Matches"] == 2
    # g1 totals 50 (30-20) and g2 totals 40 (25-15); both were won by 10.
    assert frame.loc[2000, "Average total score"] == 45.0
    assert frame.loc[2000, "Average winning margin"] == 10.0
    # The drawn final totals 80 and was won by nobody.
    assert frame.loc[2001, "Average total score"] == 80.0
    assert frame.loc[2001, "Average winning margin"] == 0.0
    con.close()


def test_bin_width_is_measured_from_the_sport_s_own_scale(game_grain):
    con = _open(game_grain)
    width = vq.margin_bin_width(game_grain.key, _revision(game_grain.db), con)
    assert width == 1                           # a 10-point peak needs no bins
    con.close()


# ---------------------------------------------------------- coverage

def test_coverage_is_the_share_of_rows_carrying_a_value(game_grain):
    """Tackles are on none of 2000's four rows and all of 2001's four."""
    con = _open(game_grain)
    frame = vq.stat_coverage(game_grain.key, _revision(game_grain.db), con,
                             ("tackles", "marks"))
    tackles = frame[frame["Statistic"] == "Tackles"].set_index("Season")
    assert tackles.loc[2000, "Coverage"] == 0.0
    assert tackles.loc[2001, "Coverage"] == 100.0
    # Alpha's 2001 final has no marks recorded: three of four rows.
    marks = frame[frame["Statistic"] == "Marks"].set_index("Season")
    assert marks.loc[2001, "Coverage"] == 75.0
    con.close()


def test_a_season_the_competition_did_not_play_has_no_cell(game_grain):
    con = _open(game_grain)
    frame = vq.stat_coverage(game_grain.key, _revision(game_grain.db), con)
    assert sorted(frame["Season"].unique()) == [2000, 2001]
    con.close()


# ------------------------------------------------------------ venues

def test_a_venue_is_counted_once_per_match_not_once_per_club(game_grain):
    con = _open(game_grain)
    caps = vq.capabilities(game_grain.key, _revision(game_grain.db), con)
    assert caps.venue_charts is True and caps.venue_coverage == 100.0
    frame = vq.busiest_venues(game_grain.key, _revision(game_grain.db), con)
    row = frame.iloc[0]
    assert row["Venue"] == "Ground"
    assert row["Matches"] == 3           # six club rows, three trusted matches
    assert row["From"] == 2000 and row["To"] == 2001
    con.close()


def test_a_barely_populated_venue_column_is_not_a_venue_capability(
        tmp_path, monkeypatch):
    """The NBA case. A column that exists and is 2% filled would rank the
    matches somebody happened to record, not the arenas."""
    path = tmp_path / "sparse.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE players (player_id INTEGER, player TEXT,
          debut_season INTEGER, final_season INTEGER, career_games INTEGER,
          career_goals REAL, finals_played INTEGER, clubs_hist TEXT,
          obscurity REAL);
        CREATE TABLE games (player_id INTEGER, season INTEGER,
          club_hist TEXT, club_now TEXT, venue TEXT, goals REAL);
        CREATE TABLE club_match_sources (
          source_game_key TEXT, source_club_id TEXT, season INTEGER,
          is_final INTEGER, team_position TEXT, result TEXT,
          points_for INTEGER, points_against INTEGER, margin INTEGER,
          venue_raw TEXT, match_status TEXT);
        INSERT INTO club_match_sources VALUES
          ('a','Aces',2000,0,'H','W',10,5,5,'Arena','unique'),
          ('a','Bruisers',2000,0,'A','L',5,10,-5,NULL,'unique'),
          ('b','Aces',2000,0,'H','W',10,5,5,NULL,'unique'),
          ('b','Bruisers',2000,0,'A','L',5,10,-5,NULL,'unique');
    """)
    con.commit()
    con.close()
    sport = _register(monkeypatch, FakeSport("sparse", path, GAME_SCHEMA))
    read = _open(sport)
    caps = vq.capabilities(sport.key, _revision(path), read)
    assert caps.venues is True                  # the column is there
    assert caps.venue_coverage == 25.0
    assert caps.venue_charts is False           # but it is not an answer
    assert vq.busiest_venues(sport.key, _revision(path), read).empty
    read.close()


def test_a_venue_trend_declines_past_the_series_cap(game_grain):
    con = _open(game_grain)
    too_many = tuple(f"Ground {i}" for i in range(charts.MAX_SERIES + 1))
    assert vq.venue_by_season(game_grain.key, _revision(game_grain.db), con,
                              too_many).empty
    con.close()


# ------------------------------------------------------------ awards

def test_award_columns_are_discovered_from_the_table(game_grain):
    con = _open(game_grain)
    caps = vq.capabilities(game_grain.key, _revision(game_grain.db), con)
    assert caps.award_charts is True
    assert caps.awards_table == "awards"
    assert caps.award_key_column == "award_slug"
    assert caps.award_name_column == "award_name"
    assert caps.award_recipient_column == "player"
    assert caps.award_player_id_column == ""     # nothing to open a card with
    con.close()


def test_an_award_roll_counts_recipients_not_seasons(game_grain):
    """A medal files one row a season and a squad files several; the
    chart says which it is showing rather than calling both 'winners'."""
    con = _open(game_grain)
    revision = _revision(game_grain.db)
    catalogue = vq.award_options(game_grain.key, revision, con)
    by_key = catalogue.set_index("Key")
    assert by_key.loc["squad", "Records"] == 2
    assert by_key.loc["medal", "Records"] == 2
    squad = vq.award_by_season(game_grain.key, revision, con, "squad")
    assert squad.set_index("Season").loc[2000, "Recipients"] == 2
    leaders = vq.award_leaders(game_grain.key, revision, con, "medal")
    assert leaders.iloc[0]["Recipient"] == "Alpha"
    assert leaders.iloc[0]["Awards"] == 2
    assert "PlayerID" not in leaders.columns
    con.close()


def test_an_award_table_keyed_only_by_id_is_named_through_the_join(
        tmp_path, monkeypatch):
    """The MLB shape: Lahman's awards carry a player id and no name."""
    path = tmp_path / "idawards.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE players (player_id INTEGER, player TEXT,
          debut_season INTEGER, final_season INTEGER, career_games INTEGER,
          career_goals REAL, finals_played INTEGER, clubs_hist TEXT,
          obscurity REAL);
        CREATE TABLE games (player_id INTEGER, season INTEGER,
          club_hist TEXT, club_now TEXT, goals REAL);
        CREATE TABLE awards (player_id INTEGER, award TEXT, season INTEGER);
        INSERT INTO players VALUES (7,'Gamma',2000,2001,10,5,0,'Aces',50.0);
        INSERT INTO awards VALUES (7,'MVP',2000), (7,'MVP',2001),
          (99,'MVP',2001);
    """)
    con.commit()
    con.close()
    sport = _register(monkeypatch, FakeSport("idawards", path, GAME_SCHEMA))
    read = _open(sport)
    caps = vq.capabilities(sport.key, _revision(path), read)
    assert caps.award_key_column == "award"
    assert caps.award_recipient_column == ""     # no name column at all
    assert caps.award_player_id_column == "player_id"
    leaders = vq.award_leaders(sport.key, _revision(path), read, "MVP")
    top = leaders.iloc[0]
    assert top["Recipient"] == "Gamma" and top["Awards"] == 2
    assert top["PlayerID"] == 7
    # The unlinked row names nobody, so it is dropped rather than drawn as
    # a blank bar at the bottom of the chart.
    assert len(leaders) == 1
    read.close()


def test_a_bare_database_offers_no_venue_or_award_charts(bare):
    caps, con = _caps(bare)
    assert caps.venue_charts is False
    assert caps.award_charts is False
    con.close()


# ----------------------------------------------------- identifier wall

@pytest.mark.parametrize("attempt", [
    "goals; DROP TABLE players",
    "goals) UNION SELECT 1 --",
    "obscurity",                 # a real column, but not a statistic
    "career_goals",
    "",
    "GOALS",                     # the allowlist is exact, not case-folded
])
def test_only_a_measured_statistic_reaches_the_sql(game_grain, attempt):
    con = _open(game_grain)
    caps = vq.capabilities(game_grain.key, _revision(game_grain.db), con)
    with pytest.raises(ValueError):
        vq._allowed_stat(caps, attempt)
    con.close()


def test_a_query_refuses_a_statistic_the_database_does_not_have(game_grain):
    con = _open(game_grain)
    revision = _revision(game_grain.db)
    for call in (lambda: vq.player_seasons(game_grain.key, revision, con, 1,
                                           "handballs"),
                 lambda: vq.volume_efficiency(game_grain.key, revision, con,
                                              "handballs")):
        with pytest.raises(ValueError):
            call()
    con.close()


def test_a_season_range_is_bound_rather_than_formatted():
    sql, params = vq._season_clause("season", (1990, 2000))
    assert sql.count("?") == 2 and "1990" not in sql
    assert params == [1990, 2000]


def test_a_season_that_is_not_a_number_is_refused_not_quoted():
    """`int()` is the gate. Refusing loudly beats building a clause around
    text that only looks like a year."""
    with pytest.raises(ValueError):
        vq._season_clause("season", ("1990); DROP TABLE x --", 2000))


def test_a_reversed_season_range_is_ordered_not_empty():
    _sql, params = vq._season_clause("season", (2010, 1990))
    assert params == [1990, 2010]


# -------------------------------------------------------- chart rules

def test_a_ninth_series_is_declined_rather_than_given_a_ninth_colour():
    frame = pd.DataFrame({
        "Season": [2000] * (charts.MAX_SERIES + 1),
        "Wins": list(range(charts.MAX_SERIES + 1)),
        "Team": [f"Club {i}" for i in range(charts.MAX_SERIES + 1)]})
    assert charts.multi_series_chart(frame, "Season", "Wins", "Team",
                                     "Season", "Wins") is None
    inside = frame.head(charts.MAX_SERIES)
    assert charts.multi_series_chart(inside, "Season", "Wins", "Team",
                                     "Season", "Wins") is not None


def _colour_of(chart, name):
    scale = chart.to_dict()["encoding"]["color"]["scale"]
    return scale["range"][scale["domain"].index(name)]


def test_a_series_that_leaves_the_data_does_not_repaint_the_rest():
    """The recolor-on-filter trap. Narrowing the seasons until the Aces
    have no row left must leave the Bruisers the colour they were."""
    frame = pd.DataFrame({"Season": [2000, 2000], "Wins": [5, 6],
                          "Team": ["Aces", "Bruisers"]})
    order = ["Aces", "Bruisers"]
    both = charts.multi_series_chart(frame, "Season", "Wins", "Team",
                                     "Season", "Wins", order=order)
    survivor = charts.multi_series_chart(
        frame[frame["Team"] == "Bruisers"], "Season", "Wins", "Team",
        "Season", "Wins", order=order)
    assert _colour_of(both, "Bruisers") == _colour_of(survivor, "Bruisers")
    assert survivor.to_dict()["encoding"]["color"]["scale"]["domain"] \
        == ["Bruisers"]


def test_a_colour_follows_the_order_it_was_given_not_the_row_order():
    """Rows arriving Bruisers-first must not hand Bruisers slot one."""
    forwards = pd.DataFrame({"Season": [2000, 2000], "Wins": [5, 6],
                             "Team": ["Aces", "Bruisers"]})
    order = ["Aces", "Bruisers"]
    shuffled = forwards.iloc[::-1].reset_index(drop=True)
    a = charts.multi_series_chart(forwards, "Season", "Wins", "Team",
                                  "Season", "Wins", order=order)
    b = charts.multi_series_chart(shuffled, "Season", "Wins", "Team",
                                  "Season", "Wins", order=order)
    assert _colour_of(a, "Aces") == _colour_of(b, "Aces")
    assert _colour_of(a, "Bruisers") == _colour_of(b, "Bruisers")
    assert _colour_of(a, "Aces") != _colour_of(a, "Bruisers")


def test_categorical_colours_never_cycle():
    assert len(charts.categorical_colours(50)) == charts.MAX_SERIES
    assert len(set(charts.categorical_colours())) == charts.MAX_SERIES


def test_a_share_axis_totals_a_hundred_per_series():
    frame = pd.DataFrame({"Bin": [0, 10, 0, 10],
                          "Matches": [1, 3, 5, 5],
                          "Side": ["Home", "Home", "Away", "Away"]})
    chart = charts.distribution_chart(frame, "Bin", "Matches", "Margin",
                                      series_column="Side", bin_width=10,
                                      share=True)
    shares = chart.data.groupby("Side")["Share"].sum()
    assert shares.loc["Home"] == 100.0 and shares.loc["Away"] == 100.0


def test_the_builders_decline_an_empty_frame_rather_than_drawing_one():
    empty = pd.DataFrame()
    assert charts.season_trend_chart(empty, "Season", "V", "V") is None
    assert charts.multi_series_chart(empty, "x", "y", "s", "X", "Y") is None
    assert charts.distribution_chart(empty, "Bin", "N", "X") is None
    assert charts.coverage_heatmap(empty, "x", "y", "v", "X") is None


def test_a_trend_chart_carries_the_brush_it_was_asked_for():
    frame = pd.DataFrame({"Season": [2000, 2001], "Players": [10, 12]})
    chart = charts.season_trend_chart(frame, "Season", "Players", "Players",
                                      brush="season_span")
    assert [p.name for p in chart.params] == ["season_span"]


def test_a_sequential_ramp_is_one_hue_light_to_dark():
    ramp = charts.sequential_range()
    assert len(ramp) == len(set(ramp)) >= 5
    assert set(ramp) <= set(charts.SEQUENTIAL_LIGHT) | set(charts.SEQUENTIAL_DARK)


# ------------------------------------------------------- live renders

PAGE = _os.path.join(_ROOT, "app_pages", "19_Visual_Explorer.py")
SECTIONS = ["Overview", "Players", "Teams", "Matches", "Venues", "Awards"]
LIVE = [s for s in sports.SPORTS.values() if s.exists()]
LIVE_IDS = [s.key for s in LIVE]


def _page(sport, section, **state):
    from streamlit.testing.v1 import AppTest

    import db_pool

    revision = _revision(sport.db)
    app = AppTest.from_file(PAGE, default_timeout=300)
    app.session_state["SPORT"] = sport
    app.session_state["DB_REVISION"] = revision
    app.session_state["con"] = db_pool.get_con(sport.db, revision)
    app.session_state[sport.k("visual", "section")] = section
    for key, value in state.items():
        # "team_span:applied" addresses the applied-range key the brush
        # writes, which is namespaced one level below the widget's own.
        app.session_state[sport.k("visual", *key.split(":"))] = value
    return app.run()


@pytest.mark.live
@pytest.mark.parametrize("sport", LIVE, ids=LIVE_IDS)
@pytest.mark.parametrize("section", SECTIONS)
def test_every_section_renders_without_an_exception(sport, section):
    app = _page(sport, section)
    assert not app.exception, str(app.exception)


@pytest.mark.live
@pytest.mark.parametrize("sport", LIVE, ids=LIVE_IDS)
def test_the_mvp_charts_are_actually_drawn(sport):
    """A section that renders but draws nothing has failed silently."""
    assert len(_page(sport, "Overview").get("vega_lite_chart")) >= 2
    assert len(_page(sport, "Players").get("vega_lite_chart")) >= 1
    assert len(_page(sport, "Teams").get("vega_lite_chart")) >= 1
    assert len(_page(sport, "Matches").get("vega_lite_chart")) >= 2


@pytest.mark.live
@pytest.mark.parametrize("sport", LIVE, ids=LIVE_IDS)
def test_a_declined_section_says_why_rather_than_going_blank(sport):
    """Both optional sections either draw something or explain the gap;
    neither may render an empty page with no account of itself."""
    import db_pool

    revision = _revision(sport.db)
    caps = vq.capabilities(sport.key, revision,
                           db_pool.get_con(sport.db, revision))
    for name, offered in (("Venues", caps.venue_charts),
                          ("Awards", caps.award_charts)):
        app = _page(sport, name)
        drawn = len(app.get("vega_lite_chart"))
        if offered:
            assert drawn >= 1, f"{sport.key} {name} drew nothing"
        else:
            assert drawn == 0 and app.info, f"{sport.key} {name} said nothing"


@pytest.mark.live
@pytest.mark.parametrize("sport", LIVE, ids=LIVE_IDS)
def test_every_statistic_the_sport_declares_can_be_charted(sport):
    """A statistic offered in the picker that raises when picked is a
    trap; the list and the queries must agree."""
    import db_pool

    revision = _revision(sport.db)
    con = db_pool.get_con(sport.db, revision)
    caps = vq.capabilities(sport.key, revision, con)
    pid = con.execute(
        f"SELECT {sport.schema.player_id} FROM {sport.schema.players} "
        f"ORDER BY {sport.schema.career_games} DESC LIMIT 1").fetchone()[0]
    failures = []
    for stat in caps.stats:
        app = _page(sport, "Players", player_stat=stat)
        app.session_state[sport.k("visual", "player") + "_pick"] = pid
        app.run()
        if app.exception:
            failures.append(f"{stat}: {app.exception[0].value}")
    assert not failures, "\n".join(failures)


@pytest.mark.live
@pytest.mark.parametrize("sport", LIVE, ids=LIVE_IDS)
def test_a_season_range_applied_from_the_brush_reaches_the_charts(sport):
    """Streamlit ignores a widget's default once its key holds a value, so
    a range applied from the Overview brush would otherwise never reach a
    section whose slider had already been drawn once."""
    first = _page(sport, "Teams")
    assert first.slider, "the Teams section draws no season slider"
    low, high = first.slider[0].value          # the sport's whole span

    # Inside this sport's own span: the NFL's weekly data starts in 1999,
    # and a range below that is correctly clamped rather than honoured.
    target = (high - 8, high - 2)
    applied = _page(sport, "Teams", **{"team_span:applied": target})
    assert applied.slider[0].value == target != (low, high)
    # The rendered Season cells are the clickable button column's text,
    # not the query's integers -- components.py converts them so a click
    # can open the season card.
    seasons = {int(season) for frame in applied.dataframe
               for season in frame.value["Season"]}
    assert seasons and min(seasons) >= target[0] and max(seasons) <= target[1]

    # And a reader dragging it afterwards wins: the applied range is
    # consumed once, not re-imposed on every rerun.
    dragged = (target[0] + 1, target[1] - 1)
    applied.slider[0].set_range(*dragged).run()
    assert applied.slider[0].value == dragged
    assert not applied.exception


@pytest.mark.live
@pytest.mark.parametrize("sport", LIVE, ids=LIVE_IDS)
def test_no_section_shows_a_reader_a_stack_trace(sport):
    for section in SECTIONS:
        app = _page(sport, section)
        for element in app.error:
            assert "Traceback" not in element.value
            assert "sqlite3." not in element.value
