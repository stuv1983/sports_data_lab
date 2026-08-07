#!/usr/bin/env python3
"""
audit_all_games_linkage.py -- How much of the all-games history actually
made it into the queryable tables?

Read-only. Writes nothing, changes nothing. This is step 0.3 of the
consolidated build plan: a baseline measurement to take before step 4
(club history layer) and step 5 (quarter-data query layer) are built,
so the size of the gap between "observed" and "queryable" is a known
number rather than an assumption.

    python audit_all_games_linkage.py
    python audit_all_games_linkage.py --db gridley.db
    python audit_all_games_linkage.py --csv

Answers:

  1. How many club-match observations exist, and what season range do
     they cover? (club_match_sources)
  2. How many linked cleanly to a match_id, and how many did not, broken
     down by the reason (unmatched / ambiguous / score_mismatch)?
  3. Of the ones that linked, how many actually produced a match_details
     row -- i.e. survived orientation and had no scoring dispute?
  4. Which seasons carry the most unresolved rows? (a season-level
     concentration usually points to one systematic cause, not scattered
     noise)
  5. How much of the canonical `matches` table now has attendance and
     quarter-by-quarter detail, versus how much still could?

The gap between (1) and (3) is exactly what a query built on
`match_details` alone will silently miss. Step 4 of the build plan
exists because that gap is non-trivial: `club_match_sources` retains
every observation regardless of link outcome, and nothing above it
currently reads that table.
"""

import argparse
import csv
import sqlite3
import sys
from pathlib import Path


def table_exists(con, name):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def rule(title):
    print(f"\n{title}\n{'-' * len(title)}")


def pct(part, whole):
    return f"{100 * part / whole:5.1f}%" if whole else "   n/a"


# --------------------------------------------------------- 1. raw observations

def audit_observations(con):
    rule("1. club_match_sources -- raw observations")
    if not table_exists(con, "club_match_sources"):
        print("  club_match_sources: MISSING.")
        print("  Run load_club_all_games.py before this audit means anything.")
        return None

    total, seasons_from, seasons_to = con.execute(
        "SELECT COUNT(*), MIN(season), MAX(season) FROM club_match_sources"
    ).fetchone()
    print(f"  observations: {total:,}")
    print(f"  season range: {seasons_from}-{seasons_to}")

    unique_keys, finals = con.execute(
        "SELECT COUNT(DISTINCT source_game_key), SUM(is_final) "
        "FROM club_match_sources"
    ).fetchone()
    # Every match should appear on exactly two club pages (home + away, or
    # both sides of a final). A count noticeably below total/2 unique keys
    # means one side of some matches never got scraped.
    expected_pairs = total / 2
    print(f"  distinct game keys: {unique_keys:,}  "
          f"(expect close to {expected_pairs:,.0f} if every match has both sides)")
    if unique_keys < expected_pairs * 0.98:
        print("  NOTE: meaningfully fewer keys than observations/2 -- "
              "some matches may be missing one side entirely.")
    print(f"  finals rows: {finals or 0:,}")

    attendance_known = con.execute(
        "SELECT COUNT(*) FROM club_match_sources WHERE attendance IS NOT NULL"
    ).fetchone()[0]
    print(f"  attendance recorded: {attendance_known:,} of {total:,} "
          f"({pct(attendance_known, total)})")
    return total


# ---------------------------------------------------------- 2. link outcomes

def audit_link_status(con, total):
    rule("2. link outcome (club_match_sources.match_status)")
    if total is None:
        print("  skipped -- no observations table.")
        return {}

    rows = con.execute(
        "SELECT match_status, COUNT(*) FROM club_match_sources "
        "GROUP BY match_status ORDER BY 2 DESC"
    ).fetchall()
    counts = dict(rows)
    for status, n in rows:
        print(f"    {status:<16} {n:>8,}  ({pct(n, total)})")

    problems = total - counts.get("unique", 0)
    print(f"  total NOT cleanly linked: {problems:,} ({pct(problems, total)})")
    print("  meaning:")
    print("    unmatched      -- no candidate row found in matches at all")
    print("    ambiguous      -- more than one candidate matched season/date/pair")
    print("    score_mismatch -- a candidate matched but the recorded scores disagree")
    return counts


# ------------------------------------------------- 3. what reached match_details

def audit_match_details(con, total):
    rule("3. match_details -- what actually reached the queryable table")
    if not table_exists(con, "match_details"):
        print("  match_details: MISSING.")
        return

    md_total = con.execute("SELECT COUNT(*) FROM match_details").fetchone()[0]
    print(f"  match_details rows: {md_total:,}")
    if total:
        # match_details is one row per match; club_match_sources is one row
        # per club per match, so divide by two for a comparable figure.
        approx_matches_observed = total / 2
        print(f"  approx. matches observed (obs/2): {approx_matches_observed:,.0f}")
        print(f"  coverage: {pct(md_total, approx_matches_observed)}")

    rows = con.execute(
        "SELECT source_status, COUNT(*) FROM match_details "
        "GROUP BY source_status ORDER BY 2 DESC"
    ).fetchall()
    for status, n in rows:
        print(f"    {status:<16} {n:>8,}  ({pct(n, md_total)})")

    unoriented_note = con.execute(
        "SELECT COUNT(*) FROM match_details WHERE orientation IS NULL"
    ).fetchone()[0] if "orientation" in [r[1] for r in con.execute(
        "PRAGMA table_info(match_details)")] else None
    if unoriented_note:
        print(f"  rows with no resolved orientation: {unoriented_note:,}")

    if table_exists(con, "club_match_source_issues"):
        issues = con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT source_game_key) "
            "FROM club_match_source_issues"
        ).fetchone()
        print(f"  recorded source disagreements: {issues[0]:,} "
              f"across {issues[1]:,} matches")


# --------------------------------------------------- 4. where problems cluster

def audit_by_season(con):
    rule("4. unresolved rows by season (top 20)")
    if not table_exists(con, "club_match_sources"):
        print("  skipped -- no observations table.")
        return

    rows = con.execute(
        "SELECT season, COUNT(*) FROM club_match_sources "
        "WHERE match_status <> 'unique' "
        "GROUP BY season ORDER BY 2 DESC LIMIT 20"
    ).fetchall()
    if not rows:
        print("  none -- every observation linked cleanly.")
        return
    print("  season   unresolved")
    for season, n in rows:
        print(f"  {season:<8} {n:>6,}")
    print("  A concentration in one season or a short run of seasons usually")
    print("  points to a single systematic cause (a round-robin/bye quirk, a")
    print("  finals-format change, a club renaming) rather than scattered noise.")
    print("  Investigate the top season before assuming the total is evenly")
    print("  spread and therefore low-risk to ignore.")


# ------------------------------------------------ 5. what matches now carries

def audit_matches_table(con):
    rule("5. matches -- attendance and quarter-detail coverage")
    if not table_exists(con, "matches"):
        print("  matches: MISSING.")
        return

    total = con.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    attendance = con.execute(
        "SELECT COUNT(*) FROM matches WHERE attendance IS NOT NULL"
    ).fetchone()[0]
    print(f"  matches: {total:,}")
    print(f"  with attendance: {attendance:,} ({pct(attendance, total)})")

    cols = [r[1] for r in con.execute("PRAGMA table_info(matches)")]
    quarter_cols = [c for c in cols if c in
                    (f"{side}_q{q}" for side in ("home", "away") for q in (1, 2, 3, 4))]
    if quarter_cols:
        any_quarter = con.execute(
            f"SELECT COUNT(*) FROM matches WHERE {quarter_cols[0]} IS NOT NULL"
        ).fetchone()[0]
        print(f"  with quarter data ({quarter_cols[0]} populated): "
              f"{any_quarter:,} ({pct(any_quarter, total)})")
    else:
        print("  no quarter columns found on matches -- check schema.")

    if total - attendance:
        print(f"  gap: {total - attendance:,} matches with no attendance yet.")
        print("  Cross-check against section 2/3 above -- these are most likely")
        print("  the unmatched/ambiguous/score_mismatch rows from step 2, plus")
        print("  any club whose all-games page has not been scraped at all.")


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None,
                    help="database path (default: data_paths.default_db('afl'))")
    ap.add_argument("--csv", action="store_true",
                    help="also write all_games_linkage_audit.csv")
    args = ap.parse_args()

    db = args.db
    if db is None:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from data_paths import default_db
            db = default_db("afl")
        except Exception:
            db = "gridley.db"
    if not Path(db).exists():
        sys.exit(f"database not found: {db}")

    print(f"database: {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        total = audit_observations(con)
        status_counts = audit_link_status(con, total)
        audit_match_details(con, total)
        audit_by_season(con)
        audit_matches_table(con)

        rule("summary")
        if total is not None:
            unique = status_counts.get("unique", 0)
            print(f"  observations           {total:,}")
            print(f"  linked cleanly (unique) {unique:,}  ({pct(unique, total)})")
            print(f"  not cleanly linked      {total - unique:,}  "
                  f"({pct(total - unique, total)})")
            print("  This last figure is what step 4's club-history layer must")
            print("  read from club_match_sources directly to avoid, since it")
            print("  never reaches match_details.")

        if args.csv:
            out = Path("all_games_linkage_audit.csv")
            with out.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["match_status", "count"])
                for status, n in status_counts.items():
                    w.writerow([status, n])
            print(f"\n  wrote {out}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
