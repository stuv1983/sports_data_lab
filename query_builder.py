"""query_builder.py -- The graphical SQL query builder on Advanced Search.

One of the page's two modes (the other is the player-search language in
advanced_search.py). It works on any table of any registered sport because
nothing in here knows a sport's columns: the whole schema -- tables,
columns, types -- is discovered from the live database at render time, and
the UI is generated from what discovery found. Load a new sport, or add a
column to an existing build, and the builder simply offers it.

Three builder modes, switched at the top of the page (everything lives in
the main column -- the sidebar is collapsed on a phone, and a control kept
there is functionally invisible):

* **Grid query** -- a real query builder over the Grid Solver's criteria
  catalogue. The query is a visible object: requirement cards combined
  with AND, each holding OR alternatives, every chip carrying a live
  player count with edit and remove controls. Criteria are found through
  one searchable picker over every question a square can ask, so "played
  100+ games at the MCG" is typing "venue" and setting two values.
* **Visual tree** -- `streamlit-condition-tree`, a drag-and-drop tree of
  nested AND/OR groups; its structured tree is compiled server-side here.
* **Table filters** -- native Streamlit widgets arranged as *condition
  groups*: each group holds any number of column conditions matched with
  its own ALL (AND) / ANY (OR) rule, and each group after the first says
  how it joins the groups above it, so "(played Collingwood AND 150+
  games) OR (drafted Hawthorn AND premiership)" is two cards. Columns are
  offered grouped into the same categories the grid criteria use, every
  active condition and group shows a live row count of its own, and the
  whole panel serialises to a compact shareable token. Compiled into a
  fully parameterised WHERE, with an optional COUNT(*)-per-group
  aggregation.

STATE MODEL (mode isolation and durability)
-------------------------------------------
The three modes never share compilation state. Each mode's widgets live
under a disjoint session-key namespace (``qbc_*`` grid, ``qb_tree`` tree,
``qbf_*`` filter groups), only the *active* mode's branch in ``page()``
renders widgets or compiles predicates, and the ParamBag handed to the
compiler is constructed fresh on every script run -- so a value typed in
one mode can never bleed into the SQL or parameter bag of another, and a
stale bag can never survive a rerun.

Durability is explicit, not assumed: Streamlit deletes a keyed widget's
state when the widget stops rendering, so every widget that must survive
a mode or page switch passes ``persist_state="session"``, and the visual
tree (a third-party component with no such argument) is mirrored into a
plain session key and handed back through its ``tree=`` argument on
remount. The authoritative query definition is structured data (condition
ASTs, criterion kind+args); widget state is the editable projection of it.

Both builders compile through one recursive, bounded group AST --
``{"type": "group", "op": "AND"|"OR", "children": [...]}`` -- so nested
shapes like ``(A AND B) OR (C AND D)`` are first-class in grid criteria
and table filters alike, with depth, node-count, value-count and total-
parameter budgets enforced by the compiler rather than by any widget.

The generated WHERE keeps every comparison's bare (quoted) column on the
left -- no ``DATE(col)``, ``LOWER(col)``, ``COALESCE(col, ...)`` or other
wrapper that would blind SQLite to an index. Day-granular date filters on
datetime-bearing columns compile to half-open ISO string ranges
(``col >= :day AND col < :next_day``) instead of ``DATE(col) =``, because
ISO-8601 text compares correctly as plain strings. That is a *left-hand
side* guarantee, not a promise that every query seeks an index:
contains/suffix text matches necessarily start with a wildcard, and no
B-tree serves those without dedicated structures (FTS/trigram, reversed
columns) this schema does not yet carry.

SECURITY AND AVAILABILITY MODEL
-------------------------------
Independent walls, so no single mistake is fatal:

1. Identifiers (table, column, sort) can only enter SQL if discovery
   returned them from the database's own catalogue -- filtered through the
   sport's explicit query-table allowlist -- and they are always
   double-quoted. Nothing the reader types is ever an identifier.
2. Values never enter SQL text, in any mode: every comparison is a
   named placeholder bound at execution. The visual builder's own compiled
   WHERE string is *never executed* — a component's return value is just a
   websocket message a hostile client can set to anything — so this page
   compiles the component's structured condition tree itself, through the
   same identifier gate and parameter bag the filter panel uses. Grid
   criteria persist only their kind and arguments; their SQL is rebuilt by
   the server-owned builder catalogue on every compile.
3. Resource limits live in the compiler, not the widgets: the row limit,
   per-condition value counts, scalar sizes, AST depth/node counts and the
   total bound-parameter budget are all enforced server-side, because a
   Streamlit widget bound is a suggestion to a browser, not a boundary.
4. The connection is read-only at the file level (`mode=ro` in the SQLite
   URL), so even a hostile WHERE clause could only read.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import zlib
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import streamlit as st

import labels
from query_filters import coerce_number

# The tree component is optional at runtime: without it the page still works,
# it just offers the filter panel alone rather than crashing at import.
try:
    from streamlit_condition_tree import condition_tree
    HAS_CONDITION_TREE = True
except ImportError:          # pragma: no cover - exercised only when absent
    condition_tree = None
    HAS_CONDITION_TREE = False

MODE_CONSTRAINTS = "Grid constraints"
MODE_TREE = "Visual builder"
MODE_FILTERS = "Filter panel"

#: A text column with at most this many distinct values renders as a
#: multiselect of the real values; above it, a free-text match. The cap also
#: bounds the IN(...) parameter count well under SQLite's 999 limit.
MAX_LIST_VALUES = 200

#: Hard ceiling on rows returned, whatever the limit widget says.
MAX_ROWS = 10_000

#: Total bound parameters one compiled query may carry, enforced by
#: ParamBag itself so no path -- widgets, tokens, the tree component --
#: can exceed it. 900 sits under legacy SQLite's 999-variable limit.
MAX_QUERY_PARAMS = 900

#: Bounds on the recursive group AST both builders compile. Deep enough
#: for any query a person would write, small enough that a doctored token
#: cannot turn the compiler into a stack or node flood.
MAX_GROUP_DEPTH = 6
MAX_GROUP_CHILDREN = 32

#: Ceiling on the disjunctive-normal-form expansion of a grid query. The
#: grid compiler distributes AND over OR so same-row pairing can hold
#: inside every conjunction (see compile_constraint_ast), and that
#: expansion is multiplicative: two OR groups of 17 alternatives under one
#: AND already mean 289 branches. Growth past this bound is refused, never
#: silently truncated.
MAX_DNF_BRANCHES = 256

#: How deep the *editing UI* nests groups -- the compiler accepts
#: MAX_GROUP_DEPTH so restored queries keep working, but the panel stops
#: offering "add subgroup" past this, because a five-deep Boolean tree in
#: cards is unreadable however correct.
MAX_UI_GROUP_DEPTH = 3

#: Only identifiers of this shape are offered to the tree component, whose
#: SQL output writes field names unquoted. The filter panel quotes its own
#: identifiers and has no such restriction.
_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ------------------------------------------------------------------ schema

@dataclass(frozen=True)
class Column:
    """One column as discovery found it: its name, the type the DDL
    declared, and the widget-facing kind that type maps to."""
    name: str
    declared: str
    kind: str                     # integer | float | boolean | date | datetime | text


def type_kind(declared: str) -> str:
    """Map a declared SQL type to the kind of widget that edits it.

    SQLite stores whatever the INSERT supplied, so the declared type is a
    statement of intent, not a guarantee -- which is exactly what a UI
    should be built from. The rules follow SQLite's own affinity rules,
    with date/time and boolean recognised before the numeric affinities
    because their declarations ("BOOLEAN", "DATETIME") also contain the
    substrings the numeric rules match on ("INT" never appears, but be
    explicit rather than lucky).
    """
    t = (declared or "").upper()
    if "BOOL" in t:
        return "boolean"
    if "TIMESTAMP" in t or "DATETIME" in t:
        return "datetime"
    if "DATE" in t or "TIME" in t:
        return "date"
    if "INT" in t:
        return "integer"
    if any(k in t for k in ("REAL", "FLOA", "DOUB", "NUMERIC", "DECIMAL")):
        return "float"
    return "text"


# ------------------------------------------------------------- categories

#: Display order for the filter panel's column categories -- the same
#: shelves the grid criteria catalogue (BUILDER_GROUPS) is arranged on, so
#: a reader who learned the grid picker finds the same map here.
FILTER_CATEGORY_ORDER = (
    "Clubs & journeys", "Career milestones", "Single-game feats",
    "Season & era", "Finals & premierships", "Grounds & venues",
    "Physical", "Draft & recruitment", "Awards & honours", "Match context",
)

#: Fallback shelf for a column no rule recognises.
FILTER_CATEGORY_OTHER = "More columns"

#: Name-pattern rules assigning a discovered column to a category. The
#: schema is discovered at runtime from any sport's database, so this is
#: necessarily a heuristic over naming conventions -- checked in order,
#: most-specific first (e.g. "finals" claims postseason columns before the
#: "season" rule can, while "final_season" falls through to Season & era).
#: A miss is harmless: the column still appears, shelved under
#: FILTER_CATEGORY_OTHER.
_FILTER_CATEGORY_RULES = (
    ("Grounds & venues", re.compile(r"venue|ground|stadium|arena|oval",
                                    re.I)),
    ("Draft & recruitment", re.compile(r"draft|pick|recruit|rookie", re.I)),
    ("Physical", re.compile(r"height|weight|birth|\bdob\b|\bage\b|hand",
                            re.I)),
    ("Awards & honours",
     re.compile(r"award|medal|brownlow|coleman|norm_smith|all_austral"
                r"|captain|rising_star|mvp|fame|honou?r", re.I)),
    ("Finals & premierships",
     re.compile(r"finals|premiership|flag|playoff|postseason|grand", re.I)),
    ("Season & era", re.compile(r"season|decade|era|debut|\byear\b", re.I)),
    ("Clubs & journeys", re.compile(r"club|team|franchise|opponent", re.I)),
    ("Single-game feats",
     re.compile(r"game_high|best_on|single_game|in_a_game", re.I)),
    ("Career milestones",
     re.compile(r"career|games|goals|behinds|disposals|kicks|marks|tackles"
                r"|hitouts|score|points|wins|losses|obscurity", re.I)),
    ("Match context",
     re.compile(r"date|round|margin|result|crowd|attendance|home|away"
                r"|umpire|time", re.I)),
)


def column_category(name: str) -> str:
    """The category shelf a discovered column is offered under."""
    for category, rule in _FILTER_CATEGORY_RULES:
        if rule.search(str(name)):
            return category
    return FILTER_CATEGORY_OTHER


def categorised_order(names) -> list[str]:
    """Column names sorted for a picker: by category shelf, then label."""
    rank = {name: i for i, name in enumerate(FILTER_CATEGORY_ORDER)}
    fallback = len(rank)
    return sorted(names, key=lambda n: (rank.get(column_category(n),
                                                 fallback),
                                        labels.words(n).lower()))


def _quote_ident(name: str) -> str:
    """Standard SQL identifier quoting; doubling any embedded quote."""
    return '"' + str(name).replace('"', '""') + '"'


def _require_known(name: str, known, what: str) -> str:
    """The identifier gate: only names discovery returned may enter SQL."""
    if name not in known:
        raise ValueError(f"Unknown {what}: {name!r}")
    return name


def _bounded_limit(value) -> int:
    """The row limit as an int inside [1, MAX_ROWS], or ValueError.

    Every execution path passes its limit through here, because the number
    widget's own min/max are browser-side UX: a forged ``-1`` reached
    SQLite as ``LIMIT -1``, which SQLite reads as *unlimited* -- the exact
    opposite of a ceiling. Fractions are refused rather than truncated.
    """
    if isinstance(value, bool):
        raise ValueError("Row limit must be a whole number.")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Row limit must be a whole number.") from exc
    if isinstance(value, float) and value != parsed:
        raise ValueError("Row limit must be a whole number.")
    return max(1, min(parsed, MAX_ROWS))


def _restored_limit(value) -> int:
    """A limit from a *restored payload*: in range or refused.

    The live widget path clamps a stray value because the person is
    right there to see the result; a share token claiming ``-1`` is a
    forged payload, and clamping it would silently execute a different
    query than the token claims to reproduce.
    """
    parsed = _bounded_limit(value)
    if parsed != value:
        raise ValueError("Row limit is out of range for this build")
    return parsed


def _db_revision(db: str) -> tuple:
    """A value that changes when the database file is replaced, used to
    invalidate every schema/profile/result cache after a rebuild. Carries
    the path, same shape as app.py's db_revision, so no two files can
    share a key on a (mtime, size) coincidence."""
    stat = os.stat(db)
    return str(db), stat.st_mtime_ns, stat.st_size


# -------------------------------------------------------------- connection

def get_connection(sport):
    """The sport's pooled SQLAlchemy connection, via ``st.connection``.

    ``st.connection`` stores the SQLConnection (and the engine's pool
    inside it) in ``st.cache_resource`` under the name given here, so every
    rerun and every session in this process shares one engine per sport
    rather than reconnecting per script run. The name must therefore be
    per-sport: a shared name would hand the AFL page an engine already
    bound to the MLB file.

    The URL opens the file read-only (`mode=ro` via SQLite's URI syntax),
    matching the guarantee the rest of the app gets from db_pool: nothing
    this page executes can write, whatever the query says.

    NullPool, not the default QueuePool: the database file is atomically
    *replaced* by every update, and a pooled handle checked in before the
    replacement would keep serving the old inode on POSIX -- and on
    Windows would hold the file open, failing the promotion's os.replace
    outright. With NullPool each query opens and closes its own handle,
    so nothing outlives the file it was opened on.
    """
    from sqlalchemy.pool import NullPool

    url = ("sqlite:///file:" + Path(sport.db).resolve().as_posix()
           + "?mode=ro&uri=true")
    return st.connection(f"sql_{sport.key}", type="sql", url=url,
                         poolclass=NullPool)


@st.cache_data(show_spinner=False)
def discover_schema(_conn, db_path: str, revision,
                    kind_overrides: tuple = ()) -> dict:
    """Every table and column the live database actually has.

    Runtime inspection, not configuration: SQLAlchemy's inspector reads the
    catalogue (sqlite_master / PRAGMA table_info under the hood), so the
    result is the schema as built, including tables added by optional
    layers. Cached on (path, revision) -- `_conn`'s leading underscore
    keeps the unhashable connection out of the cache key, and `revision`
    changes when the file is rebuilt, so a refresh is picked up without a
    process restart.

    ``kind_overrides`` is the sport's own word on columns whose declared
    SQL type misleads: every build here stores dates as TEXT and flags as
    INTEGER, so declaration alone would hand games.date a substring
    widget and is_final arbitrary arithmetic. Shape: a tuple of
    ("table.column", kind) pairs -- a tuple, not a dict, so the cache key
    stays deterministic. Explicit declarations, never name-sniffing: a
    heuristic that guesses types is the wrong foundation for a compiler
    that chooses operators from them.
    """
    from sqlalchemy import inspect

    overrides = dict(kind_overrides)
    inspector = inspect(_conn.engine)
    schema: dict[str, tuple] = {}
    for table in sorted(inspector.get_table_names()):
        schema[table] = tuple(
            Column(col["name"], str(col["type"]),
                   overrides.get(f"{table}.{col['name']}",
                                 type_kind(str(col["type"]))))
            for col in inspector.get_columns(table)
        )
    return schema


@st.cache_data(show_spinner=False, max_entries=1024)
def column_profile(_conn, db_path: str, revision, table: str, column: str,
                   kind: str) -> dict:
    """What a column holds, measured, so its widget can be populated.

    Numeric and date kinds get real bounds for sliders and date pickers;
    text gets its distinct values (up to MAX_LIST_VALUES + 1, the +1 being
    how "too many to list" is detected without counting them all). The
    identifiers interpolated here are safe by construction: the page only
    calls this with names discovery returned, and they are quoted anyway.
    """
    q = _quote_ident
    profile: dict = {"kind": kind}
    if kind in ("integer", "float", "date", "datetime"):
        frame = _conn.query(
            f"SELECT MIN({q(column)}) AS lo, MAX({q(column)}) AS hi "
            f"FROM {q(table)} /* rev {revision} */",
            ttl=3600,
        )
        profile["lo"] = frame.at[0, "lo"] if not frame.empty else None
        profile["hi"] = frame.at[0, "hi"] if not frame.empty else None
    elif kind == "text":
        frame = _conn.query(
            f"SELECT DISTINCT {q(column)} AS v FROM {q(table)} "
            f"WHERE {q(column)} IS NOT NULL ORDER BY 1 "
            f"LIMIT {MAX_LIST_VALUES + 1} /* rev {revision} */",
            ttl=3600,
        )
        values = frame["v"].astype(str).tolist()
        if len(values) <= MAX_LIST_VALUES:
            profile["values"] = values
    return profile


# ------------------------------------------------- WHERE construction (B)

class ParamBag:
    """Named-placeholder allocator: hands out :p0, :p1, ... and remembers
    the binding, so every clause builder stays a pure string-plus-dict
    affair that the tests can call without a database.

    The bag is also the query's global parameter budget. Per-condition
    caps bound each IN list, but a query is many conditions, and only the
    one object every placeholder passes through can bound their sum --
    which is what keeps a crafted token from compiling megabytes of SQL
    or tripping SQLite's variable limit at execution time.
    """

    def __init__(self):
        self.values: dict = {}

    def add(self, value) -> str:
        if len(self.values) >= MAX_QUERY_PARAMS:
            raise ValueError(
                f"A query may bind at most {MAX_QUERY_PARAMS} values.")
        name = f"p{len(self.values)}"
        self.values[name] = value
        return f":{name}"


def _bounded_values(raw) -> list:
    """One condition's value list, shape- and size-checked.

    Applied inside the compiler, not only in the widgets: a token or
    component payload reaches compile_condition without ever touching a
    widget. Rejects non-lists (iterating a string would explode it into
    characters), nested containers, oversized lists, oversized strings
    and non-finite floats.
    """
    if not isinstance(raw, (list, tuple)):
        raise ValueError("Condition values must be a list")
    if len(raw) > MAX_RULE_VALUES:
        raise ValueError(
            f"A condition may hold at most {MAX_RULE_VALUES} values")
    values = []
    for value in raw:
        if isinstance(value, (dict, list, tuple, set)):
            raise ValueError("Nested condition values are unsupported")
        if isinstance(value, str) and len(value) > MAX_SCALAR_CHARS:
            raise ValueError("A condition value is too long")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Condition values must be finite")
        values.append(value)
    return values


def in_clause(column: str, values, bag: ParamBag) -> str:
    """`col IN (:p0, :p1, ...)` -- one placeholder per chosen value."""
    marks = ", ".join(bag.add(v) for v in values)
    return f"{_quote_ident(column)} IN ({marks})"


def between_clause(column: str, lo, hi, bag: ParamBag) -> str:
    return (f"{_quote_ident(column)} BETWEEN {bag.add(lo)} "
            f"AND {bag.add(hi)}")


def equals_clause(column: str, value, bag: ParamBag) -> str:
    return f"{_quote_ident(column)} = {bag.add(value)}"


def like_clause(column: str, text: str, mode: str, bag: ParamBag) -> str:
    """A LIKE match whose wildcards are ours, never the reader's.

    The typed text is escaped so `%` and `_` match themselves -- a search
    for "100%" must not become "starts with 100" -- and the pattern is
    still bound as a parameter, not spliced into the SQL.
    """
    escaped = (text.replace("\\", "\\\\")
                   .replace("%", "\\%")
                   .replace("_", "\\_"))
    pattern = {"contains": f"%{escaped}%",
               "starts with": f"{escaped}%",
               "ends with": f"%{escaped}",
               "equals": escaped}[mode]
    return f"{_quote_ident(column)} LIKE {bag.add(pattern)} ESCAPE '\\'"


def pattern_clause(column: str, pattern: str, bag: ParamBag) -> str:
    """A LIKE match whose wildcards ARE the reader's, deliberately.

    The one mode where `%` and `_` keep their SQL meanings -- "sm_th"
    matches Smith and Smyth -- offered under a label that says so. The
    pattern still rides as a bound parameter; only its wildcard
    characters are live, never its structure.
    """
    return f"{_quote_ident(column)} LIKE {bag.add(pattern)}"


def comparison_clause(column: str, operator: str, value, bag: ParamBag) -> str:
    """One vetted comparison; the operator comes from this map, never text."""
    sql_op = {"=": "=", "!=": "<>", ">": ">", ">=": ">=",
              "<": "<", "<=": "<="}[operator]
    return f"{_quote_ident(column)} {sql_op} {bag.add(value)}"


def null_clause(column: str, missing: bool) -> str:
    """`IS NULL` / `IS NOT NULL` -- no parameter, nothing user-typed."""
    return f"{_quote_ident(column)} IS {'NULL' if missing else 'NOT NULL'}"


def parse_number_list(text: str) -> list[float | int]:
    """Comma-separated numbers for a numeric IN(...), bad tokens dropped.

    Forgiving about junk tokens -- the input is live typing -- but bounded
    in count: past MAX_RULE_VALUES the list is an error, not a thousand
    placeholders.
    """
    out: list[float | int] = []
    for token in str(text).split(","):
        token = token.strip()
        if not token:
            continue
        try:
            value = coerce_number(token)
        except ValueError:
            continue
        out.append(value)
        if len(out) > MAX_RULE_VALUES:
            raise ValueError(
                f"A condition may hold at most {MAX_RULE_VALUES} values")
    return out


# ------------------------------------------------ condition specs (B)

#: Operators offered per widget kind. The label the reader picks maps to
#: exactly one clause builder in compile_condition; nothing typed ever
#: becomes an operator. These tuples are the single source of truth for
#: both the widgets (what the selectbox offers) and the compiler (what it
#: will accept), so UI and SQL can never drift apart.
_NUMERIC_OPS = ("≥", "≤", "=", "≠", ">", "<", "between", "one of",
                "is missing", "is present")
_TEXT_OPS = ("contains", "starts with", "ends with", "equals", "not equals",
             "one of", "pattern (% and _ wildcards)",
             "is missing", "is present")
_DATE_OPS = ("between", "on", "after", "before", "on or after",
             "on or before", "is missing", "is present")
_BOOLEAN_OPS = ("is true", "is false", "is missing", "is present")

SPEC_OPS = {"integer": _NUMERIC_OPS, "float": _NUMERIC_OPS,
            "text": _TEXT_OPS, "date": _DATE_OPS, "datetime": _DATE_OPS,
            "boolean": _BOOLEAN_OPS}

_NUMERIC_SQL = {"≥": ">=", "≤": "<=", "=": "=", "≠": "!=", ">": ">", "<": "<"}


def _next_day(iso: str) -> str:
    """The day after an ISO date, for half-open day-granular ranges."""
    return (dt.date.fromisoformat(str(iso)[:10])
            + dt.timedelta(days=1)).isoformat()


def compile_condition(spec: dict, known_columns, bag: ParamBag) -> str | None:
    """One condition spec -> one SARGable, fully parameterised predicate.

    A *spec* is a plain JSON-able dict -- ``{"column", "kind", "op"}`` plus
    the operator's values (``value``, ``lo``/``hi``, or ``values``) -- the
    shape the filter widgets emit, the share token stores, and this
    function compiles. Keeping it a value object means the same condition
    can be compiled twice without re-rendering a widget: once alone into a
    fresh bag for its live count, once into the query's shared bag.

    Guarantees, in order of importance:

    * the column passes the discovery gate (`_require_known`) and is
      quoted; the operator must be in the kind's fixed vocabulary; every
      value rides as a bound parameter -- the three walls, unchanged;
    * the compiled comparison keeps the bare column on the left (never
      ``DATE(col)``/``LOWER(col)``), so SQLite can drive it from an index;
      day-granular filters over datetime-bearing columns become half-open
      ISO ranges (``col >= :day AND col < :next_day``);
    * a spec still missing its value compiles to None -- a half-built
      condition filters nothing rather than erroring per keystroke.
    """
    column = _require_known(str(spec.get("column")), known_columns, "column")
    kind = spec.get("kind")
    ops = SPEC_OPS.get(kind)
    if ops is None:
        raise ValueError(f"Unknown condition kind: {kind!r}")
    op = spec.get("op")
    if op not in ops:
        raise ValueError(f"Unsupported {kind} operator: {op!r}")

    if op == "is missing":
        return null_clause(column, missing=True)
    if op == "is present":
        return null_clause(column, missing=False)

    if kind == "boolean":
        return equals_clause(column, 1 if op == "is true" else 0, bag)

    if kind in ("integer", "float"):
        # coerce_number is exact (Decimal-backed) and bounded to what
        # SQLite can bind; integer kinds refuse fractions rather than
        # silently compiling int(1.9) == 1.
        integer = kind == "integer"
        if op == "between":
            lo, hi = spec.get("lo"), spec.get("hi")
            if lo is None or hi is None:
                return None
            lo, hi = sorted((coerce_number(lo, integer=integer),
                             coerce_number(hi, integer=integer)))
            return between_clause(column, lo, hi, bag)
        if op == "one of":
            values = [coerce_number(v, integer=integer)
                      for v in _bounded_values(spec.get("values") or [])]
            return in_clause(column, values, bag) if values else None
        value = spec.get("value")
        if value is None:
            return None
        return comparison_clause(column, _NUMERIC_SQL[op],
                                 coerce_number(value, integer=integer), bag)

    if kind in ("date", "datetime"):
        return _compile_date_condition(column, spec, op, bag)

    # -- text ------------------------------------------------------------
    if op == "one of":
        values = [str(v) for v in _bounded_values(spec.get("values") or [])]
        return in_clause(column, values, bag) if values else None
    value = spec.get("value")
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list, tuple, set)):
        raise ValueError("Condition value must be a scalar")
    text = str(value)
    if len(text) > MAX_SCALAR_CHARS:
        raise ValueError("A condition value is too long")
    if op == "equals":
        return equals_clause(column, text, bag)
    if op == "not equals":
        return comparison_clause(column, "!=", text, bag)
    if op.startswith("pattern"):
        return pattern_clause(column, text, bag)
    return like_clause(column, text, op, bag)


def _compile_date_condition(column, spec, op, bag: ParamBag) -> str | None:
    """Day-granular date filters that never wrap the column in a function.

    ISO-8601 text sorts identically to the moments it names, so the raw
    stored string can be compared directly -- the index stays usable where
    the old ``DATE(col)`` normalisation forced a scan. ``day_ceiling``
    marks a column whose values may carry a time-of-day (a datetime kind,
    or bounds longer than a bare date): its day-granular ceilings become
    half-open next-day bounds, so "on or before the 8th" still catches
    ``...T14:30`` on the 8th.
    """
    q = _quote_ident(column)
    ceiling = bool(spec.get("day_ceiling"))
    if op == "between":
        lo, hi = spec.get("lo"), spec.get("hi")
        if not lo or not hi:
            return None
        lo, hi = sorted((str(lo), str(hi)))
        if ceiling:
            return (f"({q} >= {bag.add(lo)} "
                    f"AND {q} < {bag.add(_next_day(hi))})")
        return between_clause(column, lo, hi, bag)
    value = spec.get("value")
    if not value:
        return None
    value = str(value)
    if op == "on":
        if ceiling:
            return (f"({q} >= {bag.add(value)} "
                    f"AND {q} < {bag.add(_next_day(value))})")
        return equals_clause(column, value, bag)
    if op == "after":
        # Strictly later than the named day. On a datetime-bearing column
        # that means "from the next day on": 14:30 on the day itself is
        # still *on* it, so the half-open bound starts at the next day.
        if ceiling:
            return comparison_clause(column, ">=", _next_day(value), bag)
        return comparison_clause(column, ">", value, bag)
    if op == "before":
        # Strictly earlier: `col < day` already excludes every moment of
        # the day itself, timestamps included, so no ceiling shift needed.
        return comparison_clause(column, "<", value, bag)
    if op == "on or after":
        return comparison_clause(column, ">=", value, bag)
    if ceiling:                                      # on or before
        return f"{q} < {bag.add(_next_day(value))}"
    return comparison_clause(column, "<=", value, bag)


def combine_condition_clauses(clauses, match: str) -> str | None:
    """AND/OR the predicates inside one group; None when nothing is set."""
    if match not in ("AND", "OR"):
        raise ValueError(f"Bad group match rule: {match!r}")
    parts = [clause for clause in clauses if clause]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return "(" + f" {match} ".join(parts) + ")"


def compile_condition_node(node, known_columns, bag: ParamBag, *,
                           _depth: int = 0,
                           _count: list | None = None) -> str | None:
    """One node of the filter panel's group AST -> a parenthesised WHERE.

    The AST is the panel's one Boolean model, recursive by construction:

        {"type": "group", "op": "AND" | "OR", "children": [node, ...]}

    with condition specs (the dicts compile_condition reads) as leaves.
    ``(A AND B) OR (C AND D)`` is an OR group of two AND groups;
    ``A AND (B OR (C AND D))`` nests one deeper -- no left-associative
    fold, no fixed two-level shape, every combination parenthesised
    exactly as the tree says.

    Compiled defensively because the same AST arrives from share tokens
    and URLs, not only from widgets: unknown node types, bad operators,
    non-list children, floods and over-deep nesting are all ValueError,
    and every parameter still passes the bag's global budget. A group
    with nothing to say compiles to None, so a half-built panel filters
    nothing instead of erroring per keystroke.
    """
    if _count is None:
        _count = [0]
    _count[0] += 1
    if _count[0] > MAX_TREE_NODES:
        raise ValueError(
            f"The query holds more than {MAX_TREE_NODES} conditions")
    if not isinstance(node, dict):
        raise ValueError("Query nodes must be objects")

    kind = node.get("type")
    if kind == "group":
        if _depth >= MAX_GROUP_DEPTH:
            raise ValueError(
                f"Groups may nest at most {MAX_GROUP_DEPTH} levels deep")
        op = node.get("op")
        if op not in ("AND", "OR"):
            raise ValueError(f"Bad group operator: {op!r}")
        children = node.get("children") or []
        if not isinstance(children, (list, tuple)):
            raise ValueError("Group children must be a list")
        if len(children) > MAX_GROUP_CHILDREN:
            raise ValueError(
                f"A group may hold at most {MAX_GROUP_CHILDREN} items")
        parts = [clause for child in children
                 if (clause := compile_condition_node(
                     child, known_columns, bag,
                     _depth=_depth + 1, _count=_count))]
        return combine_condition_clauses(parts, op)
    if kind in (None, "condition"):
        return compile_condition(node, known_columns, bag)
    raise ValueError(f"Unsupported query node type: {kind!r}")


# ------------------------------------------------------- shareable state

#: Hard ceiling on a share token's *decompressed* size. A legitimate token
#: holds a few groups of conditions -- hundreds of bytes, kilobytes at the
#: outside -- while deflate can pack ~1000:1, so an unbounded
#: zlib.decompress() would let a 64 KB query parameter unpack into tens of
#: megabytes. Decompression is capped here, before json.loads ever runs.
MAX_STATE_BYTES = 256 * 1024

#: And on the compressed token itself, so the decoder never even feeds a
#: multi-megabyte parameter to zlib.
MAX_TOKEN_CHARS = 64 * 1024

#: Bounds on the decoded payload's shape. The restore path iterates
#: groups and conditions, and future nesting must not turn a doctored
#: token into deep recursion or a million-node walk.
MAX_TREE_DEPTH = 16
MAX_TREE_NODES = 2_000

#: No single string in a payload may exceed this. A node count alone is
#: not enough: one 100 MB string is one "node", and downstream code that
#: iterates a string (list(), a join, a LIKE-escape) would amplify it.
MAX_SCALAR_CHARS = 8_192

#: And no rule may carry more values than a legitimate multiselect could.
MAX_RULE_VALUES = 200


def validate_tree(node, *, _depth: int = 0, _count: list | None = None) -> None:
    """Refuse a payload nested deeper than MAX_TREE_DEPTH, holding more
    than MAX_TREE_NODES containers/values, or carrying any single string
    longer than MAX_SCALAR_CHARS. Raises ValueError.

    Structural only: names and values are still vetted against the live
    schema by _apply_restored_state. This guard exists so that vetting
    (and json.dumps re-serialisation, and Streamlit state seeding) only
    ever runs over a payload of sane size and shape.
    """
    if _count is None:
        _count = [0]
    _count[0] += 1
    if _count[0] > MAX_TREE_NODES:
        raise ValueError(
            f"Query payload holds more than {MAX_TREE_NODES} nodes")
    if _depth > MAX_TREE_DEPTH:
        raise ValueError(
            f"Query payload nests deeper than {MAX_TREE_DEPTH} levels")
    if isinstance(node, str):
        if len(node) > MAX_SCALAR_CHARS:
            raise ValueError(
                f"Query payload holds a value longer than "
                f"{MAX_SCALAR_CHARS} characters")
        return
    if isinstance(node, dict):
        for key, value in node.items():
            validate_tree(str(key), _depth=_depth + 1, _count=_count)
            validate_tree(value, _depth=_depth + 1, _count=_count)
    elif isinstance(node, (list, tuple)):
        for value in node:
            validate_tree(value, _depth=_depth + 1, _count=_count)


#: The share-token envelope version this build writes and reads.
TOKEN_VERSION = 1


def _reject_constant(text: str):
    """json.loads hook: Infinity/-Infinity/NaN are not query values."""
    raise ValueError(f"Non-finite JSON value: {text}")


def _reject_nonfinite(node) -> None:
    """Refuse any non-finite float anywhere in a payload.

    A belt over parse_constant's braces: floats can also arrive from
    payloads built in Python (a doctored session value, a buggy caller),
    and one inf seeded into a number widget takes the page down far from
    the cause.
    """
    if isinstance(node, float) and not math.isfinite(node):
        raise ValueError("Query state contains a non-finite number")
    if isinstance(node, dict):
        for key, value in node.items():
            _reject_nonfinite(key)
            _reject_nonfinite(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _reject_nonfinite(value)


def serialize_state(payload: dict) -> str:
    """The builder's state as a compact URL-safe token.

    Deterministic JSON (sorted keys, no whitespace), deflated and
    base64url-encoded -- ready to ride in a query parameter. The token
    holds only column names, operator labels and typed values; it is
    *data about a query*, and everything in it still passes the discovery
    gate and the parameter bag when it is compiled after a restore, so a
    doctored token can rename nothing and inject nothing.

    Strict on the way out as well as in: no ``default=`` hook silently
    stringifying unsupported objects, no NaN/Infinity (json would happily
    write literals its own loads-with-guards then rejects), and the
    payload passes the same shape bounds the decoder applies -- a token
    this function returns is always one deserialize_state accepts.
    """
    validate_tree(payload)
    _reject_nonfinite(payload)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True,
                     allow_nan=False).encode("utf-8")
    if len(raw) > MAX_STATE_BYTES:
        raise ValueError("Query state is too large to share")
    token = base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii")
    # The decoder's ceiling, applied on the way out: MAX_STATE_BYTES
    # bounds the JSON but says nothing about the *encoded* size, and an
    # incompressible payload under the first limit can still exceed the
    # second -- handing the reader a "shareable" token no restore would
    # ever accept. Fail here, immediately and with the reason, instead.
    if len(token) > MAX_TOKEN_CHARS:
        raise ValueError("Compressed query state is too large to share")
    return token


def deserialize_state(token: str) -> dict:
    """Decode a share token back to its payload dict, or raise ValueError.

    Decompression is bounded: the compressed input is size-checked and
    strictly base64-decoded (no charset garbage), the inflater is given a
    hard output ceiling (MAX_STATE_BYTES), and unconsumed or trailing
    input makes the token invalid -- a decompression bomb or concatenated
    stream dies here as a ValueError, never as memory exhaustion. The
    decoded payload's shape is then bounded by validate_tree and swept
    for non-finite numbers before anything walks it.
    """
    text = str(token).strip()
    if len(text) > MAX_TOKEN_CHARS:
        raise ValueError("Not a query token: too large")
    try:
        packed = base64.b64decode(text.encode("ascii"), altchars=b"-_",
                                  validate=True)
        inflater = zlib.decompressobj()
        raw = inflater.decompress(packed, MAX_STATE_BYTES)
        if (inflater.unconsumed_tail or inflater.unused_data
                or not inflater.eof):
            raise ValueError(
                f"decompressed size exceeds {MAX_STATE_BYTES} bytes")
        payload = json.loads(raw.decode("utf-8"),
                             parse_constant=_reject_constant)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Not a query token: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Not a query-state payload")
    validate_tree(payload)
    _reject_nonfinite(payload)
    return payload


def build_select(table: str, columns, where: str | None,
                 order_by: str | None, descending: bool,
                 limit: int, bag: ParamBag,
                 known_tables, known_columns) -> str:
    """Assemble the final statement from vetted parts.

    Every identifier is checked against what discovery returned and then
    quoted; the WHERE expression arrives already parameterised (one
    compiled group-AST clause carrying its own parentheses -- the old
    flat AND/OR combinator between loose predicates is gone with the flat
    model); the limit is validated server-side and bound as a parameter.
    Duplicate display columns are rejected: they can only come from forged
    widget state, and duplicated names make the frame ambiguous.
    """
    _require_known(table, known_tables, "table")
    columns = list(columns)
    if len(columns) != len(set(columns)):
        raise ValueError("Duplicate display columns are not allowed")
    select_list = ", ".join(
        _quote_ident(_require_known(c, known_columns, "column"))
        for c in columns) or "*"
    sql = f"SELECT {select_list}\nFROM {_quote_ident(table)}"
    if where:
        sql += f"\nWHERE {where}"
    if order_by:
        _require_known(order_by, known_columns, "sort column")
        sql += (f"\nORDER BY {_quote_ident(order_by)} "
                f"{'DESC' if descending else 'ASC'}")
    sql += f"\nLIMIT {bag.add(_bounded_limit(limit))}"
    return sql


def build_group_select(table: str, group_columns, where: str | None,
                       limit: int, bag: ParamBag,
                       known_tables, known_columns) -> str:
    """A COUNT(*) AS total per group, filtered the same way build_select is.

    The aggregation the row-limit question keeps turning into: "how many
    games at each venue", "players per debut season". Identifiers pass the
    same discovery gate; groups sort by their count, biggest first, so the
    answer leads with the headline.
    """
    _require_known(table, known_tables, "table")
    group_columns = list(group_columns)
    if not group_columns:
        raise ValueError("Group by at least one column.")
    if len(group_columns) != len(set(group_columns)):
        raise ValueError("Duplicate group-by columns are not allowed")
    grouped = ", ".join(
        _quote_ident(_require_known(c, known_columns, "column"))
        for c in group_columns)
    sql = (f"SELECT {grouped}, COUNT(*) AS total"
           f"\nFROM {_quote_ident(table)}")
    if where:
        sql += f"\nWHERE {where}"
    sql += (f"\nGROUP BY {grouped}"
            f"\nORDER BY total DESC, {grouped}"
            f"\nLIMIT {bag.add(_bounded_limit(limit))}")
    return sql


# ------------------------------------------------- WHERE construction (A)

def condition_tree_config(cols, profiles: dict) -> dict:
    """A condition-tree field config generated from the discovered schema.

    The same measured profiles that drive the native widgets drive the
    tree: numeric fields get their real min/max, low-cardinality text
    becomes a select of the values the database actually contains. Columns
    whose names would need quoting are left out -- the component writes
    field names into its SQL output verbatim -- and every build here has
    snake_case columns, so in practice nothing is lost.
    """
    type_map = {"integer": "number", "float": "number", "boolean": "boolean",
                "date": "date", "datetime": "datetime", "text": "text"}
    fields: dict = {}
    for col in cols:
        if not _SAFE_IDENT.match(col.name):
            continue
        cfg: dict = {"label": labels.words(col.name),
                     "type": type_map[col.kind]}
        profile = profiles.get(col.name, {})
        if col.kind in ("integer", "float"):
            # No fieldSettings min/max: the column's observed extremes are
            # a fact about yesterday's data, not a cap on what may be
            # asked -- bounding the input at max(goals)=40 forbade the
            # very question "what if someone kicks 41".
            pass
        elif col.kind == "text" and profile.get("values"):
            cfg["type"] = "select"
            cfg["fieldSettings"] = {"listValues": [
                {"value": v, "title": v} for v in profile["values"]]}
        fields[col.name] = cfg
    return {"fields": fields}


#: Tree operators that map one-for-one onto a SQL comparison.
_TREE_BINARY_OPS = {
    "equal": "=", "not_equal": "<>",
    "less": "<", "less_or_equal": "<=",
    "greater": ">", "greater_or_equal": ">=",
    "select_equals": "=", "select_not_equals": "<>",
}

#: Which tree operators are legitimate for each column kind. The
#: component's own UI already offers only the right ones, but its return
#: value is a websocket message: a doctored payload could apply LIKE to a
#: numeric column or arithmetic to text, compiling to a predicate that
#: silently matches wrongly (and scans). Enforced whenever the compiler
#: is handed full column metadata (a {name: Column} mapping) rather than
#: a bare name set.
_TREE_COMPARISONS = ("equal", "not_equal", "less", "less_or_equal",
                     "greater", "greater_or_equal", "between", "not_between")
_TREE_NULL_OPS = ("is_null", "is_not_null")
_TREE_SELECT_OPS = ("select_equals", "select_not_equals", "select_any_in",
                    "select_not_any_in", "multiselect_equals")
_TREE_TEXT_OPS = ("like", "not_like", "starts_with", "ends_with",
                  "is_empty", "is_not_empty")
TREE_OPS_BY_KIND = {
    "integer": (*_TREE_COMPARISONS, *_TREE_NULL_OPS),
    "float": (*_TREE_COMPARISONS, *_TREE_NULL_OPS),
    "boolean": ("equal", "not_equal", *_TREE_NULL_OPS),
    "date": (*_TREE_COMPARISONS, *_TREE_NULL_OPS),
    "datetime": (*_TREE_COMPARISONS, *_TREE_NULL_OPS),
    "text": ("equal", "not_equal", *_TREE_SELECT_OPS, *_TREE_TEXT_OPS,
             *_TREE_NULL_OPS),
}


def _tree_children(node: dict) -> list:
    """A group's children, whichever container shape the component used.

    streamlit_condition_tree 0.3 renames the builder's `children1` to
    `children` (and strips node ids) in the tree it hands back to Python,
    while raw react-awesome-query-builder exports keep `children1` as a
    list or an id-keyed object -- accept every shape it actually sends.
    Strictly a dict or a list beyond that: `list()` over any other
    iterable is an amplifier -- a hostile string of N characters would
    become N child "nodes" -- so anything else is refused, never coerced.
    """
    children = node.get("children")
    if children is None:
        children = node.get("children1")
    if not children:
        return []
    if isinstance(children, dict):
        return list(children.values())
    if not isinstance(children, (list, tuple)):
        raise ValueError("Tree children must be a list or an object")
    return list(children)


def _tree_scalar(operator: str, values: list, position: int = 0):
    """One bound-parameter-safe scalar from a rule's value list."""
    if position >= len(values):
        raise ValueError(f"{operator} needs a value")
    value = values[position]
    if isinstance(value, (dict, list, tuple)):
        raise ValueError(f"Unsupported value shape for {operator}")
    return value


def _coerce_tree_value(col: Column | None, value):
    """One tree-rule value, validated against its column's kind.

    The operator allowlist (TREE_OPS_BY_KIND) stops LIKE reaching a
    number, but the *value* in a comparison is its own attack surface: a
    doctored payload can put text under an integer field or a dict where
    a scalar belongs, and an unchecked bind then compares wrongly instead
    of failing. Values are coerced through the same gates the native
    panel uses -- coerce_number for numbers, ISO parsing for dates -- so
    a value that cannot mean what the column stores is a ValueError,
    never a silently-empty (or silently-broad) result.

    ``col`` is None when the caller supplied a bare name set instead of
    column metadata; shape and size are still enforced, kind is not.
    """
    if isinstance(value, (dict, list, tuple, set)):
        raise ValueError("Tree condition values must be scalars")
    if value is None:
        raise ValueError("Tree condition value cannot be null")
    if isinstance(value, str) and len(value) > MAX_SCALAR_CHARS:
        raise ValueError("Tree condition value is too long")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Tree condition values must be finite")
    if col is None:
        return value

    if col.kind == "integer":
        return coerce_number(value, integer=True)
    if col.kind == "float":
        return coerce_number(value)
    if col.kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError("Boolean conditions require true or false")
        # Flags are stored as 0/1 INTEGER in every build here.
        return int(value)
    if col.kind == "date":
        try:
            return dt.date.fromisoformat(str(value)).isoformat()
        except ValueError as exc:
            raise ValueError(
                "Date conditions require an ISO YYYY-MM-DD value") from exc
    if col.kind == "datetime":
        text = str(value)
        try:
            dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                "Datetime conditions require an ISO-8601 value") from exc
        # Bound as sent: the stored TEXT is compared lexically, and
        # rewriting the caller's spelling could move the boundary.
        return text
    return str(value)


def compile_tree_node(node, known_columns, bag: ParamBag) -> str | None:
    """Compile one node of the component's condition tree to safe SQL.

    This walks the *structured* tree the component exports, not the SQL
    string it compiles in the browser: the string is a component return
    value, and a component return value is attacker-controlled. Every
    field name is checked against discovery, every value becomes a bound
    parameter, and an operator this walker does not know is a red box
    rather than a pass-through.

    Returns None for a node with nothing to say yet — an empty group, or a
    rule the reader is still filling in — so a half-built tree filters
    nothing instead of erroring on every keystroke.

    ``known_columns`` is ideally a ``{name: Column}`` mapping, which
    additionally gates each operator against the column's kind (LIKE on a
    number is a doctored payload, not a keystroke); a bare name set keeps
    the identifier wall alone, for callers without metadata.
    """
    if not isinstance(node, dict):
        raise ValueError("Tree nodes must be objects")
    kind = node.get("type")
    properties = node.get("properties")
    if properties is None:
        properties = {}
    if not isinstance(properties, dict):
        raise ValueError("Tree node properties must be an object")
    if kind in ("group", "rule_group"):
        parts = [sql for child in _tree_children(node)
                 if (sql := compile_tree_node(child, known_columns, bag))]
        if not parts:
            return None
        conjunction = str(properties.get("conjunction") or "AND").upper()
        if conjunction not in ("AND", "OR"):
            raise ValueError(f"Bad conjunction: {conjunction!r}")
        joined = "(" + f" {conjunction} ".join(parts) + ")"
        return f"NOT {joined}" if properties.get("not") else joined
    if kind != "rule":
        raise ValueError(f"Unsupported tree node type: {kind!r}")

    field, operator = properties.get("field"), properties.get("operator")
    if field is None or operator is None:
        return None                       # rule still being built
    if not isinstance(field, str) or not isinstance(operator, str):
        raise ValueError("Tree field and operator must be strings")
    if not field or not operator:
        return None
    _require_known(field, known_columns, "column")
    col_meta = (known_columns.get(field)
                if isinstance(known_columns, dict) else None)
    if col_meta is not None and operator not in TREE_OPS_BY_KIND.get(
            col_meta.kind, ()):
        raise ValueError(
            f"{operator!r} is not valid for the {col_meta.kind} "
            f"column {field!r}")
    column = _quote_ident(field)
    # The component always exports `value` as a list. Anything else is a
    # doctored payload: `list()` over a string would explode it into
    # per-character values, so the shape is enforced, not coerced.
    raw_values = properties.get("value")
    if raw_values is None:
        raw_values = []
    if not isinstance(raw_values, (list, tuple)):
        raise ValueError("Rule values must be a list")
    if len(raw_values) > MAX_RULE_VALUES:
        raise ValueError(
            f"Rule holds more than {MAX_RULE_VALUES} values")
    values = list(raw_values)

    if operator in _TREE_BINARY_OPS:
        if not values or values[0] is None:
            return None                   # rule still being built
        value = _coerce_tree_value(col_meta, _tree_scalar(operator, values))
        return f"{column} {_TREE_BINARY_OPS[operator]} {bag.add(value)}"
    if operator in ("between", "not_between"):
        if len(values) < 2 or values[0] is None or values[1] is None:
            return None                   # rule still being built
        lo = _coerce_tree_value(col_meta, _tree_scalar(operator, values, 0))
        hi = _coerce_tree_value(col_meta, _tree_scalar(operator, values, 1))
        clause = f"{column} BETWEEN {bag.add(lo)} AND {bag.add(hi)}"
        return f"NOT ({clause})" if operator == "not_between" else clause
    if operator in ("select_any_in", "select_not_any_in", "multiselect_equals"):
        chosen = values[0] if values and isinstance(values[0], list) else values
        # Fail closed, never narrow: a malformed member invalidates the
        # whole rule. The predecessor filtered bad members out, which
        # quietly changed ["A", {...}] into IN ("A") -- a different query
        # than the payload specified. Only the genuinely-empty selection
        # (a rule mid-edit) stays a no-op.
        chosen = [_coerce_tree_value(col_meta, value)
                  for value in _bounded_values(chosen)]
        if not chosen:
            return None                   # rule still being built
        marks = ", ".join(bag.add(v) for v in chosen)
        clause = f"{column} IN ({marks})"
        return f"NOT ({clause})" if operator == "select_not_any_in" else clause
    if operator in ("like", "not_like", "starts_with", "ends_with"):
        if not values or values[0] is None:
            return None                   # rule still being built
        text = str(_coerce_tree_value(col_meta,
                                      _tree_scalar(operator, values)))
        escaped = (text.replace("\\", "\\\\")
                       .replace("%", "\\%")
                       .replace("_", "\\_"))
        pattern = {"like": f"%{escaped}%", "not_like": f"%{escaped}%",
                   "starts_with": f"{escaped}%",
                   "ends_with": f"%{escaped}"}[operator]
        clause = f"{column} LIKE {bag.add(pattern)} ESCAPE '\\'"
        return f"NOT ({clause})" if operator == "not_like" else clause
    if operator in ("is_null", "is_not_null"):
        return f"{column} IS {'NOT ' if operator == 'is_not_null' else ''}NULL"
    if operator == "is_empty":
        # Bare column both sides of the OR -- the old COALESCE(col, '')
        # wrapped the indexed side, turning a two-way index probe into a
        # scan of the covering index.
        return f"({column} IS NULL OR {column} = {bag.add('')})"
    if operator == "is_not_empty":
        return f"({column} IS NOT NULL AND {column} <> {bag.add('')})"
    raise ValueError(f"Unsupported operator: {operator!r}")


# --------------------------------------------------------------- execution

def run_query(conn, sql: str, params: dict, revision) -> pd.DataFrame:
    """Execute through st.connection's own result cache.

    ``conn.query`` caches on (sql, params) with the given ttl; the revision
    comment appended here folds the database file's identity into that key,
    so a rebuilt database busts the cache immediately instead of serving
    the old file's rows until the ttl expires.

    ``convert_dtypes`` keeps a NULLable INTEGER column integer with real
    <NA> holes instead of floating the whole column -- a games count of
    123.0/NaN visually asserts a precision the data never had.
    """
    frame = conn.query(f"{sql}\n/* rev {revision} */", params=params, ttl=600)
    return frame.convert_dtypes()


# ------------------------------------------------------------------- page

def _filter_widget(container, sport, table: str, col: Column, profile: dict,
                   bag: ParamBag) -> str | None:
    """One column's filter as a compiled predicate (compatibility shim).

    The widgets now emit *specs* (see `_spec_widget` / `compile_condition`)
    so a condition can be counted, serialised and compiled independently
    of its widgets; this wrapper keeps the old render-and-compile contract
    for callers like scripts/filter_widget_demo.py.
    """
    spec = _spec_widget(container, sport.k("qb", table, col.name), col,
                        profile)
    if spec is None:
        return None
    return compile_condition(spec, {col.name}, bag)


def _spec_widget(container, key: str, col: Column,
                 profile: dict) -> dict | None:
    """One column's filter: an operator picked for its type, then inputs.

    Returns the JSON-able condition spec the widgets currently describe,
    or None while the widget sits in its "no filter" state. Every kind
    offers the full operator set its type supports -- comparisons, ranges,
    lists, text patterns, and IS NULL / IS NOT NULL for the columns whose
    empty cells are the finding. Numeric inputs carry NO upper bound: the
    column's observed maximum is shown as a hint, never enforced, because
    yesterday's record is not a cap on what may be asked.
    """
    label = labels.words(col.name)
    if col.kind in ("integer", "float"):
        return _numeric_spec(container, key, label, col, profile)
    if col.kind == "boolean":
        st.session_state.setdefault(f"{key}:bool", "Any")
        choice = container.segmented_control(
            label, ["Any", "Yes", "No", "Missing", "Present"],
            key=f"{key}:bool", persist_state="session")
        op = {"Yes": "is true", "No": "is false", "Missing": "is missing",
              "Present": "is present"}.get(choice)
        return {"column": col.name, "kind": "boolean", "op": op} \
            if op else None
    if col.kind in ("date", "datetime"):
        return _date_spec(container, key, label, col, profile)
    return _text_spec(container, key, label, col, profile)


def _numeric_spec(container, key, label, col, profile):
    lo, hi = profile.get("lo"), profile.get("hi")
    span = ("" if lo is None or hi is None
            else f"data spans {lo:g}–{hi:g}")
    # Integer columns step and format as integers, so the widget can never
    # hand the compiler 1.9 to truncate; the compiler still refuses
    # fractions server-side because widget behavior is browser advice.
    number_kwargs = ({"step": 1, "format": "%d"}
                     if col.kind == "integer" else {"step": 0.01})
    op_col, value_col = container.columns((1, 2))
    operator = op_col.selectbox(label, _NUMERIC_OPS, key=f"{key}:op",
                                persist_state="session")
    spec = {"column": col.name, "kind": col.kind, "op": operator}
    if operator in ("is missing", "is present"):
        return spec
    if operator == "between":
        a, b = value_col.columns(2)
        st.session_state.setdefault(f"{key}:lo", None)
        st.session_state.setdefault(f"{key}:hi", None)
        low = a.number_input("from", key=f"{key}:lo",
                             label_visibility="collapsed",
                             persist_state="session", **number_kwargs,
                             placeholder=None if lo is None else f"{lo:g}")
        high = b.number_input("to", key=f"{key}:hi",
                              label_visibility="collapsed",
                              persist_state="session", **number_kwargs,
                              placeholder=None if hi is None else f"{hi:g}")
        if span:
            value_col.caption(span)
        if low is None or high is None:
            return None
        spec.update(lo=low, hi=high)
        return spec
    if operator == "one of":
        typed = value_col.text_input(
            "values", key=f"{key}:in", label_visibility="collapsed",
            persist_state="session", placeholder="e.g. 1, 5, 10")
        try:
            chosen = parse_number_list(typed)
        except ValueError as exc:
            value_col.error(str(exc))
            return None
        if not chosen:
            return None
        spec["values"] = chosen
        return spec
    st.session_state.setdefault(f"{key}:val", None)
    value = value_col.number_input(
        "value", key=f"{key}:val",
        label_visibility="collapsed",
        persist_state="session", **number_kwargs,
        placeholder=span or "value",
        help=None if not span else
        f"No cap — {span}, but any value may be asked.")
    if value is None:
        return None
    spec["value"] = value
    return spec


def _text_spec(container, key, label, col, profile):
    values = profile.get("values")
    op_col, value_col = container.columns((1, 2))
    st.session_state.setdefault(
        f"{key}:op", "one of" if values is not None else "contains")
    operator = op_col.selectbox(label, _TEXT_OPS, key=f"{key}:op",
                                persist_state="session")
    spec = {"column": col.name, "kind": "text", "op": operator}
    if operator in ("is missing", "is present"):
        return spec
    if operator == "one of":
        if values is not None:
            pick_key = f"{key}:pick"
            parked = st.session_state.get(pick_key)
            if parked is not None:
                # The offered values are re-measured per revision; a
                # parked pick outside today's list would crash the
                # multiselect rather than filter.
                st.session_state[pick_key] = [v for v in parked
                                              if v in values]
            chosen = value_col.multiselect(
                "values", values, key=pick_key,
                label_visibility="collapsed", persist_state="session")
        else:
            typed = value_col.text_input(
                "values", key=f"{key}:list", label_visibility="collapsed",
                persist_state="session",
                placeholder="comma, separated, values")
            chosen = [part.strip() for part in typed.split(",")
                      if part.strip()]
        if not chosen:
            return None
        spec["values"] = chosen
        return spec
    typed = value_col.text_input("value", key=f"{key}:val",
                                 label_visibility="collapsed",
                                 persist_state="session",
                                 placeholder="text")
    if not typed:
        return None
    spec["value"] = typed
    return spec


def _date_spec(container, key, label, col, profile):
    bounds = _date_bounds(profile)
    op_col, value_col = container.columns((1, 2))
    operator = op_col.selectbox(label, _DATE_OPS, key=f"{key}:op",
                                persist_state="session")
    if operator in ("is missing", "is present"):
        return {"column": col.name, "kind": col.kind, "op": operator}
    if bounds is None:
        # Bounds unreadable as dates (SQLite will store anything): a text
        # match is honest where a picker would lie.
        typed = value_col.text_input("value", key=f"{key}:txt",
                                     label_visibility="collapsed",
                                     persist_state="session")
        return ({"column": col.name, "kind": "text", "op": "contains",
                 "value": typed} if typed else None)
    # A datetime kind, or bounds longer than a bare date, may carry a
    # time-of-day: day-granular ceilings then need half-open next-day
    # bounds. Either way the raw column stays on the left -- ISO text
    # compares correctly as strings, so no DATE() wrapper, no lost index.
    ceiling = (col.kind == "datetime"
               or len(str(profile.get("hi") or "")) > 10)
    spec = {"column": col.name, "kind": col.kind, "op": operator,
            "day_ceiling": ceiling}
    if operator == "between":
        st.session_state.setdefault(f"{key}:range", bounds)
        picked = value_col.date_input(label, key=f"{key}:range",
                                      persist_state="session",
                                      label_visibility="collapsed")
        if not isinstance(picked, tuple) or len(picked) != 2:
            return None          # picker mid-edit: one end chosen so far
        if picked == bounds:
            return None          # full range = not actually filtering
        spec.update(lo=picked[0].isoformat(), hi=picked[1].isoformat())
        return spec
    st.session_state.setdefault(
        f"{key}:one",
        bounds[0] if operator in ("on", "after", "on or after")
        else bounds[1])
    picked = value_col.date_input(label, key=f"{key}:one",
                                  persist_state="session",
                                  label_visibility="collapsed")
    if picked is None:
        return None
    spec["value"] = picked.isoformat()
    return spec


def _date_bounds(profile: dict):
    """The column's (min, max) as dates, or None when they don't parse."""
    try:
        lo = dt.date.fromisoformat(str(profile.get("lo"))[:10])
        hi = dt.date.fromisoformat(str(profile.get("hi"))[:10])
    except (TypeError, ValueError):
        return None
    return (lo, hi) if lo <= hi else None


# ---------------------------------------------- filter panel: groups (B)

def _clause_count(conn, table: str, clause: str, params: dict,
                  revision) -> int | None:
    """COUNT(*) for one compiled clause, through st.connection's cache.

    ``conn.query`` caches on (sql, params); the revision comment folds the
    file's identity into the key, exactly as run_query does. Returns None
    rather than raising -- a count badge must never be the thing that
    breaks the panel.
    """
    try:
        frame = conn.query(
            f"SELECT COUNT(*) AS n FROM {_quote_ident(table)} "
            f"WHERE {clause} /* rev {revision} */",
            params=params, ttl=600)
        return 0 if frame.empty else int(frame.at[0, "n"])
    except Exception:
        return None


def _condition_count(conn, table: str, spec: dict, known_columns,
                     revision) -> int | None:
    """How many rows one condition matches on its own.

    The spec compiles into a bag of its own here, so the count query's
    placeholders are self-contained and cache-stable whatever position
    the condition holds in the full query.
    """
    bag = ParamBag()
    try:
        clause = compile_condition(spec, known_columns, bag)
    except ValueError:
        return None
    if clause is None:
        return None
    return _clause_count(conn, table, clause, bag.values, revision)


def _groups_state(sport, table: str) -> dict:
    """The filter panel's one session dict, per table.

    root -- the panel's Boolean *shape*: a tree of ``{"gid": n,
            "children": [...]}`` group nodes. Everything a group *says*
            (its ALL/ANY rule, chosen columns, operator settings) lives in
            widget state under keys derived from the gid -- with
            ``persist_state="session"`` so a hidden mode keeps them --
            and the compiled AST mirrors both into one structure.
    next -- the next gid to mint. Gids are never reused: a widget key
            built from a gid must never resurrect the values of a group
            that was deleted, and a restore must land on keys no widget
            has rendered yet.
    """
    key = sport.k("qbf_state", table)
    state = st.session_state.get(key)
    if not isinstance(state, dict) or "root" not in state:
        state = {"root": {"gid": 0, "children": []}, "next": 1}
        st.session_state[key] = state
    return state


def _match_rule(label: str | None) -> str:
    return "AND" if not label or label.startswith("all") else "OR"


def _filter_groups(sport, table: str, cols, profiles: dict, conn, revision,
                   bag: ParamBag) -> tuple[str | None, dict | None]:
    """The recursive condition-group UI, compiled to one WHERE expression.

    Each bordered card is a group: any number of column conditions plus
    any number of nested subgroups, matched with the group's own ALL
    (AND) / ANY (OR) rule -- so ``(played Collingwood AND 150+ games) OR
    (drafted Hawthorn AND premiership player)`` is an ANY root holding
    two ALL cards, and ``A AND (B OR (C AND D))`` just nests one deeper.
    Every active condition and multi-condition group shows a live count
    of the rows it matches alone, and the whole panel round-trips through
    a shareable token / URL.

    Returns ``(where, query)``: the compiled expression (None while
    nothing filters) with its parameters in ``bag``, and the JSON-able
    group AST the share token stores.
    """
    state = _groups_state(sport, table)
    by_name = {c.name: c for c in cols}
    ordered = categorised_order(by_name)

    query = _render_filter_group(sport, table, state, state["root"], None,
                                 by_name, ordered, profiles, conn, revision,
                                 depth=0)
    where = None
    if query is not None:
        try:
            where = compile_condition_node(query, set(by_name), bag)
        except ValueError as exc:
            st.error(str(exc))
            return None, None
    return where, query


def _render_filter_group(sport, table: str, state: dict, node: dict,
                         parent: dict | None, by_name: dict, ordered,
                         profiles: dict, conn, revision,
                         depth: int) -> dict | None:
    """One group card, recursively: conditions, subgroups, controls.

    Returns the group's AST node -- ``{"type": "group", "op", "children"}``
    with condition specs and child groups as children -- or None while the
    group holds nothing that filters.
    """
    gid = node["gid"]
    base = sport.k("qbf", table, gid)
    known = set(by_name)

    def shelf_label(name):
        return f"{labels.words(name)} — {column_category(name)}"

    head = st.container(horizontal=True, vertical_alignment="center",
                        gap="small")
    with head:
        with st.container(width="stretch"):
            st.markdown("**Conditions**" if depth == 0
                        else "**Subgroup**")
        # setdefault, not default=: a restore pre-seeds this key, and a
        # widget given both a default and session state logs a warning.
        st.session_state.setdefault(f"{base}:match", "all (AND)")
        match_label = st.segmented_control(
            "Match", ["all (AND)", "any (OR)"],
            key=f"{base}:match", label_visibility="collapsed",
            persist_state="session",
            help="Whether a row must satisfy all of this group's "
                 "conditions and subgroups, or any one of them.")
        if parent is not None and st.button(
                ":material/delete:", key=f"{base}:drop",
                type="tertiary", help="Remove this group"):
            parent["children"] = [child for child in parent["children"]
                                  if child["gid"] != gid]
            st.rerun()

    chosen = st.multiselect(
        "Conditions — columns are grouped by category", ordered,
        key=f"{base}:cols", format_func=shelf_label,
        placeholder="Type to search columns by name or category…",
        persist_state="session")

    match = _match_rule(match_label)
    children: list[dict] = []
    condition_count = 0
    for name in chosen:
        # The multiselect's options come from discovery, but its *state*
        # is a session value a client can forge: gate before dereference.
        col = by_name.get(str(name))
        if col is None:
            st.error(f"Unknown column: {name!r}")
            continue
        box = st.container(border=True)
        spec = _spec_widget(box, f"{base}:{name}", col,
                            profiles.get(name, {}))
        if spec is None:
            box.caption("No filter yet — choose an operator and value.")
            continue
        children.append(spec)
        condition_count += 1
        alone = _condition_count(conn, table, spec, known, revision)
        if alone is not None:
            box.caption(f":material/filter_alt: matches "
                        f"{alone:,} row{'' if alone == 1 else 's'} "
                        f"on its own")

    for child in list(node["children"]):
        with st.container(border=True):
            child_ast = _render_filter_group(
                sport, table, state, child, node, by_name, ordered,
                profiles, conn, revision, depth + 1)
        if child_ast is not None:
            children.append(child_ast)

    if depth < MAX_UI_GROUP_DEPTH and st.button(
            "Add a subgroup", icon=":material/account_tree:",
            key=f"{base}:subgroup", type="tertiary",
            help="A nested group with its own ALL/ANY rule — for shapes "
                 "like (played Collingwood AND 150+ games) OR (drafted "
                 "Hawthorn AND premiership player)."):
        node["children"].append({"gid": state["next"], "children": []})
        state["next"] += 1
        st.rerun()

    if not children:
        return None
    ast = {"type": "group", "op": match, "children": children}
    if len(children) > 1:
        # The group's own live count, from a self-contained bag so the
        # badge query is cache-stable whatever the group's position.
        group_bag = ParamBag()
        try:
            shown = compile_condition_node(ast, known, group_bag)
        except ValueError:
            shown = None
        if shown:
            n = _clause_count(conn, table, shown, group_bag.values,
                              revision)
            if n is not None:
                st.caption(f"Group matches {n:,} "
                           f"row{'' if n == 1 else 's'} on its own")
    return ast



#: Session-state modes a token's ``mode`` field maps onto.
_TOKEN_MODES = {"filters": MODE_FILTERS, "tree": MODE_TREE,
                "grid": MODE_CONSTRAINTS}


def build_share_envelope(sport, mode: str, query, *, table: str | None = None,
                         display: dict | None = None) -> dict:
    """The versioned payload a share token / URL carries.

    Everything needed to reproduce the query -- sport, builder mode,
    table, display settings and the canonical query AST -- under an
    explicit version so a future format change can migrate or refuse old
    tokens instead of guessing at them.
    """
    payload = {"v": TOKEN_VERSION, "sport": sport.key, "mode": mode,
               "query": query}
    if table is not None:
        payload["table"] = table
    if display is not None:
        payload["display"] = display
    return payload


#: Exact field vocabulary and obligations *per mode*. The union of every
#: mode's fields would accept a grid token carrying table/columns/sort --
#: fields grid restore ignores -- and a token whose fields are silently
#: ignored is not reproduced, it is reinterpreted. ``sport`` stays
#: optional everywhere: migrated legacy tokens never knew theirs, and the
#: page refuses a mismatched one either way.
_ENVELOPE_KEYS_BY_MODE = {
    "grid": {"v", "sport", "mode", "display", "query"},
    "filters": {"v", "sport", "mode", "table", "display", "query"},
    "tree": {"v", "sport", "mode", "table", "display", "query"},
}
_REQUIRED_KEYS_BY_MODE = {
    "grid": {"v", "mode", "query"},
    "filters": {"v", "mode", "table", "query"},
    "tree": {"v", "mode", "table", "query"},
}
_DISPLAY_KEYS_BY_MODE = {
    "grid": {"order", "limit"},
    "filters": {"columns", "sort", "descending", "limit", "group_by"},
    "tree": {"columns", "sort", "descending", "limit", "group_by"},
}


def validate_envelope(payload: dict) -> dict:
    """Version/shape-check a decoded token, migrating the legacy format.

    Returns the (possibly migrated) envelope or raises ValueError. Every
    envelope -- including one lifted out of the legacy shape -- passes
    the same v1 validator, so migration cannot become a side door around
    the field rules.
    """
    if not isinstance(payload, dict):
        raise ValueError("Not a query-state payload")
    if "v" not in payload:
        if "groups" in payload and "table" in payload:
            payload = _migrate_legacy_payload(payload)
        else:
            raise ValueError("Not a query token this build understands")
    return _validate_v1_envelope(payload)


def _validate_v1_envelope(payload: dict) -> dict:
    """The v1 rules: known version and mode, then that mode's exact
    field set -- unknown fields refused, required fields demanded, and
    the display vocabulary matched to the mode. A field the restore
    would ignore is a lie in the token, so it is an error here."""
    if payload.get("v") != TOKEN_VERSION:
        raise ValueError(
            f"Unsupported query-token version: {payload.get('v')!r}")
    mode = payload.get("mode")
    if mode not in _TOKEN_MODES:
        raise ValueError(f"Unsupported query-token mode: {mode!r}")
    extra = set(payload) - _ENVELOPE_KEYS_BY_MODE[mode]
    if extra:
        raise ValueError(f"Unknown {mode} token fields: {sorted(extra)}")
    missing = _REQUIRED_KEYS_BY_MODE[mode] - set(payload)
    if missing:
        raise ValueError(f"Missing {mode} token fields: {sorted(missing)}")
    display = payload.get("display")
    if display is not None:
        if not isinstance(display, dict):
            raise ValueError("Token display settings must be an object")
        bad = set(display) - _DISPLAY_KEYS_BY_MODE[mode]
        if bad:
            raise ValueError(
                f"Unknown {mode} display fields: {sorted(bad)}")
    sport_key = payload.get("sport")
    if sport_key is not None and not isinstance(sport_key, str):
        raise ValueError("Token sport must be text")
    return payload


def _migrate_legacy_payload(payload: dict) -> dict:
    """The pre-versioning token shape, lifted into a v1 filters envelope.

    Old tokens were ``{"table", "groups": [{joiner, match, conditions}]}``
    with a left-associative joiner fold; the fold becomes explicit nesting
    -- ``((g1 AND g2) OR g3)`` -- so the restored query means exactly what
    the old panel showed. Migration is strict: a malformed legacy token is
    an error, never a partial restore.
    """
    groups = payload.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("Legacy token holds no condition groups")
    expression: dict | None = None
    for stored in groups:
        if not isinstance(stored, dict):
            raise ValueError("Legacy token group is malformed")
        match = stored.get("match", "AND")
        if match not in ("AND", "OR"):
            raise ValueError(f"Legacy token match rule invalid: {match!r}")
        conditions = stored.get("conditions")
        if not isinstance(conditions, list) or not conditions:
            raise ValueError("Legacy token group holds no conditions")
        if not all(isinstance(spec, dict) for spec in conditions):
            raise ValueError("Legacy token condition is malformed")
        node = {"type": "group", "op": match, "children": list(conditions)}
        if expression is None:
            expression = node
            continue
        joiner = stored.get("joiner", "AND")
        if joiner not in ("AND", "OR"):
            raise ValueError(f"Legacy token joiner invalid: {joiner!r}")
        expression = {"type": "group", "op": joiner,
                      "children": [expression, node]}
    return {"v": TOKEN_VERSION, "mode": "filters",
            "table": payload.get("table"), "query": expression}


def _share_controls(sport, table: str, mode: str, query: dict | None,
                    display: dict) -> None:
    """Serialize the panel to a token and the URL; accept one back.

    Restoring cannot touch widget keys mid-run -- most of them belong to
    widgets already rendered above -- so the pasted token is parked in a
    plain session slot and applied at the top of the *next* run, before
    any widget exists (see the pending-restore hand-off in page()).
    """
    with st.expander("Share or restore this query"):
        if query is not None:
            try:
                token = serialize_state(build_share_envelope(
                    sport, mode, query, table=table, display=display))
            except ValueError as exc:
                st.warning(f"This query cannot be shared: {exc}")
                token = None
            if token:
                st.caption("This token reproduces the panel exactly — "
                           "groups, operators, values and display "
                           "settings. Paste it below on any session.")
                st.code(token, language=None)
                if st.button("Put this query in the URL",
                             icon=":material/link:",
                             key=sport.k("qbf_share_url", table),
                             help="Writes a ?qb= parameter, so the "
                                  "browser's address bar becomes the "
                                  "share link."):
                    _write_share_url(sport, token)
        else:
            st.caption("Set a condition and a shareable token for the "
                       "current panel appears here.")
        pasted = st.text_area("Restore from a token",
                              key=sport.k("qbf_token", table),
                              placeholder="Paste a shared token…")
        if st.button("Restore", key=sport.k("qbf_restore", table),
                     icon=":material/settings_backup_restore:",
                     disabled=not (pasted or "").strip()):
            st.session_state[sport.k("qbf_pending")] = pasted.strip()
            st.rerun()


def _write_share_url(sport, token: str) -> None:
    """Put a share token in the URL without re-consuming it ourselves."""
    st.session_state[sport.k("qb_url_seen")] = token
    st.query_params["qb"] = token


def _consume_share_url(sport) -> str | None:
    """The ?qb= token if the URL carries one this session has not seen.

    Tracked through a seen-marker so the token restores once when the URL
    changes -- a link opened, a new token pasted into the address bar --
    rather than reapplying on every rerun and fighting the user's edits.
    """
    token = st.query_params.get("qb") or ""
    seen_key = sport.k("qb_url_seen")
    if token and token != st.session_state.get(seen_key):
        st.session_state[seen_key] = token
        return token
    st.session_state[seen_key] = token or None
    return None


def _apply_restored_state(sport, envelope: dict, schema: dict, conn,
                          revision) -> None:
    """Seed session state from a validated envelope -- atomically.

    Every name passes the discovery gate against the *live* schema, every
    condition trial-compiles, every display setting is checked -- and only
    then does a single ``st.session_state.update`` commit the whole
    restore. Nothing is written while validation can still fail, so a
    stale or doctored token can never leave half a query behind, and
    nothing invalid is silently dropped: a token that cannot be reproduced
    exactly is refused with the reason.

    Groups land on freshly minted gids, so every key written here belongs
    to a widget that has never rendered (assigning a rendered widget's key
    is a Streamlit error, and reusing an old gid would collide with parked
    widget state).
    """
    mode = envelope.get("mode")
    if mode not in ("filters", "tree"):
        # validate_envelope routes grid tokens elsewhere; a caller that
        # reaches here with one is a programming error surfaced early.
        raise ValueError(f"Unsupported query-token mode: {mode!r}")
    staged: dict[str, object] = {}
    table = _require_known(str(envelope.get("table")), set(schema), "table")
    by_name = {c.name: c for c in schema[table]}

    if mode == "filters":
        prior = st.session_state.get(sport.k("qbf_state", table)) or {}
        gid = int(prior.get("next", 0) or 0)
        root, gid = _stage_filter_group(
            sport, table, envelope.get("query"), by_name, conn, revision,
            staged, gid, depth=0)
        staged[sport.k("qbf_state", table)] = {"root": root, "next": gid}
    elif mode == "tree":
        tree = envelope.get("query")
        validate_tree(tree)
        # Trial-compile against the live columns: unknown fields, bad
        # operators and malformed shapes all fail here, before commit.
        compile_tree_node(tree, by_name, ParamBag())
        staged[sport.k("qb_tree_state", table)] = tree
    else:
        raise ValueError(f"Unsupported query-token mode: {mode!r}")

    _stage_display(sport, table, envelope.get("display"), by_name, staged)
    staged[sport.k("qb_table")] = table

    # One mutation, after complete validation.
    st.session_state.update(staged)


def _stage_filter_group(sport, table: str, node, by_name: dict, conn,
                        revision, staged: dict, gid: int,
                        depth: int) -> tuple[dict, int]:
    """Validate one restored group node and stage its widget state.

    Returns ``({"gid", "children"}, next_gid)`` for _groups_state's shape.
    Raises ValueError on anything that cannot be reproduced exactly:
    unknown or duplicate columns, foreign operators, values the widgets
    cannot hold, over-deep nesting. Writing goes to ``staged`` only --
    the caller commits all-or-nothing.
    """
    if not isinstance(node, dict) or node.get("type") != "group":
        raise ValueError("Restored query must be a group tree")
    if depth >= MAX_GROUP_DEPTH:
        raise ValueError(
            f"Groups may nest at most {MAX_GROUP_DEPTH} levels deep")
    op = node.get("op")
    if op not in ("AND", "OR"):
        raise ValueError(f"Bad group operator: {op!r}")
    children = node.get("children")
    if not isinstance(children, list) or not children:
        raise ValueError("A restored group holds no conditions")
    if len(children) > MAX_GROUP_CHILDREN:
        raise ValueError(
            f"A group may hold at most {MAX_GROUP_CHILDREN} items")

    my_gid = gid
    gid += 1
    base = sport.k("qbf", table, my_gid)
    staged[f"{base}:match"] = "all (AND)" if op == "AND" else "any (OR)"

    chosen: list[str] = []
    child_nodes: list[dict] = []
    for child in children:
        if not isinstance(child, dict):
            raise ValueError("Restored query nodes must be objects")
        if child.get("type") == "group":
            child_node, gid = _stage_filter_group(
                sport, table, child, by_name, conn, revision, staged, gid,
                depth + 1)
            child_nodes.append(child_node)
            continue
        if child.get("type") not in (None, "condition"):
            raise ValueError(
                f"Unsupported query node type: {child.get('type')!r}")
        column = str(child.get("column"))
        col = by_name.get(column)
        if col is None:
            raise ValueError(f"Unknown column: {column!r}")
        if column in chosen:
            raise ValueError(
                f"Duplicate conditions on {column!r} in one group")
        profile = column_profile(conn, sport.db, revision, table,
                                 col.name, col.kind)
        _seed_spec_state(f"{base}:{column}", col, child, profile, staged)
        # The seeded widgets must reproduce a real predicate: trial-
        # compile the spec exactly as the panel will.
        if compile_condition(child, {column}, ParamBag()) is None:
            raise ValueError(f"Incomplete condition on {column!r}")
        chosen.append(column)

    staged[f"{base}:cols"] = chosen
    return {"gid": my_gid, "children": child_nodes}, gid


def _stage_display(sport, table: str, display, by_name: dict,
                   staged: dict) -> None:
    """Validate restored display settings into ``staged``; all or nothing."""
    if display is None:
        return
    known = set(by_name)
    columns = display.get("columns")
    if columns is not None:
        if (not isinstance(columns, list)
                or len(columns) != len(set(columns))):
            raise ValueError("Display columns must be a unique list")
        staged[sport.k("qb_cols", table)] = [
            _require_known(str(c), known, "column") for c in columns]
    sort = display.get("sort")
    if sort is not None:
        _require_known(str(sort), known, "sort column")
        staged[sport.k("qb_sort", table)] = str(sort)
    descending = display.get("descending")
    if descending is not None:
        if not isinstance(descending, bool):
            raise ValueError("Display 'descending' must be true or false")
        staged[sport.k("qb_desc", table)] = descending
    limit = display.get("limit")
    if limit is not None:
        staged[sport.k("qb_limit")] = _restored_limit(limit)
    group_by = display.get("group_by")
    if group_by:
        if (not isinstance(group_by, list)
                or len(group_by) != len(set(group_by))):
            raise ValueError("Display group_by must be a unique list")
        staged[sport.k("qb_groupby", table)] = [
            _require_known(str(c), known, "column") for c in group_by]
        staged[sport.k("qb_agg", table)] = True


def _seed_spec_state(base: str, col: Column, spec: dict, profile: dict,
                     staged: dict) -> None:
    """Write one condition spec into its widgets' staged session keys.

    Raises ValueError -- with the reason -- when the spec cannot drive
    this column's widgets: an operator outside the kind's vocabulary,
    values that don't parse, a kind that no longer matches the live
    column. Failing closed here is what stops a stale or doctored token
    silently *broadening* a shared query by dropping its conditions.
    """
    op, kind = spec.get("op"), spec.get("kind")

    if col.kind == "boolean":
        if kind != "boolean":
            raise ValueError(f"{col.name} is a boolean column")
        choice = {"is true": "Yes", "is false": "No",
                  "is missing": "Missing", "is present": "Present"}.get(op)
        if not choice:
            raise ValueError(f"Unsupported boolean operator: {op!r}")
        staged[f"{base}:bool"] = choice
        return

    if col.kind in ("date", "datetime"):
        if kind == "text":       # the unreadable-bounds fallback
            if op != "contains" or not spec.get("value"):
                raise ValueError(
                    f"Unsupported date condition on {col.name}")
            staged[f"{base}:txt"] = str(spec["value"])
            return
        if kind not in ("date", "datetime") or op not in _DATE_OPS:
            raise ValueError(f"Unsupported date operator: {op!r}")
        staged[f"{base}:op"] = op
        try:
            if op == "between":
                lo = dt.date.fromisoformat(str(spec["lo"])[:10])
                hi = dt.date.fromisoformat(str(spec["hi"])[:10])
                staged[f"{base}:range"] = (lo, hi)
            elif op not in ("is missing", "is present"):
                staged[f"{base}:one"] = \
                    dt.date.fromisoformat(str(spec["value"])[:10])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Invalid date value for {col.name}: {exc}") from exc
        return

    if col.kind in ("integer", "float"):
        if kind not in ("integer", "float") or op not in _NUMERIC_OPS:
            raise ValueError(f"Unsupported numeric operator: {op!r}")
        integer = col.kind == "integer"

        def number(value):
            # The widget's step type must match its seeded value type, so
            # a float column always seeds floats even for whole numbers.
            parsed = coerce_number(value, integer=integer)
            return parsed if integer else float(parsed)

        staged[f"{base}:op"] = op
        try:
            if op == "between":
                staged[f"{base}:lo"] = number(spec["lo"])
                staged[f"{base}:hi"] = number(spec["hi"])
            elif op == "one of":
                values = _bounded_values(spec.get("values") or [])
                if not values:
                    raise ValueError("empty value list")
                staged[f"{base}:in"] = ", ".join(
                    str(number(v)) for v in values)
            elif op not in ("is missing", "is present"):
                staged[f"{base}:val"] = number(spec["value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Invalid numeric value for {col.name}: {exc}") from exc
        return

    if kind != "text" or op not in _TEXT_OPS:
        raise ValueError(f"Unsupported text operator: {op!r}")
    staged[f"{base}:op"] = op
    if op == "one of":
        values = [str(v) for v in _bounded_values(spec.get("values") or [])]
        if not values:
            raise ValueError(f"Empty value list for {col.name}")
        offered = profile.get("values")
        if offered is not None:
            # The multiselect refuses values outside its options, and
            # silently intersecting would *change* the shared query --
            # so a value the current data no longer offers is an error.
            missing = [v for v in values if v not in offered]
            if missing:
                raise ValueError(
                    f"{col.name} no longer offers: {', '.join(missing)}")
            staged[f"{base}:pick"] = values
        else:
            staged[f"{base}:list"] = ", ".join(values)
    elif op not in ("is missing", "is present"):
        value = spec.get("value")
        if value in (None, ""):
            raise ValueError(f"Empty text condition on {col.name}")
        staged[f"{base}:val"] = str(value)


# ------------------------------------------------- grid-constraint mode

#: A grid criterion carries at most this many arguments; the widest real
#: builder takes four, so anything past this is a doctored payload.
MAX_CRITERION_ARGS = 12


def _resolve_criterion(sport, kind, args) -> tuple[str, list]:
    """(sql, params) for one criterion, rebuilt by the server-owned builder.

    The one gate criterion definitions pass on their way to SQL: the kind
    must name a builder in the sport's own catalogue, the arguments must
    be a short list of scalars, and the SQL is *always* the builder's
    fresh output -- session state and share tokens store only
    ``{"kind", "args"}``, never executable text, so a doctored payload
    can at worst ask a real question with strange numbers.
    """
    builders = getattr(sport.C, "BUILDERS", {}) or {}
    if not isinstance(kind, str) or kind not in builders:
        raise ValueError(f"Unknown criterion: {kind!r}")
    if not isinstance(args, (list, tuple)):
        raise ValueError("Criterion arguments must be a list")
    if len(args) > MAX_CRITERION_ARGS:
        raise ValueError("Criterion holds too many arguments")
    for value in args:
        if isinstance(value, (dict, list, tuple, set)):
            raise ValueError("Criterion arguments must be scalars")
        if isinstance(value, str) and len(value) > MAX_SCALAR_CHARS:
            raise ValueError("A criterion argument is too long")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Criterion arguments must be finite")
    fn, _argnames = builders[kind]
    try:
        sql, params = fn(*args)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"{kind}: {exc}") from exc
    return sql, list(params)


def _constraint_dnf(node, resolve, *, _depth: int = 0,
                    _count: list | None = None) -> list[list[tuple]]:
    """One grid-query AST node -> bounded disjunctive normal form.

    Returns a list of alternative *branches*; each branch is a flat list
    of resolved ``(sql, params)`` leaves that must all hold together.
    ``A AND (B OR C)`` therefore becomes ``[[A, B], [A, C]]`` -- the
    distribution is what lets every conjunction pass through one
    core._where call, however deep the AND/OR shape nested (see
    compile_constraint_ast for why that matters).

    An empty group is the panel's "Add a subgroup" placeholder, not a
    Boolean TRUE: it contributes nothing, exactly as when it was skipped
    as a ``1=1`` member. A wholly empty tree is the single no-op branch
    ``[[]]``.

    Expansion is multiplicative, so it is bounded (MAX_DNF_BRANCHES) on
    top of the depth/children/node bounds -- oversize growth is refused,
    never truncated to some subset of the query's meaning.
    """
    if _count is None:
        _count = [0]
    _count[0] += 1
    if _count[0] > MAX_TREE_NODES:
        raise ValueError(
            f"The query holds more than {MAX_TREE_NODES} criteria")
    if not isinstance(node, dict):
        raise ValueError("Query nodes must be objects")

    kind = node.get("type")
    if kind in ("criterion", "fragment"):
        return [[_resolve_leaf(node, resolve)]]
    if kind != "group":
        raise ValueError(f"Unsupported query node type: {kind!r}")

    if _depth >= MAX_GROUP_DEPTH:
        raise ValueError(
            f"Groups may nest at most {MAX_GROUP_DEPTH} levels deep")
    op = node.get("op")
    if op not in ("AND", "OR"):
        raise ValueError(f"Bad group operator: {op!r}")
    children = node.get("children") or []
    if not isinstance(children, (list, tuple)):
        raise ValueError("Group children must be a list")
    if len(children) > MAX_GROUP_CHILDREN:
        raise ValueError(
            f"A group may hold at most {MAX_GROUP_CHILDREN} items")

    if op == "OR":
        branches: list[list[tuple]] = []
        for child in children:
            alternatives = [branch for branch in _constraint_dnf(
                child, resolve, _depth=_depth + 1, _count=_count) if branch]
            if not alternatives:        # placeholder subgroup: no-op
                continue
            if len(branches) + len(alternatives) > MAX_DNF_BRANCHES:
                raise ValueError(
                    f"The query expands past {MAX_DNF_BRANCHES} OR "
                    f"branches")
            branches.extend(alternatives)
        return branches or [[]]

    branches = [[]]
    for child in children:
        alternatives = _constraint_dnf(
            child, resolve, _depth=_depth + 1, _count=_count)
        if len(branches) * len(alternatives) > MAX_DNF_BRANCHES:
            raise ValueError(
                f"The query expands past {MAX_DNF_BRANCHES} OR branches")
        branches = [current + alternative
                    for current in branches
                    for alternative in alternatives]
    return branches


def compile_constraint_ast(schema, node, resolve) -> tuple[str, list]:
    """One grid-query AST node -> ``(where, params)`` over ``players p``.

    The AST mirrors the filter panel's -- ``{"type": "group", "op",
    "children"}`` -- with criterion leaves resolved to ``(sql, params)``
    by ``resolve(kind, args)`` (the server-owned builder catalogue).
    ``(A AND B) OR (C AND D)`` and deeper shapes compile exactly as
    written, under the same depth/children/node bounds as the filter AST
    plus a DNF expansion ceiling.

    Pairing rule: the tree is first distributed into disjunctive normal
    form, and *every* resulting conjunction compiles through one
    core._where call -- so the Immaculate Grid team-and-season pairing
    holds across nested Boolean boundaries, not only between direct
    siblings. The predecessor paired direct AND children only, which made
    "team A AND (100 goals OR 200 goals)" accept a player whose team-A
    rows and 100-goal rows were different rows: DNF turns it into
    "(team A AND 100 goals) OR (team A AND 200 goals)", each half paired.

    Internal fragment leaves (``{"type": "fragment", "sql", "params"}``)
    exist for pre-built constraints inside this process; the token
    validator refuses them, so no serialized payload can smuggle SQL in.
    """
    import core

    clauses: list[str] = []
    params: list = []
    for branch in _constraint_dnf(node, resolve):
        clause, bound = core._where(branch, schema)
        if len(params) + len(bound) > MAX_QUERY_PARAMS:
            raise ValueError(
                f"A query may bind at most {MAX_QUERY_PARAMS} values.")
        clauses.append(clause)
        params.extend(bound)
    if len(clauses) == 1:
        return clauses[0], params
    # Every branch gets its own parentheses: the query must mean what the
    # panel showed by punctuation, never by SQL operator precedence.
    joined = " OR ".join(f"({clause})" for clause in clauses)
    return f"({joined})", params


def _resolve_leaf(node: dict, resolve) -> tuple[str, list]:
    """One criterion/fragment leaf -> the (sql, params) it stands for."""
    if node.get("type") == "fragment":
        sql, params = node.get("sql"), node.get("params")
        if not isinstance(sql, str):
            raise ValueError("Fragment leaves need SQL text")
        return sql, list(params or [])
    return resolve(node.get("kind"), node.get("args") or [])


def compile_constraint_sets(schema, groups):
    """AND of groups, OR within a group, over grid-builder constraints.

    The flat compatibility shape -- a list of lists of ``(sql, params)``
    exactly as BUILDERS produce them -- expressed as the two-level case
    of the general AST and compiled by compile_constraint_ast. Singleton
    groups therefore always join the AND branch's shared core._where
    call: the old fast path applied pairing only when *every* group was a
    singleton, so adding one unrelated OR card silently turned
    "team X AND 100-RBI season" into two independent facts.
    """
    def fragment(member):
        sql, params = member
        return {"type": "fragment", "sql": sql, "params": list(params)}

    children = []
    for group in groups:
        members = [fragment(member) for member in group]
        if not members:
            continue
        children.append(members[0] if len(members) == 1
                        else {"type": "group", "op": "OR",
                              "children": members})
    root = {"type": "group", "op": "AND", "children": children}
    return compile_constraint_ast(
        schema, root, resolve=lambda kind, args: (_ for _ in ()).throw(
            ValueError("criterion leaves need a sport catalogue")))


@st.cache_data(show_spinner=False, max_entries=128)
def _cached_players(sql, params, revision, _con) -> pd.DataFrame:
    frame = pd.read_sql_query(sql, _con, params=list(params))
    return frame.convert_dtypes()


def _ensure_layers(sport, con) -> None:
    """Connection-local placeholder tables for the optional layers, so a
    criterion over an unloaded layer selects nothing instead of erroring."""
    for helper in ("ensure_captain_table", "ensure_rising_star_table",
                   "ensure_brownlow_table",
                   "ensure_family_relationship_tables",
                   "ensure_all_australian_history_table"):
        ensure = getattr(sport.C, helper, None)
        if ensure:
            ensure(con)


@st.cache_data(show_spinner=False, max_entries=1024)
def _criterion_count(sport_key, sql, params, revision) -> int:
    """How many players one stored criterion matches.

    Keyed on the compiled SQL and the database revision, so the number on
    a chip survives every rerun and dies with the file it measured.
    """
    import db_pool
    import sports

    sport = sports.get(sport_key)
    con = db_pool.get_con(sport.db, revision)
    _ensure_layers(sport, con)
    return sport.C.count(con, [(sql, list(params))])


@st.cache_data(show_spinner=False, max_entries=256)
def _query_count(sport_key, where, params, revision) -> int:
    """How many players the whole query matches, past any row limit."""
    import db_pool
    import sports

    sport = sports.get(sport_key)
    con = db_pool.get_con(sport.db, revision)
    _ensure_layers(sport, con)
    return con.execute(
        f"SELECT COUNT(*) FROM {sport.schema.players} p WHERE {where}",
        list(params)).fetchone()[0]


def _builder_state(sport) -> dict:
    """The one dict the grid builder keeps in the session.

    root    -- the query itself: a group AST (op AND/OR, children of
               criterion leaves and nested groups), the same recursive
               shape the filter panel and the share tokens use. Criterion
               leaves store ``kind`` and ``args`` -- their SQL is rebuilt
               by the sport's own BUILDERS on every compile, never
               persisted.
    editing -- path (list of child indices from root) of the criterion
               open in the panel, or None.
    adding  -- path of the group a new criterion should join, or None for
               the root.
    nonce   -- folded into the panel's widget keys; bumping it hands the
               panel fresh widgets whenever its contents must change under
               it, because Streamlit ignores a new default once a key
               exists.
    next_nid -- mints stable ids for group nodes, used in their widget
               keys. Like gids, nids are never reused: a fresh group must
               never inherit a deleted group's parked ALL/ANY choice.
    """
    key = sport.k("qbc_query")
    state = st.session_state.get(key)
    if not isinstance(state, dict) or "root" not in state:
        state = {"root": {"type": "group", "op": "AND", "nid": 0,
                          "children": []},
                 "editing": None, "adding": None, "nonce": 0,
                 "next_nid": 1}
        st.session_state[key] = state
    return state


def _mint_nid(state: dict) -> int:
    nid = state["next_nid"]
    state["next_nid"] += 1
    return nid


def _md(text) -> str:
    """Escape markdown formatting characters in database-derived text."""
    return re.sub(r"([\\`*_~\[\]])", r"\\\1", str(text))


def _store_criterion(sport, kind, args, player_label=None) -> dict:
    """Everything a chip needs to display, re-edit and rebuild itself.

    No SQL: the criterion is its kind and arguments, validated against
    the sport's builder catalogue here (a broken combination fails at
    commit, not at query time), and resolved to SQL freshly on every
    compile by _resolve_criterion.
    """
    import ui_widgets

    _resolve_criterion(sport, kind, list(args))
    _fn, argnames = sport.C.BUILDERS[kind]
    defaults: dict = {}
    for name, value in zip(argnames, args):
        if name == "player_id":
            defaults["player"] = player_label or ""
        else:
            defaults[name] = value
    label = ui_widgets.builder_label(kind, list(args), sport, player_label)
    return {"type": "criterion", "kind": kind,
            "label": " ".join(str(label).split()),
            "args": list(args), "defaults": defaults}


def _criterion_catalogue(sport, available):
    """Every offered builder, ordered by its BUILDER_GROUPS category."""
    groups = getattr(sport.C, "BUILDER_GROUPS", {}) or {}
    ordered: list = []
    category: dict = {}
    for name, kinds in groups.items():
        for kind in kinds:
            if kind in available and kind not in category:
                ordered.append(kind)
                category[kind] = name
    for kind in available:
        if kind not in category:
            ordered.append(kind)
            category[kind] = "More"
    return ordered, category


def _example_queries(sport, revision, available):
    """One-tap starter queries, built only from builders this database
    offers. Each is wrapped defensively: an example must never be the
    thing that breaks the page."""
    import ui_widgets

    out = []
    offered = set(available)
    clubs = list(sport.schema.clubs)
    V = sport.vocab
    try:
        if "X+ games at venue" in offered:
            venues = ui_widgets.venue_options(sport.key, sport.db, revision)
            if venues:
                venue = venues[0][0]
                out.append((f"100+ {V.games} at {venue}",
                            [[("X+ games at venue", [venue, 100])]]))
    except Exception:
        pass
    try:
        if clubs and {"Played for club", "150+ / X+ career games"} <= offered:
            club = "Collingwood" if "Collingwood" in clubs else clubs[0]
            out.append((f"{club} + 150 {V.games}",
                        [[("Played for club", [club])],
                         [("150+ / X+ career games", [150])]]))
    except Exception:
        pass
    try:
        if len(clubs) > 1 and {"Drafted by club",
                               "Premiership player"} <= offered:
            a, b = (("Hawthorn", "Geelong")
                    if {"Hawthorn", "Geelong"} <= set(clubs)
                    else (clubs[0], clubs[1]))
            out.append((f"Drafted {a} or {b}, won a {V.title}",
                        [[("Drafted by club", [a]),
                          ("Drafted by club", [b])],
                         [("Premiership player", [])]]))
    except Exception:
        pass
    return out


def _load_example(sport, spec, state) -> list:
    """Compile one example spec ([(kind, args), ...] per group) to root
    children: singleton groups become bare criterion chips, alternatives
    become an OR group card."""
    children = []
    for group in spec:
        crits = [_store_criterion(sport, kind, args) for kind, args in group]
        if len(crits) == 1:
            children.append(crits[0])
        else:
            children.append({"type": "group", "op": "OR",
                             "nid": _mint_nid(state), "children": crits})
    return children


# ---------------------------------------------------- grid AST plumbing

def _grid_node_at(root: dict, path) -> dict | None:
    """The node a child-index path names, or None when the path is stale."""
    node = root
    for index in path:
        children = node.get("children")
        if (not isinstance(children, list) or not isinstance(index, int)
                or not 0 <= index < len(children)):
            return None
        node = children[index]
    return node


def _grid_drop(state: dict, path) -> None:
    """Remove the node at ``path`` and prune any group left empty."""
    parent = _grid_node_at(state["root"], path[:-1])
    if parent is None:
        return
    children = parent.get("children")
    if isinstance(children, list) and 0 <= path[-1] < len(children):
        children.pop(path[-1])
    _prune_grid_groups(state["root"])


def _prune_grid_groups(node: dict) -> None:
    kept = []
    for child in node.get("children") or []:
        if child.get("type") == "group":
            _prune_grid_groups(child)
            if not child["children"]:
                continue
        kept.append(child)
    node["children"] = kept


def _grid_query_for_token(node: dict) -> dict:
    """The AST stripped to what a token may carry: shape and definitions.

    Labels, defaults and nids are display furniture rebuilt on restore;
    a token holds only group shape and criterion kind+args, so there is
    nothing in it to spoof and nothing for stale display state to leak.
    """
    if node.get("type") == "criterion":
        return {"type": "criterion", "kind": node.get("kind"),
                "args": list(node.get("args") or [])}
    return {"type": "group", "op": node.get("op"),
            "children": [_grid_query_for_token(child)
                         for child in node.get("children") or []]}


def _validated_grid_query(sport, node, state_counter: list,
                          _depth: int = 0) -> dict:
    """One restored grid node, validated and rebuilt server-side.

    Criterion leaves must name a real builder and carry scalar arguments
    the builder accepts (trial-resolved through _resolve_criterion);
    labels and defaults are rebuilt here, never read from the token.
    Groups get fresh nids from the session's own counter so a restored
    group can never inherit a deleted group's parked widget state.
    Raises ValueError on anything else -- including the internal
    "fragment" leaf type, which no serialized payload may carry.
    """
    if not isinstance(node, dict):
        raise ValueError("Query nodes must be objects")
    kind = node.get("type")
    if kind == "criterion":
        return _store_criterion(sport, node.get("kind"),
                                list(node.get("args") or []))
    if kind != "group":
        raise ValueError(f"Unsupported query node type: {kind!r}")
    if _depth >= MAX_GROUP_DEPTH:
        raise ValueError(
            f"Groups may nest at most {MAX_GROUP_DEPTH} levels deep")
    op = node.get("op")
    if op not in ("AND", "OR"):
        raise ValueError(f"Bad group operator: {op!r}")
    children = node.get("children")
    if not isinstance(children, list) or not children:
        raise ValueError("A restored group holds no criteria")
    if len(children) > MAX_GROUP_CHILDREN:
        raise ValueError(
            f"A group may hold at most {MAX_GROUP_CHILDREN} items")
    return {"type": "group", "op": op, "nid": _pop_nid(state_counter),
            "children": [_validated_grid_query(sport, child, state_counter,
                                               _depth + 1)
                         for child in children]}


def _pop_nid(counter: list) -> int:
    counter[0] += 1
    return counter[0] - 1


def _grid_order_options(sport) -> dict:
    sc = sport.schema
    V = sport.vocab
    return {
        "Most obscure": f"p.{sc.obscurity} DESC, p.{sc.career_games} ASC",
        f"Most {V.games}": f"p.{sc.career_games} DESC",
        f"Fewest {V.games}": f"p.{sc.career_games} ASC",
        "Newest": f"p.{sc.final_season} DESC, p.{sc.player}",
        "Oldest": f"p.{sc.debut_season} ASC, p.{sc.player}",
        "Name": f"p.{sc.player} COLLATE NOCASE ASC",
    }


def _apply_grid_restore(sport, envelope: dict) -> None:
    """Seed the grid builder from a validated envelope -- atomically.

    The whole query AST is validated and trial-compiled (criteria
    rebuilt by the server-owned builders, parameter budget included)
    before a single session key is written, so a failing token leaves
    the existing query untouched.
    """
    if envelope.get("mode") != "grid":
        raise ValueError("Grid restore requires a grid token")
    prior = st.session_state.get(sport.k("qbc_query")) or {}
    counter = [int(prior.get("next_nid", 1) or 1)]
    root = _validated_grid_query(sport, envelope.get("query"), counter)
    if root.get("type") != "group":
        raise ValueError("Restored query must be a group tree")
    where, params = compile_constraint_ast(
        sport.schema, root,
        resolve=lambda kind, args: _resolve_criterion(sport, kind, args))
    if len(params) > MAX_QUERY_PARAMS:
        raise ValueError(
            f"A query may bind at most {MAX_QUERY_PARAMS} values.")

    staged: dict[str, object] = {
        sport.k("qbc_query"): {
            "root": root, "editing": None, "adding": None,
            "nonce": int(prior.get("nonce", 0)) + 1,
            "next_nid": counter[0]},
    }
    display = envelope.get("display") or {}
    order = display.get("order")
    if order is not None:
        if order not in _grid_order_options(sport):
            raise ValueError(f"Unknown ranking: {order!r}")
        staged[sport.k("qbc_order")] = order
    limit = display.get("limit")
    if limit is not None:
        staged[sport.k("qbc_limit")] = _restored_limit(limit)
    st.session_state.update(staged)


# ------------------------------------------------------ grid UI widgets

def _query_chips(sport, revision) -> None:
    """The query as an object: cards, chips, edit and remove, recursively."""
    state = _builder_state(sport)
    _render_grid_children(sport, revision, state, state["root"], ())


def _render_grid_children(sport, revision, state: dict, node: dict,
                          path: tuple) -> None:
    for i, child in enumerate(list(node.get("children") or [])):
        if i:
            st.markdown(f"<div class='qb-joiner'>{node['op']}</div>",
                        unsafe_allow_html=True)
        child_path = path + (i,)
        with st.container(border=True):
            if child.get("type") == "criterion":
                _grid_chip(sport, revision, state, node, child, child_path)
            else:
                _grid_group_card(sport, revision, state, child, child_path)


def _grid_chip(sport, revision, state: dict, parent: dict, crit: dict,
               path: tuple) -> None:
    row = st.container(horizontal=True, gap="small",
                       vertical_alignment="center")
    with row:
        with st.container(width="stretch", gap=None):
            st.markdown(f"**{_md(crit['label'])}**")
            try:
                sql, params = _resolve_criterion(sport, crit["kind"],
                                                 crit["args"])
                n = _criterion_count(sport.key, sql, tuple(params),
                                     revision)
                st.caption(f"{n:,} player{'' if n == 1 else 's'}")
            except Exception:
                st.caption("—")
        if st.button(":material/edit:",
                     key=sport.k("qbc_edit_btn", *path),
                     type="tertiary", help="Edit this criterion"):
            state.update(editing=list(path), adding=None)
            state["nonce"] += 1
            st.rerun()
        if st.button(":material/close:",
                     key=sport.k("qbc_drop_btn", *path),
                     type="tertiary", help="Remove this criterion"):
            _grid_drop(state, path)
            state.update(editing=None, adding=None)
            state["nonce"] += 1
            st.rerun()
    if parent.get("op") == "OR" and path[:-1]:
        return                       # the card's own "or…" button serves
    if st.button("or…", key=sport.k("qbc_or_btn", *path),
                 type="tertiary", icon=":material/add:",
                 help="Add an either/or alternative to this requirement"):
        wrapped = {"type": "group", "op": "OR", "nid": _mint_nid(state),
                   "children": [crit]}
        parent["children"][path[-1]] = wrapped
        state.update(adding=list(path), editing=None)
        state["nonce"] += 1
        st.rerun()


def _grid_group_card(sport, revision, state: dict, node: dict,
                     path: tuple) -> None:
    head = st.container(horizontal=True, vertical_alignment="center",
                        gap="small")
    op_key = sport.k("qbc_group_op", node["nid"])
    st.session_state.setdefault(
        op_key, "all (AND)" if node.get("op") == "AND" else "any (OR)")
    with head:
        with st.container(width="stretch"):
            choice = st.segmented_control(
                "Group rule", ["all (AND)", "any (OR)"], key=op_key,
                label_visibility="collapsed", persist_state="session",
                help="Whether this card requires all of its criteria "
                     "or any one of them.")
            node["op"] = _match_rule(choice)
        if st.button(":material/delete:",
                     key=sport.k("qbc_dropgrp_btn", *path),
                     type="tertiary", help="Remove this whole group"):
            _grid_drop(state, path)
            state.update(editing=None, adding=None)
            state["nonce"] += 1
            st.rerun()
    _render_grid_children(sport, revision, state, node, path)
    controls = st.container(horizontal=True, gap="small")
    with controls:
        if st.button("Add here…", key=sport.k("qbc_into_btn", *path),
                     type="tertiary", icon=":material/add:",
                     help="Configure a criterion below and it will join "
                          "this card."):
            state.update(adding=list(path), editing=None)
            state["nonce"] += 1
            st.rerun()
        if len(path) < MAX_UI_GROUP_DEPTH and st.button(
                "Add a subgroup", key=sport.k("qbc_subgrp_btn", *path),
                type="tertiary", icon=":material/account_tree:",
                help="A nested ALL/ANY group inside this card."):
            node["children"].append({"type": "group", "op": "AND",
                                     "nid": _mint_nid(state),
                                     "children": []})
            state["nonce"] += 1
            st.rerun()


def _criterion_panel(sport, revision, available) -> None:
    """Where a criterion is configured: one searchable picker over every
    question the grid can ask, then that question's own inputs, a live
    count, and a single commit button whose meaning follows the state --
    add, add-into-a-group, or save an edit."""
    import ui_widgets

    state = _builder_state(sport)
    editing = tuple(state["editing"]) if state.get("editing") else None
    adding = tuple(state["adding"]) if state.get("adding") is not None \
        else None
    key = sport.k("qbc_panel", state["nonce"])

    ordered, category = _criterion_catalogue(sport, available)
    if not ordered:
        st.info("No criteria are available for this database.")
        return

    default_kind, defaults = None, None
    if editing is not None:
        crit = _grid_node_at(state["root"], editing)
        if crit is not None and crit.get("type") == "criterion":
            default_kind, defaults = crit["kind"], crit["defaults"]
        else:
            state["editing"] = editing = None

    target_group = None
    if adding is not None:
        target_group = _grid_node_at(state["root"], adding)
        if target_group is None or target_group.get("type") != "group":
            state["adding"] = adding = target_group = None

    if editing is not None:
        title = "Edit criterion"
    elif target_group is not None:
        rule = "either/or alternative" if target_group["op"] == "OR" \
            else "requirement"
        title = f"Add an {rule} to the group" \
            if rule.startswith("e") else f"Add a {rule} to the group"
    else:
        title = ("Add a criterion" if state["root"]["children"]
                 else "Start your query")

    with st.container(border=True):
        head = st.container(horizontal=True, vertical_alignment="center")
        with head:
            with st.container(width="stretch"):
                st.markdown(f"**{title}**")
            if editing is not None or adding is not None:
                if st.button("Cancel", key=f"{key}_cancel",
                             type="tertiary", icon=":material/close:"):
                    state.update(editing=None, adding=None)
                    state["nonce"] += 1
                    st.rerun()

        picked = st.selectbox(
            "Question — type to search",
            ordered,
            index=(ordered.index(default_kind)
                   if default_kind in ordered else 0),
            key=f"{key}_kind",
            format_func=lambda k:
                f"{ui_widgets._builder_label(k, sport.vocab)} — {category[k]}",
            help="Every question a grid square can ask, searchable. Try "
                 "typing “venue”, “premiership”, “drafted” or a category "
                 "name.")

        label, built, args, player_label = ui_widgets.criterion_inputs(
            f"{key}_args", picked,
            defaults if picked == default_kind else None,
            sport, revision)

        pretty = " ".join(str(label).split())
        if built is not None:
            try:
                n = _criterion_count(sport.key, built[0], tuple(built[1]),
                                     revision)
                st.caption(f"**{_md(pretty)}** — {n:,} "
                           f"player{'' if n == 1 else 's'} qualify")
            except Exception as exc:
                st.warning(f"This criterion cannot run here: {exc}")
                built = None

        verb = ("Save changes" if editing is not None
                else "Add to group" if target_group is not None
                else "Add to query")
        icon = ":material/check:" if editing is not None \
            else ":material/add:"
        if st.button(verb, key=f"{key}_commit", type="primary", icon=icon,
                     disabled=built is None):
            try:
                crit = _store_criterion(sport, picked, args, player_label)
            except ValueError as exc:
                st.error(str(exc))
                return
            if editing is not None:
                parent = _grid_node_at(state["root"], editing[:-1])
                if parent is not None and parent.get("type") == "group":
                    parent["children"][editing[-1]] = crit
            else:
                group = target_group if target_group is not None \
                    else state["root"]
                if len(group["children"]) >= MAX_GROUP_CHILDREN:
                    st.error(f"A group may hold at most "
                             f"{MAX_GROUP_CHILDREN} items")
                    return
                group["children"].append(crit)
            state.update(editing=None, adding=None)
            state["nonce"] += 1
            st.rerun()


def _grid_sentence(node: dict) -> str:
    if node.get("type") == "criterion":
        return str(node.get("label", ""))
    parts = [_grid_sentence(child) for child in node.get("children") or []]
    if not parts:
        return ""
    joined = f" {node.get('op', 'AND')} ".join(parts)
    return f"({joined})" if len(parts) > 1 else joined


def _grid_share_controls(sport, root: dict, order, limit) -> None:
    """The grid query as a token and a URL, and a restore box."""
    with st.expander("Share or restore this query"):
        try:
            token = serialize_state(build_share_envelope(
                sport, "grid", _grid_query_for_token(root),
                display={"order": order,
                         "limit": _bounded_limit(limit)}))
        except ValueError as exc:
            st.warning(f"This query cannot be shared: {exc}")
            token = None
        if token:
            st.caption("This token reproduces the query — criteria, "
                       "groups, ranking and row limit. Paste it below on "
                       "any session.")
            st.code(token, language=None)
            if st.button("Put this query in the URL",
                         icon=":material/link:",
                         key=sport.k("qbc_share_url"),
                         help="Writes a ?qb= parameter, so the browser's "
                              "address bar becomes the share link."):
                _write_share_url(sport, token)
        pasted = st.text_area("Restore from a token",
                              key=sport.k("qbc_token"),
                              placeholder="Paste a shared token…")
        if st.button("Restore", key=sport.k("qbc_restore"),
                     icon=":material/settings_backup_restore:",
                     disabled=not (pasted or "").strip()):
            st.session_state[sport.k("qbf_pending")] = pasted.strip()
            st.rerun()


def _constraints_mode(sport, revision) -> None:
    """A query builder over the grid-criteria catalogue.

    The query is a visible object, not a pile of dropdowns: criterion
    chips and group cards the reader adds to, edits and removes, arranged
    as a recursive ALL/ANY tree -- "(drafted Hawthorn AND premiership)
    OR (drafted Geelong AND 300 games)" is the root set to ANY with two
    ALL cards. "Played in 100 or more games at the MCG" is still one
    search away.

    Everything renders in the main column. The sidebar owns nothing here,
    because on a phone the sidebar is collapsed and anything living there
    -- as the add/remove buttons once did -- simply does not exist.
    """
    import components
    import db_pool

    state = _builder_state(sport)
    available = list(st.session_state.get("AVAILABLE") or sport.C.BUILDERS)
    root = state["root"]

    if root["children"]:
        if len(root["children"]) > 1:
            root_key = sport.k("qbc_group_op", root["nid"])
            st.session_state.setdefault(
                root_key,
                "all (AND)" if root["op"] == "AND" else "any (OR)")
            choice = st.segmented_control(
                "Players must satisfy", ["all (AND)", "any (OR)"],
                key=root_key, persist_state="session",
                help="ALL: every requirement below. ANY: at least one "
                     "of them — the outer OR in shapes like (A AND B) "
                     "OR (C AND D).")
            root["op"] = _match_rule(choice)
        else:
            st.caption("Players must satisfy every card; alternatives "
                       "inside a card count as either/or.")
        _query_chips(sport, revision)
    else:
        st.caption("Build a search from the questions grid squares ask — "
                   "pick one below, set its details, add it, and chain "
                   "more with AND / OR, or group them into cards.")
        examples = _example_queries(sport, revision, available)
        if examples:
            pick = st.pills("Or start from an example",
                            [name for name, _ in examples],
                            key=sport.k("qbc_examples"))
            if pick:
                try:
                    state["root"]["children"] = _load_example(
                        sport, dict(examples)[pick], state)
                    state["nonce"] += 1
                    st.rerun()
                except Exception as exc:
                    st.warning(f"Could not load that example: {exc}")

    _criterion_panel(sport, revision, available)

    root = state["root"]
    if not root["children"]:
        return

    # -- compile ----------------------------------------------------------
    sc = sport.schema
    orders = _grid_order_options(sport)
    try:
        where, params = compile_constraint_ast(
            sc, root,
            resolve=lambda kind, args: _resolve_criterion(sport, kind,
                                                          args))
        if len(params) > MAX_QUERY_PARAMS:
            raise ValueError(
                f"A query may bind at most {MAX_QUERY_PARAMS} values.")
    except ValueError as exc:
        st.error(f"Could not compile the query: {exc}")
        return

    con = db_pool.get_con(sport.db, revision)
    _ensure_layers(sport, con)
    try:
        total = _query_count(sport.key, where, tuple(params), revision)
    except (sqlite3.Error, pd.errors.DatabaseError) as exc:
        st.error(f"Database error while counting: {exc}")
        return

    order_key, limit_key = sport.k("qbc_order"), sport.k("qbc_limit")
    st.session_state.setdefault(order_key, next(iter(orders)))
    if st.session_state.get(order_key) not in orders:
        st.session_state[order_key] = next(iter(orders))
    st.session_state.setdefault(limit_key, 100)

    bar = st.container(horizontal=True, vertical_alignment="bottom",
                       gap="small")
    with bar:
        with st.container(width="stretch", gap=None):
            st.metric("Matching players", f"{total:,}")
        order = st.selectbox("Rank by", list(orders), key=order_key,
                             persist_state="session")
        limit = st.number_input("Rows", 1, MAX_ROWS, step=25,
                                key=limit_key, persist_state="session")
        if st.button("Clear query", key=sport.k("qbc_clear"),
                     type="tertiary", icon=":material/delete_sweep:"):
            state.update(root={"type": "group", "op": "AND",
                               "nid": _mint_nid(state), "children": []},
                         editing=None, adding=None)
            state["nonce"] += 1
            st.rerun()

    st.caption(_md(_grid_sentence(root)))
    _grid_share_controls(sport, root, order, limit)

    if not total:
        st.info("No players satisfy the query — loosen a card or turn a "
                "requirement into an OR alternative.")
        return

    safe_limit = _bounded_limit(limit)
    sql = (
        f'SELECT p.{sc.player} AS Player, '
        f'p.{sc.debut_season} AS "From", p.{sc.final_season} AS "To", '
        f'p.{sc.career_games} AS Games, p.{sc.career_score} AS Score, '
        f'p.{sc.career_postseason} AS Postseason, '
        f'p.{sc.clubs_hist} AS Teams, p.{sc.obscurity} AS ObscurityRaw, '
        f'p.{sc.player_id} AS PlayerID '
        f'FROM {sc.players} p WHERE {where} '
        f'ORDER BY {orders[order]} LIMIT ?'
    )

    # The counts above are the live preview; the full result query only
    # runs on an explicit action, and any change to the query, ranking or
    # limit invalidates the previous run.
    signature = hashlib.sha256(json.dumps(
        [sql, params, safe_limit], sort_keys=True,
        default=str).encode("utf-8")).hexdigest()
    run_key = sport.k("qbc_run_signature")
    if st.button("Run query", type="primary", icon=":material/play_arrow:",
                 key=sport.k("qbc_run")):
        st.session_state[run_key] = signature
    if st.session_state.get(run_key) != signature:
        st.caption("Review the live counts, then run the query to fetch "
                   "the matching players.")
        return

    try:
        frame = _cached_players(sql, tuple([*params, safe_limit]),
                                revision, con)
    except (sqlite3.Error, pd.errors.DatabaseError) as exc:
        st.error(f"Database error while searching: {exc}")
        return

    if frame.empty:
        st.info("No players satisfy every criterion.")
    else:
        shown_count = len(frame)
        st.caption(f"Showing {shown_count:,} of {total:,}."
                   if total > shown_count
                   else f"{shown_count:,} result"
                        f"{'s' if shown_count != 1 else ''}.")
        shown = components.player_results_table(
            frame, sport, con, key=sport.k("qbc_results"))
        st.download_button(
            "Download results as CSV",
            data=shown.to_csv(index=False).encode("utf-8"),
            file_name=f"{sport.key}_grid_constraint_search.csv",
            mime="text/csv",
        )
    with st.expander("SQL and parameters"):
        st.code(sql, language="sql")
        st.code(repr([*params, safe_limit]), language="python")


#: Short names for the mode switcher; the stored values stay the long
#: strings so existing sessions keep their choice.
_MODE_SHORT = {MODE_CONSTRAINTS: "Grid query",
               MODE_TREE: "Visual tree",
               MODE_FILTERS: "Table filters"}


def _allowed_tables(sport) -> set:
    """The sport's explicit query-table allowlist.

    Discovery finds *every* table -- staging, manifests, link tables,
    sqlite_stat1 -- and read-only mode prevents modification, not
    disclosure or expensive scans, so exposure is opt-in: the sport
    declares ``query_tables``, and a sport that declares nothing offers
    only its core analytical tables.
    """
    declared = tuple(getattr(sport, "query_tables", ()) or ())
    if declared:
        return set(declared)
    schema = sport.schema
    return {name for name in (getattr(schema, "players", ""),
                              getattr(schema, "games", ""),
                              getattr(schema, "matches", ""))
            if name}


def _active_filter_columns(sport, table: str, state: dict) -> set:
    """Column names any group in the panel currently conditions on."""
    names: set[str] = set()

    def walk(node):
        base = sport.k("qbf", table, node.get("gid"))
        for name in st.session_state.get(f"{base}:cols") or []:
            names.add(str(name))
        for child in node.get("children") or []:
            walk(child)

    walk(state["root"])
    return names


def _tree_profile_columns(sport, table: str, cols) -> list:
    """Which of a table's columns the visual tree may profile.

    Only text columns the sport has declared low-cardinality
    (query_low_cardinality_columns) -- each profile is a DISTINCT scan,
    and speculatively scanning *every* text column cost ~4 s on the NFL
    games table's 19 before the component could render. An undeclared
    text column stays a free-text field; numeric and date fields never
    needed profiles here (their widgets carry no observed bounds by
    design). The declaration is matched against live discovery, so a
    stale entry selects nothing rather than probing a ghost column.
    """
    configured = set(
        getattr(sport, "query_low_cardinality_columns", ()) or ())
    return [c for c in cols
            if c.kind == "text" and f"{table}.{c.name}" in configured]


def page(sport, heading=True):
    """Render the query builder for whichever sport is active.

    Every control lives in the main column: the page has to work on a
    phone, where the sidebar is collapsed and anything kept there is
    functionally invisible.
    """
    from sqlalchemy.exc import SQLAlchemyError

    if heading:
        st.markdown("# Advanced Search")

    try:
        revision = _db_revision(sport.db)
    except OSError as exc:
        st.error(f"Could not read the {sport.label} database: {exc}")
        return

    # A pasted share token is parked by the share controls -- and a ?qb=
    # URL parameter is consumed once when it changes -- and applied HERE,
    # before any widget renders: seeding a widget's session key is only
    # legal while its widget does not yet exist this run. Grid restores
    # need no table discovery and apply immediately; table restores are
    # validated against the live schema after discovery, still ahead of
    # the widgets they seed. Restores are atomic: a token that fails
    # validation changes nothing.
    restored = None
    pending = st.session_state.pop(sport.k("qbf_pending"), None)
    if pending is None:
        pending = _consume_share_url(sport)
    if pending is not None:
        try:
            envelope = validate_envelope(deserialize_state(pending))
        except ValueError as exc:
            st.warning(f"That token could not be read — {exc}")
        else:
            token_sport = envelope.get("sport")
            if token_sport is not None and token_sport != sport.key:
                st.warning(f"That query belongs to the "
                           f"{str(token_sport).upper()} database — switch "
                           f"sport to open it.")
            elif envelope["mode"] == "tree" and not HAS_CONDITION_TREE:
                st.warning("That query needs the visual-tree component, "
                           "which is not installed here.")
            elif envelope["mode"] == "grid":
                try:
                    _apply_grid_restore(sport, envelope)
                    st.session_state[sport.k("qb_mode")] = MODE_CONSTRAINTS
                except ValueError as exc:
                    st.warning(f"Could not restore that query: {exc}")
            else:
                restored = envelope
                st.session_state[sport.k("qb_mode")] = \
                    _TOKEN_MODES[envelope["mode"]]

    modes = [MODE_CONSTRAINTS] \
        + ([MODE_TREE] if HAS_CONDITION_TREE else []) + [MODE_FILTERS]
    mode_key = sport.k("qb_mode")
    if st.session_state.get(mode_key) not in modes:
        st.session_state[mode_key] = modes[0]
    mode = st.segmented_control(
        "Builder mode", modes, key=mode_key,
        format_func=lambda m: _MODE_SHORT.get(m, m),
        label_visibility="collapsed",
        persist_state="session") or modes[0]

    if mode == MODE_CONSTRAINTS:
        # The grid catalogue needs no table discovery: its criteria are
        # the sport's own builders, compiled against the players table.
        _constraints_mode(sport, revision)
        return

    st.caption(
        "Query the live database's public tables. Tables, columns and "
        "value ranges are discovered from the data itself; values are "
        "parameterised and the connection is read-only."
        + ("" if HAS_CONDITION_TREE else
           " Install `streamlit-condition-tree` for the drag-and-drop "
           "visual tree."))

    # -- connect and discover, failing as a red box rather than a traceback
    try:
        conn = get_connection(sport)
        overrides = tuple(sorted(
            (getattr(sport, "query_column_kinds", {}) or {}).items()))
        discovered = discover_schema(conn, sport.db, revision, overrides)
    except (OSError, sqlite3.Error, SQLAlchemyError) as exc:
        st.error(f"Could not read the {sport.label} database: {exc}")
        return
    allowed = _allowed_tables(sport)
    schema = {name: columns for name, columns in discovered.items()
              if name in allowed}
    if not schema:
        st.error("The database has no queryable tables.")
        return

    if restored is not None:
        try:
            _apply_restored_state(sport, restored, schema, conn, revision)
        except (ValueError, sqlite3.Error, SQLAlchemyError,
                pd.errors.DatabaseError) as exc:
            st.warning(f"Could not restore that query: {exc}")

    tables = list(schema)
    default_table = getattr(sport.schema, "players", None)
    table_key = sport.k("qb_table")
    if st.session_state.get(table_key) not in tables:
        st.session_state[table_key] = (default_table
                                       if default_table in tables
                                       else tables[0])

    top = st.container(horizontal=True, vertical_alignment="bottom",
                       gap="small")
    with top:
        with st.container(width="stretch"):
            table = st.selectbox("Table", tables, key=table_key,
                                 persist_state="session")
        display = st.popover("Display", icon=":material/tune:")

    table = _require_known(str(table), set(schema), "table")
    cols = schema[table]
    names = [c.name for c in cols]
    by_name = {c.name: c for c in cols}

    with display:
        cols_key = sport.k("qb_cols", table)
        stored_cols = st.session_state.get(cols_key)
        if stored_cols is None:
            st.session_state[cols_key] = list(names)
        else:
            st.session_state[cols_key] = [c for c in stored_cols
                                          if c in by_name]
        shown = st.multiselect("Columns to show", names, key=cols_key,
                               persist_state="session")
        sort_options = ["(database order)"] + names
        sort_key = sport.k("qb_sort", table)
        if st.session_state.get(sort_key) not in sort_options:
            st.session_state[sort_key] = sort_options[0]
        order_by = st.selectbox("Sort by", sort_options, key=sort_key,
                                persist_state="session")
        order_by = None if order_by == "(database order)" else order_by
        desc_key = sport.k("qb_desc", table)
        st.session_state.setdefault(desc_key, True)
        descending = st.toggle("Descending", key=desc_key,
                               persist_state="session",
                               disabled=order_by is None)
        limit_key = sport.k("qb_limit")
        st.session_state.setdefault(limit_key, 500)
        limit = st.number_input("Row limit", 1, MAX_ROWS, step=100,
                                key=limit_key, persist_state="session")
        agg_key = sport.k("qb_agg", table)
        st.session_state.setdefault(agg_key, False)
        aggregate = st.toggle(
            "Count per group", key=agg_key, persist_state="session",
            help="Group the matching rows and count them — e.g. games per "
                 "venue, players per debut season.")
        group_columns = []
        if aggregate:
            group_key = sport.k("qb_groupby", table)
            stored_groups = st.session_state.get(group_key)
            if stored_groups is not None:
                st.session_state[group_key] = [c for c in stored_groups
                                               if c in by_name]
            group_columns = st.multiselect("Group by", names,
                                           key=group_key,
                                           persist_state="session")

    # Profiles are measured lazily: only the columns something actually
    # renders a control for. The old page profiled every column of every
    # selected table -- one query each, ~30 seconds on the NFL's
    # 155-column games table -- before either builder appeared.
    if mode == MODE_TREE:
        needed = _tree_profile_columns(sport, table, cols)
    else:
        active = _active_filter_columns(sport, table,
                                        _groups_state(sport, table))
        needed = [c for c in cols if c.name in active]
    try:
        profiles = {c.name: column_profile(conn, sport.db, revision, table,
                                           c.name, c.kind)
                    for c in needed}
    except (sqlite3.Error, SQLAlchemyError, pd.errors.DatabaseError) as exc:
        st.error(f"Could not profile {table}: {exc}")
        return

    # One bag per script run, written by exactly one mode: the branch
    # below renders (and compiles) only the active mode's widgets, so the
    # inactive mode's parked session state can never reach this bag or
    # the SQL built from it.
    bag = ParamBag()
    where_clause = None
    token_mode = "filters"
    token_query = None

    if mode == MODE_TREE:
        token_mode = "tree"
        st.markdown(f"Drag conditions together below to filter "
                    f"**{table}**. Groups nest, and each group can match "
                    f"all (AND) or any (OR) of its rules.")
        widget_key = sport.k("qb_tree", table)
        store_key = sport.k("qb_tree_state", table)
        # The component deletes its widget state when unmounted, so the
        # last validated tree is mirrored under store_key and handed back
        # through tree= on remount. The component's return value (its own
        # compiled SQL) is discarded on purpose: it is a browser-supplied
        # string. The structured tree is what gets compiled, server-side.
        saved_tree = st.session_state.get(store_key)
        condition_tree(
            condition_tree_config(cols, profiles),
            tree=saved_tree if isinstance(saved_tree, dict) else None,
            return_type="sql",
            placeholder="Add a rule to start building your query",
            min_height=300,
            key=widget_key,
        )
        tree = st.session_state.get(widget_key)
        if isinstance(tree, str):        # some component versions store JSON
            if not tree.strip():
                tree = None
            else:
                try:
                    tree = json.loads(tree)
                except ValueError as exc:
                    st.error(f"The visual tree returned invalid state: "
                             f"{exc}")
                    return
        if tree is not None:
            # A component return value is a websocket message a hostile
            # client can set to anything: bound its shape before the
            # recursive compile walks it, and only store what validated.
            try:
                validate_tree(tree)
                where_clause = compile_tree_node(tree, by_name, bag)
            except ValueError as exc:
                st.error(str(exc))
                return
            st.session_state[store_key] = tree
        token_query = st.session_state.get(store_key)
    else:
        st.markdown(
            f"Build condition groups over **{table}**. A group matches "
            f"all or any of its conditions and subgroups, so *(played "
            f"Collingwood AND 150+ games) OR (drafted Hawthorn AND "
            f"premiership player)* is an ANY group of two ALL cards.")
        try:
            where_clause, token_query = _filter_groups(
                sport, table, cols, profiles, conn, revision, bag)
        except ValueError as exc:
            st.error(str(exc))
            return

    # -- assemble ---------------------------------------------------------
    try:
        safe_limit = _bounded_limit(limit)
        if group_columns:
            sql = build_group_select(
                table, group_columns, where_clause, safe_limit, bag,
                known_tables=set(schema), known_columns=set(names))
        else:
            sql = build_select(
                table, shown, where_clause, order_by, descending,
                safe_limit, bag,
                known_tables=set(schema), known_columns=set(names))
    except ValueError as exc:
        st.error(str(exc))
        return

    display_state = {"columns": list(shown), "sort": order_by,
                     "descending": bool(descending), "limit": safe_limit,
                     "group_by": list(group_columns)}
    _share_controls(sport, table, token_mode, token_query, display_state)

    st.markdown("#### Generated SQL")
    st.code(sql, language="sql")
    if bag.values:
        with st.expander("Bound parameters"):
            st.code(repr(bag.values), language="python")

    # -- execute, but only on an explicit run -----------------------------
    # Count badges above are the live preview; fetching the rows is an
    # action. The signature ties a run to the exact SQL and bindings, so
    # any change to conditions, columns, sort or limit invalidates it.
    signature = hashlib.sha256(json.dumps(
        [sql, bag.values], sort_keys=True,
        default=str).encode("utf-8")).hexdigest()
    run_key = sport.k("qb_run_signature", table)
    if st.button("Run query", type="primary",
                 icon=":material/play_arrow:",
                 key=sport.k("qb_run", table)):
        st.session_state[run_key] = signature
    if st.session_state.get(run_key) != signature:
        st.caption("Review the live counts, then run the query to fetch "
                   "rows.")
        return

    try:
        frame = run_query(conn, sql, bag.values, revision)
    except (sqlite3.Error, SQLAlchemyError, pd.errors.DatabaseError) as exc:
        st.error(f"Database error while searching: {exc}")
        return

    if frame.empty:
        st.info("No rows match every filter.")
        return
    capped = len(frame) >= safe_limit
    st.caption(f"{len(frame):,} row{'s' if len(frame) != 1 else ''} shown"
               + (f" (limit reached — raise the row limit to see more)"
                  if capped else "."))
    st.dataframe(frame, width="stretch", hide_index=True)
    st.download_button(
        "Download results as CSV",
        data=frame.to_csv(index=False).encode("utf-8"),
        file_name=f"{sport.key}_{table}_query.csv",
        mime="text/csv",
    )
