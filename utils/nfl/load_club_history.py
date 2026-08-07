#!/usr/bin/env python3
"""Club-history rows for the NFL, derived from the schedule already loaded.

    python -m utils.nfl.load_club_history

Fills ``club_match_sources``, the table afl/club_history.py reads. That
module is generic despite where it lives, so populating this table is the
whole of what turns the Past Games page and the Team Explorer's match tabs
on for the NFL -- exactly as mlb/load_retrosheet.py does for the MLB.

Unlike the MLB, nothing is downloaded. build_nfl_db.py already imports
nflverse's schedule into ``matches``: 7,276 games from 1999 to 2025, each
with both sides, both scores, the stadium and the game type. This module is
a projection of that table into the two-rows-per-match shape club_history
expects, so it re-runs in a second and needs no network.

Three things the source decides for us
--------------------------------------

**Attendance is not in the data.** nflverse's schedule carries no crowd
figure, so every row here has ``attendance IS NULL``. club_history already
treats a missing crowd as missing rather than as zero -- ``crowds()``
excludes those rows instead of ranking them as the smallest ever -- so the
crowd filters simply return nothing for the NFL rather than lying. Filling
this in needs a second source and is not attempted here.

**Ties are 'D'.** The NFL has fifteen of them in this period. club_history
aggregates draws as ``result='D'``, so that is what a tie is written as,
rather than the NFL's own 'T'.

**A franchise's old identity stays its own club.** The team codes resolve
through ``teams`` to the name that franchise carried at the time, so Oakland
Raiders games belong to the Oakland Raiders and not to Las Vegas. This
matches the AFL layer, where Fitzroy is Fitzroy; the current-franchise view
is what ``Schema.club_lineage`` expresses, and folding the history in here
would leave no way back to it.
"""

from __future__ import annotations

import argparse
import sqlite3

from data_paths import sport_db

#: The club-history table afl/club_history.py reads.
MATCH_TABLE = "club_match_sources"

#: nflverse `game_type` -> the round label to record. Regular-season games
#: are labelled by week instead ('W1'...'W18'), which is what makes the
#: page's round filter worth having: 'REG' on 6,967 of 7,276 rows would
#: select almost everything.
PLAYOFF_ROUNDS = {"WC": "WC", "DIV": "DIV", "CON": "CON", "SB": "SB"}


def _ensure_match_table(con: sqlite3.Connection) -> None:
    """Create the table in the shape club_history reads.

    Identical to the MLB's, deliberately: the reader is one generic module
    and a sport that invents its own column names cannot be read by it.
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


def team_names(con: sqlite3.Connection) -> dict[str, str]:
    """Team code -> the franchise name that code stands for.

    `teams` holds both 'LA' and 'LAR' for the Rams; only 'LA' appears in
    `matches`, so the duplicate is harmless here.
    """
    return {code: name for code, name in con.execute(
        "SELECT team_abbr, team_name FROM teams "
        "WHERE team_abbr IS NOT NULL AND team_name IS NOT NULL")}


def _round_label(game_type: str, week) -> str | None:
    if game_type in PLAYOFF_ROUNDS:
        return PLAYOFF_ROUNDS[game_type]
    return f"W{int(week)}" if week is not None else None


def _match_rows(row, names: dict[str, str]):
    """One schedule row as its two club-perspective rows.

    club_history's whole model is that a match is two rows sharing a
    source_game_key, which it self-joins to put the opponent beside the
    club. Both sides are emitted together or neither is: a single-sided row
    would join to nothing and silently vanish from every total.
    """
    (game_id, season, game_type, week, gameday,
     away, away_score, home, home_score, location, stadium) = row

    # A scheduled-but-unplayed game has no result to record. Skipping keeps
    # a mid-season refresh from inventing 0-0 draws for next week's fixtures.
    if home_score is None or away_score is None or not gameday:
        return

    home_score, away_score = int(home_score), int(away_score)
    is_final = 0 if game_type == "REG" else 1
    round_label = _round_label(game_type, week)

    if home_score > away_score:
        home_result, away_result = "W", "L"
    elif away_score > home_score:
        home_result, away_result = "L", "W"
    else:
        home_result = away_result = "D"

    # The Super Bowl is the one fixture with no home side at all, so it gets
    # club_history's 'F' -- the same value the AFL uses for a final, and the
    # one that stops home_away_splits inventing a host. Every other game
    # keeps H/A, including the neutral-site international ones: the source
    # designates a home team there (a club gives up a home game for it), and
    # that designation is a fact rather than an assumption.
    positions = ("F", "F") if game_type == "SB" else ("H", "A")

    for code, position, result, scored, conceded in (
        (home, positions[0], home_result, home_score, away_score),
        (away, positions[1], away_result, away_score, home_score),
    ):
        yield (game_id, names.get(code, code), int(season), round_label,
               is_final, gameday, stadium or None, position, result,
               scored, conceded, scored - conceded, None, None, "unique")


def load_matches(con: sqlite3.Connection) -> int:
    """Fill club_match_sources from `matches`. Returns the match count."""
    _ensure_match_table(con)
    con.execute(f"DELETE FROM {MATCH_TABLE}")

    names = team_names(con)
    schedule = con.execute(
        "SELECT game_id, season, game_type, week, gameday, "
        "       away_team, away_score, home_team, home_score, "
        "       location, stadium "
        "FROM matches ORDER BY gameday, game_id").fetchall()

    def every_row():
        for row in schedule:
            yield from _match_rows(row, names)

    con.executemany(
        f"INSERT OR REPLACE INTO {MATCH_TABLE} VALUES "
        f"({','.join('?' * 15)})", every_row())
    con.commit()
    return con.execute(
        f"SELECT COUNT(DISTINCT source_game_key) FROM {MATCH_TABLE}"
    ).fetchone()[0]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=sport_db("nfl"),
                        help="database to write (default: the NFL database)")
    args = parser.parse_args()

    connection = sqlite3.connect(args.db)
    try:
        matches = load_matches(connection)
        rows, lo, hi = connection.execute(
            f"SELECT COUNT(*), MIN(season), MAX(season) FROM {MATCH_TABLE}"
        ).fetchone()
        print(f"{MATCH_TABLE}: {matches:,} matches "
              f"({rows:,} club rows, {lo}-{hi})")
    finally:
        connection.close()
