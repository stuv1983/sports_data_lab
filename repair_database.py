#!/usr/bin/env python3
"""Apply data migrations that do not require re-downloading the AFL source.

Removes duplicate player-game rows, repairs the historical ladder/wooden-spoon
derivation, and adds chronological club-path fields to ``players``. Safe to
re-run. A timestamped database backup is recommended before any migration of a
working copy.

Removing a duplicate changes career_games, so run ``recompute_obscurity.py``
afterwards if this reports any rows dropped -- obscurity ranks on those totals.
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


#: Career columns that are a function of the games rows, and so have to be
#: recomputed after any row is removed. Kept narrow on purpose: the columns
#: here are the ones a phantom appearance actually corrupts.
_CAREER_RECOUNT = """
    UPDATE players SET
        career_games   = (SELECT COUNT(*)      FROM games g WHERE g.player_id = players.player_id),
        career_goals   = (SELECT SUM(g.goals)  FROM games g WHERE g.player_id = players.player_id),
        finals_played  = (SELECT SUM(g.is_final) FROM games g WHERE g.player_id = players.player_id),
        debut_season   = (SELECT MIN(g.season) FROM games g WHERE g.player_id = players.player_id),
        final_season   = (SELECT MAX(g.season) FROM games g WHERE g.player_id = players.player_id)
    WHERE player_id IN (SELECT player_id FROM games)
"""


def drop_duplicate_games(con) -> tuple[int, list]:
    """Remove duplicate (player, date) rows and recount the careers they hit.

    The build now rejects these at source (see dedupe_games), but a database
    built before that gate still carries them, and rebuilding from scratch is
    a multi-minute download. Returns (rows_dropped, unresolved).
    """
    import dedupe_games

    keys = [tuple(r) for r in con.execute("""
        SELECT player_id, date FROM games
        GROUP BY player_id, date HAVING COUNT(*) > 1
    """)]
    if not keys:
        return 0, []

    affected = sorted({player_id for player_id, _ in keys})
    marks = ",".join("?" for _ in affected)
    columns = [r[1] for r in con.execute("PRAGMA table_info(games)")]
    selected = ", ".join(f"g.{c}" for c in columns)
    rows = [
        {
            "player_id": row[columns.index("player_id") + 1],
            "date": row[columns.index("date") + 1],
            "career_game_no": row[columns.index("career_game_no") + 1],
            "ref": row[0],
            "fields": tuple(row[1:]),
        }
        for row in con.execute(
            f"SELECT g.rowid, {selected} FROM games g "
            f"WHERE g.player_id IN ({marks})", affected)
    ]

    drop, unresolved = dedupe_games.resolve(rows)
    if drop:
        con.executemany("DELETE FROM games WHERE rowid = ?",
                        [(ref,) for ref in drop])
        con.execute(_CAREER_RECOUNT)
    return len(drop), unresolved


def run(db_path: str) -> None:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(path)
    con = sqlite3.connect(path)
    try:
        if not table_exists(con, "games") or not table_exists(con, "players"):
            raise RuntimeError("database is missing players or games")
        con.execute("BEGIN IMMEDIATE")
        # Before the ladders: team_seasons counts distinct appearances, so a
        # duplicate player-game row must be gone before anything derives
        # from it.
        dropped, unresolved = drop_duplicate_games(con)
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
    print(f"duplicate player-game rows removed: {dropped:,}")
    for item in unresolved:
        print(f"  UNRESOLVED player {item.player_id} on {item.date}: "
              f"career_game_no candidates {item.candidates} -- {item.reason}")
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
