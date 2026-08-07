#!/usr/bin/env python3
"""Small fixture test for the non-download database repair."""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---


import sqlite3
import tempfile
from pathlib import Path

from utils.shared import repair_database


def run():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "fixture.db"
        con = sqlite3.connect(path)
        con.executescript("""
          CREATE TABLE players (
            player_id INTEGER, player TEXT, n_clubs INTEGER, clubs_hist TEXT
          );
          CREATE TABLE games (
            player_id INTEGER, season INTEGER, date TEXT, career_game_no INTEGER,
            club_hist TEXT, club_now TEXT, result TEXT, points_for REAL,
            points_against REAL, is_final INTEGER
          );
          CREATE TABLE meta (key TEXT, value TEXT);
          INSERT INTO players VALUES (1,'Journey',3,'A|B|C');
          INSERT INTO games VALUES
            (1,2000,'2000-01-01',1,'A','A','W',100,50,0),
            (1,2001,'2001-01-01',2,'B','B','L',50,70,0),
            (1,2002,'2002-01-01',3,'A','A','W',80,60,0),
            (1,2003,'2003-01-01',4,'C','C','L',40,90,0);
          INSERT INTO players VALUES (2,'Other',1,'D');
          INSERT INTO games VALUES
            (2,2000,'2000-01-01',1,'D','D','L',50,100,0),
            (2,2001,'2001-01-01',2,'D','D','W',70,50,0),
            (2,2002,'2002-01-01',3,'D','D','L',60,80,0),
            (2,2003,'2003-01-01',4,'D','D','W',90,40,0);
        """)
        con.close()
        repair_database.run(str(path))
        con = sqlite3.connect(path)
        assert con.execute(
            "SELECT club_path_hist FROM players WHERE player_id=1"
        ).fetchone()[0] == "A|B|A|C"
        assert not con.execute(
            "SELECT season FROM team_seasons GROUP BY season "
            "HAVING SUM(wooden_spoon) <> 1"
        ).fetchall()
        con.close()
    print("database repair tests: passed")


if __name__ == "__main__":
    run()
