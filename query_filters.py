"""Safe query-language compiler for Sports Data Lab Advanced Search.

The compiler accepts a compact, shell-like query string and produces one
parameterised SQLite statement. Only schema-declared columns and statistics
can become SQL identifiers; user values always remain bound parameters.

Sport-specific tokens (captaincy, draft, family) are not built in: a sport
registers :class:`SearchExtension` handlers via ``search_extension_modules``
on its Sport entry, and callers pass ``sport.search_extensions()`` through
``compile_query``. This module stays free of any sport's tables.

INDEX BEHAVIOUR -- what this compiler does and does not promise
---------------------------------------------------------------
Most predicates keep the bare column on the left and seek an index:
club filters expand to UNION'd ``IN`` lists over the indexed club
columns (never ``LOWER(col)=``; an unknown name is rejected rather than
scanned for), numeric and season comparisons compare stored values
directly, and date filters compare ISO text as text.

These are known to scan, and are recorded here rather than claimed away:

* ``name:`` / ``player:`` -- substring matching needs a leading
  wildcard (``LIKE '%x%'``), and folding runs through a registered
  ``search_key()`` function, so neither side can use a B-tree. Bounded
  by running over ``players`` (~13k rows), not ``games`` (~694k).
* ``season.*`` / ``career.*`` / ``avg.*`` -- ``GROUP BY ... HAVING``
  aggregates are computed per player-season, so they read the games
  rows the rest of the WHERE has not already excluded.
* Family and recruitment tokens in a sport's extensions (see
  afl/search_tokens.py) match relationship *labels* with
  ``LOWER(...) LIKE '%brother%'`` over small curated tables.

Removing these needs schema work this build does not carry -- FTS5 or
trigram indexes for substring name search, pre-aggregated player-season
and career tables for the HAVING filters, normalised relationship codes
for the family labels -- not a rewrite of the SQL emitted here. Until
then the honest claim is that *values are always bound and identifiers
always validated*, which is a safety guarantee, and that most but not
all predicates are index-seekable, which is a performance observation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import math
import re
import shlex
import sqlite3
from typing import Any

import core
import names


class QuerySyntaxError(ValueError):
    """Raised when an Advanced Search token cannot be interpreted safely."""


#: Server-side bounds on a free-form query. The text area mirrors
#: MAX_QUERY_CHARS as a UX hint, but the compiler enforces them: widget
#: limits are client-side and a crafted request bypasses them entirely.
#: Without these, a 349 KB query of 20,000 club tokens compiled into
#: megabytes of SQL and tens of thousands of bound parameters.
MAX_QUERY_CHARS = 16_384
MAX_QUERY_TOKENS = 256
MAX_TOKEN_CHARS = 2_048

#: Total bound values one compiled query may carry, for *both* search
#: systems -- the query builder imports this and enforces it inside
#: ParamBag, and compile_query below enforces it on the free-form
#: language. 900 sits under legacy SQLite's 999-variable limit.
#:
#: The token and character caps above do not imply this one: a club token
#: expands to two bound values per lineage identity, so 256 legal
#: `club:"Brisbane Lions"` tokens (5,631 characters, well inside every
#: other bound) compiled to 1,537 parameters. Modern SQLite binds 32,766
#: and ran it; a build with the historical 999 limit would refuse it at
#: execution, far from the token that caused it.
MAX_QUERY_PARAMS = 900

#: SQLite binds integers as signed 64-bit. A Python int outside this range
#: raises OverflowError at execution time -- far from the typed token that
#: caused it -- so the parser owns the bound instead.
SQLITE_INT_MIN = -(2 ** 63)
SQLITE_INT_MAX = 2 ** 63 - 1


def coerce_number(value, *, integer: bool = False) -> int | float:
    """One number, parsed exactly, bounded to what SQLite can bind.

    The shared numeric gate for both search systems (the query language
    here, the query builder's widgets/token restores). Decimal, not float:
    ``float("9007199254740993")`` silently returns ...92, and ``1e100``
    becomes an integer SQLite refuses to bind -- both must be errors or
    exact values, never quiet corruption. Raises ValueError (so
    QuerySyntaxError callers can re-wrap it).
    """
    if isinstance(value, bool):
        raise ValueError("Expected a number, got a boolean")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"Expected a number, got {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"Expected a finite number, got {value!r}")

    if number == number.to_integral_value():
        parsed = int(number)
        if not SQLITE_INT_MIN <= parsed <= SQLITE_INT_MAX:
            raise ValueError(
                f"{value!r} is outside the range a query can bind")
        return parsed

    if integer:
        raise ValueError(f"Expected a whole number, got {value!r}")
    floating = float(number)
    if not math.isfinite(floating):
        raise ValueError(f"{value!r} is outside the range a query can bind")
    return floating


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
        return coerce_number(value)
    except ValueError as exc:
        raise QuerySyntaxError(str(exc)) from exc


def _range(value: str, label: str) -> tuple[int, int]:
    match = _RANGE.fullmatch(value.strip())
    if not match:
        raise QuerySyntaxError(f"{label} must be YEAR or FROM..TO")
    try:
        lo = int(coerce_number(match.group(1), integer=True))
        hi = int(coerce_number(match.group(2) or match.group(1),
                               integer=True))
    except ValueError as exc:
        raise QuerySyntaxError(str(exc)) from exc
    return tuple(sorted((lo, hi)))


def _comparison(column: str, operator: str, value: str) -> tuple[str, list[Any]]:
    op = "=" if operator == ":" else operator
    if op not in {"=", ">", ">=", "<", "<="}:
        raise QuerySyntaxError(f"Unsupported comparison operator {operator!r}")
    return f"{column} {op} ?", [_number(value)]


def _quote_ident(name: str) -> str:
    """Standard SQL identifier quoting, doubling any embedded quote.

    Every identifier this module interpolates is code-owned (schema
    declarations, catalogue lookups), but the uniform rule is quote anyway:
    an identifier wall with holes in it is a wall someone will eventually
    walk through.
    """
    if not isinstance(name, str) or not name:
        raise QuerySyntaxError("Invalid SQL identifier")
    return '"' + name.replace('"', '""') + '"'


def _table_exists(con, name: str) -> bool:
    if con is None:
        return True
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def _columns(con, table: str) -> set[str]:
    if con is None or not _table_exists(con, table):
        return set()
    # pragma_table_info is the table-valued form of PRAGMA table_info: the
    # table name rides as a bound parameter instead of interpolated text.
    return {row[0] for row in con.execute(
        "SELECT name FROM pragma_table_info(?)", (table,))}


def _has_values(con, table: str, column: str) -> bool:
    """Whether `column` holds at least one value.

    Column existence is not enough. club_player_register carries height_cm
    and weight_kg in every sport's schema but only the AFL import fills
    them, so an existence check compiled a filter that quietly matched
    nobody instead of saying the layer is not loaded. Both identifiers are
    validated against the live catalogue before they are interpolated, and
    quoted; only sqlite3's own errors are treated as "no" so a programming
    fault still surfaces.
    """
    if con is None:
        return True
    if column not in _columns(con, table):
        return False
    try:
        return bool(con.execute(
            f"SELECT 1 FROM {_quote_ident(table)} "
            f"WHERE {_quote_ident(column)} IS NOT NULL LIMIT 1"
        ).fetchone())
    except sqlite3.Error:
        return False


#: Advanced Search key -> the column the shared club_player_register layer
#: stores it in. This is the *fallback* home of the physicals; a sport that
#: declares its own player column (``schema.height`` / ``schema.weight`` --
#: the NFL's are literally ``height`` and ``weight``, in inches and pounds)
#: is compiled against that column first. Hardcoding these two names for
#: every sport made ``height>=72`` answer "height data is not loaded" on an
#: NFL database that carries height for every player.
_REGISTER_PHYSICAL_COLUMNS = {"height": "height_cm", "weight": "weight_kg"}


class SearchExtension:
    """One sport's extra search tokens, registered on its Sport entry.

    The compiler offers every token its built-in fields do not recognise
    to each extension in turn. A ``claim`` returning True consumes the
    token -- accumulating whatever state the extension needs, and raising
    :class:`QuerySyntaxError` for a value it cannot read -- and ``finish``
    then returns ``(sql, params)`` fragments to AND into the player WHERE
    clause, each referencing ``p.<player_id>``. Instances are created
    fresh per query by the sport's ``extensions()`` factory, so claims may
    keep state across tokens; checks that need the database (is a layer
    loaded?) belong in ``finish``, which is where ``con`` arrives.

    This is what keeps the compiler sport-agnostic: captaincy, draft and
    family tokens are AFL data shapes and live in afl/search_tokens.py,
    declared by ``Sport.search_extension_modules`` the same way BUILDERS
    declare grid constraints.
    """

    def claim(self, key: str, operator: str, value: str) -> bool:
        return False

    def finish(self, schema, con) -> list:
        return []


def _field_only(key: str, operator: str) -> None:
    """Reject comparison operators on tokens that only take field:value."""
    if operator not in {":", "="}:
        raise QuerySyntaxError(f"{key} supports only field:value syntax")


def _club_identities(schema, value: str) -> list | None:
    """Canonical identities for a typed club name, or None when unknown.

    Case-insensitive against the sport's club list and every era name its
    lineages mention, so ``club:"brisbane lions"`` expands to the Bears and
    Fitzroy exactly the way the grid square for the same question does,
    while ``club:Fitzroy`` stays Fitzroy — lineage is one-directional.
    Resolving in Python keeps the SQL an exact, indexable IN; the old
    ``LOWER(col)=LOWER(?)`` both scanned the games table per token and
    silently disagreed with the constraint engine about what a club means.
    """
    known = {str(club).casefold(): str(club) for club in schema.clubs}
    for lineage in schema.club_lineage.values():
        for name in lineage:
            known.setdefault(str(name).casefold(), str(name))
    canonical = known.get(value.strip().casefold())
    if canonical is None:
        return None
    return schema.club_identities(canonical)


def tokenize(query: str) -> list[str]:
    """Split a query like shlex.split, with double quotes the only quoting.

    Every documented example quotes phrases with double quotes, and a
    single quote is how half the surnames in the database are spelled --
    `name:o'brien` used to die with "No closing quotation" instead of
    finding anybody.

    Size, token-count and token-length bounds are enforced here, at the
    compiler's front door, because this is the one gate every query passes:
    the page's text area mirrors MAX_QUERY_CHARS for UX, but a widget bound
    is advice to a browser, not a limit on a request.
    """
    if not isinstance(query, str):
        raise QuerySyntaxError("Query must be text")
    if len(query) > MAX_QUERY_CHARS:
        raise QuerySyntaxError(
            f"Query exceeds {MAX_QUERY_CHARS:,} characters")
    lex = shlex.shlex(query, posix=True)
    lex.whitespace_split = True
    lex.commenters = ""
    lex.quotes = '"'
    try:
        tokens = list(lex)
    except ValueError as exc:
        raise QuerySyntaxError(str(exc)) from exc
    if len(tokens) > MAX_QUERY_TOKENS:
        raise QuerySyntaxError(
            f"Query exceeds {MAX_QUERY_TOKENS} filters")
    if any(len(token) > MAX_TOKEN_CHARS for token in tokens):
        raise QuerySyntaxError("A query value is too long")
    return tokens


def quote_token(token: str) -> str:
    """The inverse of `tokenize` for one token: double quotes when needed.

    shlex.quote wraps in *single* quotes, which `tokenize` reads as letters
    of the value -- re-joining `club:"New York Yankees"` through it handed
    the parser a token starting with a literal apostrophe.
    """
    if token and not re.search(r'[\s"\\]', token):
        return token
    escaped = token.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def join_tokens(tokens) -> str:
    """Join tokens into a query string that `tokenize` reads back intact."""
    return " ".join(quote_token(token) for token in tokens)


def name_terms(query: str) -> list[str]:
    """The value of every name:/player: token in a query, or nothing.

    For the search page's did-you-mean: an empty result with a name filter
    in it is usually a misspelled name, and the page can only offer close
    spellings if it can see what was typed. Parse errors return nothing --
    the page is already showing them as errors.
    """
    try:
        tokens = tokenize(query)
    except QuerySyntaxError:
        return []
    terms = []
    for raw in tokens:
        match = _TOKEN.fullmatch(raw)
        if (match and match.group(2) == ":"
                and match.group(1).lower() in {"name", "player"}
                and match.group(3).strip()):
            terms.append(match.group(3).strip())
    return terms


def replace_name_term(query: str, term: str, name: str) -> str:
    """The query with a name:/player: value swapped for a real name.

    Behind the search page's did-you-mean buttons: only the token whose
    value is the misspelling changes, every other filter rides along
    untouched. A query that does not parse comes back unchanged -- the
    page is already showing it as an error.
    """
    try:
        tokens = tokenize(query)
    except QuerySyntaxError:
        return query
    out = []
    for raw in tokens:
        match = _TOKEN.fullmatch(raw)
        if (match and match.group(2) == ":"
                and match.group(1).lower() in {"name", "player"}
                and match.group(3).strip() == term):
            out.append(f"{match.group(1)}:{quote_token(name)}")
        else:
            out.append(quote_token(raw))
    return " ".join(out)


def _parse(query: str) -> tuple[list[tuple[str, str, str]], QuerySpec]:
    raw_tokens = tokenize(query)

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
            _field_only(key, operator)
            spec.sort = value.lower().replace(" ", "_")
        elif key == "limit":
            _field_only(key, operator)
            limit = _number(value)
            if not isinstance(limit, int):
                raise QuerySyntaxError("limit must be a whole number")
            if not 1 <= limit <= 500:
                raise QuerySyntaxError("limit must be between 1 and 500")
            spec.limit = limit
        else:
            tokens.append((key, operator, value))
            spec.filters.append((key, operator, value))
    return tokens, spec


def compile_query(schema, query: str, con=None, extensions=()):
    """Compile a search expression into ``(sql, params, QuerySpec)``.

    ``extensions`` is the sport's :class:`SearchExtension` list, usually
    ``sport.search_extensions()``; tokens no built-in field recognises are
    offered to them before being rejected as unknown.
    """
    tokens, spec = _parse(query)
    s = schema
    stats = set(s.stats)
    rate_stats = set(getattr(s, "rate_stats", ()) or ())
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
        elif key in _REGISTER_PHYSICAL_COLUMNS:
            # Where a sport keeps physicals differs, in table AND name. The
            # sport's own schema declaration wins: the NFL and NBA builds
            # put them on `players` (`height`/`weight` in inches and pounds,
            # `height_cm`/`weight_kg` in metric respectively), declared as
            # schema.height / schema.weight. The AFL import instead fills
            # height_cm/weight_kg on club_player_register, one row per club
            # a player registered at, and declares nothing on `players`.
            # Hardcoding the metric names made every NFL height search
            # answer "not loaded" while the data sat in `players.height`.
            player_col = getattr(s, key, "") or _REGISTER_PHYSICAL_COLUMNS[key]
            register_col = _REGISTER_PHYSICAL_COLUMNS[key]
            if player_col in _columns(con, s.players):
                fragment, bound = _comparison(
                    f"p.{player_col}", operator, value)
                player_where.append(fragment)
            elif (register_col in _columns(con, "club_player_register")
                    and _has_values(con, "club_player_register",
                                    register_col)):
                fragment, bound = _comparison(register_col, operator, value)
                player_where.append(
                    f"p.{s.player_id} IN (SELECT player_id "
                    f"FROM club_player_register "
                    f"WHERE player_id IS NOT NULL AND {fragment})")
            else:
                raise QuerySyntaxError(
                    f"{key.title()} data is not loaded for this sport")
            params.extend(bound)
        elif key in aliases:
            fragment, bound = _comparison(f"p.{aliases[key]}", operator, value)
            player_where.append(fragment)
            params.extend(bound)
        elif key in {"name", "player"}:
            # Matched on letters alone, the way the player picker already
            # does: `name:acuna` has to find Acuña, and `name:o'brien` the
            # OBrien that AFL Tables strips the apostrophe from. SQLite's
            # LOWER and LIKE fold ASCII only, so the folding runs in
            # Python, registered on the connection the query executes on.
            # Wildcards in the typed text are escaped so "o_brien" is a
            # name, not a pattern.
            _field_only(key, operator)
            folded = names.search_key(value)
            if not folded:
                # Folding strips punctuation, so a query of nothing but
                # punctuation would leave an empty pattern -- which LIKE
                # reads as "match everybody".
                raise QuerySyntaxError(
                    f"A name needs at least one letter or digit, "
                    f"got {value!r}")
            if con is not None:
                con.create_function("search_key", 1, names.search_key,
                                    deterministic=True)
                player_where.append(
                    f"search_key(p.{s.player}) LIKE ? ESCAPE '\\'")
                params.append(names.like_contains(folded))
            else:
                player_where.append(
                    f"LOWER(p.{s.player}) LIKE ? ESCAPE '\\'")
                params.append(names.like_contains(value.lower()))
        elif key == "club":
            _field_only(key, operator)
            club_all.append(value)
        elif key == "club_any":
            _field_only(key, operator)
            club_any.append(value)
        elif key in {"played", "season"}:
            _field_only(key, operator)
            lo, hi = _range(value, key)
            player_where.append(
                f"p.{s.player_id} IN (SELECT yr.{s.player_id} FROM {s.games} yr "
                f"WHERE yr.{s.season} BETWEEN ? AND ?)"
            )
            params.extend([lo, hi])
        elif key in {"debut_year", "debut_range"}:
            _field_only(key, operator)
            lo, hi = _range(value, "debut")
            player_where.append(f"p.{s.debut_season} BETWEEN ? AND ?")
            params.extend([lo, hi])
        elif key == "postseason":
            _field_only(key, operator)
            wanted = _boolean(value)
            predicate = (
                f"p.{s.player_id} IN (SELECT pg.{s.player_id} FROM {s.games} pg "
                f"WHERE pg.{s.is_final}=1)"
            )
            player_where.append(predicate if wanted else f"NOT {predicate}")
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
            if stat in rate_stats:
                raise QuerySyntaxError(
                    f"{stat} is a rate and cannot be summed across a season; "
                    f"use avg.{stat} or game.{stat}")
            fragment, bound = _comparison(f"SUM(ss.{stat})", operator, value)
            season_conditions.append(fragment)
            season_params.extend(bound)
        elif key.startswith("avg."):
            stat = key.split(".", 1)[1]
            if stat not in stats:
                raise QuerySyntaxError(f"Unknown average statistic: {stat}")
            fragment, bound = _comparison(f"AVG(av.{stat})", operator, value)
            # The games floor counts games where the stat was recorded --
            # COUNT(column) skips NULLs the same way AVG does -- so a
            # career straddling a stat's first recorded season is judged
            # on the same games the average is computed over. COUNT(*)
            # counted unrecorded games toward the floor, which is the one
            # place the NULL-never-zero rule used to slip. Per-stat, so
            # two avg. filters over different eras each get their own.
            avg_conditions.append(
                f"COUNT(av.{stat}) >= "
                f"{int(core.Generic.SEASON_AVG_MIN_GAMES)} AND {fragment}")
            avg_params.extend(bound)
        elif key.startswith("career."):
            stat = key.split(".", 1)[1]
            if stat == "games":
                fragment, bound = _comparison(f"p.{s.career_games}", operator, value)
                player_where.append(fragment)
                params.extend(bound)
            elif stat in rate_stats:
                raise QuerySyntaxError(
                    f"{stat} is a rate and cannot be summed across a career; "
                    f"use avg.{stat} or game.{stat}")
            elif stat in stats:
                fragment, bound = _comparison(f"SUM(cr.{stat})", operator, value)
                career_conditions.append(fragment)
                career_params.extend(bound)
            else:
                raise QuerySyntaxError(f"Unknown career statistic: {stat}")
        else:
            for extension in extensions:
                if extension.claim(key, operator, value):
                    break
            else:
                raise QuerySyntaxError(f"Unknown search field: {key}")

    for club in club_all:
        names_list = _club_identities(s, club)
        if not names_list:
            # Aliases and era names were already tried: _club_identities
            # folds the sport's club list and every lineage name, case-
            # insensitively. A miss is a typo or another sport's club, and
            # the old forgiving fallback -- LOWER(col)=LOWER(?) over the
            # whole games table -- was a full scan that almost always
            # matched nothing anyway. Fail with the name, not silently.
            raise QuerySyntaxError(f"Unknown club: {club!r}")
        # UNION rather than OR, same as core.played_for: it lets SQLite
        # use the separate club_now/club_hist indexes instead of
        # scanning the games table once per club token.
        marks = ",".join("?" for _ in names_list)
        player_where.append(
            f"p.{s.player_id} IN ("
            f"SELECT ca.{s.player_id} FROM {s.games} ca "
            f"WHERE ca.{s.club_now} IN ({marks}) "
            f"UNION "
            f"SELECT ca.{s.player_id} FROM {s.games} ca "
            f"WHERE ca.{s.club_hist} IN ({marks}))"
        )
        params.extend([*names_list, *names_list])

    if club_any:
        # Pure OR of memberships, so every resolved identity pools into
        # one IN list. Unknown names are rejected the same way club: is.
        any_names: list[str] = []
        for club in club_any:
            names_list = _club_identities(s, club)
            if not names_list:
                raise QuerySyntaxError(f"Unknown club: {club!r}")
            any_names.extend(names_list)
        marks = ",".join("?" for _ in any_names)
        player_where.append(
            f"p.{s.player_id} IN (SELECT co.{s.player_id} FROM {s.games} co "
            f"WHERE (co.{s.club_now} IN ({marks}) "
            f"OR co.{s.club_hist} IN ({marks})))"
        )
        params.extend([*any_names, *any_names])

    if game_conditions:
        player_where.append(
            f"p.{s.player_id} IN (SELECT gm.{s.player_id} FROM {s.games} gm "
            f"WHERE "
            + " AND ".join(game_conditions) + ")"
        )
        params.extend(game_params)

    if season_conditions:
        player_where.append(
            f"p.{s.player_id} IN (SELECT ss.{s.player_id} FROM {s.games} ss "
            f"GROUP BY ss.{s.player_id}, ss.{s.season} HAVING "
            + " AND ".join(season_conditions) + ")"
        )
        params.extend(season_params)

    if avg_conditions:
        # The floor rides inside each condition (COUNT of the recorded
        # games for that stat), read from core so a season average means
        # the same thing in a query as it does in a grid square.
        player_where.append(
            f"p.{s.player_id} IN (SELECT av.{s.player_id} FROM {s.games} av "
            f"GROUP BY av.{s.player_id}, av.{s.season} "
            f"HAVING "
            + " AND ".join(avg_conditions) + ")"
        )
        params.extend(avg_params)

    if career_conditions:
        player_where.append(
            f"p.{s.player_id} IN (SELECT cr.{s.player_id} FROM {s.games} cr "
            f"GROUP BY cr.{s.player_id} HAVING "
            + " AND ".join(career_conditions) + ")"
        )
        params.extend(career_params)

    for extension in extensions:
        for fragment, values in extension.finish(s, con):
            player_where.append(fragment)
            params.extend(values)

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
        f'p.{s.clubs_hist} AS Teams, p.{s.obscurity} AS ObscurityRaw, '
        f'p.{s.player_id} AS PlayerID '
        f'FROM {s.players} p WHERE {where_sql} '
        f'ORDER BY {orders[spec.sort]} LIMIT ?'
    )
    params.append(spec.limit)
    # One budget for the whole compiled query, checked after every
    # contributor -- built-in fields, club lineage expansion and the
    # sport's own extensions alike. Nothing is executed on the way here,
    # so this is a refusal, not a partial query.
    if len(params) > MAX_QUERY_PARAMS:
        raise QuerySyntaxError(
            f"This search needs {len(params):,} bound values, more than "
            f"the {MAX_QUERY_PARAMS:,} one query may use. Use fewer "
            f"club, name or list filters.")
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
            tokens.append(f"{target}:{quote_token(value)}")

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
    for filter_name, (lo_key, hi_key) in pairs.items():
        lo, hi = values(lo_key), values(hi_key)
        if lo or hi:
            left = lo[-1] if lo else hi[-1]
            right = hi[-1] if hi else lo[-1]
            tokens.append(f"{filter_name}:{left}..{right}")

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
