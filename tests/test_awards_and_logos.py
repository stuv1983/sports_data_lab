#!/usr/bin/env python3
"""Player comparison, logo resolution, Hall of Fame and Team of the Century.

The linking tests matter most here. Both new sources are name-based, and
name-based linking is exactly where a plausible-looking wrong answer gets
in: these teams are full of fathers and sons who share a name exactly.
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
from pathlib import Path

import pytest

from afl import club_logos as CL
from utils.afl import load_hall_of_fame as HOF
from utils.afl import load_teams_of_the_century as TOC
import player_compare as PC


# --------------------------------------------------------- player compare

def compare_fixture():
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE players (
          player_id INTEGER, player TEXT, debut_season INTEGER,
          final_season INTEGER, career_games INTEGER, career_goals INTEGER,
          finals_played INTEGER, clubs_hist TEXT, obscurity REAL
        );
        CREATE TABLE games (
          player_id INTEGER, season INTEGER, club_hist TEXT, match_id INTEGER,
          goals REAL, disposals REAL, tackles REAL
        );
        INSERT INTO players VALUES
          (1,'Old Timer',1960,1970,200,300,10,'A',60.0),
          (2,'Modern Player',2005,2018,250,150,20,'B',20.0);
        -- tackles were not recorded in the 1960s: NULL, never 0.
        INSERT INTO games VALUES
          (1,1960,'A',900,3,20,NULL),
          (1,1961,'A',901,2,25,NULL),
          (2,2005,'B',900,1,30,5),
          (2,2006,'B',902,4,28,7);
    """)
    return con


class _Schema:
    players = "players"; games = "games"
    player_id = "player_id"; player = "player"
    debut_season = "debut_season"; final_season = "final_season"
    career_games = "career_games"; career_score = "career_goals"
    career_postseason = "finals_played"; clubs_hist = "clubs_hist"
    obscurity = "obscurity"; season = "season"; club_hist = "club_hist"
    stats = ["goals", "disposals", "tackles"]


def test_profile_reads_totals_and_coverage():
    con = compare_fixture()
    p = PC.profile(con, 1, _Schema(), with_honours=False)
    assert p.player == "Old Timer"
    assert p.totals["goals"] == 5
    assert p.covered["disposals"] == (1960, 1961)
    assert p.span == "1960–1970"


def test_an_unrecorded_stat_is_absent_not_zero():
    """A 1960s player has no tackles because nobody counted them."""
    con = compare_fixture()
    p = PC.profile(con, 1, _Schema(), with_honours=False)
    assert "tackles" not in p.totals
    assert "goals" in p.totals


def test_comparable_stats_are_the_intersection():
    con = compare_fixture()
    a = PC.profile(con, 1, _Schema(), with_honours=False)
    b = PC.profile(con, 2, _Schema(), with_honours=False)
    shared = PC.comparable_stats(a, b)
    assert "goals" in shared and "disposals" in shared
    assert "tackles" not in shared


def test_era_gap_explains_the_missing_statistic():
    con = compare_fixture()
    a = PC.profile(con, 1, _Schema(), with_honours=False)
    b = PC.profile(con, 2, _Schema(), with_honours=False)
    notes = PC.era_gap(a, b, _Schema().stats)
    assert len(notes) == 1
    assert "tackles" in notes[0]
    assert "Modern Player" in notes[0] and "Old Timer" in notes[0]


def test_overlap_separates_teammates_from_opponents():
    con = compare_fixture()
    a = PC.profile(con, 1, _Schema(), with_honours=False)
    b = PC.profile(con, 2, _Schema(), with_honours=False)
    both = PC.overlap(con, a, b, _Schema())
    assert both["against"] == 1        # match 900, different clubs
    assert both["together"] == 0


def test_profile_of_an_unknown_player_is_none():
    con = compare_fixture()
    assert PC.profile(con, 999, _Schema(), with_honours=False) is None


# --------------------------------------------------------------- logos

def logo_dir(tmp_path: Path, names) -> Path:
    for name in names:
        (tmp_path / name).write_bytes(b"<svg/>")
    return tmp_path


def test_logo_matching_is_longest_key_first(tmp_path):
    """'adelaide' is inside 'PortAdelaide'; 'melbourne' inside 'North'."""
    folder = logo_dir(tmp_path, [
        "AdelaideCrows_2024.svg", "PortAdelaideFootballClub_2019.svg",
        "MelbourneFC_2016.svg", "North_Melbourne_FC_logo.svg"])
    clubs = {
        "adelaide": {"name": "Adelaide"},
        "port_adelaide": {"name": "Port Adelaide"},
        "melbourne": {"name": "Melbourne"},
        "north_melbourne": {"name": "North Melbourne"},
    }
    found = CL.resolve(clubs, folder)
    assert found["adelaide"].name == "AdelaideCrows_2024.svg"
    assert found["port_adelaide"].name == "PortAdelaideFootballClub_2019.svg"
    assert found["melbourne"].name == "MelbourneFC_2016.svg"
    assert found["north_melbourne"].name == "North_Melbourne_FC_logo.svg"


def test_a_nickname_filename_still_matches(tmp_path):
    folder = logo_dir(tmp_path, ["GCSuns_2024.svg"])
    found = CL.resolve({"gold_coast": {"name": "Gold Coast",
                                       "nickname": "Suns"}}, folder)
    assert found["gold_coast"].name == "GCSuns_2024.svg"


def test_override_file_wins(tmp_path):
    folder = logo_dir(tmp_path, ["Carlton.svg", "something_odd.svg"])
    (folder / "logos.csv").write_text(
        "club_id,filename\ncarlton,something_odd.svg\n", encoding="utf-8")
    found = CL.resolve({"carlton": {"name": "Carlton"}}, folder)
    assert found["carlton"].name == "something_odd.svg"


def test_unmatched_and_missing_are_reported(tmp_path):
    folder = logo_dir(tmp_path, ["Carlton.svg", "TasmaniaDevils_2024.svg"])
    clubs = {"carlton": {"name": "Carlton"}, "geelong": {"name": "Geelong"}}
    assert [p.name for p in CL.unmatched(clubs, folder)] == \
        ["TasmaniaDevils_2024.svg"]
    assert CL.missing(clubs, folder) == ["geelong"]


def test_no_folder_is_not_an_error(tmp_path):
    assert CL.resolve({"carlton": {"name": "Carlton"}},
                      tmp_path / "nope") == {}


# --------------------------------------------------- hall of fame linking

def hof_index():
    """Two Gary Abletts, as the real player table has."""
    return {
        "gary ablett": [(1, "Gary Ablett", 1982, 1996, 248),
                        (2, "Gary Ablett", 2002, 2020, 357)],
        "solo player": [(3, "Solo Player", 1950, 1960, 100)],
    }


def test_hof_links_on_name_and_career_not_name_alone():
    row = {"name": "Solo Player", "playing_career": "1950-1960"}
    pid, status, method, _n, _notes = HOF._resolve(row, hof_index())
    assert (pid, status) == (3, "unique")
    assert method == "name+career"


def test_hof_suffix_separates_father_and_son():
    elder = {"name": "Gary Ablett Sr.", "playing_career": "1982-1996"}
    younger = {"name": "Gary Ablett Jr.", "playing_career": "2002-2020"}
    assert HOF._resolve(elder, hof_index())[0] == 1
    assert HOF._resolve(younger, hof_index())[0] == 2


def test_hof_wrong_era_is_implausible_not_a_guess():
    row = {"name": "Solo Player", "playing_career": "1990-2000"}
    pid, status, *_ = HOF._resolve(row, hof_index())
    assert pid is None and status == "implausible"


def test_hof_unknown_name_is_unmatched():
    row = {"name": "Nobody At All", "playing_career": "1950-1960"}
    pid, status, *_ = HOF._resolve(row, hof_index())
    assert pid is None and status == "unmatched"


# ------------------------------------------------ team selection linking

def toc_refs():
    index = {
        "ron barassi": [(1, "Ron Barassi", 1936, 1940, 58),
                        (2, "Ron Barassi", 1953, 1969, 254)],
        "bernie smith": [(3, "Bernie Smith", 1948, 1957, 178)],
        "john smith": [(4, "John Smith", 1950, 1955, 40),
                       (5, "John Smith", 1980, 1990, 200)],
    }
    clubs = {1: {"melbourne"}, 2: {"melbourne", "carlton"},
             3: {"geelong"}, 4: {"carlton"}, 5: {"essendon"}}
    return index, clubs


def test_toc_club_disambiguates_a_shared_name():
    index, clubs = toc_refs()
    row = {"name": "John Smith", "club": "Essendon"}
    pid, status, method, *_ = TOC._resolve(row, index, clubs)
    assert (pid, status, method) == (5, "unique", "name+club")


def test_toc_generation_suffix_picks_the_right_one():
    index, clubs = toc_refs()
    elder = TOC._resolve({"name": "Ron Barassi, Sr", "club": ""}, index, clubs)
    younger = TOC._resolve({"name": "Ron Barassi Jr", "club": ""}, index, clubs)
    assert elder[0] == 1 and elder[1] == "resolved"
    assert younger[0] == 2


def test_toc_bare_shared_name_stays_ambiguous():
    """No suffix and no club: guessing the famous one is how you get it
    wrong, so it must not resolve."""
    index, clubs = toc_refs()
    pid, status, *_ = TOC._resolve({"name": "Ron Barassi", "club": ""},
                                   index, clubs)
    assert pid is None and status == "ambiguous"


def test_toc_nickname_in_quotes_is_stripped():
    index = {"graham farmer": [(9, "Graham Farmer", 1962, 1971, 101)]}
    pid, status, *_ = TOC._resolve(
        {"name": "Graham 'Polly' Farmer", "club": ""}, index, {9: {"geelong"}})
    assert (pid, status) == (9, "unique")


def test_toc_single_name_match_is_unique():
    index, clubs = toc_refs()
    pid, status, *_ = TOC._resolve({"name": "Bernie Smith", "club": "Geelong"},
                                   index, clubs)
    assert (pid, status) == (3, "unique")


# ============================================================= live data

def live():
    from data_paths import default_db
    db = default_db("afl")
    if not Path(db).exists():
        return None
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _has(con, table):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone() is not None


def test_live_every_current_club_has_a_logo():
    con = live()
    if con is None or not _has(con, "clubs"):
        pytest.skip("no built database")
    clubs = CL.clubs_from_db(con)
    assert len(clubs) == 18
    assert CL.missing(clubs) == []


def test_live_abletts_link_to_different_players():
    """The suffix rule, on the real data."""
    con = live()
    if con is None or not _has(con, "hall_of_fame"):
        pytest.skip("Hall of Fame not loaded")
    rows = con.execute(
        "SELECT name, player_id FROM hall_of_fame "
        "WHERE name LIKE 'Gary Ablett%' ORDER BY name").fetchall()
    assert len(rows) == 2
    assert rows[0][1] is not None and rows[1][1] is not None
    assert rows[0][1] != rows[1][1]


def test_live_hall_of_fame_shape():
    con = live()
    if con is None or not _has(con, "hall_of_fame"):
        pytest.skip("Hall of Fame not loaded")
    total, legends = con.execute(
        "SELECT COUNT(*), SUM(is_legend) FROM hall_of_fame").fetchone()
    assert total > 300
    assert legends == 34          # as stated by the source article
    # Non-playing categories exist and are not expected to link.
    categories = {r[0] for r in con.execute(
        "SELECT DISTINCT category FROM hall_of_fame")}
    assert {"player", "umpire", "media", "administrator"} <= categories


def test_live_team_selections_shape():
    con = live()
    if con is None or not _has(con, "team_selections"):
        pytest.skip("Team selections not loaded")
    teams = dict(con.execute(
        "SELECT team_name, COUNT(*) FROM team_selections GROUP BY 1"))
    assert len(teams) == 5
    for team, n in teams.items():
        assert 18 <= n <= 30, (team, n)
    # Every team names exactly one captain.
    for team, n in con.execute(
            "SELECT team_name, COUNT(*) FROM team_selections "
            "WHERE role='Captain' GROUP BY 1"):
        assert n == 1, (team, n)


def test_live_no_selection_links_to_two_players():
    con = live()
    if con is None or not _has(con, "team_selections"):
        pytest.skip("Team selections not loaded")
    dupes = con.execute(
        "SELECT team_name, player_id, COUNT(*) FROM team_selections "
        "WHERE player_id IS NOT NULL GROUP BY 1,2 HAVING COUNT(*) > 1"
    ).fetchall()
    assert not dupes, dupes


def run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if "tmp_path" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
                continue
            try:
                fn()
            except Exception as exc:
                if exc.__class__.__name__ == "Skipped":
                    continue
                raise
    print("awards and logos tests: passed")


if __name__ == "__main__":
    run()
