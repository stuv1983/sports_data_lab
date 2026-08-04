"""Safe query-language compiler for Sports Data Lab Advanced Search.

The compiler accepts a compact, shell-like query string and produces one
parameterised SQLite statement. Only schema-declared columns and statistics
can become SQL identifiers; user values always remain bound parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import shlex
from typing import Any

import core


class QuerySyntaxError(ValueError):
    """Raised when an Advanced Search token cannot be interpreted safely."""


@dataclass
class QuerySpec:
    filters: list[tuple[str, str, str]] = field(default_factory=list)
    sort: str = "obscurity"
    limit: int = 100


_TOKEN = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)(:|>=|<=|=|>|<)(.*)$")
_RANGE = re.compile(r"^(-?\d+)(?:\.\.(-?\d+))?$")
_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


def _boolean(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise QuerySyntaxError(f"Expected true/false, got {value!r}")


def _number(value: str) -> int | float:
    try:
        number = float(value)
    except ValueError as exc:
        raise QuerySyntaxError(f"Expected a number, got {value!r}") from exc
    return int(number) if number.is_integer() else number


def _range(value: str, label: str) -> tuple[int, int]:
    match = _RANGE.fullmatch(value.strip())
    if not match:
        raise QuerySyntaxError(f"{label} must be YEAR or FROM..TO")
    lo = int(match.group(1))
    hi = int(match.group(2) or match.group(1))
    return tuple(sorted((lo, hi)))


def _comparison(column: str, operator: str, value: str) -> tuple[str, list[Any]]:
    op = "=" if operator == ":" else operator
    if op not in {"=", ">", ">=", "<", "<="}:
        raise QuerySyntaxError(f"Unsupported comparison operator {operator!r}")
    return f"{column} {op} ?", [_number(value)]


def _table_exists(con, name: str) -> bool:
    if con is None:
        return True
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def _parse(query: str) -> tuple[list[tuple[str, str, str]], QuerySpec]:
    try:
        raw_tokens = shlex.split(query, posix=True)
    except ValueError as exc:
        raise QuerySyntaxError(str(exc)) from exc

    spec = QuerySpec()
    tokens: list[tuple[str, str, str]] = []
    for raw in raw_tokens:
        match = _TOKEN.fullmatch(raw)
        if not match:
            raise QuerySyntaxError(
                f"Could not parse {raw!r}. Use field:value or field>=number."
            )
        key, operator, value = match.groups()
        key = key.lower().replace("-", "_")
        if value == "":
            raise QuerySyntaxError(f"{key} needs a value")
        if key == "sort":
            spec.sort = value.lower().replace(" ", "_")
        elif key == "limit":
            limit = int(_number(value))
            if not 1 <= limit <= 500:
                raise QuerySyntaxError("limit must be between 1 and 500")
            spec.limit = limit
        else:
            tokens.append((key, operator, value))
            spec.filters.append((key, operator, value))
    return tokens, spec


def compile_query(schema, query: str, con=None):
    """Compile a search expression into ``(sql, params, QuerySpec)``."""
    tokens, spec = _parse(query)
    s = schema
    stats = set(s.stats)
    player_where: list[str] = []
    params: list[Any] = []
    club_all: list[str] = []
    club_any: list[str] = []
    game_conditions: list[str] = []
    game_params: list[Any] = []
    season_conditions: list[str] = []
    season_params: list[Any] = []
    avg_conditions: list[str] = []
    avg_params: list[Any] = []
    career_conditions: list[str] = []
    career_params: list[Any] = []
    captain_conditions: list[str] = []
    captain_params: list[Any] = []

    aliases = {
        "games": s.career_games,
        "career_games": s.career_games,
        "score": s.career_score,
        "career_score": s.career_score,
        "goals": s.career_score,
        "points": s.career_score,
        "debut": s.debut_season,
        "final": s.final_season,
        "last": s.final_season,
        "postseason_games": s.career_postseason,
        "finals": s.career_postseason,
        "playoffs": s.career_postseason,
    }

    for key, operator, value in tokens:
        if key == "debut" and operator == ":" and ".." in value:
            lo, hi = _range(value, "debut")
            player_where.append(f"p.{s.debut_season} BETWEEN ? AND ?")
            params.extend([lo, hi])
        elif key in aliases:
            fragment, bound = _comparison(f"p.{aliases[key]}", operator, value)
            player_where.append(fragment)
            params.extend(bound)
        elif key in {"name", "player"}:
            player_where.append(f"LOWER(p.{s.player}) LIKE ?")
            params.append(f"%{value.lower()}%")
        elif key == "club":
            club_all.append(value)
        elif key == "club_any":
            club_any.append(value)
        elif key in {"played", "season"}:
            lo, hi = _range(value, key)
            player_where.append(
                f"EXISTS (SELECT 1 FROM {s.games} yr "
                f"WHERE yr.{s.player_id}=p.{s.player_id} "
                f"AND yr.{s.season} BETWEEN ? AND ?)"
            )
            params.extend([lo, hi])
        elif key in {"debut_year", "debut_range"}:
            lo, hi = _range(value, "debut")
            player_where.append(f"p.{s.debut_season} BETWEEN ? AND ?")
            params.extend([lo, hi])
        elif key == "postseason":
            wanted = _boolean(value)
            predicate = (
                f"EXISTS (SELECT 1 FROM {s.games} pg "
                f"WHERE pg.{s.player_id}=p.{s.player_id} "
                f"AND pg.{s.is_final}=1)"
            )
            player_where.append(predicate if wanted else f"NOT {predicate}")
        elif key == "captain":
            if not _boolean(value):
                player_where.append(
                    "NOT EXISTS (SELECT 1 FROM captaincies cp "
                    "WHERE cp.player_id=p." + s.player_id + " "
                    "AND cp.match_status IN ('unique','resolved'))"
                )
            else:
                captain_conditions.append("1=1")
        elif key == "captain_club":
            captain_conditions.append("LOWER(cp.club)=LOWER(?)")
            captain_params.append(value)
        elif key in {"captain_year", "captain_season"}:
            lo, hi = _range(value, key)
            captain_conditions.append("cp.season BETWEEN ? AND ?")
            captain_params.extend([lo, hi])
        elif key == "award":
            if not _table_exists(con, "awards") or not _table_exists(con, "person_links"):
                raise QuerySyntaxError("Award data is not loaded")
            player_where.append(
                "EXISTS (SELECT 1 FROM awards a JOIN person_links al "
                "ON al.dg_person_id=a.dg_person_id "
                f"WHERE al.player_id=p.{s.player_id} "
                "AND al.match_status IN ('from_draft','unique','resolved') "
                "AND a.award_slug=?)"
            )
            params.append(value)
        elif key == "drafted_by":
            if not _table_exists(con, "draft") or not _table_exists(con, "draft_links"):
                raise QuerySyntaxError("Draft data is not loaded")
            player_where.append(
                "EXISTS (SELECT 1 FROM draft d JOIN draft_links dl "
                "ON dl.draft_rowid=d.rowid "
                f"WHERE dl.player_id=p.{s.player_id} "
                "AND dl.match_status IN ('unique','resolved') "
                "AND LOWER(d.club) LIKE ?)"
            )
            params.append(f"%{value.lower()}%")
        elif key.startswith("game."):
            stat = key.split(".", 1)[1]
            if stat not in stats:
                raise QuerySyntaxError(f"Unknown game statistic: {stat}")
            fragment, bound = _comparison(f"gm.{stat}", operator, value)
            game_conditions.append(fragment)
            game_params.extend(bound)
        elif key.startswith("season."):
            stat = key.split(".", 1)[1]
            if stat not in stats:
                raise QuerySyntaxError(f"Unknown season statistic: {stat}")
            fragment, bound = _comparison(f"SUM(ss.{stat})", operator, value)
            season_conditions.append(fragment)
            season_params.extend(bound)
        elif key.startswith("avg."):
            stat = key.split(".", 1)[1]
            if stat not in stats:
                raise QuerySyntaxError(f"Unknown average statistic: {stat}")
            fragment, bound = _comparison(f"AVG(av.{stat})", operator, value)
            avg_conditions.append(fragment)
            avg_params.extend(bound)
        elif key.startswith("career."):
            stat = key.split(".", 1)[1]
            if stat == "games":
                fragment, bound = _comparison(f"p.{s.career_games}", operator, value)
                player_where.append(fragment)
                params.extend(bound)
            elif stat in stats:
                fragment, bound = _comparison(f"SUM(cr.{stat})", operator, value)
                career_conditions.append(fragment)
                career_params.extend(bound)
            else:
                raise QuerySyntaxError(f"Unknown career statistic: {stat}")
        else:
            raise QuerySyntaxError(f"Unknown search field: {key}")

    for club in club_all:
        player_where.append(
            f"EXISTS (SELECT 1 FROM {s.games} ca "
            f"WHERE ca.{s.player_id}=p.{s.player_id} "
            f"AND (LOWER(ca.{s.club_now})=LOWER(?) "
            f"OR LOWER(ca.{s.club_hist})=LOWER(?)))"
        )
        params.extend([club, club])

    if club_any:
        marks = []
        for club in club_any:
            marks.append(
                f"LOWER(co.{s.club_now})=LOWER(?) OR "
                f"LOWER(co.{s.club_hist})=LOWER(?)"
            )
            params.extend([club, club])
        player_where.append(
            f"EXISTS (SELECT 1 FROM {s.games} co "
            f"WHERE co.{s.player_id}=p.{s.player_id} AND ("
            + " OR ".join(f"({mark})" for mark in marks) + "))"
        )

    if game_conditions:
        player_where.append(
            f"EXISTS (SELECT 1 FROM {s.games} gm "
            f"WHERE gm.{s.player_id}=p.{s.player_id} AND "
            + " AND ".join(game_conditions) + ")"
        )
        params.extend(game_params)

    if season_conditions:
        player_where.append(
            f"EXISTS (SELECT 1 FROM {s.games} ss "
            f"WHERE ss.{s.player_id}=p.{s.player_id} "
            f"GROUP BY ss.{s.player_id}, ss.{s.season} HAVING "
            + " AND ".join(season_conditions) + ")"
        )
        params.extend(season_params)

    if avg_conditions:
        player_where.append(
            f"EXISTS (SELECT 1 FROM {s.games} av "
            f"WHERE av.{s.player_id}=p.{s.player_id} "
            f"GROUP BY av.{s.player_id}, av.{s.season} "
            # Read from core rather than repeated here: a season average
            # means the same thing in a query as it does in a grid square,
            # and two copies of the floor is how they stop meaning that.
            f"HAVING COUNT(*) >= {core.Generic.SEASON_AVG_MIN_GAMES} AND "
            + " AND ".join(avg_conditions) + ")"
        )
        params.extend(avg_params)

    if career_conditions:
        player_where.append(
            f"EXISTS (SELECT 1 FROM {s.games} cr "
            f"WHERE cr.{s.player_id}=p.{s.player_id} "
            f"GROUP BY cr.{s.player_id} HAVING "
            + " AND ".join(career_conditions) + ")"
        )
        params.extend(career_params)

    if captain_conditions:
        if not _table_exists(con, "captaincies"):
            raise QuerySyntaxError("Captaincy data is not loaded")
        player_where.append(
            "EXISTS (SELECT 1 FROM captaincies cp "
            f"WHERE cp.player_id=p.{s.player_id} "
            "AND cp.match_status IN ('unique','resolved') AND "
            + " AND ".join(captain_conditions) + ")"
        )
        params.extend(captain_params)

    orders = {
        "obscurity": f"p.{s.obscurity} DESC, p.{s.career_games} ASC",
        "games": f"p.{s.career_games} DESC, p.{s.obscurity} DESC",
        "fewest_games": f"p.{s.career_games} ASC, p.{s.obscurity} DESC",
        "oldest": f"p.{s.final_season} ASC, p.{s.player} ASC",
        "newest": f"p.{s.final_season} DESC, p.{s.player} ASC",
        "name": f"p.{s.player} COLLATE NOCASE ASC",
        "score": f"p.{s.career_score} DESC, p.{s.career_games} DESC",
    }
    if spec.sort not in orders:
        raise QuerySyntaxError(
            "sort must be obscurity, games, fewest_games, oldest, newest, name or score"
        )

    where_sql = " AND ".join(player_where) if player_where else "1=1"
    sql = (
        f'SELECT p.{s.player} AS Player, '
        f'p.{s.debut_season} AS "From", p.{s.final_season} AS "To", '
        f'p.{s.career_games} AS Games, p.{s.career_score} AS Score, '
        f'p.{s.career_postseason} AS Postseason, '
        f'p.{s.clubs_hist} AS Teams, p.{s.obscurity} AS ObscurityRaw '
        f'FROM {s.players} p WHERE {where_sql} '
        f'ORDER BY {orders[spec.sort]} LIMIT ?'
    )
    params.append(spec.limit)
    return sql, params, spec


def describe(spec: QuerySpec) -> list[str]:
    descriptions = [f"{key}{operator}{value}" for key, operator, value in spec.filters]
    descriptions.append(f"sort={spec.sort}")
    descriptions.append(f"limit={spec.limit}")
    return descriptions


def query_from_params(query_params: dict) -> str:
    """Translate Streamlit URL parameters into the compact query language."""
    def values(key):
        value = query_params.get(key, [])
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value if str(item) != ""]

    direct = values("q")
    if direct:
        return direct[-1]

    tokens: list[str] = []
    mapping = {
        "club": "club",
        "club_any": "club_any",
        "captain_club": "captain_club",
        "award": "award",
        "drafted_by": "drafted_by",
        "name": "name",
        "sort": "sort",
        "limit": "limit",
    }
    for source, target in mapping.items():
        for value in values(source):
            tokens.append(f"{target}:{shlex.quote(value)}")

    booleans = {"captain": "captain", "postseason": "postseason"}
    for source, target in booleans.items():
        vals = values(source)
        if vals:
            tokens.append(f"{target}:{vals[-1]}")

    numeric = {
        "games_min": "games>=",
        "games_max": "games<=",
        "score_min": "score>=",
        "score_max": "score<=",
        "debut_min": "debut>=",
        "debut_max": "debut<=",
    }
    for source, prefix in numeric.items():
        vals = values(source)
        if vals:
            tokens.append(prefix + vals[-1])

    pairs = {
        "played": ("played_from", "played_to"),
        "captain_year": ("captain_from", "captain_to"),
    }
    for field, (lo_key, hi_key) in pairs.items():
        lo, hi = values(lo_key), values(hi_key)
        if lo or hi:
            left = lo[-1] if lo else hi[-1]
            right = hi[-1] if hi else lo[-1]
            tokens.append(f"{field}:{left}..{right}")

    # Structured game-stat parameters: game_disposals_min=30.
    for key in query_params:
        match = re.fullmatch(r"(game|season|career|avg)_([a-z0-9_]+)_(min|max)", key)
        if not match:
            continue
        vals = values(key)
        if not vals:
            continue
        scope, stat, edge = match.groups()
        operator = ">=" if edge == "min" else "<="
        tokens.append(f"{scope}.{stat}{operator}{vals[-1]}")

    return " ".join(tokens)
