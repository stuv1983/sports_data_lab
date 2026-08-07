#!/usr/bin/env python3
"""Wins Above Replacement, from Baseball-Reference's published WAR files.

    python -m utils.mlb.load_war --dir C:\\sample\\mlb

Adds ``games.war`` (a player's WAR in one season with one team) and
``players.career_war``, which is what lets the grid answer "5+ WAR season"
and "60+ career WAR" squares.

Where the numbers come from
---------------------------

WAR is not in Lahman. It is not a counting stat that can be read off a
box score: it folds batting, baserunning, fielding, positional adjustment,
park factors and a replacement baseline into one number, and the weights
are the publisher's. Lahman ships none of those inputs, so there is nothing
here to compute it from and no honest way to approximate it -- an
approximation called "WAR" would be compared against the published figure
by every user who checked one, and would lose.

Baseball-Reference publishes the real thing as two flat files, updated
daily, precisely so that people take the file instead of crawling the site:

    https://www.baseball-reference.com/data/war_daily_bat.txt
    https://www.baseball-reference.com/data/war_daily_pitch.txt

Download both with a browser and point ``--dir`` at the folder. This
loader deliberately does not fetch them itself: Sports-Reference's terms
prohibit automated access without written permission, and their CDN
refuses it in any case. One manual download of a file they publish for
download is the sanctioned path; a scraper in this repository would not be.

The figures are therefore bWAR. FanGraphs' fWAR is a different model and
gives different numbers for the same player-season; mixing the two in one
column would make every square unanswerable. Only one is loaded, and the
UI names it.

Joining to this database
------------------------

The files' ``player_ID`` is Baseball-Reference's id, which for all but a
handful of players is byte-identical to Lahman's ``playerID`` and therefore
to this database's ``player_id`` -- 'aardsda01' in all three. Lahman's
``People.csv`` carries a ``bbrefID`` column for exactly the cases where it
is not, and that is used as a fallback so those players are not dropped.

Two-way players
---------------

A season appears in the batting file, the pitching file, or both. Ohtani's
2021 is in both, and his WAR that year is the sum. So the two files are
summed per (player, season, team) rather than one overwriting the other,
and a season is split across ``stint_ID`` rows when a player changed teams
mid-year, which sum the same way.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from collections import defaultdict
from pathlib import Path

from data_paths import sport_db

DEFAULT_DIR = Path(r"C:\sample\mlb")

BAT_FILE = "war_daily_bat.txt"
PITCH_FILE = "war_daily_pitch.txt"

DOWNLOAD_HINT = (
    "Download both files with a browser and pass their folder:\n"
    "  https://www.baseball-reference.com/data/war_daily_bat.txt\n"
    "  https://www.baseball-reference.com/data/war_daily_pitch.txt"
)


def _number(value: str) -> float | None:
    """BR writes an unavailable figure as 'NULL' or an empty cell."""
    text = (value or "").strip()
    if not text or text.upper() == "NULL":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def bbref_crosswalk(folder: Path) -> dict[str, str]:
    """bbrefID -> Lahman playerID, for the few where the two differ."""
    path = folder / "People.csv"
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return {row["bbrefID"].strip(): row["playerID"].strip()
                for row in csv.DictReader(handle)
                if row.get("bbrefID") and row.get("playerID")
                and row["bbrefID"].strip() != row["playerID"].strip()}


def team_crosswalk(folder: Path) -> dict[tuple[int, str], str]:
    """(season, Baseball-Reference team id) -> Lahman season team name."""
    path = folder / "Teams.csv"
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        output = {}
        for row in csv.DictReader(handle):
            team = (row.get("teamIDBR") or "").strip()
            name = (row.get("name") or "").strip()
            try:
                season = int(row["yearID"])
            except (KeyError, TypeError, ValueError):
                continue
            if team and name:
                output[(season, team)] = name
        return output


def read_war(path: Path) -> dict[tuple[str, int, str], float]:
    """(player, season, team) -> WAR, summed over stints.

    Rows with no WAR figure contribute nothing rather than a zero: a
    missing value means the publisher does not state one, and zero is a
    real WAR that would drag a career total down.
    """
    totals: dict[tuple[str, int, str], float] = defaultdict(float)
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            war = _number(row.get("WAR"))
            if war is None:
                continue
            player = (row.get("player_ID") or "").strip()
            team = (row.get("team_ID") or "").strip()
            try:
                season = int(row["year_ID"])
            except (KeyError, TypeError, ValueError):
                continue
            if player:
                totals[(player, season, team)] += war
    return dict(totals)


def _add_columns(con: sqlite3.Connection) -> None:
    """Add war / career_war once, leaving them alone on a re-run."""
    games = {r[1] for r in con.execute("PRAGMA table_info(games)")}
    if "war" not in games:
        con.execute("ALTER TABLE games ADD COLUMN war REAL")
    players = {r[1] for r in con.execute("PRAGMA table_info(players)")}
    if "career_war" not in players:
        con.execute("ALTER TABLE players ADD COLUMN career_war REAL")


def load(con: sqlite3.Connection, folder: Path) -> dict:
    """Fill games.war and players.career_war. Returns a coverage summary."""
    bat, pitch = folder / BAT_FILE, folder / PITCH_FILE
    missing = [f.name for f in (bat, pitch) if not f.exists()]
    if missing:
        raise SystemExit(f"missing {', '.join(missing)} in {folder}\n"
                         f"{DOWNLOAD_HINT}")

    _add_columns(con)
    con.execute("UPDATE games SET war = NULL")
    con.execute("UPDATE players SET career_war = NULL")

    war = defaultdict(float)
    for path in (bat, pitch):
        for key, value in read_war(path).items():
            war[key] += value

    crosswalk = bbref_crosswalk(folder)

    # `games` is keyed by the club's display name, while the WAR files use
    # Baseball-Reference team codes. Lahman's Teams.csv carries both
    # (`teamIDBR` and `name`), which preserves traded-player stints instead
    # of writing a combined season WAR onto every club row.
    team_names = team_crosswalk(folder)
    by_player_season: dict[tuple[str, int], float] = defaultdict(float)
    mapped: dict[tuple[str, int, str], float] = defaultdict(float)
    fallback: dict[tuple[str, int], float] = defaultdict(float)
    mapped_pairs = set()
    for (player, season, team), value in war.items():
        player_id = crosswalk.get(player, player)
        by_player_season[(player_id, season)] += value
        club = team_names.get((season, team))
        if club:
            mapped[(player_id, season, club)] += value
            mapped_pairs.add((player_id, season))
        else:
            fallback[(player_id, season)] += value

    # Materialize once and let SQLite join the indexed set. The old
    # executemany issued more than 100,000 UPDATE statements and took
    # minutes on the production database.
    con.execute("DROP TABLE IF EXISTS temp._war_load")
    con.execute("""CREATE TEMP TABLE _war_load (
        player_id TEXT, season INTEGER, club_hist TEXT, war REAL,
        PRIMARY KEY (player_id, season, club_hist))""")
    con.executemany(
        "INSERT INTO _war_load VALUES (?,?,?,?)",
        [(player_id, season, club, value)
         for (player_id, season, club), value in mapped.items()])
    # A missing team-code mapping is safe only when the database has one
    # club for that player-season. Current-year rows absent from Lahman and
    # ambiguous split seasons stay unmatched rather than being guessed.
    fallback_rows = [
        (player_id, season, None, value)
        for (player_id, season), value in fallback.items()
        if (player_id, season) not in mapped_pairs
    ]
    con.executemany(
        "INSERT INTO _war_load VALUES (?,?,?,?)", fallback_rows)

    # Resolve only the small fallback set, not every games row. A correlated
    # fallback on games made SQLite repeat this uniqueness test hundreds of
    # thousands of times.
    con.execute("""
        UPDATE _war_load AS w SET club_hist = (
            SELECT MIN(g.club_hist) FROM games g
             WHERE g.player_id=w.player_id AND g.season=w.season
               AND g.is_postseason=0
            HAVING COUNT(DISTINCT g.club_hist)=1)
         WHERE w.club_hist IS NULL
    """)
    con.execute("DELETE FROM _war_load WHERE club_hist IS NULL")
    con.execute("""
        UPDATE games AS g SET war=w.war
          FROM _war_load AS w
         WHERE g.is_postseason=0 AND g.player_id=w.player_id
           AND g.season=w.season AND g.club_hist=w.club_hist
    """)
    con.execute("DROP TABLE temp._war_load")

    # A career total is summed from what actually landed on `games`, not
    # from the file, so it can never claim WAR for a season this database
    # has no row for.
    con.execute("""
        UPDATE players SET career_war = (
            SELECT ROUND(SUM(g.war), 1) FROM games g
            WHERE g.player_id = players.player_id AND g.war IS NOT NULL)
    """)
    con.commit()

    seasons = con.execute(
        "SELECT COUNT(*) FROM games WHERE war IS NOT NULL").fetchone()[0]
    careers = con.execute(
        "SELECT COUNT(*) FROM players WHERE career_war IS NOT NULL"
    ).fetchone()[0]
    unmatched = len(by_player_season) - con.execute(
        "SELECT COUNT(DISTINCT player_id || '-' || season) FROM games "
        "WHERE war IS NOT NULL").fetchone()[0]
    return {"file_pairs": len(by_player_season), "season_rows": seasons,
            "players": careers, "unmatched": max(unmatched, 0)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR,
                        help="folder holding the two war_daily files")
    parser.add_argument("--db", default=sport_db("mlb"))
    args = parser.parse_args()

    connection = sqlite3.connect(args.db)
    try:
        report = load(connection, args.dir)
        print(f"games.war:          {report['season_rows']:,} season rows")
        print(f"players.career_war: {report['players']:,} players")
        if report["unmatched"]:
            print(f"  {report['unmatched']:,} player-seasons in the files "
                  f"have no row in this database")
    finally:
        connection.close()
