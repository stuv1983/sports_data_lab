#!/usr/bin/env python3
"""Build a standalone SQLite NFL database from nflreadpy/nflverse data.

Default output:
    data/nfl/nfl.db

Default data included:
    players          comprehensive player identities and metadata
    teams            NFL team catalogue
    matches          schedules and game results
    games            weekly player statistics (one source row per player/game)
    team_games       weekly team statistics
    rosters          season rosters
    draft_picks      draft history

The builder also creates derived tables:
    player_seasons, player_teams, team_seasons, stat_coverage,
    source_manifest, table_counts, meta

Optional extended data can be added with --extended. Play-by-play is excluded
by design; it is substantially larger than the core database.

Install:
    python -m pip install nflreadpy

Examples:
    python build_nfl_db.py
    python build_nfl_db.py --from-season 1999 --through-season 2025
    python build_nfl_db.py --all-history
    python build_nfl_db.py --extended
    python build_nfl_db.py --db C:\\sports_data_lab\\data\\nfl\\nfl.db
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os

# nflreadpy reads these settings when it is imported. Force its internal
# cache off before that import occurs; this builder writes its own raw
# Parquet snapshots and does not need nflreadpy's cache layer.
os.environ["NFLREADPY_CACHE"] = "off"
os.environ.pop("NFLREADPY_CACHE_DIR", None)

import re
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

BUILDER_VERSION = "1.0.3"
DEFAULT_DB = Path("data/nfl/nfl.db")
DEFAULT_CACHE = Path("data/nfl/cache/nflreadpy")
DEFAULT_RAW = Path("data/nfl/raw/nflreadpy")

CORE_MIN_SEASON = 1999
ROSTER_MIN_SEASON = 1920
DRAFT_MIN_SEASON = 1980
WEEKLY_ROSTER_MIN_SEASON = 2002
DEPTH_CHART_MIN_SEASON = 2001
INJURY_MIN_SEASON = 2009
SNAP_MIN_SEASON = 2012
OFFICIALS_MIN_SEASON = 2015
NEXTGEN_MIN_SEASON = 2016


@dataclass(frozen=True)
class DatasetSpec:
    table: str
    loader: str
    season_mode: str = "range"  # range, all, none
    minimum_season: int | None = None
    kwargs: dict[str, Any] | None = None
    required: bool = True


CORE_DATASETS = (
    DatasetSpec("players_source", "load_players", season_mode="none"),
    DatasetSpec("teams", "load_teams", season_mode="none"),
    DatasetSpec("matches", "load_schedules", minimum_season=ROSTER_MIN_SEASON),
    DatasetSpec(
        "games",
        "load_player_stats",
        kwargs={"summary_level": "week"},
    ),
    DatasetSpec(
        "team_games",
        "load_team_stats",
        kwargs={"summary_level": "week"},
    ),
    DatasetSpec("rosters", "load_rosters", minimum_season=ROSTER_MIN_SEASON),
    DatasetSpec("draft_picks", "load_draft_picks", minimum_season=DRAFT_MIN_SEASON),
)

EXTENDED_DATASETS = (
    DatasetSpec(
        "rosters_weekly",
        "load_rosters_weekly",
        minimum_season=WEEKLY_ROSTER_MIN_SEASON,
        required=False,
    ),
    DatasetSpec(
        "snap_counts",
        "load_snap_counts",
        minimum_season=SNAP_MIN_SEASON,
        required=False,
    ),
    DatasetSpec(
        "injuries",
        "load_injuries",
        minimum_season=INJURY_MIN_SEASON,
        required=False,
    ),
    DatasetSpec(
        "officials",
        "load_officials",
        minimum_season=OFFICIALS_MIN_SEASON,
        required=False,
    ),
    DatasetSpec("combine", "load_combine", required=False),
    DatasetSpec(
        "depth_charts",
        "load_depth_charts",
        minimum_season=DEPTH_CHART_MIN_SEASON,
        required=False,
    ),
    DatasetSpec("contracts", "load_contracts", season_mode="none", required=False),
    DatasetSpec("trades", "load_trades", season_mode="none", required=False),
)


class BuildError(RuntimeError):
    """Raised when a database cannot be built safely."""


def quote_ident(value: str) -> str:
    """Quote one SQLite identifier."""
    return '"' + str(value).replace('"', '""') + '"'


def safe_index_name(table: str, columns: Sequence[str]) -> str:
    raw = "ix_" + table + "_" + "_".join(columns)
    return re.sub(r"[^a-zA-Z0-9_]+", "_", raw)[:60]


def normalise_name(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def sqlite_type(dtype: object) -> str:
    """Map a Polars data type to a conservative SQLite storage class."""
    name = str(dtype).lower()
    if any(token in name for token in ("int", "uint", "bool")):
        return "INTEGER"
    if any(token in name for token in ("float", "decimal")):
        return "REAL"
    if "binary" in name:
        return "BLOB"
    return "TEXT"


def sqlite_value(value: Any) -> Any:
    """Convert a Polars/Python scalar to something sqlite3 can bind."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (str, int, bytes)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple, set, dict)):
        return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return sqlite_value(item())
        except Exception:
            pass
    return str(value)


def chunks(rows: Iterable[Sequence[Any]], size: int = 5000) -> Iterator[list[Sequence[Any]]]:
    batch: list[Sequence[Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def dataframe_columns(frame: Any) -> list[str]:
    columns = list(getattr(frame, "columns", []) or [])
    if not columns:
        raise BuildError("nflreadpy returned a dataframe with no columns")
    if len(columns) != len(set(columns)):
        raise BuildError("nflreadpy returned duplicate column names")
    return [str(column) for column in columns]


def dataframe_schema(frame: Any, columns: Sequence[str]) -> dict[str, object]:
    schema = getattr(frame, "schema", None)
    if isinstance(schema, dict):
        return {str(key): value for key, value in schema.items()}
    if schema is not None:
        try:
            return {str(key): value for key, value in schema.items()}
        except Exception:
            pass
    return {column: "Text" for column in columns}


def dataframe_rows(frame: Any) -> Iterator[tuple[Any, ...]]:
    iterator = getattr(frame, "iter_rows", None)
    if not callable(iterator):
        raise BuildError("nflreadpy did not return a Polars-compatible dataframe")
    for row in iterator(named=False):
        yield tuple(sqlite_value(value) for value in row)


def dataframe_height(frame: Any) -> int:
    height = getattr(frame, "height", None)
    if height is not None:
        return int(height)
    shape = getattr(frame, "shape", None)
    if shape:
        return int(shape[0])
    return sum(1 for _ in dataframe_rows(frame))


def write_frame(
    con: sqlite3.Connection,
    table: str,
    frame: Any,
    *,
    batch_size: int = 5000,
) -> tuple[int, list[str]]:
    """Replace one SQLite table with the complete Polars dataframe."""
    columns = dataframe_columns(frame)
    schema = dataframe_schema(frame, columns)

    con.execute(f"DROP TABLE IF EXISTS {quote_ident(table)}")
    definitions = ", ".join(
        f"{quote_ident(column)} {sqlite_type(schema.get(column))}"
        for column in columns
    )
    con.execute(f"CREATE TABLE {quote_ident(table)} ({definitions})")

    marks = ",".join("?" for _ in columns)
    insert_sql = (
        f"INSERT INTO {quote_ident(table)} "
        f"({','.join(quote_ident(column) for column in columns)}) "
        f"VALUES ({marks})"
    )
    total = 0
    for batch in chunks(dataframe_rows(frame), batch_size):
        con.executemany(insert_sql, batch)
        total += len(batch)
    return total, columns


def save_parquet(frame: Any, path: Path) -> tuple[int, str]:
    writer = getattr(frame, "write_parquet", None)
    if not callable(writer):
        raise BuildError("Polars write_parquet is unavailable")
    path.parent.mkdir(parents=True, exist_ok=True)
    writer(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return path.stat().st_size, digest.hexdigest()


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (table,),
    ).fetchone() is not None


def table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in con.execute(f"PRAGMA table_info({quote_ident(table)})")]


def first_existing(columns: Iterable[str], choices: Sequence[str]) -> str | None:
    have = set(columns)
    return next((choice for choice in choices if choice in have), None)


def add_column(con: sqlite3.Connection, table: str, column: str, sql_type: str) -> None:
    if column not in table_columns(con, table):
        con.execute(
            f"ALTER TABLE {quote_ident(table)} ADD COLUMN "
            f"{quote_ident(column)} {sql_type}"
        )


def create_index_if_possible(
    con: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    *,
    unique: bool = False,
) -> None:
    if not table_exists(con, table):
        return
    have = set(table_columns(con, table))
    selected = [column for column in columns if column in have]
    if len(selected) != len(columns):
        return
    index = safe_index_name(table, selected)
    con.execute(
        f"CREATE {'UNIQUE ' if unique else ''}INDEX IF NOT EXISTS "
        f"{quote_ident(index)} ON {quote_ident(table)} "
        f"({','.join(quote_ident(column) for column in selected)})"
    )


def create_source_indexes(con: sqlite3.Connection) -> None:
    for table, columns in (
        ("matches", ("game_id",)),
        ("matches", ("season", "week")),
        ("matches", ("home_team",)),
        ("matches", ("away_team",)),
        ("games", ("player_id",)),
        ("games", ("game_id",)),
        ("games", ("season", "week")),
        ("games", ("team", "season")),
        ("team_games", ("game_id",)),
        ("team_games", ("team", "season")),
        ("rosters", ("season", "team")),
        ("draft_picks", ("season",)),
    ):
        create_index_if_possible(con, table, columns)

    if table_exists(con, "players_source"):
        cols = table_columns(con, "players_source")
        source_id = first_existing(cols, ("gsis_id", "player_id"))
        if source_id:
            create_index_if_possible(con, "players_source", (source_id,))


def scalar(con: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> Any:
    row = con.execute(sql, params).fetchone()
    return None if row is None else row[0]


def create_players(con: sqlite3.Connection) -> None:
    """Create a query-friendly player table from identities and weekly stats."""
    con.execute("DROP TABLE IF EXISTS players")
    con.execute("CREATE TABLE players AS SELECT * FROM players_source")

    source_cols = table_columns(con, "players")
    source_id = first_existing(source_cols, ("gsis_id", "player_id"))
    if source_id is None:
        raise BuildError("players_source has neither gsis_id nor player_id")

    name_col = first_existing(
        source_cols,
        ("display_name", "full_name", "football_name", "short_name", "name"),
    )

    # Retain the nflverse identifier and expose stable convenience columns.
    if "player_id" not in source_cols:
        add_column(con, "players", "player_id", "TEXT")
        con.execute(
            f"UPDATE players SET player_id={quote_ident(source_id)} "
            "WHERE player_id IS NULL"
        )
    elif source_id != "player_id":
        con.execute(
            f"UPDATE players SET player_id=COALESCE(player_id,{quote_ident(source_id)})"
        )

    add_column(con, "players", "player", "TEXT")
    if name_col:
        con.execute(
            f"UPDATE players SET player=COALESCE(NULLIF(player,''),"
            f"{quote_ident(name_col)})"
        )

    add_column(con, "players", "name_key", "TEXT")
    rows = con.execute("SELECT rowid, player FROM players").fetchall()
    con.executemany(
        "UPDATE players SET name_key=? WHERE rowid=?",
        [(normalise_name(name), rowid) for rowid, name in rows],
    )

    for column, sql_type in (
        ("debut_season", "INTEGER"),
        ("final_season", "INTEGER"),
        ("career_games", "INTEGER"),
        ("career_regular_games", "INTEGER"),
        ("career_postseason_games", "INTEGER"),
        ("teams_hist", "TEXT"),
        ("n_teams", "INTEGER"),
        ("career_touchdowns", "REAL"),
        ("career_passing_yards", "REAL"),
        ("career_rushing_yards", "REAL"),
        ("career_receiving_yards", "REAL"),
        ("career_tackles", "REAL"),
        ("career_sacks", "REAL"),
        ("career_interceptions", "REAL"),
    ):
        add_column(con, "players", column, sql_type)

    game_cols = table_columns(con, "games")
    game_player = first_existing(game_cols, ("player_id", "gsis_id"))
    if game_player is None:
        raise BuildError("games has neither player_id nor gsis_id")
    if "season" not in game_cols:
        raise BuildError("games is missing season")

    game_key = first_existing(game_cols, ("game_id",))
    season_type = first_existing(game_cols, ("season_type", "game_type"))
    team_col = first_existing(game_cols, ("team", "recent_team"))

    count_expr = (
        f"COUNT(DISTINCT {quote_ident(game_key)})"
        if game_key
        else "COUNT(*)"
    )
    regular_expr = count_expr
    postseason_expr = "0"
    if season_type:
        regular_expr = (
            f"COUNT(DISTINCT CASE WHEN UPPER({quote_ident(season_type)})='REG' "
            f"THEN {quote_ident(game_key) if game_key else 'rowid'} END)"
        )
        postseason_expr = (
            f"COUNT(DISTINCT CASE WHEN UPPER({quote_ident(season_type)})<>'REG' "
            f"THEN {quote_ident(game_key) if game_key else 'rowid'} END)"
        )

    sums: dict[str, str] = {
        "career_passing_yards": "passing_yards",
        "career_rushing_yards": "rushing_yards",
        "career_receiving_yards": "receiving_yards",
        "career_tackles": first_existing(
            game_cols,
            ("tackles", "def_tackles", "def_tackles_combined"),
        ) or "",
        "career_sacks": first_existing(
            game_cols,
            ("sacks", "def_sacks", "def_sacks_solo"),
        ) or "",
        "career_interceptions": first_existing(
            game_cols,
            ("def_interceptions", "interceptions", "defensive_interceptions"),
        ) or "",
    }
    touchdown_columns = [
        column
        for column in (
            "passing_tds",
            "rushing_tds",
            "receiving_tds",
            "special_teams_tds",
            "def_tds",
            "defensive_tds",
        )
        if column in game_cols
    ]

    aggregate_fields = [
        f"{quote_ident(game_player)} AS player_id",
        "MIN(season) AS debut_season",
        "MAX(season) AS final_season",
        f"{count_expr} AS career_games",
        f"{regular_expr} AS career_regular_games",
        f"{postseason_expr} AS career_postseason_games",
    ]
    for target, source in sums.items():
        if source and source in game_cols:
            aggregate_fields.append(
                f"SUM(COALESCE({quote_ident(source)},0)) AS {quote_ident(target)}"
            )
        else:
            aggregate_fields.append(f"0 AS {quote_ident(target)}")
    if touchdown_columns:
        touchdown_sum = "+".join(
            f"COALESCE({quote_ident(column)},0)" for column in touchdown_columns
        )
        aggregate_fields.append(
            f"SUM({touchdown_sum}) AS career_touchdowns"
        )
    else:
        aggregate_fields.append("0 AS career_touchdowns")

    con.execute("DROP TABLE IF EXISTS _player_aggregates")
    con.execute(
        "CREATE TEMP TABLE _player_aggregates AS SELECT "
        + ",".join(aggregate_fields)
        + f" FROM games WHERE {quote_ident(game_player)} IS NOT NULL "
        + f"GROUP BY {quote_ident(game_player)}"
    )

    targets = [
        "debut_season",
        "final_season",
        "career_games",
        "career_regular_games",
        "career_postseason_games",
        "career_touchdowns",
        "career_passing_yards",
        "career_rushing_yards",
        "career_receiving_yards",
        "career_tackles",
        "career_sacks",
        "career_interceptions",
    ]
    assignments = ",".join(
        f"{quote_ident(column)}=(SELECT a.{quote_ident(column)} "
        "FROM _player_aggregates a WHERE a.player_id=players.player_id)"
        for column in targets
    )
    con.execute(
        f"UPDATE players SET {assignments} "
        "WHERE EXISTS (SELECT 1 FROM _player_aggregates a "
        "WHERE a.player_id=players.player_id)"
    )

    # Add player IDs that appear in weekly stats but are absent from identities.
    player_name_col = first_existing(
        game_cols,
        ("player_display_name", "player_name", "player"),
    )
    missing = con.execute(
        f"SELECT DISTINCT g.{quote_ident(game_player)}, "
        + (f"g.{quote_ident(player_name_col)}" if player_name_col else "NULL")
        + " FROM games g LEFT JOIN players p "
        f"ON p.player_id=g.{quote_ident(game_player)} "
        f"WHERE g.{quote_ident(game_player)} IS NOT NULL AND p.player_id IS NULL"
    ).fetchall()
    if missing:
        # The players source has many columns. Insert only the convenience fields.
        con.executemany(
            "INSERT INTO players (player_id, player, name_key) VALUES (?,?,?)",
            [(pid, name, normalise_name(name)) for pid, name in missing],
        )
        con.execute(
            f"UPDATE players SET {assignments} "
            "WHERE EXISTS (SELECT 1 FROM _player_aggregates a "
            "WHERE a.player_id=players.player_id)"
        )

    if team_col:
        con.execute("DROP TABLE IF EXISTS _player_teams")
        con.execute(
            "CREATE TEMP TABLE _player_teams AS "
            f"SELECT {quote_ident(game_player)} AS player_id, "
            f"{quote_ident(team_col)} AS team, MIN(season) AS first_season, "
            f"MAX(season) AS last_season, {count_expr} AS games "
            f"FROM games WHERE {quote_ident(game_player)} IS NOT NULL "
            f"AND {quote_ident(team_col)} IS NOT NULL "
            f"GROUP BY {quote_ident(game_player)}, {quote_ident(team_col)}"
        )
        team_rows = con.execute(
            "SELECT player_id, team FROM _player_teams "
            "ORDER BY player_id, first_season, team"
        ).fetchall()
        by_player: dict[str, list[str]] = {}
        for pid, team in team_rows:
            by_player.setdefault(str(pid), []).append(str(team))
        con.executemany(
            "UPDATE players SET teams_hist=?, n_teams=? WHERE player_id=?",
            [("|".join(teams), len(teams), pid) for pid, teams in by_player.items()],
        )

    con.execute("DROP TABLE IF EXISTS _player_aggregates")
    con.execute("DROP TABLE IF EXISTS _player_teams")
    create_index_if_possible(con, "players", ("player_id",), unique=False)
    create_index_if_possible(con, "players", ("name_key",))
    create_index_if_possible(con, "players", ("debut_season", "final_season"))


def numeric_columns(con: sqlite3.Connection, table: str) -> list[str]:
    result = []
    for row in con.execute(f"PRAGMA table_info({quote_ident(table)})"):
        name, declared = str(row[1]), str(row[2] or "").upper()
        if declared in {"INTEGER", "REAL", "NUMERIC"}:
            result.append(name)
    return result


def create_player_seasons(con: sqlite3.Connection) -> None:
    con.execute("DROP TABLE IF EXISTS player_seasons")
    cols = table_columns(con, "games")
    player = first_existing(cols, ("player_id", "gsis_id"))
    team = first_existing(cols, ("team", "recent_team"))
    if player is None or team is None or "season" not in cols:
        raise BuildError("games lacks player/team/season columns for player_seasons")

    group = [player, "season", team]
    passthrough = [
        column for column in ("player_display_name", "player_name", "position", "position_group")
        if column in cols
    ]
    exclusions = set(group) | set(passthrough) | {
        "week",
        "game_id",
        "opponent_team",
        "season_type",
    }
    totals = [column for column in numeric_columns(con, "games") if column not in exclusions]
    fields = [
        f"{quote_ident(player)} AS player_id",
        "season",
        f"{quote_ident(team)} AS team",
    ]
    fields.extend(f"MAX({quote_ident(column)}) AS {quote_ident(column)}" for column in passthrough)
    if "game_id" in cols:
        fields.append("COUNT(DISTINCT game_id) AS games")
    else:
        fields.append("COUNT(*) AS games")
    fields.extend(
        f"SUM(COALESCE({quote_ident(column)},0)) AS {quote_ident(column)}"
        for column in totals
    )
    con.execute(
        "CREATE TABLE player_seasons AS SELECT "
        + ",".join(fields)
        + " FROM games GROUP BY "
        + ",".join(quote_ident(column) for column in group)
    )
    create_index_if_possible(con, "player_seasons", ("player_id", "season"))
    create_index_if_possible(con, "player_seasons", ("team", "season"))


def create_player_teams(con: sqlite3.Connection) -> None:
    con.execute("DROP TABLE IF EXISTS player_teams")
    cols = table_columns(con, "games")
    player = first_existing(cols, ("player_id", "gsis_id"))
    team = first_existing(cols, ("team", "recent_team"))
    if player is None or team is None:
        raise BuildError("games lacks player/team columns for player_teams")
    game_count = "COUNT(DISTINCT game_id)" if "game_id" in cols else "COUNT(*)"
    con.execute(
        "CREATE TABLE player_teams AS "
        f"SELECT {quote_ident(player)} AS player_id, "
        f"{quote_ident(team)} AS team, MIN(season) AS first_season, "
        f"MAX(season) AS last_season, {game_count} AS games "
        f"FROM games WHERE {quote_ident(player)} IS NOT NULL "
        f"AND {quote_ident(team)} IS NOT NULL "
        f"GROUP BY {quote_ident(player)}, {quote_ident(team)}"
    )
    create_index_if_possible(con, "player_teams", ("player_id", "team"))
    create_index_if_possible(con, "player_teams", ("team", "first_season", "last_season"))


def create_team_seasons(con: sqlite3.Connection) -> None:
    con.execute("DROP TABLE IF EXISTS team_seasons")
    cols = table_columns(con, "team_games")
    team = first_existing(cols, ("team", "recent_team"))
    if team is None or "season" not in cols:
        raise BuildError("team_games lacks team/season columns for team_seasons")
    exclusions = {
        team,
        "season",
        "week",
        "game_id",
        "opponent_team",
        "season_type",
    }
    totals = [column for column in numeric_columns(con, "team_games") if column not in exclusions]
    fields = [f"{quote_ident(team)} AS team", "season"]
    if "game_id" in cols:
        fields.append("COUNT(DISTINCT game_id) AS games")
    else:
        fields.append("COUNT(*) AS games")
    fields.extend(
        f"SUM(COALESCE({quote_ident(column)},0)) AS {quote_ident(column)}"
        for column in totals
    )
    con.execute(
        "CREATE TABLE team_seasons AS SELECT "
        + ",".join(fields)
        + f" FROM team_games GROUP BY {quote_ident(team)}, season"
    )
    create_index_if_possible(con, "team_seasons", ("team", "season"))


def create_stat_coverage(con: sqlite3.Connection) -> None:
    con.execute("DROP TABLE IF EXISTS stat_coverage")
    con.execute(
        "CREATE TABLE stat_coverage ("
        "table_name TEXT NOT NULL, stat_name TEXT NOT NULL, "
        "available_from INTEGER, available_to INTEGER, "
        "populated_rows INTEGER NOT NULL, total_rows INTEGER NOT NULL, "
        "PRIMARY KEY (table_name, stat_name))"
    )
    for table in ("games", "team_games"):
        cols = set(table_columns(con, table))
        if "season" not in cols:
            continue
        total = int(scalar(con, f"SELECT COUNT(*) FROM {quote_ident(table)}") or 0)
        for stat in numeric_columns(con, table):
            if stat in {"season", "week"}:
                continue
            sql = (
                f"SELECT MIN(CASE WHEN {quote_ident(stat)} IS NOT NULL "
                f"AND {quote_ident(stat)}<>0 THEN season END), "
                f"MAX(CASE WHEN {quote_ident(stat)} IS NOT NULL "
                f"AND {quote_ident(stat)}<>0 THEN season END), "
                f"SUM({quote_ident(stat)} IS NOT NULL) "
                f"FROM {quote_ident(table)}"
            )
            first, last, populated = con.execute(sql).fetchone()
            con.execute(
                "INSERT INTO stat_coverage VALUES (?,?,?,?,?,?)",
                (table, stat, first, last, int(populated or 0), total),
            )


def create_compatibility_views(con: sqlite3.Connection) -> None:
    for view in ("schedules", "player_stats", "team_stats"):
        con.execute(f"DROP VIEW IF EXISTS {quote_ident(view)}")
    con.execute("CREATE VIEW schedules AS SELECT * FROM matches")
    con.execute("CREATE VIEW player_stats AS SELECT * FROM games")
    con.execute("CREATE VIEW team_stats AS SELECT * FROM team_games")


def validate_database(con: sqlite3.Connection) -> None:
    required = (
        "players",
        "teams",
        "matches",
        "games",
        "team_games",
        "rosters",
        "draft_picks",
        "player_seasons",
        "player_teams",
        "team_seasons",
    )
    missing = [table for table in required if not table_exists(con, table)]
    if missing:
        raise BuildError(f"database is missing tables: {', '.join(missing)}")

    empty = [
        table
        for table in ("players", "teams", "matches", "games", "team_games")
        if int(scalar(con, f"SELECT COUNT(*) FROM {quote_ident(table)}") or 0) == 0
    ]
    if empty:
        raise BuildError(f"required tables are empty: {', '.join(empty)}")

    match_cols = set(table_columns(con, "matches"))
    required_match_cols = {"game_id", "season", "home_team", "away_team"}
    if not required_match_cols <= match_cols:
        raise BuildError(
            "matches is missing required nflverse columns: "
            + ", ".join(sorted(required_match_cols - match_cols))
        )
    duplicate_games = int(
        scalar(
            con,
            "SELECT COUNT(*) FROM (SELECT game_id FROM matches "
            "WHERE game_id IS NOT NULL GROUP BY game_id HAVING COUNT(*)>1)",
        )
        or 0
    )
    if duplicate_games:
        raise BuildError(f"matches contains {duplicate_games} duplicate game_id values")

    game_cols = set(table_columns(con, "games"))
    required_game_cols = {"player_id", "season", "game_id", "team"}
    if not required_game_cols <= game_cols:
        raise BuildError(
            "games is missing required nflverse columns: "
            + ", ".join(sorted(required_game_cols - game_cols))
        )

    invalid_schedule = int(
        scalar(
            con,
            "SELECT COUNT(*) FROM matches WHERE home_team=away_team "
            "AND home_team IS NOT NULL",
        )
        or 0
    )
    if invalid_schedule:
        raise BuildError(f"matches contains {invalid_schedule} games with the same home and away team")

    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise BuildError(f"SQLite integrity_check failed: {integrity}")


def create_table_counts(con: sqlite3.Connection) -> None:
    con.execute("DROP TABLE IF EXISTS table_counts")
    con.execute(
        "CREATE TABLE table_counts (table_name TEXT PRIMARY KEY, row_count INTEGER NOT NULL)"
    )
    names = [
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    con.executemany(
        "INSERT INTO table_counts VALUES (?,?)",
        [
            (
                name,
                int(scalar(con, f"SELECT COUNT(*) FROM {quote_ident(name)}") or 0),
            )
            for name in names
            if name != "table_counts"
        ],
    )


def configure_nflreadpy(cache_dir: Path, verbose: bool) -> Any:
    """Import nflreadpy with its internal cache disabled before import.

    nflreadpy reads environment configuration during package import. A fresh
    builder process therefore sets ``NFLREADPY_CACHE=off`` at module startup
    and avoids passing ``cache_dir`` through nflreadpy altogether. The local
    directory remains available for this builder's own durable snapshots.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Reassert the values here so a caller cannot change them between module
    # import and the first dataset load. Do not call update_config(cache_dir=...)
    # because affected nflreadpy releases retain strings and later call mkdir().
    os.environ["NFLREADPY_CACHE"] = "off"
    os.environ.pop("NFLREADPY_CACHE_DIR", None)
    os.environ["NFLREADPY_VERBOSE"] = "true" if verbose else "false"
    os.environ["NFLREADPY_TIMEOUT"] = "120"
    os.environ["NFLREADPY_USER_AGENT"] = (
        f"sports-data-lab-nfl-builder/{BUILDER_VERSION}"
    )

    try:
        import nflreadpy as nfl
    except ImportError as exc:
        raise BuildError(
            "nflreadpy is not installed. Run: python -m pip install nflreadpy"
        ) from exc

    return nfl


def dataset_seasons(
    spec: DatasetSpec,
    from_season: int,
    through_season: int,
    all_history: bool,
) -> list[int] | bool | None:
    if spec.season_mode == "none":
        return None
    if spec.season_mode == "all":
        return True
    minimum = spec.minimum_season or CORE_MIN_SEASON
    start = minimum if all_history else max(from_season, minimum)
    if start > through_season:
        return []
    return list(range(start, through_season + 1))


def call_loader(
    nfl: Any,
    spec: DatasetSpec,
    seasons: list[int] | bool | None,
) -> Any:
    loader = getattr(nfl, spec.loader, None)
    if not callable(loader):
        raise BuildError(f"installed nflreadpy has no {spec.loader}()")
    kwargs = dict(spec.kwargs or {})
    if spec.season_mode != "none":
        kwargs["seasons"] = seasons
    return loader(**kwargs)


def build_database(args: argparse.Namespace) -> Path:
    db_path = Path(args.db).expanduser().resolve()
    building = db_path.with_suffix(db_path.suffix + ".building")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if building.exists():
        building.unlink()

    print(f"NFL database builder v{BUILDER_VERSION}")
    nfl = configure_nflreadpy(Path(args.cache_dir), args.verbose_downloads)
    current = int(nfl.get_current_season())
    if args.through_season is not None:
        through = args.through_season
    else:
        # nflreadpy can identify the new league year before weekly statistical
        # files exist. Default to the latest completed season until week 22.
        try:
            week = int(nfl.get_current_week(use_date=False))
        except TypeError:
            week = int(nfl.get_current_week())
        except Exception:
            week = 0
        through = current if week >= 22 else current - 1
    if through > current:
        raise BuildError(f"through season {through} is later than nflreadpy current season {current}")
    if through < args.from_season and not args.all_history:
        raise BuildError("through-season must be greater than or equal to from-season")

    datasets = list(CORE_DATASETS)
    if args.extended:
        datasets.extend(EXTENDED_DATASETS)

    con = sqlite3.connect(building)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-200000")
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute(
        "CREATE TABLE source_manifest ("
        "table_name TEXT PRIMARY KEY, loader TEXT NOT NULL, "
        "season_from INTEGER, season_to INTEGER, rows INTEGER NOT NULL, "
        "columns_json TEXT NOT NULL, parquet_path TEXT, parquet_bytes INTEGER, "
        "sha256 TEXT, loaded_at TEXT NOT NULL, status TEXT NOT NULL, error TEXT)"
    )
    con.execute(
        "CREATE TABLE build_warnings (dataset TEXT, warning TEXT, created_at TEXT)"
    )

    loaded_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        for spec in datasets:
            seasons = dataset_seasons(
                spec,
                args.from_season,
                through,
                args.all_history,
            )
            if seasons == []:
                print(f"skip {spec.table}: no seasons in requested range")
                continue
            label = "all" if seasons is True else (
                "n/a" if seasons is None else f"{seasons[0]}-{seasons[-1]}"
            )
            print(f"loading {spec.table:<18} via {spec.loader} ({label})")
            try:
                frame = call_loader(nfl, spec, seasons)
                rows, columns = write_frame(
                    con,
                    spec.table,
                    frame,
                    batch_size=args.batch_size,
                )
                parquet_path = None
                parquet_bytes = None
                digest = None
                if args.save_raw:
                    raw_path = Path(args.raw_dir) / f"{spec.table}.parquet"
                    parquet_bytes, digest = save_parquet(frame, raw_path)
                    parquet_path = str(raw_path)
                season_from = None
                season_to = None
                if isinstance(seasons, list) and seasons:
                    season_from, season_to = seasons[0], seasons[-1]
                con.execute(
                    "INSERT INTO source_manifest VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        spec.table,
                        spec.loader,
                        season_from,
                        season_to,
                        rows,
                        json.dumps(columns),
                        parquet_path,
                        parquet_bytes,
                        digest,
                        loaded_at,
                        "loaded",
                        None,
                    ),
                )
                con.commit()
                print(f"  wrote {rows:,} rows, {len(columns)} columns")
            except Exception as exc:
                if spec.required:
                    raise BuildError(f"{spec.loader} failed: {type(exc).__name__}: {exc}") from exc
                warning = f"{type(exc).__name__}: {exc}"
                con.execute(
                    "INSERT INTO source_manifest VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        spec.table,
                        spec.loader,
                        None,
                        None,
                        0,
                        "[]",
                        None,
                        None,
                        None,
                        loaded_at,
                        "skipped",
                        warning,
                    ),
                )
                con.execute(
                    "INSERT INTO build_warnings VALUES (?,?,?)",
                    (spec.table, warning, loaded_at),
                )
                con.commit()
                print(f"  warning: skipped optional dataset: {warning}")

        print("creating indexes and derived tables")
        create_source_indexes(con)
        create_players(con)
        create_player_seasons(con)
        create_player_teams(con)
        create_team_seasons(con)
        create_stat_coverage(con)
        create_compatibility_views(con)

        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        con.executemany(
            "INSERT INTO meta VALUES (?,?)",
            [
                ("sport", "nfl"),
                ("builder_version", BUILDER_VERSION),
                ("built_at_utc", loaded_at),
                ("from_season", str(args.from_season)),
                ("through_season", str(through)),
                ("all_history", str(bool(args.all_history)).lower()),
                ("extended", str(bool(args.extended)).lower()),
                ("source", "nflreadpy / nflverse"),
            ],
        )

        validate_database(con)
        create_table_counts(con)
        con.commit()
        con.execute("PRAGMA optimize")
        con.commit()
    except Exception:
        con.close()
        if building.exists() and not args.keep_failed:
            building.unlink()
        raise
    else:
        con.close()

    if db_path.exists():
        if not args.replace:
            building.unlink(missing_ok=True)
            raise BuildError(
                f"database already exists: {db_path}. Re-run with --replace"
            )
        backup = db_path.with_suffix(
            db_path.suffix + ".bak-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        )
        os.replace(db_path, backup)
        print(f"backup: {backup}")
    os.replace(building, db_path)
    return db_path


def show_summary(path: Path) -> None:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        print(f"\ndatabase: {path}")
        print(f"size:     {path.stat().st_size / (1024 * 1024):,.1f} MiB")
        print("\ntables:")
        for table, count in con.execute(
            "SELECT table_name, row_count FROM table_counts ORDER BY table_name"
        ):
            print(f"  {table:<24} {count:>12,}")
        warnings = con.execute(
            "SELECT dataset, warning FROM build_warnings ORDER BY dataset"
        ).fetchall()
        if warnings:
            print("\nwarnings:")
            for dataset, warning in warnings:
                print(f"  {dataset}: {warning}")
    finally:
        con.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--from-season", type=int, default=CORE_MIN_SEASON)
    parser.add_argument("--through-season", type=int)
    parser.add_argument(
        "--all-history",
        action="store_true",
        help="use each dataset's earliest supported season where practical",
    )
    parser.add_argument(
        "--extended",
        action="store_true",
        help="also load weekly rosters, snaps, injuries, officials, combine, depth charts, contracts and trades",
    )
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--save-raw", action="store_true")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW))
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--verbose-downloads", action="store_true")
    parser.add_argument("--keep-failed", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        path = build_database(args)
        show_summary(path)
        return 0
    except (BuildError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
