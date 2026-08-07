#!/usr/bin/env python3
"""Import the Wikipedia sports scrape into the NBA, NFL and MLB databases.

    python -m utils.shared.load_wiki_reference                    # all sports
    python -m utils.shared.load_wiki_reference --sport mlb
    python -m utils.shared.load_wiki_reference --check
    python -m utils.shared.load_wiki_reference --profile mlb

Reads ``wiki_sports_scraper.py``'s output tree -- ``C:\\data_lab\\wiki`` by
default -- and stages it into each sport's own database. wiki_reference.py
holds the schema and the rules; this is the order they run in and what the
run reports.

The order is not arbitrary
--------------------------

1. Validate the sport's files. Nothing is written if this fails.
2. Open an import batch, so a half-finished run is identifiable afterwards.
3. Stage ``scrape_log.csv``. The scraper's own WARN lines belong beside the
   data they describe, not in a text file nobody opens again.
4. Stage ``team.csv`` and map its teams to ``clubs``.
5. Stage ``league_stats.csv``, ``hall_of_fame.csv`` and ``team_stats.csv``.
6. Fill the Team Explorer's empty fields from the catalogue.

Team mapping comes before the records because the records are keyed by the
same wiki team names, and a name that resolves to no club is worth knowing
about before three thousand rows are staged against it.

What this run found
-------------------

**MLB's league_stats.csv is empty**, which the scraper itself flagged. The
`Major League Baseball` page has exactly one heading the league matcher
hits -- "Teams" -- and the scraper skips that one deliberately, because the
franchise catalogue is already written to team.csv. Its remaining sections
are History, Organizational structure, League organization, Uniforms,
Season structure, International play, Performance-enhancing drugs and Media
coverage: none is the championships-or-awards section the NBA's
`Championships` (35 rows) and the NFL's `Team trophies` (12 rows) are.

Widening the heading list would only pull in prose about the playoff
format, and the database already has 12,667 MLB award rows across 85 award
types from Lahman -- keyed to a player, which a Wikipedia section is not.
So this is imported as zero rows and warned about, not treated as a failure
and not patched around.

**Four MLB clubs are alias matches.** Wikipedia names a franchise as it is
named now; this database was built from sources that named four of them as
they used to be -- Guardians/Indians, Marlins' two cities, the Angels'
several names, and the Athletics' since they left Oakland. All four are
matched through ``wiki_reference.ALIASES`` and recorded as `alias_match`,
so a review can see the join was not on the name itself.

Filling the Team Explorer's blanks
----------------------------------

The catalogue carries a current arena or stadium and its capacity for all
92 clubs, which is more coverage than any of the three sports' existing
sources managed: the NBA's ``team_details.csv`` is missing five clubs
outright, and the NFL and MLB had no capacity at all.

Those fields are written **only where the club has no value for the key
already**. The existing loaders read curated sources -- Lahman's World
Series counts, the NBA's front office -- and a Wikipedia infobox cell is
not a reason to overwrite one. Run with ``--no-fields`` to stage the
reference data and leave ``club_wikipedia_fields`` alone entirely.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import club_reference as CR
import wiki_reference as W
from data_paths import sport_db

SOURCE_LABEL = "Wikipedia scrape team.csv"

#: Catalogue column -> (field key, label), per sport. The keys are
#: club_reference's shared vocabulary, not the CSV's column names: `ground`
#: is the Team Explorer's headline venue tile whether the sport calls it an
#: arena, a stadium or a ballpark.
FIELDS = {
    "nba": (
        ("arena", "ground", "Arena"),
        ("capacity", "arena_capacity", "Arena capacity"),
        ("conference", "conference", "Conference"),
        ("division", "division", "Division"),
        ("location", "location", "Location"),
        ("founded", "founded", "Founded"),
        ("joined", "joined_league", "Joined the NBA"),
    ),
    "nfl": (
        ("stadium", "ground", "Home stadium"),
        ("capacity", "stadium_capacity", "Stadium capacity"),
        ("conference", "conference", "Conference"),
        ("division", "division", "Division"),
        ("city", "location", "Location"),
        ("head_coach", "head_coach", "Head coach"),
        ("first_season", "founded", "First season"),
    ),
    "mlb": (
        ("stadium", "ground", "Ballpark"),
        ("capacity", "stadium_capacity", "Ballpark capacity"),
        ("league", "conference", "League"),
        ("division", "division", "Division"),
        ("city", "location", "Location"),
        ("founded", "founded", "Founded"),
        ("joined", "joined_league", "Joined the league"),
    ),
}

#: Keys whose value is a crowd figure, shown with thousands separators.
_CAPACITY_KEYS = ("arena_capacity", "stadium_capacity")


def _display(key: str, value: str) -> str:
    """One catalogue cell as the Team Explorer should show it."""
    text = (value or "").strip()
    if key in _CAPACITY_KEYS:
        number = W.numeric(text)
        return f"{number:,}" if number is not None else text
    return text


def fill_fields(con: sqlite3.Connection, sport: str,
                teams: list[dict], club_ids: dict[str, str]) -> tuple[int, int]:
    """Write catalogue values for keys a club has no value for yet.

    Returns (fields written, values withheld because one was already
    there). The second number is the point of the function: it is how much
    of this source the database already had from somewhere it trusts more.
    """
    CR.ensure_table(con)
    existing = {(club, key) for club, key in con.execute(
        f"SELECT club_id, field_key FROM {CR.TABLE}")}

    rows, held = [], 0
    for row in teams:
        club_id = club_ids.get((row.get("team") or "").strip())
        if not club_id:
            continue
        for column, key, label in FIELDS[sport]:
            text = _display(key, row.get(column))
            if not text:
                continue
            if (club_id, key) in existing:
                held += 1
                continue
            rows.append((club_id, key, label, text))

    return CR.write_fields(con, rows, SOURCE_LABEL), held


def import_sport(sport: str, root: Path, db: str, *, check_only: bool = False,
                 write_fields: bool = True) -> bool:
    """Validate and import one sport. Returns True when it was imported."""
    print(f"\n=== {sport.upper()} ===")
    report = W.validate(root, sport)
    report.print()
    if not report.ok:
        print("  not imported -- fix the errors above and re-run")
        return False
    if check_only:
        return True

    folder = root / sport
    meta = W.read_json(folder / "metadata.json")
    run_meta = W.read_json(root / "scrape_metadata.json")

    con = sqlite3.connect(db)
    try:
        batch = W.open_batch(con, sport, root, meta, run_meta)
        print(f"  import batch {batch} -> {db}")

        statuses = W.load_log(con, sport, root, batch)
        if statuses:
            print("  scrape log: " + ", ".join(
                f"{count} {status}" for status, count in sorted(statuses.items())))

        teams = W.read_csv(folder / "team.csv")
        staged_teams = W.load_team_catalogue(con, sport, teams, batch)
        club_ids = W.map_teams(con, sport, teams)
        aliased = sum(1 for row in con.execute(
            f"SELECT 1 FROM {W.MAP_TABLE} WHERE sport = ? "
            f"AND match_status = 'alias_match'", (sport,)))
        print(f"  {W.TEAM_STAGE}: {staged_teams} teams "
              f"({len(club_ids)} mapped to a club, {aliased} by alias)")

        for name, status in W.unresolved(con, sport):
            print(f"    {status}: {name}")

        counts, collapsed = W.load_records(con, sport, folder, batch)
        total = con.execute(
            f"SELECT COUNT(*) FROM {W.REFERENCE_STAGE} WHERE sport = ?",
            (sport,)).fetchone()[0]
        print(f"  {W.REFERENCE_STAGE}: {total:,} records "
              f"(team {counts.get('team', 0):,}, "
              f"league {counts.get('league', 0):,}, "
              f"hall of fame {counts.get('hall_of_fame', 0):,})")
        if collapsed:
            print(f"    {collapsed} duplicate record(s) collapsed by hash")

        orphans = W.orphan_records(con, sport)
        if orphans:
            print(f"    {orphans:,} record(s) name a team with no club")

        if write_fields:
            written, held = fill_fields(con, sport, teams, club_ids)
            clubs, fields = CR.summarise(con)
            print(f"  {CR.TABLE}: {written} new field(s) written, "
                  f"{held} withheld (a value was already there)")
            print(f"    now {fields} fields across {clubs} clubs")

        note = "; ".join(report.warnings) if report.warnings else ""
        W.close_batch(con, batch, "complete", note)
        return True
    finally:
        con.close()


def show_profile(sport: str, db: str, limit: int) -> None:
    """Print the data_json inventory a normalisation pass is designed from."""
    con = sqlite3.connect(db)
    try:
        rows = W.profile(con, sport, limit)
        if not rows:
            print(f"no staged table rows for {sport} -- import it first")
            return
        print(f"\n{sport.upper()}: the {len(rows)} largest table-row shapes")
        for section, _, keys, count in rows:
            print(f"  {count:5d}  {section[:38]:38}  {keys[:70]}")
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=W.DEFAULT_ROOT,
                        help="the scrape's output root")
    parser.add_argument("--sport", choices=W.SPORTS, action="append",
                        help="import one sport (repeatable; default all)")
    parser.add_argument("--db", help="database to write "
                                     "(only with a single --sport)")
    parser.add_argument("--check", action="store_true",
                        help="validate the files and write nothing")
    parser.add_argument("--no-fields", action="store_true",
                        help="stage the data but leave "
                             "club_wikipedia_fields alone")
    parser.add_argument("--profile", choices=W.SPORTS,
                        help="print the staged JSON key sets and exit")
    parser.add_argument("--profile-limit", type=int, default=25)
    args = parser.parse_args(argv)

    if args.profile:
        show_profile(args.profile, args.db or sport_db(args.profile),
                     args.profile_limit)
        return 0

    sports = args.sport or list(W.SPORTS)
    if args.db and len(sports) != 1:
        parser.error("--db needs exactly one --sport")

    if not args.dir.is_dir():
        print(f"no scrape output at {args.dir}")
        return 1

    run_meta = W.read_json(args.dir / "scrape_metadata.json")
    if run_meta:
        print(f"{run_meta.get('application')} {run_meta.get('version')}, "
              f"run {run_meta.get('generated_at_utc')}")
        print(f"source: {run_meta.get('source')}")
        print(f"licence: {run_meta.get('source_licence')}")

    imported = [sport for sport in sports
                if import_sport(sport, args.dir, args.db or sport_db(sport),
                                check_only=args.check,
                                write_fields=not args.no_fields)]

    verb = "validated" if args.check else "imported"
    print(f"\n{len(imported)}/{len(sports)} sport(s) {verb}")
    return 0 if len(imported) == len(sports) else 1


if __name__ == "__main__":
    sys.exit(main())
