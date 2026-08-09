#!/usr/bin/env python3
"""Import the Wikipedia arena/stadium scrape into the MLB, NBA and NFL databases.

    python -m utils.shared.load_arenas                # all three sports
    python -m utils.shared.load_arenas --sport mlb
    python -m utils.shared.load_arenas --check

Reads ``wiki_sports_scraper_arenas.py``'s output tree (``data/arena`` by
default) and writes two tables per sport database:

``arenas``
    One row per venue: name, location, capacity, surface, roof type, year
    opened, plus a small ``extra_info`` JSON blob for the handful of fields
    that only one sport has (MLB's outfield distance and stadium "type",
    the NBA's season of first game).

``arena_teams``
    A venue can host more than one team -- MetLife Stadium is the Giants'
    and the Jets' -- so the team link is a separate table rather than a
    column on ``arenas``. ``team_id`` is the sport's own identifier
    (``clubs.club_id`` for MLB, ``teams.team_id`` for NBA, ``teams.team_abbr``
    for NFL, matched by name) and is left NULL when no team of that name
    could be found, rather than dropping the row.

Only the master list per sport (``master_stadiums.csv`` / ``master_arenas.csv``)
is loaded. The per-venue ``*_infobox.csv`` files the scraper also writes are
a raw Wikipedia infobox dumped as two columns with no fixed row set from one
venue to the next -- useful to open by hand, not to parse generically -- so
they are left on disk as a citation trail rather than imported.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd

from data_paths import sport_db
from wiki_reference import ALIASES

DEFAULT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "arena"

TABLE_ARENAS = "arenas"
TABLE_ARENA_TEAMS = "arena_teams"

DDL = """
CREATE TABLE IF NOT EXISTS arenas (
    arena_id    TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    location    TEXT,
    capacity    INTEGER,
    surface     TEXT,
    roof_type   TEXT,
    opened      INTEGER,
    extra_info  TEXT,
    source_url  TEXT,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS arena_teams (
    arena_id  TEXT NOT NULL REFERENCES arenas(arena_id),
    team_id   TEXT,
    team_name TEXT NOT NULL,
    PRIMARY KEY (arena_id, team_name)
);
"""

#: Per-sport master-list layout: which CSV, which columns hold what, and
#: whether the team column can name more than one team (only the NFL's can).
SPORT_CONFIG = {
    "mlb": dict(
        csv="mlb/stadiums/master_stadiums.csv",
        name_col="Name", team_col="Team", location_col="Location",
        capacity_col="Capacity", surface_col="Surface", roof_col="Roof type",
        opened_col="Opened", multi_team=False,
        extra_cols={"type": "Type",
                    "distance_to_center_field": "Distance to center field"},
        team_table="clubs", team_id_col="club_id", team_name_col="name",
    ),
    "nba": dict(
        csv="nba/stadiums/master_arenas.csv",
        name_col="Arena", team_col="Team", location_col="Location",
        capacity_col="Capacity", surface_col=None, roof_col=None,
        opened_col="Opened", multi_team=False,
        extra_cols={"season_of_first_game": "Season of first NBA game"},
        team_table="teams", team_id_col="team_id", team_name_col="name",
    ),
    "nfl": dict(
        csv="nfl/stadiums/master_stadiums.csv",
        name_col="Name", team_col="Team(s)", location_col="Location",
        capacity_col="Capacity", surface_col="Surface", roof_col="Roof",
        opened_col="Opened", multi_team=True,
        extra_cols={},
        team_table="teams", team_id_col="team_abbr", team_name_col="team_name",
    ),
}


def slug(name: str) -> str:
    """'Yankee Stadium' -> 'yankee_stadium'."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return cleaned or "arena"


def _clean_int(value) -> int | None:
    if value is None:
        return None
    digits = re.sub(r"[^0-9]", "", str(value))
    return int(digits) if digits else None


def _clean_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def team_ids_by_name(con: sqlite3.Connection, cfg: dict) -> dict[str, str]:
    rows = con.execute(
        f"SELECT {cfg['team_id_col']}, {cfg['team_name_col']} "
        f"FROM {cfg['team_table']}")
    mapping: dict[str, str] = {}
    for team_id, name in rows:
        if name:
            mapping.setdefault(name, team_id)
    return mapping


def match_teams(team_field: str, team_ids: dict[str, str], multi_team: bool) -> list[str]:
    """Split a master-list team cell into the team name(s) it names.

    A single-team cell is just the name itself. The NFL's shared-stadium
    rows concatenate two full team names with no separator ("New York
    Giants New York Jets"), so those are recovered by checking which known
    team names occur in the cell, longest first so e.g. "Rams" full names
    aren't half-matched inside a longer one.
    """
    text = (team_field or "").strip()
    if not text:
        return []
    if not multi_team:
        return [text]
    found = [name for name in sorted(team_ids, key=len, reverse=True) if name in text]
    return found or [text]


def load_sport(con: sqlite3.Connection, sport: str, root: Path) -> dict:
    cfg = SPORT_CONFIG[sport]
    csv_path = root / cfg["csv"]
    if not csv_path.exists():
        return {"error": f"no {csv_path}"}

    df = pd.read_csv(csv_path)
    missing = [c for c in (cfg["name_col"], cfg["team_col"]) if c not in df.columns]
    if missing:
        return {"error": f"{csv_path} missing column(s): {missing}"}

    con.executescript(DDL)
    team_ids = team_ids_by_name(con, cfg)
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    arenas_written = 0
    links_written = 0
    unmatched: list[str] = []

    for _, row in df.iterrows():
        name = _clean_text(row.get(cfg["name_col"]))
        if not name:
            continue
        arena_id = slug(name)

        extra = {}
        for key, col in cfg["extra_cols"].items():
            value = _clean_text(row.get(col)) if col in df.columns else None
            if value:
                extra[key] = value

        con.execute(
            f"INSERT OR REPLACE INTO {TABLE_ARENAS} "
            f"(arena_id, name, location, capacity, surface, roof_type, "
            f" opened, extra_info, source_url, imported_at) "
            f"VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                arena_id,
                name,
                _clean_text(row.get(cfg["location_col"])) if cfg["location_col"] else None,
                _clean_int(row.get(cfg["capacity_col"])) if cfg["capacity_col"] else None,
                _clean_text(row.get(cfg["surface_col"])) if cfg["surface_col"] else None,
                _clean_text(row.get(cfg["roof_col"])) if cfg["roof_col"] else None,
                _clean_int(row.get(cfg["opened_col"])) if cfg["opened_col"] else None,
                json.dumps(extra) if extra else None,
                f"https://en.wikipedia.org/wiki/{name.replace(' ', '_')}",
                stamp,
            ),
        )
        arenas_written += 1

        aliases = ALIASES.get(sport, {})
        for team_name in match_teams(row.get(cfg["team_col"]), team_ids, cfg["multi_team"]):
            team_id = team_ids.get(team_name)
            if team_id is None and team_name in aliases:
                team_id = team_ids.get(aliases[team_name])
            if team_id is None:
                unmatched.append(f"{name}: {team_name}")
            con.execute(
                f"INSERT OR REPLACE INTO {TABLE_ARENA_TEAMS} "
                f"(arena_id, team_id, team_name) VALUES (?,?,?)",
                (arena_id, team_id, team_name),
            )
            links_written += 1

    con.commit()
    return {
        "arenas": arenas_written,
        "links": links_written,
        "unmatched": unmatched,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR,
                        help="the arena scrape's output root (default: data/arena)")
    parser.add_argument("--sport", choices=sorted(SPORT_CONFIG), action="append",
                        help="load one sport (repeatable; default all three)")
    parser.add_argument("--db", help="database to write (only with a single --sport)")
    parser.add_argument("--check", action="store_true",
                        help="report what would be loaded and write nothing")
    args = parser.parse_args(argv)

    sports = args.sport or sorted(SPORT_CONFIG)
    if args.db and len(sports) != 1:
        parser.error("--db needs exactly one --sport")
    if not args.dir.is_dir():
        print(f"no arena data at {args.dir}")
        return 1

    ok = 0
    for sport in sports:
        print(f"\n=== {sport.upper()} ===")
        db = args.db or sport_db(sport)
        con = sqlite3.connect(db)
        try:
            if args.check:
                cfg = SPORT_CONFIG[sport]
                csv_path = args.dir / cfg["csv"]
                if not csv_path.exists():
                    print(f"  missing {csv_path}")
                    continue
                df = pd.read_csv(csv_path)
                print(f"  {csv_path}: {len(df)} row(s), would write to {db}")
                ok += 1
                continue

            result = load_sport(con, sport, args.dir)
            if "error" in result:
                print(f"  {result['error']}")
                continue
            print(f"  {db}: {result['arenas']} arena(s), "
                  f"{result['links']} team link(s)")
            if result["unmatched"]:
                print(f"    {len(result['unmatched'])} team name(s) with no club/team match:")
                for line in result["unmatched"]:
                    print(f"      {line}")
            ok += 1
        finally:
            con.close()

    verb = "checked" if args.check else "loaded"
    print(f"\n{ok}/{len(sports)} sport(s) {verb}")
    return 0 if ok == len(sports) else 1


if __name__ == "__main__":
    sys.exit(main())
