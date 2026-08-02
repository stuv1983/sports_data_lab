#!/usr/bin/env python3
"""
make_sql.py -- Write one .sql file per square of a grid.

Edit ROWS and COLS below to match the day's grid, then:

    python make_sql.py

...which writes sql/cell_r1c1.sql etc. Run any of them with:

    sqlite3 gridley.db ".read sql/cell_r1c1.sql"

Or run the whole board at once:

    sqlite3 gridley.db ".read sql/run_all.sql"
"""

# Run standalone from anywhere: this file lives at the project root.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import os
import constraints as C

# ---- Gridley #1106 ---------------------------------------------------
COLS = [
    ("St Kilda",          C.played_for("St Kilda")),
    ("North Melbourne",   C.played_for("North Melbourne")),
    ("150+ games played", C.career_games_min(150)),
]

ROWS = [
    ("30+ goals two diff clubs", C.goals_at_multiple_clubs(30, 2)),
    ("Mason Wood teammate",      C.teammate_of("Mason Wood")),
    ("No finals wins",           C.no_finals_wins()),
]
# ----------------------------------------------------------------------

LIMIT = 25
OUT = "sql"


def main():
    os.makedirs(OUT, exist_ok=True)
    runner = []

    for ri, (rlab, rcon) in enumerate(ROWS, 1):
        for ci, (clab, ccon) in enumerate(COLS, 1):
            name = f"cell_r{ri}c{ci}.sql"
            body = C.to_standalone_sql([rcon, ccon], LIMIT)
            header = (f"-- Gridley square R{ri}C{ci}\n"
                      f"-- {rlab}  x  {clab}\n"
                      f"-- Most obscure first.\n\n"
                      f".mode column\n.headers on\n\n")
            with open(os.path.join(OUT, name), "w") as f:
                f.write(header + body + "\n")
            runner.append((ri, ci, rlab, clab, name))
            print(f"wrote {OUT}/{name}   {rlab} x {clab}")

    # .read resolves relative to sqlite3's working directory, not to the
    # file doing the reading. Absolute paths remove that ambiguity so
    # run_all.sql works from anywhere.
    absdir = os.path.abspath(OUT)
    with open(os.path.join(OUT, "run_all.sql"), "w") as f:
        f.write("-- Runs every square in order.\n"
                "-- Paths are absolute: .read resolves against sqlite3's\n"
                "-- working directory, not this file's location.\n"
                ".mode column\n.headers on\n\n")
        for ri, ci, rlab, clab, name in runner:
            f.write(f".print ''\n")
            f.write(f".print '=== R{ri}C{ci}  {rlab} x {clab} ==='\n")
            f.write(f".read {os.path.join(absdir, name)}\n")
    print(f"\nwrote {OUT}/run_all.sql (absolute paths)")
    print(f'Run with:  sqlite3 gridley.db ".read {absdir}/run_all.sql"')


if __name__ == "__main__":
    main()
