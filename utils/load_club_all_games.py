#!/usr/bin/env python3
"""Load AFL Tables All Games observations and enrich the canonical matches table.

Three stages, each independently re-runnable:

  parse   cached club pages          -> club_match_sources
  link    club_match_sources         -> matches.match_id
  apply   agreed observations        -> match_details, matches.attendance, quarters

Why the split matters
---------------------
``derive_matches.py`` writes the matches table with ``if_exists="replace"``, so
attendance and the quarter columns are cleared by every database rebuild. The
source observations live in their own table and survive, which means a rebuild
is followed by ``--apply-only`` rather than a re-fetch.

Reconciliation
--------------
Every match appears on two club pages. The AFL Tables game key is order
independent (``{low_code}{high_code}{YYYYMMDD}``), so both observations share
it exactly. Where the two pages disagree the row is recorded as a conflict and
nothing is written to matches: a disagreement between sources is a finding, not
something to average away.

Orientation
-----------
The game key does not say who was at home. H/A rows orient themselves; ``T=F``
finals do not, and are oriented from the linked matches row (which carries the
home/away flag persisted by build_db.py). A final that cannot be oriented keeps
its quarter data in match_details as for/against only.

Usage:
    python utils/load_club_all_games.py --report
    python utils/load_club_all_games.py --club richmond --details
    python utils/load_club_all_games.py --apply-only      # after a rebuild
    python utils/load_club_all_games.py --dry-run
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from club_all_games import (MatchObservation, check_against_footers,
                                parse_all_games, parse_season_footers)
    from club_sources import ALL_GAMES_BY_ID
else:
    from .club_all_games import (MatchObservation, check_against_footers,
                                 parse_all_games, parse_season_footers)
    from .club_sources import ALL_GAMES_BY_ID

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "gridley.db"
DEFAULT_RAW_DIR = PROJECT_ROOT / "data" / "afl" / "raw" / "clubs"
SOURCE_FILENAME = "afltables_all_games.html"

SOURCE_COLUMNS = [
    "source_club_id", "source_club_label", "season", "round", "is_final",
    "team_position", "opponent_raw", "scoring_for_raw", "scoring_against_raw",
    "points_for", "points_against", "result", "margin", "season_wins_after",
    "season_draws_after", "season_losses_after", "venue_raw", "attendance",
    "date_text", "match_date", "match_time", "match_datetime",
    "source_game_url", "source_game_key", "team_code_low", "team_code_high",
    "home_team_raw", "away_team_raw",
] + [f"q{q}_{side}_{stat}"
     for side in ("for", "against")
     for q in (1, 2, 3, 4)
     for stat in ("goals", "behinds", "points")]

# Fields both club pages report identically about the same match.
SHARED_FIELDS = ["season", "round", "match_date", "match_time", "venue_raw",
                 "attendance"]
# Fields each page reports from its own side: one page's 'for' is the other's
# 'against', so they are compared crosswise.
MIRRORED_FIELDS = [("points_for", "points_against"),
                   ("scoring_for_raw", "scoring_against_raw")]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


# --------------------------------------------------------------------------
# schema


def create_schema(con: sqlite3.Connection) -> None:
    source_defs = ",\n            ".join(
        f"{name} {'TEXT' if name in {'source_club_id','source_club_label','round','team_position','opponent_raw','scoring_for_raw','scoring_against_raw','result','venue_raw','date_text','match_date','match_time','match_datetime','source_game_url','source_game_key','team_code_low','team_code_high','home_team_raw','away_team_raw'} else 'INTEGER'}"
        for name in SOURCE_COLUMNS)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS club_match_sources (
            {source_defs},
            match_id INTEGER,
            match_status TEXT NOT NULL DEFAULT 'unlinked',
            source_fetched_at TEXT,
            imported_at TEXT,
            PRIMARY KEY (source_club_id, source_game_key)
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS match_details (
            match_id INTEGER PRIMARY KEY,
            afltables_game_key TEXT,
            afltables_game_url TEXT,
            attendance INTEGER,
            match_time TEXT,
            scheduled_datetime TEXT,
            home_source_club TEXT,
            away_source_club TEXT,
            orientation TEXT,
            home_q1_goals INTEGER, home_q1_behinds INTEGER, home_q1_points INTEGER,
            home_q2_goals INTEGER, home_q2_behinds INTEGER, home_q2_points INTEGER,
            home_q3_goals INTEGER, home_q3_behinds INTEGER, home_q3_points INTEGER,
            home_q4_goals INTEGER, home_q4_behinds INTEGER, home_q4_points INTEGER,
            away_q1_goals INTEGER, away_q1_behinds INTEGER, away_q1_points INTEGER,
            away_q2_goals INTEGER, away_q2_behinds INTEGER, away_q2_points INTEGER,
            away_q3_goals INTEGER, away_q3_behinds INTEGER, away_q3_points INTEGER,
            away_q4_goals INTEGER, away_q4_behinds INTEGER, away_q4_points INTEGER,
            observations INTEGER,
            source_status TEXT,
            imported_at TEXT
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS club_match_source_issues (
            source_game_key TEXT,
            issue TEXT,
            field TEXT,
            club_a TEXT, value_a TEXT,
            club_b TEXT, value_b TEXT,
            detected_at TEXT
        )""")
    for statement in (
        "CREATE INDEX IF NOT EXISTS ix_cms_key ON club_match_sources(source_game_key)",
        "CREATE INDEX IF NOT EXISTS ix_cms_match ON club_match_sources(match_id)",
        "CREATE INDEX IF NOT EXISTS ix_cms_season ON club_match_sources(season, round)",
        "CREATE INDEX IF NOT EXISTS ix_md_key ON match_details(afltables_game_key)",
    ):
        con.execute(statement)
    con.commit()


# --------------------------------------------------------------------------
# parse


def source_files(raw_dir: Path, club_ids: list[str] | None) -> list[tuple[str, Path]]:
    found = []
    for directory in sorted(p for p in raw_dir.iterdir() if p.is_dir()) if raw_dir.exists() else []:
        if club_ids and directory.name not in club_ids:
            continue
        path = directory / SOURCE_FILENAME
        if path.exists() and path.stat().st_size > 0:
            found.append((directory.name, path))
    return found


def parse_sources(raw_dir: Path, club_ids: list[str] | None
                  ) -> tuple[list[MatchObservation], list[str]]:
    observations: list[MatchObservation] = []
    errors: list[str] = []
    files = source_files(raw_dir, club_ids)
    if not files:
        print(f"No cached {SOURCE_FILENAME} files under {raw_dir}")
    for club_id, path in files:
        rows, problems = parse_all_games(path, club_id, strict=False)
        # Each season table states its own played/W-D-L/points/crowd totals.
        problems += [f"{club_id} {line}"
                     for line in check_against_footers(
                         rows, parse_season_footers(path))]
        print(f"{club_id:22} {len(rows):>6,} matches"
              + (f"  ({len(problems)} source disagreements)" if problems else ""))
        observations.extend(rows)
        errors.extend(problems)
    return observations, errors


def write_sources(con: sqlite3.Connection, observations: list[MatchObservation],
                  club_ids: list[str] | None) -> None:
    now = utc_now()
    if club_ids:
        con.executemany("DELETE FROM club_match_sources WHERE source_club_id = ?",
                        [(club_id,) for club_id in club_ids])
    else:
        con.execute("DELETE FROM club_match_sources")
    placeholders = ", ".join("?" for _ in SOURCE_COLUMNS + ["imported_at"])
    con.executemany(
        f"INSERT OR REPLACE INTO club_match_sources "
        f"({', '.join(SOURCE_COLUMNS)}, imported_at) VALUES ({placeholders})",
        [tuple(obs.flat()[name] for name in SOURCE_COLUMNS) + (now,)
         for obs in observations])
    con.commit()


# --------------------------------------------------------------------------
# link


def normalise_date(value: object) -> str:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else text


def match_lookup(con: sqlite3.Connection) -> dict:
    """(season, date, club_now) -> [match rows], indexed from both sides.

    ``home_team_now``/``away_team_now`` on ``matches`` already carry the
    canonical current-club identity -- the same lineage mapping build_db.py
    uses for ``games.club_now``. Matching on that, rather than on any text
    parsed from a source page, sidesteps a real problem: several AFL Tables
    All Games pages carry a *combined* heading across a club's renamed eras
    (Sydney's is literally "South Melbourne/Sydney - All Games - By Season"),
    so text parsed from the page can never equal either historical name that
    actually appears in ``matches``. A club plays at most one match per date,
    so (season, date, club_now) is enough to find it without needing the
    opponent's identity at all.
    """
    lookup: dict = {}
    for row in con.execute(
            "SELECT match_id, season, round, match_date, venue, home_team, "
            "away_team, home_team_now, away_team_now, home_score, "
            "away_score, home_away_known FROM matches"):
        date = normalise_date(row[3])
        for side_now in (row[7], row[8]):
            lookup.setdefault((int(row[1]), date, side_now), []).append(row)
    return lookup


def link_sources(con: sqlite3.Connection) -> dict:
    lookup = match_lookup(con)
    counts = {"unique": 0, "score_mismatch": 0, "ambiguous": 0, "unmatched": 0}
    updates = []
    for row in con.execute(
            "SELECT rowid, source_club_id, season, match_date, "
            "points_for, points_against FROM club_match_sources"):
        rid, source_club_id, season, date, points_for, points_against = row
        club_now = ALL_GAMES_BY_ID.get(source_club_id)
        club_now = club_now.db_club_now if club_now else None
        candidates = (lookup.get((int(season), normalise_date(date), club_now), [])
                     if club_now else [])
        # A club_now can legitimately appear on both sides only if it played
        # itself, which never happens, so duplicate list entries only arise
        # when the same match_id was appended from both the home and away
        # branch -- deduplicate by match_id before judging ambiguity.
        candidates = list({match[0]: match for match in candidates}.values())
        if not candidates:
            status, match_id = "unmatched", None
        elif len(candidates) > 1:
            status, match_id = "ambiguous", None
        else:
            match = candidates[0]
            is_home = match[7] == club_now
            my_score = match[9] if is_home else match[10]
            other_score = match[10] if is_home else match[9]
            status = ("unique"
                      if {my_score, other_score} == {points_for, points_against}
                      else "score_mismatch")
            match_id = match[0] if status == "unique" else None
        counts[status] += 1
        updates.append((match_id, status, rid))
    con.executemany(
        "UPDATE club_match_sources SET match_id = ?, match_status = ? "
        "WHERE rowid = ?", updates)
    con.commit()
    return counts


# --------------------------------------------------------------------------
# apply


def _quarters(row: sqlite3.Row, side: str) -> list[tuple[int, int, int]]:
    return [(row[f"q{q}_{side}_goals"], row[f"q{q}_{side}_behinds"],
             row[f"q{q}_{side}_points"]) for q in (1, 2, 3, 4)]


def apply_details(con: sqlite3.Connection) -> dict:
    con.row_factory = sqlite3.Row
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in con.execute(
            "SELECT * FROM club_match_sources WHERE match_status = 'unique' "
            "AND match_id IS NOT NULL"):
        grouped.setdefault(row["match_id"], []).append(row)

    # home_team_now already carries the canonical current identity computed
    # by derive_matches.py -- the same target vocabulary as
    # ClubSource.db_club_now, so orientation never depends on any text parsed
    # from a source page.
    homes_now = {row[0]: row[1] for row in
                con.execute("SELECT match_id, home_team_now FROM matches")}
    known = {row[0] for row in con.execute(
        "SELECT match_id FROM matches WHERE home_away_known = 1")}

    now = utc_now()
    stats = {"matches": 0, "two_sided": 0, "one_sided": 0, "conflicts": 0,
             "scoring_conflicts": 0, "unoriented": 0}
    conflicts = []
    details = []

    for match_id, rows in grouped.items():
        home_now = homes_now.get(match_id)

        def club_now_of(row):
            club = ALL_GAMES_BY_ID.get(row["source_club_id"])
            return club.db_club_now if club else None

        disputed: set[str] = set()
        if len(rows) > 1:
            first, second = rows[0], rows[1]
            for field in SHARED_FIELDS:
                if str(first[field]) != str(second[field]):
                    disputed.add(field)
                    conflicts.append((
                        first["source_game_key"], "source disagreement", field,
                        first["source_club_id"], str(first[field]),
                        second["source_club_id"], str(second[field]), now))
            for own, other in MIRRORED_FIELDS:
                if str(first[own]) != str(second[other]):
                    disputed.update({own, other})
                    conflicts.append((
                        first["source_game_key"], "mirrored disagreement", own,
                        first["source_club_id"], str(first[own]),
                        second["source_club_id"], str(second[other]), now))
            stats["two_sided"] += 1
        else:
            stats["one_sided"] += 1

        oriented = match_id in known and home_now is not None
        home_row = next((row for row in rows
                         if club_now_of(row) == home_now), None)
        away_row = next((row for row in rows
                         if club_now_of(row) != home_now), None)

        if home_row is not None:
            home_q = _quarters(home_row, "for")
            away_q = _quarters(home_row, "against")
            orientation = "home_page"
        elif away_row is not None and oriented:
            home_q = _quarters(away_row, "against")
            away_q = _quarters(away_row, "for")
            orientation = "away_page"
        else:
            stats["unoriented"] += 1
            continue

        first = rows[0]
        scoring_disputed = bool(disputed & {"points_for", "points_against",
                                            "scoring_for_raw",
                                            "scoring_against_raw"})
        if scoring_disputed:
            stats["scoring_conflicts"] += 1
            continue
        attendance = (None if "attendance" in disputed else
                      next((row["attendance"] for row in rows
                            if row["attendance"] is not None), None))
        values = {
            "match_id": match_id,
            "afltables_game_key": first["source_game_key"],
            "afltables_game_url": first["source_game_url"],
            "attendance": attendance,
            "match_time": first["match_time"],
            "scheduled_datetime": first["match_datetime"],
            "home_source_club": home_now,
            "away_source_club": (away_row or first)["opponent_raw"]
            if home_row is not None else club_now_of(first),
            "orientation": orientation,
            "observations": len(rows),
            "source_status": ("disputed" if disputed else
                              "agreed" if len(rows) > 1 else "single_source"),
            "imported_at": now,
        }
        for side, quarters in (("home", home_q), ("away", away_q)):
            for index, (goals, behinds, points) in enumerate(quarters, start=1):
                values[f"{side}_q{index}_goals"] = goals
                values[f"{side}_q{index}_behinds"] = behinds
                values[f"{side}_q{index}_points"] = points
        details.append(values)
        stats["matches"] += 1

    if details:
        names = list(details[0])
        con.execute("DELETE FROM match_details")
        con.executemany(
            f"INSERT INTO match_details ({', '.join(names)}) "
            f"VALUES ({', '.join('?' for _ in names)})",
            [tuple(item[name] for name in names) for item in details])
    if conflicts:
        con.execute("DELETE FROM club_match_source_issues")
        con.executemany(
            "INSERT INTO club_match_source_issues VALUES (?,?,?,?,?,?,?,?)",
            conflicts)
    stats["conflicts"] = len(conflicts)
    con.commit()
    con.row_factory = None
    return stats


def mirror_onto_matches(con: sqlite3.Connection) -> int:
    """Copy attendance and cumulative quarter points onto matches.

    matches reserves eight quarter columns; they hold the cumulative points
    shown by AFL Tables. Per-quarter scoring and the goal/behind breakdown stay
    in match_details, which survives a rebuild.
    """
    if not table_exists(con, "matches"):
        return 0
    available = columns(con, "matches")
    assignments = ["attendance = (SELECT d.attendance FROM match_details d "
                   "WHERE d.match_id = matches.match_id)"]
    for side in ("home", "away"):
        for quarter in (1, 2, 3, 4):
            column = f"{side}_q{quarter}"
            if column in available:
                assignments.append(
                    f"{column} = (SELECT d.{side}_q{quarter}_points "
                    f"FROM match_details d WHERE d.match_id = matches.match_id)")
    con.execute(
        f"UPDATE matches SET {', '.join(assignments)} "
        f"WHERE match_id IN (SELECT match_id FROM match_details)")
    con.commit()
    return con.execute(
        "SELECT COUNT(*) FROM matches WHERE attendance IS NOT NULL").fetchone()[0]


# --------------------------------------------------------------------------


def run(db_path: Path, raw_dir: Path, *, club_ids=None, apply_only=False,
        dry_run=False, details=False) -> int:
    con = sqlite3.connect(db_path)
    create_schema(con)

    if not apply_only:
        observations, errors = parse_sources(raw_dir, club_ids)
        if errors:
            print(f"\n{len(errors)} parse error(s):")
            for line in errors[:20]:
                print(f"  {line}")
        if dry_run:
            print("\n--dry-run: nothing written")
            con.close()
            return 1 if errors else 0
        write_sources(con, observations, club_ids)

    if not table_exists(con, "matches"):
        print("No matches table. Run derive_matches.py first.")
        con.close()
        return 1

    link_counts = link_sources(con)
    print("\nLinking")
    for status, count in link_counts.items():
        print(f"  {status:16} {count:>7,}")

    stats = apply_details(con)
    print("\nMatch enrichment")
    for label, count in stats.items():
        print(f"  {label:16} {count:>7,}")

    filled = mirror_onto_matches(con)
    print(f"\nmatches.attendance populated: {filled:,}")

    if details:
        print("\nOutstanding source rows")
        for row in con.execute(
                "SELECT match_status, season, round, source_club_label, "
                "opponent_raw, match_date FROM club_match_sources "
                "WHERE match_status <> 'unique' ORDER BY season, match_date "
                "LIMIT 40"):
            print("  " + "  ".join(str(value) for value in row))
    con.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    ap.add_argument("--club", action="append", dest="clubs")
    ap.add_argument("--apply-only", action="store_true",
                    help="re-link and re-apply from stored observations")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--details", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args(argv)
    return run(args.db, args.raw_dir, club_ids=args.clubs,
               apply_only=args.apply_only, dry_run=args.dry_run,
               details=args.details or args.report)


if __name__ == "__main__":
    sys.exit(main())
