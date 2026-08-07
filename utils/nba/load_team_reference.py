#!/usr/bin/env python3
"""Team reference fields for the NBA Team Explorer.

    python -m utils.nba.load_team_reference

Fills ``club_wikipedia_fields`` from the NBA dataset's team files --
nickname, year founded, arena and its capacity, city and the club's current
front office. derive_club_tables.py creates that table for every sport but
can only fill it where a source has been loaded, so the NBA's was empty and
its Team Explorer showed four dashes.

Two files, because the detailed one is short
--------------------------------------------

``team_details.csv`` has the arena, its capacity and the front office, but
only for 25 of the 30 clubs: the Celtics, Cavaliers, Pelicans, Knicks and
Magic are simply absent from it. ``team.csv`` has all 30 but carries only
nickname, city, state and founding year. So `team.csv` is read as the base
and `team_details.csv` layered over it, which leaves every club with a
nickname and a founding year and the twenty-five with an arena as well --
rather than five clubs with nothing at all.

No championship count, and why not
---------------------------------

The Championships tile is left empty on purpose. Counting titles from the
schedule looks sound -- whoever wins the last Finals game of a season won
the series, because a best-of-seven ends when somebody reaches four wins --
but it depends on the Finals being labelled, and in this source they are
not reliably labelled at all:

* Five seasons (1946, 1947, 1948, 1954, 1955) carry no ``round='F'`` row.
* Every season from 2020 to 2025 has six playoff games with no round on
  them, which is the shape of an unlabelled Finals.

Derived that way the Lakers come out at 16 against an actual 17, and the
recent champions are miscounted too. A tile reading "Championships 16" is
worse than a blank one: it is wrong, and it is wrong in a way nobody
checking the page would catch. Fixing this needs a source that states
series outcomes -- see nba/sport.py, which declines to set `title_round`
for a related reason.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path

import club_reference as CR
from data_paths import sport_db

SOURCE_LABEL = "team.csv + team_details.csv"

DEFAULT_DIR = Path(r"C:\sample\nba")

#: All thirty clubs, but only the basics.
BASE_FIELDS = (
    ("nickname", "nickname", "Nickname"),
    ("year_founded", "founded", "Founded"),
    ("city", "city", "City"),
    ("state", "state", "State"),
    ("abbreviation", "abbreviation", "Abbreviation"),
)

#: Twenty-five clubs, but the interesting columns. Layered over the base,
#: so a club missing from this file keeps whatever the base gave it.
DETAIL_FIELDS = (
    ("arena", "ground", "Arena"),
    ("arenacapacity", "arena_capacity", "Arena capacity"),
    ("owner", "owner", "Owner"),
    ("generalmanager", "general_manager", "General manager"),
    ("headcoach", "head_coach", "Head coach"),
    ("dleagueaffiliation", "g_league", "G League affiliate"),
)


#: Columns written by pandas as floats ('1949.0'), which must not reach a
#: tile reading "Founded 1949.0".
_NUMERIC = ("yearfounded", "year_founded", "arenacapacity")


def _clean(column: str, value: str) -> str:
    """Trim, and drop the '.0' pandas left on the numeric columns."""
    text = (value or "").strip()
    if column in _NUMERIC and text.endswith(".0"):
        text = text[:-2]
    if column == "arenacapacity" and text.isdigit():
        return f"{int(text):,}"
    return text


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def load(con: sqlite3.Connection, folder: Path) -> tuple[int, int]:
    """Fill club_wikipedia_fields. Returns (clubs matched, fields written)."""
    by_name = CR.club_ids_by_name(con)
    rows, matched, unmatched = [], set(), set()

    def add(name: str, row: dict, fields) -> str | None:
        club_id = by_name.get(name)
        if not club_id:
            unmatched.add(name)
            return None
        matched.add(club_id)
        for column, key, label in fields:
            rows.append((club_id, key, label, _clean(column, row.get(column))))
        return club_id

    for row in _read(folder / "team.csv"):
        add((row.get("full_name") or "").strip(), row, BASE_FIELDS)

    # Layered second so its arena and front office win where both files
    # describe the same club.
    for row in _read(folder / "team_details.csv"):
        name = f"{(row.get('city') or '').strip()} " \
               f"{(row.get('nickname') or '').strip()}".strip()
        add(name, row, DETAIL_FIELDS)

    written = CR.write_fields(con, rows, SOURCE_LABEL)
    if unmatched:
        print(f"  no club row for: {', '.join(sorted(unmatched))}")
    return len(matched), written


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR,
                        help="folder holding team.csv and team_details.csv")
    parser.add_argument("--db", default=sport_db("nba"))
    args = parser.parse_args()

    if not (args.dir / "team.csv").exists():
        raise SystemExit(f"no team.csv in {args.dir}")

    connection = sqlite3.connect(args.db)
    try:
        clubs, fields = load(connection, args.dir)
        print(f"{CR.TABLE}: {fields:,} fields for {clubs} clubs")
    finally:
        connection.close()
