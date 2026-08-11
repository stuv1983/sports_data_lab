"""query_builder.py -- The graphical SQL query builder behind Advanced Search.

Replaces the text-syntax search with a visual builder that works for every
registered sport, because nothing in here knows a sport's columns: the whole
schema -- tables, columns, types -- is discovered from the live database at
render time, and the UI is generated from what discovery found. Load a new
sport, or add a column to an existing build, and the builder simply offers it.

Two builder modes, chosen in the sidebar:

* **Visual builder** -- `streamlit-condition-tree`, a drag-and-drop tree of
  nested AND/OR groups that the component itself compiles to a SQL WHERE
  clause.
* **Filter panel** -- native Streamlit widgets, one per column the reader
  chooses to filter on, compiled here into a fully parameterised WHERE.

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

import datetime as dt
import json
import os
import re
import sqlite3
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
    """
    url = ("sqlite:///file:" + Path(sport.db).resolve().as_posix()
           + "?mode=ro&uri=true")
    return st.connection(f"sql_{sport.key}", type="sql", url=url)


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
               "equals": escaped}[mode]
    return f"{_quote_ident(column)} LIKE {bag.add(pattern)} ESCAPE '\\'"


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
            lo, hi = profile.get("lo"), profile.get("hi")
            if lo is not None and hi is not None:
                cfg["fieldSettings"] = {"min": float(lo), "max": float(hi)}
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
    """A group's children, whichever container shape the component used."""
    children = node.get("children1") or []
    if isinstance(children, dict):
        return list(children.values())
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
    values = list(properties.get("value") or [])

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
    """One column's filter widget, mapped from its discovered kind, and the
    predicate the reader's setting compiles to -- or None while the widget
    sits in its "no filter" state.

    The kind->widget mapping is the heart of the dynamic UI: integers get a
    range slider over the column's real bounds, floats a min/max pair (a
    slider's fixed step suits counts, not averages), booleans a three-way
    Any/Yes/No (a plain checkbox cannot say "don't filter"), dates a range
    picker, and text either a multiselect of the actual values or a
    wildcard-safe LIKE, depending on measured cardinality.
    """
    key = sport.k("qb", table, col.name)
    label = labels.words(col.name)

    if col.kind == "integer":
        lo, hi = profile.get("lo"), profile.get("hi")
        if lo is None or hi is None or lo >= hi:
            value = container.number_input(label, key=key, value=None)
            return None if value is None else equals_clause(col.name, int(value), bag)
        picked = container.slider(label, int(lo), int(hi),
                                  (int(lo), int(hi)), key=key)
        if picked == (int(lo), int(hi)):
            return None          # full range = not actually filtering
        return between_clause(col.name, picked[0], picked[1], bag)

    if col.kind == "float":
        lo, hi = profile.get("lo"), profile.get("hi")
        a, b = container.columns(2)
        low = a.number_input(f"{label} min", key=f"{key}:lo", value=None,
                             placeholder=None if lo is None else f"{lo:g}")
        high = b.number_input(f"{label} max", key=f"{key}:hi", value=None,
                              placeholder=None if hi is None else f"{hi:g}")
        if low is not None and high is not None:
            return between_clause(col.name, float(low), float(high), bag)
        if low is not None:
            return f"{_quote_ident(col.name)} >= {bag.add(float(low))}"
        if high is not None:
            return f"{_quote_ident(col.name)} <= {bag.add(float(high))}"
        return None

    if col.kind == "boolean":
        choice = container.segmented_control(
            label, ["Any", "Yes", "No"], default="Any", key=key)
        if choice == "Yes":
            return equals_clause(col.name, 1, bag)
        if choice == "No":
            return equals_clause(col.name, 0, bag)
        return None

    if col.kind in ("date", "datetime"):
        bounds = _date_bounds(profile)
        if bounds is None:
            # Bounds unreadable as dates (SQLite will store anything):
            # fall back to a text match rather than a picker that lies.
            typed = container.text_input(label, key=key)
            return like_clause(col.name, typed, "contains", bag) if typed else None
        picked = container.date_input(label, value=bounds, key=key,
                                      min_value=bounds[0], max_value=bounds[1])
        if not isinstance(picked, tuple) or len(picked) != 2:
            return None          # picker mid-edit: one end chosen so far
        if picked == bounds:
            return None
        # DATE() normalises whatever ISO-ish form the column stores before
        # comparing, at the cost of the index -- correctness over speed.
        return (f"DATE({_quote_ident(col.name)}) BETWEEN "
                f"{bag.add(picked[0].isoformat())} AND "
                f"{bag.add(picked[1].isoformat())}")

    # text
    values = profile.get("values")
    if values is not None:
        chosen = container.multiselect(label, values, key=key)
        return in_clause(col.name, chosen, bag) if chosen else None
    a, b = container.columns((2, 1))
    typed = a.text_input(label, key=key)
    mode = b.selectbox("Match", ["contains", "starts with", "equals"],
                       key=f"{key}:mode")
    return like_clause(col.name, typed, mode, bag) if typed else None


def _date_bounds(profile: dict):
    """The column's (min, max) as dates, or None when they don't parse."""
    try:
        lo = dt.date.fromisoformat(str(profile.get("lo"))[:10])
        hi = dt.date.fromisoformat(str(profile.get("hi"))[:10])
    except (TypeError, ValueError):
        return None
    return (lo, hi) if lo <= hi else None


def page(sport):
    """Render the query builder for whichever sport is active."""
    from sqlalchemy.exc import SQLAlchemyError

    st.markdown("# Advanced Search")
    st.caption(
        "Build a query visually against the live database. Tables, columns "
        "and value ranges below are discovered from the data itself; values "
        "are parameterised and the connection is read-only."
    )

    # -- connect and discover, failing as a red box rather than a traceback
    try:
        conn = get_connection(sport)
        revision = _db_revision(sport.db)
        schema = discover_schema(conn, sport.db, revision)
    except (OSError, sqlite3.Error, SQLAlchemyError) as exc:
        st.error(f"Could not read the {sport.label} database: {exc}")
        return
    if not schema:
        st.error("The database contains no tables.")
        return

    # -- sidebar: mode toggle and query scaffolding -----------------------
    side = st.sidebar
    side.markdown("### Query builder")
    modes = [MODE_TREE, MODE_FILTERS] if HAS_CONDITION_TREE else [MODE_FILTERS]
    mode = side.segmented_control("Mode", modes, default=modes[0],
                                  key=sport.k("qb_mode")) or modes[0]
    if not HAS_CONDITION_TREE:
        side.caption("Install `streamlit-condition-tree` to enable the "
                     "drag-and-drop visual builder.")

    tables = list(schema)
    default_table = getattr(sport.schema, "players", None)
    table = side.selectbox(
        "Table", tables,
        index=tables.index(default_table) if default_table in tables else 0,
        key=sport.k("qb_table"))
    cols = schema[table]
    names = [c.name for c in cols]

    shown = side.multiselect("Columns to show", names, default=names,
                             key=sport.k("qb_cols", table))
    order_by = side.selectbox("Sort by", ["(database order)"] + names,
                              key=sport.k("qb_sort", table))
    order_by = None if order_by == "(database order)" else order_by
    descending = side.toggle("Descending", value=True,
                             key=sport.k("qb_desc", table),
                             disabled=order_by is None)
    limit = side.number_input("Row limit", 1, MAX_ROWS, 500, step=100,
                              key=sport.k("qb_limit"))

    # Profiles feed both modes: widget bounds in B, field config in A.
    try:
        profiles = {c.name: column_profile(conn, sport.db, revision, table,
                                           c.name, c.kind)
                    for c in cols}
    except (sqlite3.Error, SQLAlchemyError, pd.errors.DatabaseError) as exc:
        st.error(f"Could not profile {table}: {exc}")
        return

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
        side.markdown("### Filters")
        combinator = side.radio("Combine filters with", ["AND", "OR"],
                                horizontal=True,
                                key=sport.k("qb_combine", table))
        chosen = side.multiselect("Filter on columns", names,
                                  key=sport.k("qb_filter_cols", table))
        for col in (c for c in cols if c.name in chosen):
            box = side.container(border=True)
            predicate = _filter_widget(box, sport, table, col,
                                       profiles[col.name], bag)
            if predicate:
                predicates.append(predicate)

    # -- compile ----------------------------------------------------------
    try:
        if mode == MODE_TREE and tree:
            clause = compile_tree_node(tree, set(names), bag)
            if clause:
                predicates = [clause]
        sql = build_select(table, shown, predicates, combinator, order_by,
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
