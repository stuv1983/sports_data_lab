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

STATE MODEL (mode isolation)
----------------------------
The three modes never share compilation state. Each mode's widgets live
under a disjoint session-key namespace (``qbc_*`` grid, ``qb_tree`` tree,
``qbf_*`` filter groups), only the *active* mode's branch in ``page()``
renders widgets or compiles predicates, and the ParamBag handed to the
compiler is constructed fresh on every script run -- so a value typed in
one mode can never bleed into the SQL or parameter bag of another, and a
stale bag can never survive a rerun. Switching modes leaves the other
mode's widget state parked but inert: parked state is data in the session,
not clauses in a query, until its mode is active again.

The generated WHERE is SARGable throughout: every comparison keeps the
bare (quoted) column on the left -- no ``DATE(col)``, ``LOWER(col)`` or
other wrapper that would blind SQLite to an index. Day-granular date
filters on datetime-bearing columns compile to half-open ISO string
ranges (``col >= :day AND col < :next_day``) instead of ``DATE(col) =``,
because ISO-8601 text compares correctly as plain strings.

SECURITY MODEL
--------------
Three independent walls, so no single mistake is fatal:

1. Identifiers (table, column, sort) can only enter SQL if discovery
   returned them from the database's own catalogue, and they are always
   double-quoted. Nothing the reader types is ever an identifier.
2. Values never enter SQL text, in either mode: every comparison is a
   named placeholder bound at execution. The visual builder's own compiled
   WHERE string is *never executed* — a component's return value is just a
   websocket message a hostile client can set to anything — so this page
   compiles the component's structured condition tree itself, through the
   same identifier gate and parameter bag the filter panel uses.
3. The connection is read-only at the file level (`mode=ro` in the SQLite
   URL), so even a hostile WHERE clause could only read.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import sqlite3
import zlib
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import streamlit as st

import labels

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
def discover_schema(_conn, db_path: str, revision) -> dict:
    """Every table and column the live database actually has.

    Runtime inspection, not configuration: SQLAlchemy's inspector reads the
    catalogue (sqlite_master / PRAGMA table_info under the hood), so the
    result is the schema as built, including tables added by optional
    layers. Cached on (path, revision) -- `_conn`'s leading underscore
    keeps the unhashable connection out of the cache key, and `revision`
    changes when the file is rebuilt, so a refresh is picked up without a
    process restart.
    """
    from sqlalchemy import inspect

    inspector = inspect(_conn.engine)
    schema: dict[str, tuple] = {}
    for table in sorted(inspector.get_table_names()):
        schema[table] = tuple(
            Column(col["name"], str(col["type"]), type_kind(str(col["type"])))
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
    affair that the tests can call without a database."""

    def __init__(self):
        self.values: dict = {}

    def add(self, value) -> str:
        name = f"p{len(self.values)}"
        self.values[name] = value
        return f":{name}"


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
    """Comma-separated numbers for a numeric IN(...), bad tokens dropped."""
    out: list[float | int] = []
    for token in str(text).split(","):
        token = token.strip()
        if not token:
            continue
        try:
            value = float(token)
        except ValueError:
            continue
        out.append(int(value) if value.is_integer() else value)
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
_DATE_OPS = ("between", "on", "on or after", "on or before",
             "is missing", "is present")
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
        cast = int if kind == "integer" else float
        if op == "between":
            lo, hi = spec.get("lo"), spec.get("hi")
            if lo is None or hi is None:
                return None
            lo, hi = sorted((cast(lo), cast(hi)))
            return between_clause(column, lo, hi, bag)
        if op == "one of":
            values = [cast(v) for v in spec.get("values") or []]
            return in_clause(column, values, bag) if values else None
        value = spec.get("value")
        if value is None:
            return None
        return comparison_clause(column, _NUMERIC_SQL[op], cast(value), bag)

    if kind in ("date", "datetime"):
        return _compile_date_condition(column, spec, op, bag)

    # -- text ------------------------------------------------------------
    if op == "one of":
        values = [str(v) for v in spec.get("values") or []]
        return in_clause(column, values, bag) if values else None
    value = spec.get("value")
    if value in (None, ""):
        return None
    text = str(value)
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


def combine_group_clauses(pairs) -> str | None:
    """Fold (joiner, clause) group pairs into one WHERE expression.

    Left-associative with explicit parentheses at every fold --
    ``((g1 OR g2) AND g3)`` -- so a mixed AND/OR chain means exactly what
    the page showed, never what SQL precedence would silently prefer.
    The first contributing group's joiner is ignored; empty groups drop
    out without consuming a joiner.
    """
    expression = None
    for joiner, clause in pairs:
        if not clause:
            continue
        if expression is None:
            expression = clause
            continue
        if joiner not in ("AND", "OR"):
            raise ValueError(f"Bad group joiner: {joiner!r}")
        expression = f"({expression} {joiner} {clause})"
    return expression


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


def serialize_state(payload: dict) -> str:
    """The builder's state as a compact URL-safe token.

    Deterministic JSON (sorted keys, no whitespace), deflated and
    base64url-encoded -- ready to ride in a query parameter. The token
    holds only column names, operator labels and typed values; it is
    *data about a query*, and everything in it still passes the discovery
    gate and the parameter bag when it is compiled after a restore, so a
    doctored token can rename nothing and inject nothing.
    """
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True,
                     default=str).encode("utf-8")
    return base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii")


def deserialize_state(token: str) -> dict:
    """Decode a share token back to its payload dict, or raise ValueError.

    Decompression is bounded: the compressed input is size-checked, the
    inflater is given a hard output ceiling (MAX_STATE_BYTES), and any
    unconsumed input past that ceiling makes the token invalid -- a
    decompression bomb dies here as a ValueError, never as memory
    exhaustion. The decoded payload's shape is then bounded by
    validate_tree before anything walks it.
    """
    text = str(token).strip()
    if len(text) > MAX_TOKEN_CHARS:
        raise ValueError("Not a query token: too large")
    try:
        packed = base64.urlsafe_b64decode(text.encode("ascii"))
        inflater = zlib.decompressobj()
        raw = inflater.decompress(packed, MAX_STATE_BYTES)
        if inflater.unconsumed_tail or not inflater.eof:
            raise ValueError(
                f"decompressed size exceeds {MAX_STATE_BYTES} bytes")
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Not a query token: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Not a query-state payload")
    validate_tree(payload)
    return payload


def build_select(table: str, columns, predicates, combinator: str,
                 order_by: str | None, descending: bool,
                 limit: int, bag: ParamBag,
                 known_tables, known_columns) -> str:
    """Assemble the final statement from vetted parts.

    Every identifier is checked against what discovery returned and then
    quoted; predicates arrive already parameterised; the limit is itself a
    bound parameter. The one free string, the AND/OR combinator, is chosen
    from a two-value radio and asserted here anyway.
    """
    if combinator not in ("AND", "OR"):
        raise ValueError(f"Bad combinator: {combinator!r}")
    _require_known(table, known_tables, "table")
    select_list = ", ".join(
        _quote_ident(_require_known(c, known_columns, "column"))
        for c in columns) or "*"
    sql = f"SELECT {select_list}\nFROM {_quote_ident(table)}"
    if predicates:
        joiner = f"\n  {combinator} "
        sql += "\nWHERE " + joiner.join(predicates)
    if order_by:
        _require_known(order_by, known_columns, "sort column")
        sql += (f"\nORDER BY {_quote_ident(order_by)} "
                f"{'DESC' if descending else 'ASC'}")
    sql += f"\nLIMIT {bag.add(min(int(limit), MAX_ROWS))}"
    return sql


def build_group_select(table: str, group_columns, predicates,
                       combinator: str, limit: int, bag: ParamBag,
                       known_tables, known_columns) -> str:
    """A COUNT(*) AS total per group, filtered the same way build_select is.

    The aggregation the row-limit question keeps turning into: "how many
    games at each venue", "players per debut season". Identifiers pass the
    same discovery gate; groups sort by their count, biggest first, so the
    answer leads with the headline.
    """
    if combinator not in ("AND", "OR"):
        raise ValueError(f"Bad combinator: {combinator!r}")
    _require_known(table, known_tables, "table")
    if not group_columns:
        raise ValueError("Group by at least one column.")
    grouped = ", ".join(
        _quote_ident(_require_known(c, known_columns, "column"))
        for c in group_columns)
    sql = (f"SELECT {grouped}, COUNT(*) AS total"
           f"\nFROM {_quote_ident(table)}")
    if predicates:
        joiner = f"\n  {combinator} "
        sql += "\nWHERE " + joiner.join(predicates)
    sql += (f"\nGROUP BY {grouped}"
            f"\nORDER BY total DESC, {grouped}"
            f"\nLIMIT {bag.add(min(int(limit), MAX_ROWS))}")
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
    """
    kind = node.get("type") if isinstance(node, dict) else None
    if kind in ("group", "rule_group"):
        parts = [sql for child in _tree_children(node)
                 if (sql := compile_tree_node(child, known_columns, bag))]
        if not parts:
            return None
        properties = node.get("properties") or {}
        conjunction = str(properties.get("conjunction") or "AND").upper()
        if conjunction not in ("AND", "OR"):
            raise ValueError(f"Bad conjunction: {conjunction!r}")
        joined = "(" + f" {conjunction} ".join(parts) + ")"
        return f"NOT {joined}" if properties.get("not") else joined
    if kind != "rule":
        raise ValueError(f"Unsupported tree node type: {kind!r}")

    properties = node.get("properties") or {}
    field, operator = properties.get("field"), properties.get("operator")
    if not field or not operator:
        return None                       # rule still being built
    _require_known(field, known_columns, "column")
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
            return None
        value = _tree_scalar(operator, values)
        return f"{column} {_TREE_BINARY_OPS[operator]} {bag.add(value)}"
    if operator in ("between", "not_between"):
        if len(values) < 2 or values[0] is None or values[1] is None:
            return None
        clause = (f"{column} BETWEEN {bag.add(_tree_scalar(operator, values, 0))} "
                  f"AND {bag.add(_tree_scalar(operator, values, 1))}")
        return f"NOT ({clause})" if operator == "not_between" else clause
    if operator in ("select_any_in", "select_not_any_in", "multiselect_equals"):
        chosen = values[0] if values and isinstance(values[0], list) else values
        if len(chosen) > MAX_RULE_VALUES:
            raise ValueError(
                f"Rule holds more than {MAX_RULE_VALUES} values")
        chosen = [v for v in chosen
                  if not isinstance(v, (dict, list, tuple)) and v is not None]
        if not chosen:
            return None
        marks = ", ".join(bag.add(v) for v in chosen)
        clause = f"{column} IN ({marks})"
        return f"NOT ({clause})" if operator == "select_not_any_in" else clause
    if operator in ("like", "not_like", "starts_with", "ends_with"):
        if not values or values[0] is None:
            return None
        text = str(_tree_scalar(operator, values))
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
    if operator in ("is_empty", "is_not_empty"):
        clause = f"COALESCE({column}, '') = ''"
        return f"NOT ({clause})" if operator == "is_not_empty" else clause
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
        choice = container.segmented_control(
            label, ["Any", "Yes", "No", "Missing", "Present"],
            default="Any", key=f"{key}:bool")
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
    op_col, value_col = container.columns((1, 2))
    operator = op_col.selectbox(label, _NUMERIC_OPS, key=f"{key}:op")
    spec = {"column": col.name, "kind": col.kind, "op": operator}
    if operator in ("is missing", "is present"):
        return spec
    if operator == "between":
        a, b = value_col.columns(2)
        low = a.number_input("from", key=f"{key}:lo", value=None,
                             label_visibility="collapsed",
                             placeholder=None if lo is None else f"{lo:g}")
        high = b.number_input("to", key=f"{key}:hi", value=None,
                              label_visibility="collapsed",
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
            placeholder="e.g. 1, 5, 10")
        chosen = parse_number_list(typed)
        if not chosen:
            return None
        spec["values"] = chosen
        return spec
    value = value_col.number_input(
        "value", key=f"{key}:val", value=None,
        label_visibility="collapsed",
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
    operator = op_col.selectbox(label, _TEXT_OPS, key=f"{key}:op",
                                index=5 if values is not None else 0)
    spec = {"column": col.name, "kind": "text", "op": operator}
    if operator in ("is missing", "is present"):
        return spec
    if operator == "one of":
        if values is not None:
            chosen = value_col.multiselect(
                "values", values, key=f"{key}:pick",
                label_visibility="collapsed")
        else:
            typed = value_col.text_input(
                "values", key=f"{key}:list", label_visibility="collapsed",
                placeholder="comma, separated, values")
            chosen = [part.strip() for part in typed.split(",")
                      if part.strip()]
        if not chosen:
            return None
        spec["values"] = chosen
        return spec
    typed = value_col.text_input("value", key=f"{key}:val",
                                 label_visibility="collapsed",
                                 placeholder="text")
    if not typed:
        return None
    spec["value"] = typed
    return spec


def _date_spec(container, key, label, col, profile):
    bounds = _date_bounds(profile)
    op_col, value_col = container.columns((1, 2))
    operator = op_col.selectbox(label, _DATE_OPS, key=f"{key}:op")
    if operator in ("is missing", "is present"):
        return {"column": col.name, "kind": col.kind, "op": operator}
    if bounds is None:
        # Bounds unreadable as dates (SQLite will store anything): a text
        # match is honest where a picker would lie.
        typed = value_col.text_input("value", key=f"{key}:txt",
                                     label_visibility="collapsed")
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
        picked = value_col.date_input(label, value=bounds,
                                      key=f"{key}:range",
                                      min_value=bounds[0],
                                      max_value=bounds[1],
                                      label_visibility="collapsed")
        if not isinstance(picked, tuple) or len(picked) != 2:
            return None          # picker mid-edit: one end chosen so far
        if picked == bounds:
            return None          # full range = not actually filtering
        spec.update(lo=picked[0].isoformat(), hi=picked[1].isoformat())
        return spec
    picked = value_col.date_input(
        label,
        value=bounds[0] if operator in ("on", "on or after") else bounds[1],
        key=f"{key}:one", label_visibility="collapsed")
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

    groups -- the panel's shape only: a list of ``{"gid": n}``. Everything
              a group *says* (its joiner, match rule, chosen columns,
              operator settings) lives in widget state under keys derived
              from the gid, so widgets stay the single source of truth.
    next   -- the next gid to mint. Gids are never reused: a widget key
              built from a gid must never resurrect the values of a group
              that was deleted, and a restore must land on keys no widget
              has rendered yet.
    """
    return st.session_state.setdefault(
        sport.k("qbf_state", table), {"groups": [{"gid": 0}], "next": 1})


def _match_rule(label: str | None) -> str:
    return "AND" if not label or label.startswith("all") else "OR"


def _filter_groups(sport, table: str, cols, profiles: dict, conn, revision,
                   bag: ParamBag) -> tuple[str | None, list[dict]]:
    """The nested condition groups UI, compiled to one WHERE expression.

    Each bordered card is a group: any number of column conditions,
    matched with the group's own ALL (AND) / ANY (OR) rule; each card
    after the first chooses how it joins the combined groups above it.
    Columns are picked from a category-grouped list (the BUILDER_GROUPS
    shelves), every active condition and every multi-condition group shows
    a live count of the rows it matches alone, and the panel's whole state
    round-trips through a shareable token.

    Returns ``(where, payload)``: the compiled expression (None while
    nothing filters) with its parameters in ``bag``, and the JSON-able
    ``[{joiner, match, conditions: [spec, ...]}, ...]`` the token stores.
    """
    state = _groups_state(sport, table)
    by_name = {c.name: c for c in cols}
    names = list(by_name)
    known = set(names)
    ordered = categorised_order(names)

    def shelf_label(name):
        return f"{labels.words(name)} — {column_category(name)}"

    pairs: list[tuple[str, str | None]] = []
    payload: list[dict] = []
    for gi, group in enumerate(list(state["groups"])):
        gid = group["gid"]
        base = sport.k("qbf", table, gid)

        joiner = "AND"
        if gi:
            joiner = st.segmented_control(
                "Joined with the groups above using", ["AND", "OR"],
                default="OR", key=f"{base}:joiner",
                help="How this group combines with everything above it: "
                     "AND narrows, OR widens.") or "OR"

        with st.container(border=True):
            head = st.container(horizontal=True,
                                vertical_alignment="center", gap="small")
            with head:
                with st.container(width="stretch"):
                    st.markdown(f"**Group {gi + 1}**")
                match_label = st.radio(
                    "Match", ["all (AND)", "any (OR)"], horizontal=True,
                    key=f"{base}:match", label_visibility="collapsed",
                    help="Whether a row must satisfy all of this group's "
                         "conditions, or any one of them.")
                if len(state["groups"]) > 1 and st.button(
                        ":material/delete:", key=f"{base}:drop",
                        type="tertiary", help="Remove this group"):
                    state["groups"] = [g for g in state["groups"]
                                       if g["gid"] != gid]
                    st.rerun()
            chosen = st.multiselect(
                "Conditions — columns are grouped by category", ordered,
                key=f"{base}:cols", format_func=shelf_label,
                placeholder="Type to search columns by name or category…")

            match = _match_rule(match_label)
            clauses: list[str] = []
            specs: list[dict] = []
            for name in chosen:
                col = by_name[name]
                box = st.container(border=True)
                spec = _spec_widget(box, f"{base}:{name}", col,
                                    profiles.get(name, {}))
                if spec is None:
                    box.caption("No filter yet — choose an operator and "
                                "value.")
                    continue
                specs.append(spec)
                alone = _condition_count(conn, table, spec, known, revision)
                if alone is not None:
                    box.caption(f":material/filter_alt: matches "
                                f"{alone:,} row{'' if alone == 1 else 's'} "
                                f"on its own")
                try:
                    clause = compile_condition(spec, known, bag)
                except ValueError as exc:
                    box.error(str(exc))
                    clause = None
                if clause:
                    clauses.append(clause)

            group_clause = combine_condition_clauses(clauses, match)
            if group_clause and len(clauses) > 1:
                # The group's own live count, from a self-contained bag so
                # the badge query is cache-stable.
                group_bag = ParamBag()
                shown = combine_condition_clauses(
                    [compile_condition(s, known, group_bag)
                     for s in specs], match)
                n = _clause_count(conn, table, shown, group_bag.values,
                                  revision)
                if n is not None:
                    st.caption(f"Group matches {n:,} "
                               f"row{'' if n == 1 else 's'} on its own")
            pairs.append((joiner, group_clause))
            payload.append({"joiner": joiner, "match": match,
                            "conditions": specs})

    if st.button("Add a condition group", icon=":material/add:",
                 key=sport.k("qbf_add", table),
                 help="A new card of conditions, joined to the ones above "
                      "with AND or OR — for queries like (played "
                      "Collingwood AND 150+ games) OR (drafted Hawthorn "
                      "AND premiership player)."):
        state["groups"].append({"gid": state["next"]})
        state["next"] += 1
        st.rerun()

    where = combine_group_clauses(pairs)
    _share_controls(sport, table, payload)
    return where, payload


def _share_controls(sport, table: str, payload: list[dict]) -> None:
    """Serialize the panel to a token; accept one back for restoring.

    Restoring cannot touch widget keys mid-run -- most of them belong to
    widgets already rendered above -- so the pasted token is parked in a
    plain session slot and applied at the top of the *next* run, before
    any widget exists (see the pending-restore hand-off in page()).
    """
    with st.expander("Share or restore this filter set"):
        if any(g["conditions"] for g in payload):
            token = serialize_state({"table": table, "groups": payload})
            st.caption("This token reproduces the panel exactly — groups, "
                       "operators and values. Paste it below on any "
                       "session (URL sharing hooks onto the same string).")
            st.code(token, language=None)
        else:
            st.caption("Set a condition and a token for the current "
                       "panel appears here.")
        pasted = st.text_area("Restore from a token",
                              key=sport.k("qbf_token", table),
                              placeholder="Paste a shared token…")
        if st.button("Restore", key=sport.k("qbf_restore", table),
                     icon=":material/settings_backup_restore:",
                     disabled=not (pasted or "").strip()):
            st.session_state[sport.k("qbf_pending")] = pasted.strip()
            st.rerun()


def _apply_restored_state(sport, payload: dict, schema: dict, conn,
                          revision) -> None:
    """Seed session state from a share token, before any widget renders.

    Every name in the token passes the discovery gate against the *live*
    schema -- a doctored token can only ever select real tables and
    columns, and its values still ride as parameters when the seeded
    widgets compile. Groups land on freshly minted gids, so every key
    written here belongs to a widget that has never rendered (assigning a
    rendered widget's key is a Streamlit error, and reusing an old gid
    would collide with parked widget state).
    """
    table = _require_known(str(payload.get("table")), set(schema), "table")
    by_name = {c.name: c for c in schema[table]}
    prior = st.session_state.get(sport.k("qbf_state", table)) or {}
    gid = int(prior.get("next", 0))

    groups: list[dict] = []
    for stored in payload.get("groups") or []:
        if not isinstance(stored, dict):
            continue
        base = sport.k("qbf", table, gid)
        joiner = stored.get("joiner")
        if joiner in ("AND", "OR"):
            st.session_state[f"{base}:joiner"] = joiner
        st.session_state[f"{base}:match"] = (
            "any (OR)" if stored.get("match") == "OR" else "all (AND)")
        chosen: list[str] = []
        for spec in stored.get("conditions") or []:
            if not isinstance(spec, dict):
                continue
            col = by_name.get(spec.get("column"))
            if col is None:
                continue
            profile = column_profile(conn, sport.db, revision, table,
                                     col.name, col.kind)
            if _seed_spec_state(f"{base}:{col.name}", col, spec, profile):
                chosen.append(col.name)
        st.session_state[f"{base}:cols"] = chosen
        groups.append({"gid": gid})
        gid += 1

    if not groups:
        raise ValueError("The token holds no conditions.")
    st.session_state[sport.k("qbf_state", table)] = {"groups": groups,
                                                     "next": gid}
    st.session_state[sport.k("qb_table")] = table


def _seed_spec_state(base: str, col: Column, spec: dict,
                     profile: dict) -> bool:
    """Write one condition spec into its widgets' session keys.

    Returns False (and writes nothing load-bearing) when the spec cannot
    drive this column's widgets -- an operator outside the kind's
    vocabulary, values that don't parse -- so a stale or doctored token
    degrades to a dropped condition, never a crashed page.
    """
    op, kind = spec.get("op"), spec.get("kind")

    if col.kind == "boolean":
        choice = {"is true": "Yes", "is false": "No",
                  "is missing": "Missing", "is present": "Present"}.get(op)
        if not choice:
            return False
        st.session_state[f"{base}:bool"] = choice
        return True

    if col.kind in ("date", "datetime"):
        if kind == "text":       # the unreadable-bounds fallback
            if op != "contains" or not spec.get("value"):
                return False
            st.session_state[f"{base}:txt"] = str(spec["value"])
            return True
        if op not in _DATE_OPS:
            return False
        st.session_state[f"{base}:op"] = op
        try:
            if op == "between":
                lo = dt.date.fromisoformat(str(spec["lo"])[:10])
                hi = dt.date.fromisoformat(str(spec["hi"])[:10])
                st.session_state[f"{base}:range"] = (lo, hi)
            elif op not in ("is missing", "is present"):
                st.session_state[f"{base}:one"] = \
                    dt.date.fromisoformat(str(spec["value"])[:10])
        except (KeyError, TypeError, ValueError):
            return False
        return True

    if col.kind in ("integer", "float"):
        if op not in _NUMERIC_OPS:
            return False
        cast = int if col.kind == "integer" else float
        st.session_state[f"{base}:op"] = op
        try:
            if op == "between":
                st.session_state[f"{base}:lo"] = cast(spec["lo"])
                st.session_state[f"{base}:hi"] = cast(spec["hi"])
            elif op == "one of":
                st.session_state[f"{base}:in"] = ", ".join(
                    str(v) for v in spec.get("values") or [])
            elif op not in ("is missing", "is present"):
                st.session_state[f"{base}:val"] = cast(spec["value"])
        except (KeyError, TypeError, ValueError):
            return False
        return True

    if op not in _TEXT_OPS:      # text
        return False
    st.session_state[f"{base}:op"] = op
    if op == "one of":
        values = [str(v) for v in spec.get("values") or []]
        offered = profile.get("values")
        if offered is not None:
            # The multiselect refuses values outside its options; keep
            # the intersection so a stale token still restores cleanly.
            st.session_state[f"{base}:pick"] = \
                [v for v in values if v in offered]
        else:
            st.session_state[f"{base}:list"] = ", ".join(values)
    elif op not in ("is missing", "is present"):
        st.session_state[f"{base}:val"] = str(spec.get("value") or "")
    return True


# ------------------------------------------------- grid-constraint mode

def compile_constraint_sets(schema, groups):
    """AND of groups, OR within a group, over grid-builder constraints.

    ``groups`` is a list of lists of ``(sql, params)`` constraints exactly
    as BUILDERS produce them. When every group holds one constraint the
    intersection compiles through core._where, keeping the Immaculate
    Grid team-and-season pairing rule; a group with alternatives compiles
    each member standalone and ORs the clauses, because "either of these"
    has no one row to pair on.
    """
    import core

    if groups and all(len(group) == 1 for group in groups):
        return core._where([group[0] for group in groups], schema)
    clauses: list[str] = []
    params: list = []
    for group in groups:
        members = []
        for sql, values in group:
            parts = core._row_parts(sql)
            if parts is not None:
                clause, bound = core._row_exists([(parts[1], values)], schema)
            else:
                clause, bound = f"p.{schema.player_id} IN ({sql})", list(values)
            members.append(clause)
            params.extend(bound)
        clauses.append("(" + " OR ".join(members) + ")"
                       if len(members) > 1 else members[0])
    return (" AND ".join(clauses) if clauses else "1=1"), params


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
    """The one dict the builder keeps in the session.

    groups  -- the query itself: a list of AND requirements, each a list
               of OR alternatives, each alternative a stored criterion.
    editing -- (group, index) of the criterion open in the panel, or None.
    or_into -- group index a new criterion should join as an OR, or None.
    nonce   -- folded into the panel's widget keys; bumping it hands the
               panel fresh widgets whenever its contents must change under
               it (loading an edit, finishing an add), because Streamlit
               ignores a new default once a key exists.
    """
    return st.session_state.setdefault(
        sport.k("qbc_query"),
        {"groups": [], "editing": None, "or_into": None, "nonce": 0})


def _md(text) -> str:
    """Escape markdown formatting characters in database-derived text."""
    return re.sub(r"([\\`*_~\[\]])", r"\\\1", str(text))


def _store_criterion(sport, kind, args, player_label=None) -> dict:
    """Everything a chip needs to display, re-edit and rebuild itself."""
    import ui_widgets

    fn, argnames = sport.C.BUILDERS[kind]
    sql, params = fn(*args)
    defaults: dict = {}
    for name, value in zip(argnames, args):
        if name == "player_id":
            defaults["player"] = player_label or ""
        else:
            defaults[name] = value
    label = ui_widgets.builder_label(kind, list(args), sport, player_label)
    return {"kind": kind, "label": " ".join(str(label).split()),
            "args": list(args), "defaults": defaults,
            "sql": sql, "params": list(params)}


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


def _load_example(sport, spec) -> list:
    """Compile one example spec ([(kind, args), ...] per group) to stored
    criteria."""
    return [[_store_criterion(sport, kind, args) for kind, args in group]
            for group in spec]


def _query_chips(sport, revision) -> None:
    """The query as an object: requirement cards, chips, edit and remove."""
    state = _builder_state(sport)
    groups = state["groups"]

    for g, group in enumerate(groups):
        if g:
            st.markdown("<div class='qb-joiner'>AND</div>",
                        unsafe_allow_html=True)
        with st.container(border=True):
            for i, crit in enumerate(group):
                row = st.container(horizontal=True, gap="small",
                                   vertical_alignment="center")
                with row:
                    with st.container(width="stretch", gap=None):
                        joiner = "*or*&ensp;" if i else ""
                        st.markdown(f"{joiner}**{_md(crit['label'])}**")
                        try:
                            n = _criterion_count(
                                sport.key, crit["sql"],
                                tuple(crit["params"]), revision)
                            st.caption(f"{n:,} player{'' if n == 1 else 's'}")
                        except Exception:
                            st.caption("—")
                    if st.button(":material/edit:",
                                 key=sport.k("qbc_edit_btn", g, i),
                                 type="tertiary",
                                 help="Edit this criterion"):
                        state.update(editing=(g, i), or_into=None)
                        state["nonce"] += 1
                        st.rerun()
                    if st.button(":material/close:",
                                 key=sport.k("qbc_drop_btn", g, i),
                                 type="tertiary",
                                 help="Remove this criterion"):
                        group.pop(i)
                        if not group:
                            groups.pop(g)
                        state.update(editing=None, or_into=None)
                        state["nonce"] += 1
                        st.rerun()
            if st.button("or…", key=sport.k("qbc_or_btn", g),
                         type="tertiary", icon=":material/add:",
                         help="Add an either/or alternative to this "
                              "requirement"):
                state.update(or_into=g, editing=None)
                state["nonce"] += 1
                st.rerun()


def _criterion_panel(sport, revision, available) -> None:
    """Where a criterion is configured: one searchable picker over every
    question the grid can ask, then that question's own inputs, a live
    count, and a single commit button whose meaning follows the state --
    add, add-as-OR, or save an edit."""
    import ui_widgets

    state = _builder_state(sport)
    editing, or_into = state["editing"], state["or_into"]
    key = sport.k("qbc_panel", state["nonce"])

    ordered, category = _criterion_catalogue(sport, available)
    if not ordered:
        st.info("No criteria are available for this database.")
        return

    default_kind, defaults = None, None
    if editing:
        g, i = editing
        if g < len(state["groups"]) and i < len(state["groups"][g]):
            crit = state["groups"][g][i]
            default_kind, defaults = crit["kind"], crit["defaults"]
        else:
            state["editing"] = editing = None

    if editing:
        title = "Edit criterion"
    elif or_into is not None:
        title = f"Add an alternative (OR) to requirement {or_into + 1}"
    else:
        title = ("Add a criterion" if state["groups"]
                 else "Start your query")

    with st.container(border=True):
        head = st.container(horizontal=True, vertical_alignment="center")
        with head:
            with st.container(width="stretch"):
                st.markdown(f"**{title}**")
            if editing or or_into is not None:
                if st.button("Cancel", key=f"{key}_cancel",
                             type="tertiary", icon=":material/close:"):
                    state.update(editing=None, or_into=None)
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

        verb = ("Save changes" if editing
                else "Add alternative" if or_into is not None
                else "Add to query")
        icon = ":material/check:" if editing else ":material/add:"
        if st.button(verb, key=f"{key}_commit", type="primary", icon=icon,
                     disabled=built is None):
            crit = _store_criterion(sport, picked, args, player_label)
            if editing:
                g, i = editing
                state["groups"][g][i] = crit
            elif or_into is not None and or_into < len(state["groups"]):
                state["groups"][or_into].append(crit)
            else:
                state["groups"].append([crit])
            state.update(editing=None, or_into=None)
            state["nonce"] += 1
            st.rerun()


def _constraints_mode(sport, revision) -> None:
    """A query builder over the grid-criteria catalogue.

    The query is a visible object, not a pile of dropdowns: requirement
    cards the reader adds to, edits and removes, combined with AND top to
    bottom, each card holding either/or alternatives. "Played in 100 or
    more games at the MCG" is one search away -- type "venue" into the
    question picker, choose the ground, set the number.

    Everything renders in the main column. The sidebar owns nothing here,
    because on a phone the sidebar is collapsed and anything living there
    -- as the add/remove buttons once did -- simply does not exist.
    """
    import components
    import db_pool

    state = _builder_state(sport)
    available = list(st.session_state.get("AVAILABLE") or sport.C.BUILDERS)
    groups = state["groups"]

    if groups:
        described = [" OR ".join(c["label"] for c in group)
                     for group in groups]
        st.caption("Players must satisfy every card; alternatives inside "
                   "a card count as either/or.")
        _query_chips(sport, revision)
    else:
        st.caption("Build a search from the questions grid squares ask — "
                   "pick one below, set its details, add it, and chain "
                   "more with AND / OR.")
        examples = _example_queries(sport, revision, available)
        if examples:
            pick = st.pills("Or start from an example",
                            [name for name, _ in examples],
                            key=sport.k("qbc_examples"))
            if pick:
                try:
                    state["groups"] = _load_example(sport,
                                                    dict(examples)[pick])
                    state["nonce"] += 1
                    st.rerun()
                except Exception as exc:
                    st.warning(f"Could not load that example: {exc}")

    _criterion_panel(sport, revision, available)

    groups = state["groups"]
    if not groups:
        return

    # -- results ----------------------------------------------------------
    sc = sport.schema
    V = sport.vocab
    orders = {
        "Most obscure": f"p.{sc.obscurity} DESC, p.{sc.career_games} ASC",
        f"Most {V.games}": f"p.{sc.career_games} DESC",
        f"Fewest {V.games}": f"p.{sc.career_games} ASC",
        "Newest": f"p.{sc.final_season} DESC, p.{sc.player}",
        "Oldest": f"p.{sc.debut_season} ASC, p.{sc.player}",
        "Name": f"p.{sc.player} COLLATE NOCASE ASC",
    }

    constraint_sets = [[(c["sql"], c["params"]) for c in group]
                       for group in groups]
    try:
        where, params = compile_constraint_sets(sc, constraint_sets)
    except Exception as exc:
        st.error(f"Could not compile the query: {exc}")
        return

    con = db_pool.get_con(sport.db, revision)
    _ensure_layers(sport, con)
    try:
        total = _query_count(sport.key, where, tuple(params), revision)
    except (sqlite3.Error, pd.errors.DatabaseError) as exc:
        st.error(f"Database error while counting: {exc}")
        return

    bar = st.container(horizontal=True, vertical_alignment="bottom",
                       gap="small")
    with bar:
        with st.container(width="stretch", gap=None):
            st.metric("Matching players", f"{total:,}")
        order = st.selectbox("Rank by", list(orders),
                             key=sport.k("qbc_order"))
        limit = st.number_input("Rows", 1, MAX_ROWS, 100, step=25,
                                key=sport.k("qbc_limit"))
        if st.button("Clear query", key=sport.k("qbc_clear"),
                     type="tertiary", icon=":material/delete_sweep:"):
            state.update(groups=[], editing=None, or_into=None)
            state["nonce"] += 1
            st.rerun()

    sentence = " AND ".join(f"({part})" if " OR " in part else part
                            for part in described)
    st.caption(_md(sentence))

    if not total:
        st.info("No players satisfy every criterion — loosen a card or "
                "turn one requirement into an OR alternative.")
        return

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
    try:
        frame = _cached_players(sql, tuple([*params, int(limit)]),
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
        st.code(repr([*params, int(limit)]), language="python")


#: Short names for the mode switcher; the stored values stay the long
#: strings so existing sessions keep their choice.
_MODE_SHORT = {MODE_CONSTRAINTS: "Grid query",
               MODE_TREE: "Visual tree",
               MODE_FILTERS: "Table filters"}


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

    # A pasted share token is parked by _share_controls and applied HERE,
    # before any widget renders: seeding a widget's session key is only
    # legal while its widget does not yet exist this run. Parsing needs no
    # database; validation against the live schema happens after discovery
    # below, still ahead of the widgets it seeds.
    restored = None
    pending = st.session_state.pop(sport.k("qbf_pending"), None)
    if pending is not None:
        try:
            restored = deserialize_state(pending)
        except ValueError:
            st.warning("That token could not be read — it may be "
                       "truncated or from an incompatible version.")
        else:
            st.session_state[sport.k("qb_mode")] = MODE_FILTERS

    modes = [MODE_CONSTRAINTS] \
        + ([MODE_TREE] if HAS_CONDITION_TREE else []) + [MODE_FILTERS]
    mode = st.segmented_control(
        "Builder mode", modes, default=modes[0], key=sport.k("qb_mode"),
        format_func=lambda m: _MODE_SHORT.get(m, m),
        label_visibility="collapsed") or modes[0]

    if mode == MODE_CONSTRAINTS:
        # The grid catalogue needs no table discovery: its criteria are
        # the sport's own builders, compiled against the players table.
        _constraints_mode(sport, revision)
        return

    st.caption(
        "Query any table of the live database. Tables, columns and value "
        "ranges are discovered from the data itself; values are "
        "parameterised and the connection is read-only."
        + ("" if HAS_CONDITION_TREE else
           " Install `streamlit-condition-tree` for the drag-and-drop "
           "visual tree."))

    # -- connect and discover, failing as a red box rather than a traceback
    try:
        conn = get_connection(sport)
        schema = discover_schema(conn, sport.db, revision)
    except (OSError, sqlite3.Error, SQLAlchemyError) as exc:
        st.error(f"Could not read the {sport.label} database: {exc}")
        return
    if not schema:
        st.error("The database contains no tables.")
        return

    if restored is not None:
        try:
            _apply_restored_state(sport, restored, schema, conn, revision)
        except (ValueError, sqlite3.Error, SQLAlchemyError,
                pd.errors.DatabaseError) as exc:
            st.warning(f"Could not restore that query: {exc}")

    tables = list(schema)
    default_table = getattr(sport.schema, "players", None)

    top = st.container(horizontal=True, vertical_alignment="bottom",
                       gap="small")
    with top:
        with st.container(width="stretch"):
            table = st.selectbox(
                "Table", tables,
                index=(tables.index(default_table)
                       if default_table in tables else 0),
                key=sport.k("qb_table"))
        display = st.popover("Display", icon=":material/tune:")

    cols = schema[table]
    names = [c.name for c in cols]

    with display:
        shown = st.multiselect("Columns to show", names, default=names,
                               key=sport.k("qb_cols", table))
        order_by = st.selectbox("Sort by", ["(database order)"] + names,
                                key=sport.k("qb_sort", table))
        order_by = None if order_by == "(database order)" else order_by
        descending = st.toggle("Descending", value=True,
                               key=sport.k("qb_desc", table),
                               disabled=order_by is None)
        limit = st.number_input("Row limit", 1, MAX_ROWS, 500, step=100,
                                key=sport.k("qb_limit"))
        aggregate = st.toggle(
            "Count per group", key=sport.k("qb_agg", table),
            help="Group the matching rows and count them — e.g. games per "
                 "venue, players per debut season.")
        group_columns = (st.multiselect("Group by", names,
                                        key=sport.k("qb_groupby", table))
                         if aggregate else [])

    # Profiles feed both modes: widget bounds in B, field config in A.
    try:
        profiles = {c.name: column_profile(conn, sport.db, revision, table,
                                           c.name, c.kind)
                    for c in cols}
    except (sqlite3.Error, SQLAlchemyError, pd.errors.DatabaseError) as exc:
        st.error(f"Could not profile {table}: {exc}")
        return

    # One bag per script run, written by exactly one mode: the branch
    # below renders (and compiles) only the active mode's widgets, so the
    # inactive mode's parked session state can never reach this bag or
    # the SQL built from it.
    bag = ParamBag()
    predicates: list[str] = []
    combinator = "AND"
    tree = None

    if mode == MODE_TREE:
        st.markdown(f"Drag conditions together below to filter "
                    f"**{table}**. Groups nest, and each group can match "
                    f"all (AND) or any (OR) of its rules.")
        tree_key = sport.k("qb_tree", table)
        # The component's return value (its own compiled SQL) is discarded
        # on purpose: it is a browser-supplied string. The structured tree
        # it stores under `tree_key` is what gets compiled, server-side.
        condition_tree(
            condition_tree_config(cols, profiles),
            return_type="sql",
            placeholder="Add a rule to start building your query",
            min_height=300,
            key=tree_key,
        )
        tree = st.session_state.get(tree_key)
        if isinstance(tree, str):        # some component versions store JSON
            try:
                tree = json.loads(tree) if tree.strip() else None
            except ValueError:
                tree = None
    else:
        st.markdown(
            f"Build condition groups over **{table}**. A group matches "
            f"all or any of its conditions; groups join with AND or OR, "
            f"so *(played Collingwood AND 150+ games) OR (drafted "
            f"Hawthorn AND premiership player)* is two cards.")
        try:
            where_clause, _payload = _filter_groups(
                sport, table, cols, profiles, conn, revision, bag)
        except ValueError as exc:
            st.error(str(exc))
            return
        if where_clause:
            # The nested expression carries its own parenthesised AND/OR
            # structure; it enters build_select as a single predicate, so
            # the legacy flat combinator no longer shapes anything.
            predicates = [where_clause]

    # -- compile ----------------------------------------------------------
    try:
        if mode == MODE_TREE and tree:
            # The tree is a component return value -- a websocket message a
            # hostile client can set to anything -- so bound its shape
            # before the recursive compile walks it.
            validate_tree(tree)
            clause = compile_tree_node(tree, set(names), bag)
            if clause:
                predicates = [clause]
        if group_columns:
            sql = build_group_select(
                table, group_columns, predicates, combinator, limit, bag,
                known_tables=set(tables), known_columns=set(names))
        else:
            sql = build_select(
                table, shown, predicates, combinator, order_by,
                descending, limit, bag,
                known_tables=set(tables), known_columns=set(names))
    except ValueError as exc:
        st.error(str(exc))
        return

    st.markdown("#### Generated SQL")
    st.code(sql, language="sql")
    if bag.values:
        with st.expander("Bound parameters"):
            st.code(repr(bag.values), language="python")

    # -- execute ----------------------------------------------------------
    try:
        frame = run_query(conn, sql, bag.values, revision)
    except (sqlite3.Error, SQLAlchemyError, pd.errors.DatabaseError) as exc:
        st.error(f"Database error while searching: {exc}")
        return

    if frame.empty:
        st.info("No rows match every filter.")
        return
    capped = len(frame) >= int(limit)
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
