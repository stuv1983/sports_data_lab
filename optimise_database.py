#!/usr/bin/env python3
"""Add missing read indexes and refresh SQLite planner statistics.

Dry-run is the default. Stop Streamlit and all import/refresh jobs before
running with ``--apply``. Existing indexes are inspected by column prefix, so
an equivalent index with another name is reused instead of duplicated.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

INDEXES = [
    # Player profile, teammate, club-history and leaderboard access paths.
    ("idx_sdl_games_player_season_date", "games",
     ("player_id", "season", "date"), None),
    ("idx_sdl_games_season_club_hist_player", "games",
     ("season", "club_hist", "player_id"), None),
    ("idx_sdl_games_season_club_now_player", "games",
     ("season", "club_now", "player_id"), None),
    ("idx_sdl_games_venue_season_player", "games",
     ("venue", "season", "player_id"), None),
    ("idx_sdl_games_opponent_season_player", "games",
     ("opponent", "season", "player_id"), None),
    ("idx_sdl_games_final_season_player", "games",
     ("is_final", "season", "player_id"), None),
    ("idx_sdl_team_seasons_season_club", "team_seasons",
     ("season", "club_now"), None),
    ("idx_sdl_season_goals_leader_player", "season_goals",
     ("is_club_leading", "player_id"), None),

    # Optional layers. These are tiny today but become important as search
    # combines them with game/career predicates.
    ("idx_sdl_captain_trusted_lookup", "captaincies",
     ("club", "season", "player_id"),
     "match_status IN ('unique','resolved')"),
    ("idx_sdl_rising_star_trusted_lookup", "rising_star_nominees",
     ("club", "season", "player_id"),
     "match_status IN ('unique','resolved')"),
    ("idx_sdl_draft_links_trusted_player", "draft_links",
     ("player_id", "draft_rowid"),
     "match_status IN ('unique','resolved') AND player_id IS NOT NULL"),
    ("idx_sdl_person_links_trusted_player", "person_links",
     ("player_id", "dg_person_id"),
     "match_status IN ('from_draft','unique','resolved') "
     "AND player_id IS NOT NULL"),
    ("idx_sdl_awards_slug_person_season", "awards",
     ("award_slug", "dg_person_id", "season"), None),
    ("idx_sdl_all_australian_person_season", "all_australian",
     ("dg_person_id", "season"), None),
]


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(
        f"PRAGMA table_info({quote_ident(table)})")}


def existing_indexes(con: sqlite3.Connection, table: str) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []
    for row in con.execute(f"PRAGMA index_list({quote_ident(table)})"):
        index_name = row[1]
        cols = tuple(info[2] for info in con.execute(
            f"PRAGMA index_info({quote_ident(index_name)})") if info[2])
        if cols:
            out.append(cols)
    return out


def covered(existing: list[tuple[str, ...]], wanted: tuple[str, ...]) -> bool:
    return any(columns[:len(wanted)] == wanted for columns in existing)


def create_sql(name: str, table: str, columns: tuple[str, ...],
               where: str | None) -> str:
    cols = ", ".join(quote_ident(column) for column in columns)
    sql = (f"CREATE INDEX IF NOT EXISTS {quote_ident(name)} "
           f"ON {quote_ident(table)} ({cols})")
    if where:
        sql += f" WHERE {where}"
    return sql


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="gridley.db",
                        help="AFL SQLite database (default: gridley.db)")
    parser.add_argument("--apply", action="store_true",
                        help="create missing indexes and refresh statistics")
    args = parser.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        parser.error(f"database not found: {db}")

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"Sports Data Lab database optimisation — {mode}")
    print(f"Database: {db.resolve()}\n")

    con = sqlite3.connect(str(db), timeout=30.0)
    try:
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        pending: list[str] = []

        for name, table, columns, where in INDEXES:
            if table not in tables:
                print(f"skip     {name}: table {table} is not loaded")
                continue
            available = table_columns(con, table)
            missing = [column for column in columns if column not in available]
            if missing:
                print(f"skip     {name}: missing column(s) {', '.join(missing)}")
                continue
            current = existing_indexes(con, table)
            if covered(current, columns):
                print(f"covered  {table} ({', '.join(columns)})")
                continue
            sql = create_sql(name, table, columns, where)
            pending.append(sql)
            print(f"create   {name} on {table} ({', '.join(columns)})")

        if not args.apply:
            print(f"\nWould create {len(pending)} index(es). No changes made.")
            return 0

        with con:
            for sql in pending:
                con.execute(sql)
            con.execute("ANALYZE")
            con.execute("PRAGMA optimize")
        print(f"\nCreated {len(pending)} index(es); ANALYZE and PRAGMA optimize completed.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
