#!/usr/bin/env python3
"""One-off migration: mlb_player_rivalry_games.game_date to ISO.

The Retrosheet loader used to store the game logs' compact ``YYYYMMDD``
text; it now writes ISO (load_retrosheet.iso_game_date), mlb/sport.py
declares the column a date, and this rewrites the rows already stored so
existing databases agree with both. Idempotent; a compact value that is
not a real calendar date is deleted and listed, never rewritten into a
pretend date. Also creates the (game_date, player_id) index the loader
now declares.

Usage:
    python -m utils.mlb.normalize_rivalry_dates --dry-run
    python -m utils.mlb.normalize_rivalry_dates
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from data_paths import default_db  # noqa: E402  (needs the path above)
from utils.mlb.load_retrosheet import iso_game_date  # noqa: E402

TABLE = "mlb_player_rivalry_games"
COMPACT = "[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"


def run(db_path: str, *, dry_run: bool = False) -> int:
    con = sqlite3.connect(db_path)
    try:
        if con.execute("SELECT 1 FROM sqlite_master WHERE name = ?",
                       (TABLE,)).fetchone() is None:
            print(f"No {TABLE} table; nothing to do.")
            return 0

        bad = []
        for (value,) in con.execute(
                f"SELECT DISTINCT game_date FROM {TABLE} "
                f"WHERE game_date GLOB '{COMPACT}'").fetchall():
            if iso_game_date(value) is None:
                bad.append(value)
        for value in bad:
            gone = con.execute(
                f"DELETE FROM {TABLE} WHERE game_date = ?",
                (value,)).rowcount
            print(f"  deleted {gone} rows with impossible date {value!r}")

        changed = con.execute(
            f"UPDATE {TABLE} SET game_date = "
            f"substr(game_date, 1, 4) || '-' || substr(game_date, 5, 2) "
            f"|| '-' || substr(game_date, 7, 2) "
            f"WHERE game_date GLOB '{COMPACT}'").rowcount
        print(f"{TABLE}: {changed:,} rows converted to ISO")

        remaining = []
        for (value,) in con.execute(f"SELECT DISTINCT game_date FROM {TABLE}"):
            text = str(value)
            round_trip = (iso_game_date(text.replace("-", ""))
                          if len(text) == 10 else None)
            if round_trip != text:
                remaining.append(text)
        if remaining:
            print(f"error: unparseable dates remain: {remaining[:5]}",
                  file=sys.stderr)
            con.rollback()
            return 1

        con.execute(
            f"CREATE INDEX IF NOT EXISTS ix_rivalry_date "
            f"ON {TABLE}(game_date, player_id)")
        if dry_run:
            con.rollback()
            print("--dry-run: nothing written")
            return 0
        con.commit()
        print("verified: every stored game_date is ISO")
    finally:
        con.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Normalize MLB rivalry game dates to ISO.")
    parser.add_argument("--db", default=default_db("mlb"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not Path(args.db).exists():
        parser.error(f"no database at {args.db}")
    return run(str(args.db), dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
