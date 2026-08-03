#!/usr/bin/env python3
"""Measure and record the era each statistic actually covers.

This is the table that lets every other layer distinguish "did not happen"
from "was not recorded". A square reading "30+ frees against in a season"
that returns nobody before 1965 must be able to say so, otherwise the
absence reads as a claim about the players rather than about the source.

Values are measured from the built database, never hand-authored:

    python load_stat_coverage.py
    python load_stat_coverage.py --db data/afl/afl.db --dry-run

`available_from` is the first season the column carries a non-null,
non-zero value, matching health.stat_era_starts. A stat that is a column on
`games` but is empty end to end is recorded with NULL bounds and a note,
rather than omitted: "loaded but never populated" is a different fact from
"not a column at all", and only the first one means a mapping in
build_db.py's HEADER_MAP is silently doing nothing.

Re-run after every rebuild. The in-code `Sport.stat_eras` dict in sports.py
remains the zero-cost fast path for the UI caption; this table is the
auditable record of where those numbers came from and is what later layers
should read once they need per-competition or per-source detail.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys

SCHEMA = """
CREATE TABLE IF NOT EXISTS stat_coverage (
    stat_name      TEXT NOT NULL,
    competition    TEXT NOT NULL DEFAULT 'AFL/VFL',
    available_from INTEGER,
    available_to   INTEGER,
    source         TEXT,
    coverage_notes TEXT,
    PRIMARY KEY (stat_name, competition)
)
"""


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def measure(con: sqlite3.Connection, stats: list[str]) -> list[tuple]:
    """One row per stat: (stat, first_season, last_season, non_null, notes)."""
    cols = set(columns(con, "games"))
    total = con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    out = []
    for stat in stats:
        if stat not in cols:
            out.append((stat, None, None, 0,
                        "not a column on games -- check build_db.HEADER_MAP"))
            continue
        first, last, non_null = con.execute(
            f"SELECT MIN(CASE WHEN {stat} IS NOT NULL AND {stat} != 0 "
            f"          THEN season END), "
            f"       MAX(CASE WHEN {stat} IS NOT NULL AND {stat} != 0 "
            f"          THEN season END), "
            f"       SUM({stat} IS NOT NULL) FROM games"
        ).fetchone()
        non_null = non_null or 0
        if first is None:
            note = ("column present but never populated -- mapped in "
                    "HEADER_MAP yet carrying no value; do not advertise")
        else:
            pct = 100 * non_null / total if total else 0
            note = f"{non_null:,} of {total:,} player-games populated ({pct:.1f}%)"
        out.append((stat, first, last, non_null, note))
    return out


def load(con: sqlite3.Connection, stats: list[str], source: str,
         competition: str = "AFL/VFL") -> list[tuple]:
    con.execute(SCHEMA)
    rows = measure(con, stats)
    now = dt.datetime.now().isoformat(timespec="seconds")
    for stat, first, last, _non_null, note in rows:
        con.execute(
            "INSERT INTO stat_coverage "
            "(stat_name, competition, available_from, available_to, "
            " source, coverage_notes) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(stat_name, competition) DO UPDATE SET "
            "  available_from=excluded.available_from, "
            "  available_to=excluded.available_to, "
            "  source=excluded.source, "
            "  coverage_notes=excluded.coverage_notes",
            (stat, competition, first, last, f"{source} (measured {now})", note))
    con.commit()
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None,
                    help="database path (default: data_paths.default_db('afl'))")
    ap.add_argument("--sport", default="afl")
    ap.add_argument("--dry-run", action="store_true",
                    help="measure and print, write nothing")
    args = ap.parse_args()

    db = args.db
    if db is None:
        from data_paths import default_db
        db = default_db(args.sport)

    import sports
    sport = sports.get(args.sport)
    stats = list(sport.schema.stats)

    if args.dry_run:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = measure(con, stats)
    else:
        con = sqlite3.connect(db)
        rows = load(con, stats, source="AFL Tables via build_db.py")

    print(f"database: {db}")
    print(f"{'stat':<18} {'from':>6} {'to':>6}  notes")
    print("-" * 78)
    empty = []
    for stat, first, last, _non_null, note in sorted(
            rows, key=lambda r: (r[1] is None, r[1] or 0)):
        print(f"{stat:<18} {str(first):>6} {str(last):>6}  {note}")
        if first is None:
            empty.append(stat)
    if empty:
        print(f"\nNOT POPULATED -- keep out of the UI: {', '.join(empty)}")

    # The UI caption reads sports.Sport.stat_eras, so a drift between the
    # in-code dict and the measured table is a caption that lies.
    drift = [(s, sport.stat_eras.get(s), f)
             for s, f, _l, _n, _note in rows
             if f is not None and sport.stat_eras.get(s) not in (None, f)]
    if drift:
        print("\nDRIFT -- sports.py stat_eras disagrees with the data:")
        for stat, coded, measured in drift:
            print(f"  {stat:<18} coded={coded}  measured={measured}")
        print("  Update sports.py, or the era caption will misstate coverage.")
    missing = [s for s, f, _l, _n, _note in rows
               if f is not None and s not in sport.stat_eras]
    if missing:
        print(f"\nNo stat_eras entry (no era caption will show): "
              f"{', '.join(missing)}")

    if not args.dry_run:
        print(f"\nwrote {len(rows)} rows to stat_coverage")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
