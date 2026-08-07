#!/usr/bin/env python3
"""Team reference fields for the NFL Team Explorer.

    python -m utils.nfl.load_team_reference --csv C:\\sample\\nfl\\NFL_team_data.csv

Fills ``club_wikipedia_fields``, the key/value table afl/club_explorer.py
reads for a club's headline tiles and Overview list. derive_club_tables.py
creates that table for every sport but can only fill it where a scrape has
run, so the NFL's was empty: the Team Explorer showed a name, a logo and
four dashes.

Two sources, because neither is complete on its own:

* ``teams``, already in the database from nflverse -- the team's nickname,
  conference and division. Authoritative and already maintained by the
  build, so it is read rather than re-imported from the CSV.
* The supplied ``NFL_team_data.csv`` -- home stadium, city, founding year,
  Super Bowl wins and the costumed mascot's name. None of these are in
  nflverse's team table.

The mascot is not the nickname
------------------------------

The CSV's ``mascot`` column holds the costumed character's name -- 'Buffalo
Billy', 'T.D.', 'Poe' -- not the team's nickname. Writing it to the
``nickname`` key would put 'Buffalo Billy' in the Team Explorer's Nickname
tile, which is why the nickname comes from ``teams.team_nick`` ('Bills')
and the mascot gets a key of its own.

Values are written exactly as the CSV has them, including the source's
'Ochard Park' typo for the Bills. A loader that quietly corrects its input
leaves no way to tell a fixed value from an original one; the Sources tab
exists so a wrong value can be seen and traced.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path

import club_reference as CR
from data_paths import sport_db

#: The CSV's own name for the file this was taken from, recorded against
#: every row so the Sources tab can say where a value came from.
SOURCE_LABEL = "NFL_team_data.csv"

#: CSV column -> (field_key, field_label). The key is what club_fields.py
#: and the Overview list look values up by; `ground`, `founded` and
#: `premierships` are the spellings afl/club_fields.py already knows, so
#: the headline tiles work without teaching that module a new sport.
CSV_FIELDS = (
    ("home_stadium", "ground", "Home stadium"),
    ("location", "location", "Location"),
    ("year_est", "founded", "Founded"),
    ("superbowl_wins", "premierships", "Super Bowl wins"),
    ("mascot", "mascot", "Mascot"),
    ("live_mascots", "live_mascots", "Live mascots"),
)

#: Columns read from `teams` rather than the CSV, as
#: (column, field_key, field_label).
TEAM_FIELDS = (
    ("team_nick", "nickname", "Nickname"),
    ("team_conf", "conference", "Conference"),
    ("team_division", "division", "Division"),
)


def _team_rows(con: sqlite3.Connection) -> dict[str, dict]:
    """Nickname, conference and division per team name, from `teams`."""
    columns = ", ".join(column for column, _, _ in TEAM_FIELDS)
    return {name: dict(zip((c for c, _, _ in TEAM_FIELDS), values))
            for name, *values in con.execute(
                f"SELECT team_name, {columns} FROM teams "
                f"WHERE team_name IS NOT NULL")}


def load(con: sqlite3.Connection, csv_path: Path) -> tuple[int, int]:
    """Fill club_wikipedia_fields. Returns (clubs matched, fields written)."""
    by_name = CR.club_ids_by_name(con)
    from_teams = _team_rows(con)

    with open(csv_path, newline="", encoding="utf-8-sig") as handle:
        source = list(csv.DictReader(handle))

    rows, matched, unmatched = [], set(), []
    for row in source:
        name = (row.get("team_name") or "").strip()
        club_id = by_name.get(name)
        if not club_id:
            unmatched.append(name)
            continue
        matched.add(club_id)

        team = from_teams.get(name, {})
        rows += [(club_id, key, label, row.get(column))
                 for column, key, label in CSV_FIELDS]
        rows += [(club_id, key, label, team.get(column))
                 for column, key, label in TEAM_FIELDS]

    written = CR.write_fields(con, rows, SOURCE_LABEL)
    if unmatched:
        print(f"  no club row for: {', '.join(sorted(unmatched))}")
    return len(matched), written


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path,
                        default=Path(r"C:\sample\nfl\NFL_team_data.csv"),
                        help="the team reference CSV to load")
    parser.add_argument("--db", default=sport_db("nfl"),
                        help="database to write (default: the NFL database)")
    args = parser.parse_args()

    if not args.csv.exists():
        raise SystemExit(f"no such file: {args.csv}")

    connection = sqlite3.connect(args.db)
    try:
        clubs, fields = load(connection, args.csv)
        print(f"{CR.TABLE}: {fields:,} fields for {clubs} clubs")
    finally:
        connection.close()
