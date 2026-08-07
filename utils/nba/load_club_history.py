#!/usr/bin/env python3
"""Club-history rows for the NBA, derived from the schedule already loaded.

    python -m utils.nba.load_club_history
    python -m utils.nba.load_club_history --attendance C:\\sample\\nba\\game_info.csv

Fills ``club_match_sources``, the table afl/club_history.py reads.
Populating it is the whole of what turns the Past Games page and the Team
Explorer's match tabs on for the NBA, exactly as nfl/load_club_history.py
does for the NFL and mlb/load_retrosheet.py for the MLB.

The schedule needs no new source: build_nba_db.py already imports
``matches``, 71,396 games from 1946 to 2025 with both sides, both scores
and the arena. This module is a projection of that table into the
two-rows-per-match shape club_history expects.

Attendance is the one thing worth importing
-------------------------------------------

``matches.attendance`` is populated for 1,319 games -- the most recent
seasons only. The NBA box-score dataset's ``game_info.csv`` carries a crowd
figure for 52,638 games back to 1946, of which 51,357 join to a match here.
Passing ``--attendance`` fills the gap, and the crowd filters and
``crowd_averages()`` go from covering 2% of the database to 72% of it. The
database's own figure is kept wherever it has one; the CSV only fills
blanks.

The join is on ``game_id`` zero-padded to ten characters: the NBA's ids are
'0024600001' and this database stores them with the leading zeros stripped.

Two things the source gets wrong
--------------------------------

**Two games are recorded 0-0.** Both are 2024-season fixtures that were
postponed, not played and lost. They are skipped rather than written as
scoreless draws.

**Two games are recorded as ties.** Portland-Toronto in 2004 at 105-105 and
Dallas-Memphis in 2010 at 90-90. The NBA plays overtime until somebody
wins, so a tie is not a possible result and both are missing their final
overtime period at source. They are written as they stand, with result 'D',
rather than being guessed at: a 'D' in an NBA record is visible as a source
problem, whereas an invented winner is not.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path

from data_paths import sport_db

#: The club-history table afl/club_history.py reads.
MATCH_TABLE = "club_match_sources"

#: Where the NBA box-score dataset's game table usually sits. Optional --
#: the loader runs without it and simply leaves attendance as the database
#: already has it.
DEFAULT_ATTENDANCE_CSV = Path(r"C:\sample\nba\game_info.csv")

#: NBA game ids are ten characters; this database stores them with leading
#: zeros stripped, so the CSV's key is padded to match rather than the
#: database's being rewritten.
ID_WIDTH = 10


def _ensure_match_table(con: sqlite3.Connection) -> None:
    """Create the table in the shape club_history reads.

    Identical to the MLB's and the NFL's, deliberately: the reader is one
    generic module and a sport that invents its own column names cannot be
    read by it.
    """
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {MATCH_TABLE} (
            source_game_key TEXT NOT NULL,
            source_club_id  TEXT NOT NULL,
            season          INTEGER NOT NULL,
            round           TEXT,
            is_final        INTEGER NOT NULL DEFAULT 0,
            match_date      TEXT NOT NULL,
            venue_raw       TEXT,
            team_position   TEXT NOT NULL,
            result          TEXT,
            points_for      INTEGER,
            points_against  INTEGER,
            margin          INTEGER,
            attendance      INTEGER,
            match_id        INTEGER,
            match_status    TEXT NOT NULL DEFAULT 'unique',
            PRIMARY KEY (source_game_key, source_club_id)
        )
    """)
    for statement in (
        f"CREATE INDEX IF NOT EXISTS ix_cms_club ON {MATCH_TABLE}(source_club_id)",
        f"CREATE INDEX IF NOT EXISTS ix_cms_season ON {MATCH_TABLE}(season)",
        f"CREATE INDEX IF NOT EXISTS ix_cms_date ON {MATCH_TABLE}(match_date)",
    ):
        con.execute(statement)


def attendance_by_game(csv_path: Path) -> dict[str, int]:
    """game_id (as this database stores it) -> crowd, from the box-score CSV.

    Zero and blank are both "not recorded" rather than "nobody came", so
    neither is returned: club_history excludes a match with no figure from
    its crowd rankings, and a zero would take every smallest-crowd place.
    """
    out: dict[str, int] = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            raw = (row.get("attendance") or "").strip()
            game_id = (row.get("game_id") or "").strip()
            if not raw or not game_id:
                continue
            try:
                crowd = int(float(raw))
            except ValueError:
                continue
            if crowd > 0:
                out[game_id.zfill(ID_WIDTH)] = crowd
    return out


def _match_rows(row, crowds: dict[str, int]):
    """One schedule row as its two club-perspective rows.

    club_history's whole model is that a match is two rows sharing a
    source_game_key, which it self-joins to put the opponent beside the
    club. Both sides are emitted together or neither is: a single-sided row
    would join to nothing and silently vanish from every total.
    """
    (match_id, season, date, phase, round_label,
     home, away, home_score, away_score, venue, attendance) = row

    if home_score is None or away_score is None or not date:
        return
    home_score, away_score = int(home_score), int(away_score)
    # A postponed game recorded 0-0 is an absence, not a scoreless draw.
    if home_score == 0 and away_score == 0:
        return

    is_final = 1 if phase == "playoff" else 0

    if home_score > away_score:
        home_result, away_result = "W", "L"
    elif away_score > home_score:
        home_result, away_result = "L", "W"
    else:
        home_result = away_result = "D"

    # The database's own figure wins where it has one; the CSV only fills
    # blanks, so an import can add coverage but never overwrite the build.
    crowd = attendance if attendance else crowds.get(
        str(match_id).zfill(ID_WIDTH))

    # Every NBA game has a home team, including the Finals -- the series is
    # best-of-seven with home-court advantage, so there is no neutral
    # fixture to give club_history's 'F' to. Playoff games are marked by
    # is_final instead, which is what scope='finals' reads.
    for club, position, result, scored, conceded in (
        (home, "H", home_result, home_score, away_score),
        (away, "A", away_result, away_score, home_score),
    ):
        yield (str(match_id), club, int(season), round_label, is_final,
               str(date)[:10], venue or None, position, result,
               scored, conceded, scored - conceded, crowd, None, "unique")


def load_matches(con: sqlite3.Connection,
                 attendance_csv: Path | None = None) -> tuple[int, int]:
    """Fill club_match_sources from `matches`.

    Returns (matches written, rows given a crowd figure).
    """
    _ensure_match_table(con)
    con.execute(f"DELETE FROM {MATCH_TABLE}")

    crowds: dict[str, int] = {}
    if attendance_csv and attendance_csv.exists():
        crowds = attendance_by_game(attendance_csv)

    schedule = con.execute(
        "SELECT match_id, season, date, phase, round, "
        "       home_team, away_team, home_score, away_score, "
        "       venue, attendance "
        "FROM matches ORDER BY date, match_id").fetchall()

    def every_row():
        for row in schedule:
            yield from _match_rows(row, crowds)

    con.executemany(
        f"INSERT OR REPLACE INTO {MATCH_TABLE} VALUES "
        f"({','.join('?' * 15)})", every_row())
    con.commit()

    matches, with_crowd = con.execute(
        f"SELECT COUNT(DISTINCT source_game_key), "
        f"       SUM(attendance IS NOT NULL) FROM {MATCH_TABLE}").fetchone()
    return matches, with_crowd or 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attendance", type=Path,
                        default=DEFAULT_ATTENDANCE_CSV,
                        help="box-score game_info.csv to read crowds from; "
                             "skipped when absent")
    parser.add_argument("--db", default=sport_db("nba"),
                        help="database to write (default: the NBA database)")
    args = parser.parse_args()

    connection = sqlite3.connect(args.db)
    try:
        matches, with_crowd = load_matches(connection, args.attendance)
        rows, lo, hi = connection.execute(
            f"SELECT COUNT(*), MIN(season), MAX(season) FROM {MATCH_TABLE}"
        ).fetchone()
        print(f"{MATCH_TABLE}: {matches:,} matches "
              f"({rows:,} club rows, {lo}-{hi})")
        print(f"  with a crowd figure: {with_crowd:,} rows "
              f"({with_crowd / rows:.0%})")
    finally:
        connection.close()
