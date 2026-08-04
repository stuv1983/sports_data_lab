#!/usr/bin/env python3
"""health.py must serve a second sport without changing what it says about
the first.

Every core probe grew an optional `schema` argument. The regression risk is
entirely in the default path: an AFL report that quietly changes shape would
be missed, because nothing else reads these numbers. So the first test here
runs both call styles against the same AFL-shaped database and demands they
be identical.

career_totals_reconcile is new. It is the check that catches an aggregate
disagreeing with the rows it was built from -- a failure mode where the
stored number stays plausible and is simply wrong, which is exactly the kind
that survives review.
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

import health
import sports

AFL = sports.AFL_SCHEMA
NBA = sports.NBA_SCHEMA


AFL_FIXTURE = """
CREATE TABLE players (
    player_id INTEGER, player TEXT, name_key TEXT, birth_year INTEGER,
    debut_season INTEGER, final_season INTEGER, career_games INTEGER,
    career_goals INTEGER, finals_played INTEGER, n_clubs INTEGER,
    clubs_hist TEXT, obscurity REAL);
CREATE TABLE games (
    player_id INTEGER, season INTEGER, date TEXT, round TEXT, venue TEXT,
    club_now TEXT, club_hist TEXT, opponent TEXT, career_game_no INTEGER,
    goals INTEGER, is_final INTEGER, result TEXT);

INSERT INTO players VALUES
  (1,'Ada Kerr','ada kerr',1980,2000,2002,3,7,1,1,'Carlton',55.0),
  (2,'Bo Nash','bo nash',1975,1995,1995,1,0,0,1,'Fitzroy',92.0);
INSERT INTO games VALUES
  (1,2000,'2000-04-01','1','M.C.G.','Carlton','Carlton','Essendon',1,3,0,'W'),
  (1,2001,'2001-04-01','1','M.C.G.','Carlton','Carlton','Essendon',2,4,0,'L'),
  (1,2002,'2002-09-01','GF','M.C.G.','Carlton','Carlton','Essendon',3,0,1,'L'),
  (2,1995,'1995-05-01','5','Princes Park','Fitzroy','Fitzroy','Carlton',1,0,0,'L');
"""


@pytest.fixture
def afl_con():
    con = sqlite3.connect(":memory:")
    con.executescript(AFL_FIXTURE)
    yield con
    con.close()


@pytest.fixture
def nba_con(nba_db):
    con = sqlite3.connect(f"file:{nba_db}?mode=ro", uri=True)
    yield con
    con.close()


# ------------------------------------------------- the AFL default path

def test_passing_no_schema_is_identical_to_passing_the_afl_one(afl_con):
    """The regression guard for the whole parameterisation."""
    assert health.collect(afl_con) == health.collect(afl_con, AFL)


def test_the_afl_report_still_has_every_section(afl_con):
    report = health.collect(afl_con)
    assert set(report) == {
        "core", "tables", "links", "untrusted", "rising_star", "stat_eras",
        "stat_coverage", "match_coverage", "inventory", "per_season",
        "file", "warnings", "meta"}
    assert report["core"]["players"] == 2
    assert report["core"]["player_games"] == 4
    assert report["core"]["season_min"] == 1995
    assert report["inventory"]["finals"] == 1
    assert report["warnings"] == []


# ------------------------------------------------------- the NBA path

def test_the_nba_report_is_clean_and_populated(nba_con):
    report = health.collect(nba_con, NBA)
    assert report["core"]["players"] > 0
    assert report["core"]["player_games"] > 0
    assert report["warnings"] == []
    assert report["inventory"]["clubs"] > 0


def test_the_nba_report_counts_playoff_games_as_the_postseason(nba_con):
    """inventory reads schema.is_final, which the NBA calls is_playoff."""
    report = health.collect(nba_con, NBA)
    expected = nba_con.execute(
        "SELECT COUNT(*) FROM games WHERE is_playoff = 1").fetchone()[0]
    assert report["inventory"]["finals"] == expected


def test_nba_stat_eras_are_measured_from_the_nba_stat_list(nba_con):
    eras = dict(health.stat_era_starts(nba_con, schema=NBA))
    assert set(eras) <= set(NBA.stats)
    assert "disposals" not in eras          # never guess at the AFL's list
    assert eras["points"] == 1971


def test_afl_only_layers_report_absent_rather_than_raising(nba_con):
    """Every optional probe is table-gated, so an NBA database is honest."""
    report = health.collect(nba_con, NBA)
    assert all(entry["state"] == "not loaded" for entry in report["links"])
    assert report["rising_star"] == {} or "state" in report["rising_star"]


# ------------------------------------------------ career reconciliation

def test_reconciliation_passes_on_a_consistent_database(afl_con):
    assert health.career_totals_reconcile(afl_con, AFL) == []


def test_a_wrong_career_games_is_reported(afl_con):
    afl_con.execute("UPDATE players SET career_games = 99 WHERE player_id = 1")
    found = health.career_totals_reconcile(afl_con, AFL)
    assert any("career games" in w for w in found), found


def test_a_wrong_career_score_is_reported(afl_con):
    afl_con.execute("UPDATE players SET career_goals = 99 WHERE player_id = 1")
    found = health.career_totals_reconcile(afl_con, AFL)
    assert any("goals" in w for w in found), found


def test_a_wrong_postseason_count_is_reported(afl_con):
    afl_con.execute("UPDATE players SET finals_played = 9 WHERE player_id = 1")
    found = health.career_totals_reconcile(afl_con, AFL)
    assert any("post-season" in w for w in found), found


def test_reconciliation_failures_surface_as_integrity_warnings(afl_con):
    afl_con.execute("UPDATE players SET career_games = 99 WHERE player_id = 1")
    assert health.integrity_warnings(afl_con, AFL) != []


def test_a_null_career_column_is_not_a_disagreement(afl_con):
    """A stat that predates its era must not be reported as a mismatch."""
    afl_con.execute("UPDATE players SET career_goals = NULL")
    assert health.career_totals_reconcile(afl_con, AFL) == []


# --------------------------------------------------- duplicate detection

def test_duplicate_detection_keys_on_the_match_when_there_is_one(nba_con):
    """A date alone cannot separate the two legs of a doubleheader."""
    assert health.integrity_warnings(nba_con, NBA) == []
    con = sqlite3.connect(":memory:")
    nba_con.backup(con)
    con.execute("INSERT INTO games SELECT * FROM games LIMIT 1")
    assert any("duplicate" in w for w in health.integrity_warnings(con, NBA))


def test_an_orphan_game_row_is_reported(afl_con):
    afl_con.execute(
        "INSERT INTO games VALUES (99,2001,'2001-05-01','2','M.C.G.',"
        "'Carlton','Carlton','Essendon',1,0,0,'W')")
    assert any("no matching player" in w
               for w in health.integrity_warnings(afl_con, AFL))


def main():
    import subprocess
    return subprocess.call([_sys.executable, "-m", "pytest", __file__, "-q"])


if __name__ == "__main__":
    _sys.exit(main())
