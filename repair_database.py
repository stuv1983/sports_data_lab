#!/usr/bin/env python3
"""Apply data migrations that do not require re-downloading the AFL source.

Repairs the historical ladder/wooden-spoon derivation and adds chronological
club-path fields to ``players``. Safe to re-run. A timestamped database backup
is recommended before any migration of a working copy.
"""

from __future__ import annotations

# Run standalone from anywhere: this file lives at the project root.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import argparse
import datetime as dt
import sqlite3
from pathlib import Path

from data_paths import default_db


def table_exists(con, name: str) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def column_exists(con, table: str, column: str) -> bool:
    return column in {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def rebuild_team_seasons(con) -> int:
    """Rebuild home-and-away ladders with lexicographic AFL ordering."""
    con.executescript("""
        DROP TABLE IF EXISTS _team_seasons_new;
        CREATE TABLE _team_seasons_new AS
        WITH appearances AS (
            SELECT DISTINCT season, club_now, date, result,
                            points_for, points_against
            FROM games
            WHERE is_final = 0
        ), totals AS (
            SELECT season, club_now,
                   COUNT(*) AS played,
                   SUM(result='W') AS wins,
                   SUM(result='D') AS draws,
                   SUM(result='L') AS losses,
                   SUM(points_for) AS points_for,
                   SUM(points_against) AS points_against
            FROM appearances
            GROUP BY season, club_now
        ), calculated AS (
            SELECT *,
                   wins * 4 + draws * 2 AS premiership_points,
                   CASE WHEN points_against <> 0
                        THEN points_for * 100.0 / points_against END AS percentage
            FROM totals
        ), ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                     PARTITION BY season
                     ORDER BY premiership_points DESC,
                              percentage DESC,
                              points_for DESC,
                              club_now ASC
                   ) AS ladder_rank,
                   COUNT(*) OVER (PARTITION BY season) AS teams_in_season
            FROM calculated
        )
        SELECT season, club_now, played, wins, draws, losses,
               points_for, points_against, premiership_points, percentage,
               ladder_rank,
               CASE WHEN ladder_rank = teams_in_season THEN 1 ELSE 0 END
                 AS wooden_spoon
        FROM ranked;
    """)
    con.execute("DROP TABLE IF EXISTS team_seasons")
    con.execute("ALTER TABLE _team_seasons_new RENAME TO team_seasons")
    con.execute(
        "CREATE INDEX IF NOT EXISTS ix_ts ON team_seasons(season, club_now)"
    )
    return con.execute("SELECT COUNT(*) FROM team_seasons").fetchone()[0]


def add_club_paths(con) -> int:
    """Store first-appearance-order team paths for every player."""
    if not column_exists(con, "players", "club_path_hist"):
        con.execute("ALTER TABLE players ADD COLUMN club_path_hist TEXT")
    if not column_exists(con, "players", "club_path_now"):
        con.execute("ALTER TABLE players ADD COLUMN club_path_now TEXT")

    current_id = None
    hist_path: list[str] = []
    now_path: list[str] = []
    updates = []
    rows = con.execute("""
        SELECT player_id, club_hist, club_now
        FROM games
        ORDER BY player_id, season, date, career_game_no, rowid
    """)
    for player_id, club_hist, club_now in rows:
        if current_id is not None and player_id != current_id:
            updates.append(("|".join(hist_path), "|".join(now_path), current_id))
            hist_path, now_path = [], []
        current_id = player_id
        if club_hist and (not hist_path or hist_path[-1] != club_hist):
            hist_path.append(club_hist)
        if club_now and (not now_path or now_path[-1] != club_now):
            now_path.append(club_now)
    if current_id is not None:
        updates.append(("|".join(hist_path), "|".join(now_path), current_id))

    con.executemany(
        "UPDATE players SET club_path_hist=?, club_path_now=? WHERE player_id=?",
        updates,
    )
    return len(updates)


def run(db_path: str) -> None:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(path)
    con = sqlite3.connect(path)
    try:
        if not table_exists(con, "games") or not table_exists(con, "players"):
            raise RuntimeError("database is missing players or games")
        con.execute("BEGIN IMMEDIATE")
        ladder_rows = rebuild_team_seasons(con)
        player_rows = add_club_paths(con)
        if table_exists(con, "meta"):
            con.execute("DELETE FROM meta WHERE key='repairs_applied'")
            con.execute(
                "INSERT INTO meta VALUES ('repairs_applied', ?)",
                (dt.datetime.now(dt.timezone.utc).isoformat(),),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    print(f"team_seasons rebuilt: {ladder_rows:,} rows")
    print(f"club paths updated: {player_rows:,} players")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=default_db("afl"))
    args = parser.parse_args()
    run(args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
