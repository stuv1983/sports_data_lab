#!/usr/bin/env python3
"""The new grid builders' arithmetic, on a hand-checkable fixture.

Venue tenure counts games at the ground, not appearances; the finals
career total sums recorded finals only (NULL is an unrecorded game, not
a zero); a decade is its ten seasons inclusive; a grand-final feat needs
the feat, not the appearance.
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


@pytest.fixture()
def con():
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE players (
          player_id INTEGER, player TEXT, debut_season INTEGER,
          final_season INTEGER, career_games INTEGER, career_goals INTEGER,
          finals_played INTEGER, clubs_hist TEXT, obscurity REAL
        );
        CREATE TABLE games (
          player_id INTEGER, player TEXT, season INTEGER, round TEXT,
          opponent TEXT, club_now TEXT, club_hist TEXT, venue TEXT,
          career_game_no INTEGER, goals REAL, disposals REAL,
          is_final INTEGER, result TEXT
        );
        INSERT INTO players VALUES
          (1,'Tenure',2005,2015,4,20,3,'A',50),
          (2,'Visitor',2009,2012,2,3,1,'A',60),
          (3,'Prewar',1930,1939,1,2,1,'B',70);
        INSERT INTO games VALUES
          -- Tenure: three games at the M.C.G., one elsewhere; finals goals
          -- 4 + 5, plus a finals game whose goals were never recorded.
          (1,'Tenure',2005,'1','B','A','A','M.C.G.',1,1,10,0,'W'),
          (1,'Tenure',2010,'QF','B','A','A','M.C.G.',2,4,12,1,'W'),
          (1,'Tenure',2011,'GF','B','A','A','M.C.G.',3,5,15,1,'L'),
          (1,'Tenure',2015,'PF','B','A','A','S.C.G.',4,NULL,NULL,1,'W'),
          -- Visitor: one M.C.G. appearance, a 2-goal grand final.
          (2,'Visitor',2009,'1','A','A','A','M.C.G.',1,1,8,0,'L'),
          (2,'Visitor',2012,'GF','A','A','A','M.C.G.',2,2,9,1,'W'),
          -- Prewar: a 1930s final with goals recorded, nothing since.
          (3,'Prewar',1935,'GF','A','B','B','M.C.G.',1,2,NULL,1,'L');
    """)
    yield con
    con.close()


G = core.Generic(core.Schema(stats=("goals", "disposals"),
                             venue_aliases={"mcg": "M.C.G."}))


def ids(con, built):
    sql, params = built
    return {row[0] for row in con.execute(sql, params)}


def test_venue_tenure_counts_games_not_appearances(con):
    assert ids(con, G.games_at_venue_min("M.C.G.", 3)) == {1}
    assert ids(con, G.games_at_venue_min("M.C.G.", 1)) == {1, 2, 3}
    # The alias resolves the same way every venue builder resolves it.
    assert ids(con, G.games_at_venue_min("MCG", 3)) == {1}


def test_finals_career_total_sums_recorded_finals_only(con):
    # Tenure's recorded finals goals are 4+5=9; the NULL final is not a 0.
    assert ids(con, G.postseason_stat_total_min("goals", 9)) == {1}
    assert ids(con, G.postseason_stat_total_min("goals", 10)) == set()
    assert ids(con, G.postseason_stat_total_min("goals", 2)) == {1, 2, 3}


def test_a_decade_is_its_ten_seasons_inclusive(con):
    assert ids(con, G.played_in_decade(2010)) == {1, 2}
    assert ids(con, G.played_in_decade(1930)) == {3}
    # Any year inside the decade names it.
    assert ids(con, G.played_in_decade(2015)) == {1, 2}
    assert ids(con, G.played_in_decade(1990)) == set()


def test_a_grand_final_feat_needs_the_feat(con):
    from afl import constraints as C

    assert ids(con, C.stat_in_a_grand_final("goals", 3)) == {1}
    assert ids(con, C.stat_in_a_grand_final("goals", 2)) == {1, 2, 3}
    with pytest.raises(ValueError):
        C.stat_in_a_grand_final("hacks", 1)
