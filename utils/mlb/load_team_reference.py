#!/usr/bin/env python3
"""Team reference fields for the MLB Team Explorer.

    python -m utils.mlb.load_team_reference --dir C:\\sample\\mlb

Fills ``club_wikipedia_fields`` from Lahman's own team files, which the
build already downloads but only ever read for player statistics.
derive_club_tables.py creates that table for every sport and could only
fill it where a source had been loaded, so the MLB's was empty and its Team
Explorer showed four dashes.

Everything here is aggregated from ``Teams.csv``, one row per team per
season since 1871:

* **Founded** -- the franchise's first season in the file.
* **Ballpark** -- the park named on its most recent season, so a club that
  has moved shows where it plays now rather than where it started.
* **World Series** -- a straight count of seasons with ``WSWin='Y'``.

That last one is worth stating plainly: it is counted from a column that
records the result, not inferred from who won some final game. Lahman marks
125 World Series wins across every franchise and gives the Yankees 27,
which is the published figure. The NBA loader deliberately does not do the
equivalent, because its source does not label its Finals reliably; here the
answer is in the data.

Franchises, not seasons
-----------------------

``Teams.csv`` is keyed by season and its ``teamID`` changes when a club
moves or renames, so aggregating on it would split the Dodgers into
Brooklyn and Los Angeles. ``franchID`` is stable across both and is what
``TeamsFranchises.csv`` names, so it is the key used throughout -- the same
choice mlb_reference.RIVALRIES already makes.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from collections import defaultdict
from pathlib import Path

import club_reference as CR
from data_paths import sport_db

SOURCE_LABEL = "Lahman Teams.csv"

DEFAULT_DIR = Path(r"C:\sample\mlb")


def _franchise_names(folder: Path) -> dict[str, str]:
    """franchID -> the franchise's current name, from TeamsFranchises.csv."""
    path = folder / "TeamsFranchises.csv"
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return {row["franchID"].strip(): (row.get("franchName") or "").strip()
                for row in csv.DictReader(handle) if row.get("franchID")}


def _aggregate(folder: Path) -> dict[str, dict]:
    """One summary per franchise, from every season row it has."""
    path = folder / "Teams.csv"
    summary: dict[str, dict] = defaultdict(
        lambda: {"first": None, "last": None, "name": "", "park": "",
                 "ws": 0, "pennants": 0, "seasons": 0})

    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            key = (row.get("franchID") or "").strip()
            if not key:
                continue
            try:
                year = int(row["yearID"])
            except (KeyError, ValueError):
                continue

            item = summary[key]
            item["seasons"] += 1
            if item["first"] is None or year < item["first"]:
                item["first"] = year
            # The most recent season supplies the name and the ballpark, so
            # a relocated club shows where it plays now.
            if item["last"] is None or year >= item["last"]:
                item["last"] = year
                item["name"] = (row.get("name") or "").strip()
                item["park"] = (row.get("park") or "").strip()
            if (row.get("WSWin") or "").strip().upper() == "Y":
                item["ws"] += 1
            if (row.get("LgWin") or "").strip().upper() == "Y":
                item["pennants"] += 1
    return dict(summary)


def load(con: sqlite3.Connection, folder: Path) -> tuple[int, int]:
    """Fill club_wikipedia_fields. Returns (clubs matched, fields written)."""
    by_name = CR.club_ids_by_name(con)
    franchises = _franchise_names(folder)
    summary = _aggregate(folder)

    # Resolve every franchise to a club first, because several defunct ones
    # share a name with a modern club: six different franchises have been
    # called the Washington Nationals, and 'Baltimore Orioles',
    # 'Cincinnati Reds' and 'Milwaukee Brewers' each name a 19th-century
    # club as well as a current one. Writing them in file order let a
    # franchise that folded in 1884 overwrite the modern club's row, and
    # because blank values are skipped the result was a mixture of the two.
    # The franchise still playing -- the one with the latest final season --
    # is the one the Team Explorer is asking about.
    best: dict[str, tuple[str, dict]] = {}
    unmatched = []
    for key, item in summary.items():
        # The club's own last-season name first, then the franchise name:
        # `clubs` is built from the database, whose club names come from the
        # season rows rather than from TeamsFranchises.
        club_id = None
        for candidate in (item["name"], franchises.get(key, "")):
            if candidate and candidate in by_name:
                club_id = by_name[candidate]
                break
        if not club_id:
            unmatched.append(item["name"] or key)
            continue
        current = best.get(club_id)
        if current is None or (item["last"] or 0) > (current[1]["last"] or 0):
            best[club_id] = (key, item)

    rows, matched = [], set(best)
    for club_id, (key, item) in best.items():
        values = [
            ("nickname", "Nickname", item["name"].split()[-1]
             if item["name"] else ""),
            ("founded", "First season", item["first"]),
            ("ground", "Ballpark", item["park"]),
            ("premierships", "World Series", item["ws"] or ""),
            ("pennants", "Pennants", item["pennants"] or ""),
            ("seasons_played", "Seasons played", item["seasons"]),
            ("last_season", "Most recent season", item["last"]),
            ("franchise_id", "Franchise code", key),
        ]
        rows += [(club_id, k, label, v) for k, label, v in values]

    written = CR.write_fields(con, rows, SOURCE_LABEL)
    if unmatched:
        print(f"  no club row for {len(unmatched)} franchises "
              f"(historical ones with no current club): "
              f"{', '.join(sorted(unmatched)[:5])}...")
    return len(matched), written


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR,
                        help="folder holding Lahman's Teams.csv")
    parser.add_argument("--db", default=sport_db("mlb"))
    args = parser.parse_args()

    if not (args.dir / "Teams.csv").exists():
        raise SystemExit(f"no Teams.csv in {args.dir}")

    connection = sqlite3.connect(args.db)
    try:
        clubs, fields = load(connection, args.dir)
        print(f"{CR.TABLE}: {fields:,} fields for {clubs} clubs")
    finally:
        connection.close()
