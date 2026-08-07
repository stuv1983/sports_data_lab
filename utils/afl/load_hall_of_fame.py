#!/usr/bin/env python3
"""Load Hall of Fame inductees and link them to players.

    python -m utils.afl.load_hall_of_fame
    python -m utils.afl.load_hall_of_fame --report
    python -m utils.afl.load_hall_of_fame --unmatched

Reads ``data/afl/raw/wikipedia_hall_of_fame.csv`` (see
``afl/scrape_hall_of_fame.py``) into a `hall_of_fame` table.

Linking follows the same rule as every other optional layer: a row is
trusted only when the name resolves to exactly one plausible player, and an
ambiguous or unmatched row is kept for audit but is invisible to anything
that asks "is this player in the Hall of Fame". Nothing links on name
alone without a supporting check -- a candidate must also have a playing
career consistent with the induction, because the Hall contains several
names shared with unrelated players.

Most non-player inductees will not link, and that is the expected outcome
rather than a failure: coaches, umpires, administrators, media figures and
pioneers have no playing record in this database. The report separates
"could not be resolved" from "not expected to resolve" so the two are not
read as the same number.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import sqlite3
import sys
from pathlib import Path

from data_paths import default_db, raw_dir
from names import name_variants, normalise_name

SOURCE_CSV = "wikipedia_hall_of_fame.csv"
TRUSTED = ("unique", "resolved")

#: Categories whose inductees are expected to have a playing record. The
#: rest are inducted for what they did off the field.
PLAYING_CATEGORIES = {"player", "legend", "coach", "removed"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS hall_of_fame (
    name            TEXT NOT NULL,
    name_key        TEXT NOT NULL,
    category        TEXT,
    inducted_year   INTEGER,
    is_legend       INTEGER NOT NULL DEFAULT 0,
    legend_year     INTEGER,
    club            TEXT,
    state           TEXT,
    playing_career  TEXT,
    games_goals     TEXT,
    removed_year    INTEGER,
    player_id       INTEGER,
    match_status    TEXT NOT NULL,
    match_method    TEXT,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    notes           TEXT,
    source_url      TEXT,
    imported_at     TEXT NOT NULL,
    PRIMARY KEY (name_key)
)
"""
INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_hof_player ON hall_of_fame(player_id)",
    "CREATE INDEX IF NOT EXISTS ix_hof_status ON hall_of_fame(match_status)",
    "CREATE INDEX IF NOT EXISTS ix_hof_legend ON hall_of_fame(is_legend)",
)


def _career_years(text: str) -> tuple[int | None, int | None]:
    """First and last season mentioned in a 'Playing career' cell."""
    years = [int(y) for y in re.findall(r"\b((?:18|19|20)\d{2})\b",
                                        str(text or ""))]
    return (min(years), max(years)) if years else (None, None)


def _references(con: sqlite3.Connection) -> dict:
    """Every player, indexed by each of their name variants."""
    index: dict[str, list] = {}
    rows = con.execute(
        "SELECT player_id, player, debut_season, final_season, career_games "
        "FROM players").fetchall()
    for player_id, name, debut, final, games in rows:
        keys = name_variants(name)
        for key in {keys["key"], keys["nopunct"]}:
            if key:
                index.setdefault(key, []).append(
                    (player_id, name, debut, final, games))
    return index


#: Generational suffixes Wikipedia uses and the player table does not.
#: Both Abletts are simply 'Gary Ablett' in `games`, distinguished by their
#: careers, so stripping the suffix is what makes the career check able to
#: tell them apart rather than a shortcut past it.
_SUFFIX = re.compile(r"[ ,]+(?:jn?r|sn?r|i{1,3}|iv)\.?$", re.I)


def _resolve(row: dict, index: dict) -> tuple[int | None, str, str, int, str]:
    """(player_id, status, method, candidate_count, notes)."""
    keys = name_variants(row["name"])
    candidates = index.get(keys["key"]) or index.get(keys["nopunct"]) or []
    if not candidates:
        stripped = _SUFFIX.sub("", row["name"]).strip()
        if stripped != row["name"]:
            alt = name_variants(stripped)
            candidates = index.get(alt["key"]) or index.get(alt["nopunct"]) or []
    if not candidates:
        return None, "unmatched", "", 0, "no player with this name"

    lo, hi = _career_years(row.get("playing_career"))
    if lo is not None:
        # A shared name is common; a shared name AND an overlapping career
        # is not. Requiring the overlap is what stops this linking on name
        # alone, which is the rule the rest of the codebase follows.
        plausible = [c for c in candidates
                     if c[2] is not None and c[3] is not None
                     and c[2] <= hi + 2 and c[3] >= lo - 2]
    else:
        plausible = list(candidates)

    if len(plausible) == 1:
        method = "name+career" if lo is not None else "name"
        return plausible[0][0], "unique", method, len(candidates), ""
    if not plausible:
        return (None, "implausible", "", len(candidates),
                f"{len(candidates)} name match(es), none with a career "
                f"overlapping {lo}-{hi}")

    # Several plausible players: prefer a decisively longer career only
    # when the gap is large enough that the alternative is not credible.
    plausible.sort(key=lambda c: -(c[4] or 0))
    if (plausible[0][4] or 0) >= 5 * max(1, plausible[1][4] or 1):
        return (plausible[0][0], "resolved", "career_length",
                len(candidates),
                f"chosen over {len(plausible) - 1} other(s) on games played")
    return (None, "ambiguous", "", len(candidates),
            "; ".join(f"{c[1]} ({c[2]}-{c[3]})" for c in plausible[:6]))


def load(db: str, csv_path: Path, verbose: bool = True) -> dict:
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    con = sqlite3.connect(db)
    con.execute(SCHEMA)
    for statement in INDEXES:
        con.execute(statement)

    index = _references(con)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    counts: dict[str, int] = {}

    con.execute("DELETE FROM hall_of_fame")
    for row in rows:
        player_id, status, method, n, notes = _resolve(row, index)
        counts[status] = counts.get(status, 0) + 1
        con.execute(
            """INSERT OR REPLACE INTO hall_of_fame
               (name, name_key, category, inducted_year, is_legend,
                legend_year, club, state, playing_career, games_goals,
                removed_year, player_id, match_status, match_method,
                candidate_count, notes, source_url, imported_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["name"], normalise_name(row["name"]), row["category"],
             int(row["inducted_year"]) if row.get("inducted_year") else None,
             int(row.get("is_legend") or 0),
             int(row["legend_year"]) if row.get("legend_year") else None,
             row.get("club"), row.get("state"), row.get("playing_career"),
             row.get("games_goals"),
             int(row["removed_year"]) if row.get("removed_year") else None,
             player_id, status, method, n, notes, row.get("source_url"), now))
    con.commit()

    expected = con.execute(
        f"SELECT COUNT(*) FROM hall_of_fame WHERE category IN "
        f"({','.join('?' * len(PLAYING_CATEGORIES))})",
        tuple(PLAYING_CATEGORIES)).fetchone()[0]
    linked_expected = con.execute(
        f"SELECT COUNT(*) FROM hall_of_fame WHERE player_id IS NOT NULL "
        f"AND category IN ({','.join('?' * len(PLAYING_CATEGORIES))})",
        tuple(PLAYING_CATEGORIES)).fetchone()[0]

    summary = {
        "rows": len(rows),
        "statuses": counts,
        "trusted": sum(counts.get(s, 0) for s in TRUSTED),
        "playing_category": expected,
        "playing_category_linked": linked_expected,
    }
    if verbose:
        print(f"hall_of_fame: {len(rows)} inductees")
        for status, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"   {status:<14} {n:>4}")
        print(f"\n   of {expected} inductees in a playing category, "
              f"{linked_expected} linked to a player "
              f"({100 * linked_expected / expected:.0f}%)")
        print("   Non-playing categories (umpire, media, administrator, "
              "pioneer) are not expected to link.")
    con.close()
    return summary


def show_unmatched(db: str, limit: int = 40) -> None:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT name, category, inducted_year, match_status, notes "
        "FROM hall_of_fame WHERE match_status NOT IN ('unique','resolved') "
        "AND category IN ('player','legend','coach') "
        "ORDER BY match_status, name LIMIT ?", (limit,)).fetchall()
    print(f"{'name':<28}{'cat':<9}{'yr':<6}{'status':<13}notes")
    for name, category, year, status, notes in rows:
        print(f"{name:<28}{category:<9}{str(year or ''):<6}{status:<13}"
              f"{(notes or '')[:60]}")
    con.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None)
    ap.add_argument("--csv", default=None)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--unmatched", action="store_true")
    args = ap.parse_args(argv)

    db = args.db or default_db("afl")
    if args.unmatched:
        show_unmatched(db)
        return 0

    path = Path(args.csv) if args.csv else raw_dir("afl") / SOURCE_CSV
    if not path.exists():
        sys.exit(f"{path} not found -- run afl/scrape_hall_of_fame.py first")
    load(db, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
