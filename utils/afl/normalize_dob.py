#!/usr/bin/env python3
"""One-off migration: players.dob and games.dob to ISO ``YYYY-MM-DD``.

The columns mixed AFL Tables' "30-Jan-1987" spelling with ISO (see
utils/afl/dob.py for how that happened and which loaders now prevent
it). This rewrites what is already stored so afl/sport.py can truthfully
declare both columns as dates. Idempotent: a second run finds nothing
left to change. Values that are not real dates become NULL and are
listed, never silently kept as text or silently discarded unreported.

Usage:
    python -m utils.afl.normalize_dob --dry-run
    python -m utils.afl.normalize_dob
    python -m utils.afl.normalize_dob --db path/to/afl.db
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
from utils.afl.dob import canonical_dob  # noqa: E402

TABLES = ("players", "games")


def normalize_table(con: sqlite3.Connection, table: str) -> dict:
    """Rewrite one table's dob column in place. Returns the tally.

    Works over the distinct values, not the rows: games holds ~700k rows
    but only ~900 distinct dobs, and `UPDATE ... WHERE dob = ?` per
    value keeps the whole pass to a couple of seconds.
    """
    tally = {"rows": 0, "already_iso": 0, "converted": 0, "nulled": []}
    for (value,) in con.execute(
            f"SELECT DISTINCT dob FROM {table} "
            f"WHERE dob IS NOT NULL").fetchall():
        canonical = canonical_dob(value)
        if canonical == value:
            tally["already_iso"] += 1
            continue
        changed = con.execute(
            f"UPDATE {table} SET dob = ? WHERE dob = ?",
            (canonical, value)).rowcount
        tally["rows"] += changed
        if canonical is None:
            tally["nulled"].append((value, changed))
        else:
            tally["converted"] += 1
    return tally


def verify(con: sqlite3.Connection, table: str) -> int:
    """How many stored values are still not canonical ISO. Must be 0."""
    return sum(
        1 for (value,) in con.execute(
            f"SELECT DISTINCT dob FROM {table} WHERE dob IS NOT NULL")
        if canonical_dob(value) != value)


def run(db_path: str, *, dry_run: bool = False) -> int:
    con = sqlite3.connect(db_path)
    try:
        for table in TABLES:
            if con.execute("SELECT 1 FROM sqlite_master WHERE name = ?",
                           (table,)).fetchone() is None:
                print(f"{table}: not in this database, skipped")
                continue
            tally = normalize_table(con, table)
            print(f"{table}: {tally['converted']} spellings converted "
                  f"({tally['rows']:,} rows), "
                  f"{tally['already_iso']} already ISO")
            for value, count in tally["nulled"]:
                print(f"  NULLed unparseable {value!r} ({count} rows)")
        if dry_run:
            con.rollback()
            print("--dry-run: nothing written")
            return 0
        con.commit()
        remaining = {
            table: verify(con, table) for table in TABLES
            if con.execute("SELECT 1 FROM sqlite_master WHERE name = ?",
                           (table,)).fetchone()}
        if any(remaining.values()):
            print(f"error: non-ISO values remain: {remaining}",
                  file=sys.stderr)
            return 1
        print("verified: every stored dob is ISO or NULL")
    finally:
        con.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Normalize AFL dob columns to ISO dates.")
    parser.add_argument("--db", default=default_db("afl"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not Path(args.db).exists():
        parser.error(f"no database at {args.db}")
    return run(str(args.db), dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
