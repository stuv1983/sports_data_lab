#!/usr/bin/env python3
"""recompute_obscurity.py -- Rescore the obscurity column in place.

Obscurity is derived entirely from columns the players table already holds
(career games, span, goals, Brownlow votes, finals, final season), so a
change to the formula does not need the multi-minute rebuild that
build_db.py performs from the raw source. This reads the table, calls the
one authoritative scorer in build_db.py, and writes the column back.

    python recompute_obscurity.py                 # rescore the AFL database
    python recompute_obscurity.py --dry-run       # report the change only
    python recompute_obscurity.py --db other.db

The formula itself lives in build_db.obscurity_score and is not duplicated
here -- a second copy is how the rebuilt database and the rescored one
start disagreeing.
"""

import argparse
import sqlite3
import sys

import pandas as pd

from build_db import obscurity_score
from data_paths import default_db

#: Everything obscurity_score reads. Selected explicitly so a formula that
#: grows a new input fails loudly here instead of scoring against NaN.
NEEDED = ["player_id", "career_games", "career_goals", "career_brownlow",
          "finals_played", "debut_season", "final_season"]


def recompute(db, dry_run=False, verbose=True):
    """Rescore `db`. Returns (rows, changed, biggest_move)."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(players)")}
        missing = [c for c in NEEDED if c not in have]
        if missing:
            raise SystemExit(
                f"players table is missing {', '.join(missing)} -- "
                "this database predates the current schema.")

        players = pd.read_sql(
            f"SELECT {', '.join(NEEDED)}, obscurity FROM players", con)
        if players.empty:
            raise SystemExit("players table is empty -- run build_db.py first.")

        players["new_obscurity"] = obscurity_score(players)
        delta = (players.new_obscurity - players.obscurity.fillna(0)).abs()
        changed = int((delta > 0.05).sum())

        if verbose:
            old, new = players.obscurity, players.new_obscurity
            print(f"{len(players):,} players")
            print(f"  old  min {old.min():5.1f}  median {old.median():5.1f}  "
                  f"max {old.max():5.1f}")
            print(f"  new  min {new.min():5.1f}  median {new.median():5.1f}  "
                  f"max {new.max():5.1f}")
            print(f"  {changed:,} scores change by more than 0.05 "
                  f"(largest move {delta.max():.1f})")

        if dry_run:
            if verbose:
                print("\n--dry-run: nothing written.")
            return len(players), changed, float(delta.max())

        con.executemany(
            "UPDATE players SET obscurity = ? WHERE player_id = ?",
            [(float(s), int(pid)) for pid, s
             in zip(players.player_id, players.new_obscurity)])
        con.commit()
        if verbose:
            print("\nWritten. Restart the app to pick up the new scores "
                  "(its caches key on the database file's revision).")
        return len(players), changed, float(delta.max())
    finally:
        con.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=default_db("afl"))
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change without writing.")
    a = ap.parse_args(argv)
    recompute(a.db, dry_run=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
