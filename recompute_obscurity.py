#!/usr/bin/env python3
"""recompute_obscurity.py -- Rescore the obscurity column in place.

Obscurity is derived entirely from columns the players table already holds
(career games, span and whatever else the sport's model names), so a change
to the formula does not need the multi-minute rebuild that build_db.py
performs from the raw source. This reads the table, calls the one
authoritative scorer, and writes the column back.

    python recompute_obscurity.py                 # rescore the AFL database
    python recompute_obscurity.py --dry-run       # report the change only
    python recompute_obscurity.py --sport nba     # rescore the NBA database
    python recompute_obscurity.py --db other.db

The formula itself lives in obscurity.py and is not duplicated here -- a
second copy is how the rebuilt database and the rescored one start
disagreeing. Which terms apply is a property of the sport, read from
sports.Sport.obscurity_model, so this script never has to know that the AFL
scores Brownlow votes and the NBA scores minutes.
"""

import argparse
import sqlite3
import sys

import pandas as pd

import obscurity as obscurity_module
import sports
from data_paths import default_db


def needed_columns(model):
    """
    Every column the model reads, plus the key.

    Selected explicitly rather than with SELECT * so a formula that grows a
    new input fails loudly here instead of quietly scoring against NaN.
    """
    return ["player_id", *model.required_columns()]


def recompute(db, model=None, dry_run=False, verbose=True):
    """Rescore `db` against `model`. Returns (rows, changed, biggest_move)."""
    if model is None:
        model = obscurity_module.AFL_MODEL
    NEEDED = needed_columns(model)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(players)")}
        missing = [c for c in NEEDED if c not in have]
        if missing:
            raise SystemExit(
                f"players table is missing {', '.join(missing)} -- either "
                "this database predates the current schema, or it belongs "
                "to a different sport than --sport says.")

        players = pd.read_sql(
            f"SELECT {', '.join(NEEDED)}, obscurity FROM players", con)
        if players.empty:
            raise SystemExit("players table is empty -- build it first.")

        components = obscurity_module.components(players, model)
        players["new_obscurity"] = components["obscurity"]
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

        # A database built by an older version has the score column but not
        # the component columns; add them rather than making the user rebuild.
        have = {r[1] for r in con.execute("PRAGMA table_info(players)")}
        columns = [c for c in components.columns if c != "obscurity"]
        for column in columns:
            if column not in have:
                kind = "INTEGER" if column == "obscurity_model" else "REAL"
                con.execute(f"ALTER TABLE players ADD COLUMN {column} {kind}")

        assignments = ", ".join(f"{c} = ?" for c in ["obscurity", *columns])
        con.executemany(
            f"UPDATE players SET {assignments} WHERE player_id = ?",
            [(float(row.obscurity),
              *(float(getattr(row, c)) for c in columns),
              int(pid))
             for pid, row in zip(players.player_id,
                                 components.itertuples(index=False))])
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
    ap.add_argument("--sport", default="afl",
                    choices=sorted(sports.SPORTS),
                    help="Which sport's model and database to use.")
    ap.add_argument("--db", default=None,
                    help="Database file. Defaults to the sport's own.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change without writing.")
    a = ap.parse_args(argv)
    sport = sports.get(a.sport)
    if sport.obscurity_model is None:
        raise SystemExit(
            f"{sport.label} declares no obscurity model, so there is "
            "nothing to rescore against.")
    recompute(a.db or default_db(a.sport), model=sport.obscurity_model,
              dry_run=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
