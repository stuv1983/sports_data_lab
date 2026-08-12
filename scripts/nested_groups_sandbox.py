#!/usr/bin/env python3
"""Sandbox for the filter panel's nested condition groups.

Two ways to run it, both with synthetic mock data and no sport database:

    python scripts/nested_groups_sandbox.py
        Headless proof: drives the real compilation layer -- condition
        specs, SARGable operator clauses, group combination, share-token
        round-trips -- against an in-memory SQLite table and asserts on
        SQL shape, bound parameters, row results and (via EXPLAIN QUERY
        PLAN) actual index usage.

    streamlit run scripts/nested_groups_sandbox.py
        The nested-groups UI in miniature: real `_spec_widget`s inside
        condition groups with per-condition live counts, AND/OR match
        rules inside groups, AND/OR joiners between them, and the
        compiled WHERE with its parameter bag printed underneath.

Everything exercised here is imported from query_builder -- the sandbox
proves the production logic, not a copy of it.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import query_builder as QB

# --------------------------------------------------------------- mock data

COLUMNS = (
    QB.Column("player", "TEXT", "text"),
    QB.Column("club", "TEXT", "text"),
    QB.Column("drafted_by", "TEXT", "text"),
    QB.Column("career_games", "INTEGER", "integer"),
    QB.Column("career_goals", "INTEGER", "integer"),
    QB.Column("height_cm", "REAL", "float"),
    QB.Column("premierships", "INTEGER", "integer"),
    QB.Column("debut_date", "DATE", "date"),
    QB.Column("scraped_at", "DATETIME", "datetime"),
)

ROWS = [
    ("Scott Pendlebury", "Collingwood", "Collingwood", 400, 195, 191.0, 1,
     "2006-04-01", "2026-08-08T14:30:00"),
    ("Nathan Buckley", "Collingwood", "Brisbane", 280, 263, 185.0, 0,
     "1993-03-27", "2026-08-08T09:00:00"),
    ("Luke Hodge", "Hawthorn", "Hawthorn", 346, 191, 185.0, 4,
     "2002-06-15", "2026-08-09T02:15:00"),
    ("Cyril Rioli", "Hawthorn", "Hawthorn", 189, 275, 180.0, 4,
     "2008-03-22", "2026-08-09T23:59:59"),
    ("Journey Mann", "Fitzroy", "Fitzroy", 40, 5, 178.0, 0,
     "1990-05-05", None),
]

KNOWN = {c.name for c in COLUMNS}


def mock_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE players (player TEXT, club TEXT, drafted_by TEXT, "
        "career_games INTEGER, career_goals INTEGER, height_cm REAL, "
        "premierships INTEGER, debut_date TEXT, scraped_at TEXT)")
    con.executemany("INSERT INTO players VALUES (?,?,?,?,?,?,?,?,?)", ROWS)
    con.execute("CREATE INDEX ix_games ON players(career_games)")
    con.execute("CREATE INDEX ix_debut ON players(debut_date)")
    con.execute("CREATE INDEX ix_scraped ON players(scraped_at)")
    return con


# ---------------------------------------------------------- headless proof

_CHECKS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    _CHECKS.append(status)
    print(f"  [{status:>4}] {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def _names(con, clause: str, params: dict) -> list[str]:
    rows = con.execute(
        f"SELECT player FROM players WHERE {clause} ORDER BY player",
        params).fetchall()
    return [r[0] for r in rows]


_FORBIDDEN_LEFT = re.compile(r"(?i)\b(DATE|DATETIME|LOWER|UPPER|CAST|"
                             r"SUBSTR|COALESCE)\s*\(")


def assert_sargable(clause: str) -> None:
    """No function may wrap a column: that is what blinds SQLite's planner."""
    if _FORBIDDEN_LEFT.search(clause):
        raise AssertionError(f"Non-SARGable clause: {clause}")


def test_operator_coverage(con) -> None:
    print("Operator coverage (every clause parameterised and SARGable):")
    cases = [
        ({"column": "club", "kind": "text", "op": "equals",
          "value": "Collingwood"}, 2),
        ({"column": "club", "kind": "text", "op": "not equals",
          "value": "Collingwood"}, 3),
        ({"column": "club", "kind": "text", "op": "one of",
          "values": ["Hawthorn", "Fitzroy"]}, 3),
        ({"column": "player", "kind": "text", "op": "starts with",
          "value": "Scott"}, 1),
        ({"column": "player", "kind": "text", "op": "ends with",
          "value": "Rioli"}, 1),
        ({"column": "player", "kind": "text", "op": "contains",
          "value": "ck"}, 1),
        ({"column": "scraped_at", "kind": "datetime", "op": "is missing"}, 1),
        ({"column": "scraped_at", "kind": "datetime", "op": "is present"}, 4),
        ({"column": "career_games", "kind": "integer", "op": "=",
          "value": 400}, 1),
        ({"column": "career_games", "kind": "integer", "op": ">",
          "value": 200}, 3),
        ({"column": "career_games", "kind": "integer", "op": "≥",
          "value": 189}, 4),
        ({"column": "career_games", "kind": "integer", "op": "<",
          "value": 100}, 1),
        ({"column": "career_games", "kind": "integer", "op": "≤",
          "value": 189}, 2),
        ({"column": "career_games", "kind": "integer", "op": "≠",
          "value": 40}, 4),
        ({"column": "career_games", "kind": "integer", "op": "between",
          "lo": 100, "hi": 300}, 2),
        ({"column": "premierships", "kind": "integer", "op": "one of",
          "values": [1, 4]}, 3),
        ({"column": "height_cm", "kind": "float", "op": ">",
          "value": 184.5}, 3),
    ]
    for spec, expected in cases:
        bag = QB.ParamBag()
        clause = QB.compile_condition(spec, KNOWN, bag)
        assert_sargable(clause)
        got = len(_names(con, clause, bag.values))
        check(f"{spec['column']} {spec['op']}", got == expected,
              f"{got} rows (wanted {expected}); {clause}")
        # Values ride as parameters, never as SQL text.
        for value in bag.values.values():
            if isinstance(value, str) and value not in ("%",):
                assert str(value) not in clause


def test_dates_are_sargable(con) -> None:
    print("Date filters: raw ISO comparison, half-open ceilings, no DATE():")
    bag = QB.ParamBag()
    clause = QB.compile_condition(
        {"column": "debut_date", "kind": "date", "op": "between",
         "lo": "2000-01-01", "hi": "2007-12-31"}, KNOWN, bag)
    assert_sargable(clause)
    check("date between (plain dates -> BETWEEN)",
          _names(con, clause, bag.values) == ["Luke Hodge",
                                              "Scott Pendlebury"], clause)

    bag = QB.ParamBag()
    clause = QB.compile_condition(
        {"column": "scraped_at", "kind": "datetime", "op": "on",
         "value": "2026-08-09", "day_ceiling": True}, KNOWN, bag)
    assert_sargable(clause)
    check("datetime 'on' is a half-open day range",
          ">=" in clause and "<" in clause and
          _names(con, clause, bag.values) == ["Cyril Rioli", "Luke Hodge"],
          f"{clause} with {bag.values}")
    check("ceiling parameter is the next day",
          "2026-08-10" in map(str, bag.values.values()), repr(bag.values))

    bag = QB.ParamBag()
    clause = QB.compile_condition(
        {"column": "scraped_at", "kind": "datetime", "op": "on or before",
         "value": "2026-08-08", "day_ceiling": True}, KNOWN, bag)
    assert_sargable(clause)
    check("'on or before' catches times on the boundary day",
          _names(con, clause, bag.values) == ["Nathan Buckley",
                                              "Scott Pendlebury"], clause)


def test_index_usage(con) -> None:
    print("EXPLAIN QUERY PLAN: the compiled clauses actually use indexes:")
    for spec, index in [
        ({"column": "career_games", "kind": "integer", "op": "≥",
          "value": 150}, "ix_games"),
        ({"column": "career_games", "kind": "integer", "op": "between",
          "lo": 100, "hi": 300}, "ix_games"),
        ({"column": "debut_date", "kind": "date", "op": "on or after",
          "value": "2000-01-01"}, "ix_debut"),
        ({"column": "scraped_at", "kind": "datetime", "op": "on",
          "value": "2026-08-09", "day_ceiling": True}, "ix_scraped"),
    ]:
        bag = QB.ParamBag()
        clause = QB.compile_condition(spec, KNOWN, bag)
        plan = " ".join(
            row[3] for row in con.execute(
                f"EXPLAIN QUERY PLAN SELECT * FROM players WHERE {clause}",
                bag.values))
        check(f"{spec['column']} {spec['op']} -> {index}",
              index in plan and "SCAN players" not in plan.replace(
                  f"USING INDEX {index}", ""), plan)
    # The control: the old DATE() wrapper forces a full scan.
    plan = " ".join(row[3] for row in con.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM players "
        "WHERE DATE(scraped_at) = '2026-08-09'"))
    check("control: DATE(col) wrapper scans instead", "SCAN" in plan
          and "ix_scraped" not in plan, plan)


def test_nested_groups(con) -> None:
    print("Nested groups: (Collingwood AND 150+ games) OR "
          "(drafted Hawthorn AND premiership):")
    bag = QB.ParamBag()
    g1 = QB.combine_condition_clauses([
        QB.compile_condition({"column": "club", "kind": "text",
                              "op": "equals", "value": "Collingwood"},
                             KNOWN, bag),
        QB.compile_condition({"column": "career_games", "kind": "integer",
                              "op": "≥", "value": 150}, KNOWN, bag),
    ], "AND")
    g2 = QB.combine_condition_clauses([
        QB.compile_condition({"column": "drafted_by", "kind": "text",
                              "op": "equals", "value": "Hawthorn"},
                             KNOWN, bag),
        QB.compile_condition({"column": "premierships", "kind": "integer",
                              "op": "≥", "value": 1}, KNOWN, bag),
    ], "AND")
    where = QB.combine_group_clauses([("AND", g1), ("OR", g2)])
    check("clause shape",
          where == f"({g1} OR {g2})", where)
    check("row results", _names(con, where, bag.values) ==
          ["Cyril Rioli", "Luke Hodge", "Nathan Buckley",
           "Scott Pendlebury"], where)

    # Mixed joiners fold left with explicit parentheses.
    bag = QB.ParamBag()
    a = QB.compile_condition({"column": "club", "kind": "text",
                              "op": "equals", "value": "Fitzroy"},
                             KNOWN, bag)
    b = QB.compile_condition({"column": "club", "kind": "text",
                              "op": "equals", "value": "Hawthorn"},
                             KNOWN, bag)
    c = QB.compile_condition({"column": "career_games", "kind": "integer",
                              "op": "≥", "value": 100}, KNOWN, bag)
    where = QB.combine_group_clauses([("AND", a), ("OR", b), ("AND", c)])
    check("((A OR B) AND C) parenthesisation",
          where == f"(({a} OR {b}) AND {c})", where)
    check("((A OR B) AND C) rows",
          _names(con, where, bag.values) == ["Cyril Rioli", "Luke Hodge"],
          where)
    # Empty groups drop out without consuming a joiner.
    check("empty group drops out",
          QB.combine_group_clauses([("AND", None), ("OR", b)]) == b)
    check("all-empty is None",
          QB.combine_group_clauses([("AND", None)]) is None)


def test_half_built_specs() -> None:
    print("Half-built specs filter nothing instead of erroring:")
    for spec in [
        {"column": "career_games", "kind": "integer", "op": "≥"},
        {"column": "career_games", "kind": "integer", "op": "between",
         "lo": 100},
        {"column": "club", "kind": "text", "op": "one of", "values": []},
        {"column": "club", "kind": "text", "op": "contains", "value": ""},
        {"column": "debut_date", "kind": "date", "op": "on"},
    ]:
        bag = QB.ParamBag()
        check(f"{spec['column']} {spec['op']} (incomplete)",
              QB.compile_condition(spec, KNOWN, bag) is None
              and bag.values == {})


def test_security_walls() -> None:
    print("Security: gate, vocabulary, parameters:")
    try:
        QB.compile_condition({"column": "club; DROP TABLE players",
                              "kind": "text", "op": "equals", "value": "x"},
                             KNOWN, QB.ParamBag())
        check("unknown column refused", False)
    except ValueError:
        check("unknown column refused", True)
    try:
        QB.compile_condition({"column": "club", "kind": "text",
                              "op": "= 1; --", "value": "x"},
                             KNOWN, QB.ParamBag())
        check("unknown operator refused", False)
    except ValueError:
        check("unknown operator refused", True)
    try:
        QB.combine_group_clauses([("AND", "1=1"), ("XOR --", "1=1")])
        check("joiner outside AND/OR refused", False)
    except ValueError:
        check("joiner outside AND/OR refused", True)
    bag = QB.ParamBag()
    hostile = "x' OR '1'='1"
    clause = QB.compile_condition({"column": "club", "kind": "text",
                                   "op": "equals", "value": hostile},
                                  KNOWN, bag)
    check("hostile value rides as a parameter",
          hostile not in clause and hostile in bag.values.values(), clause)


def test_share_token_round_trip() -> None:
    print("Share tokens: compressed, URL-safe, lossless:")
    payload = {"table": "players", "groups": [
        {"joiner": "AND", "match": "AND", "conditions": [
            {"column": "club", "kind": "text", "op": "equals",
             "value": "Collingwood"},
            {"column": "career_games", "kind": "integer", "op": "≥",
             "value": 150}]},
        {"joiner": "OR", "match": "AND", "conditions": [
            {"column": "drafted_by", "kind": "text", "op": "equals",
             "value": "Hawthorn"},
            {"column": "premierships", "kind": "integer", "op": "≥",
             "value": 1}]},
    ]}
    token = QB.serialize_state(payload)
    check("token is URL-safe ASCII",
          re.fullmatch(r"[A-Za-z0-9_=-]+", token) is not None,
          token[:60] + "…")
    check("round-trip is lossless", QB.deserialize_state(token) == payload)
    try:
        QB.deserialize_state("not-a-token!!")
        check("garbage token raises ValueError", False)
    except ValueError:
        check("garbage token raises ValueError", True)


def run_headless() -> int:
    # The operator labels are Unicode (≥, ≤, ≠); Windows consoles often
    # default to cp1252, which cannot print them.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    con = mock_connection()
    test_operator_coverage(con)
    test_dates_are_sargable(con)
    test_index_usage(con)
    test_nested_groups(con)
    test_half_built_specs()
    test_security_walls()
    test_share_token_round_trip()
    print(f"\n{len(_CHECKS)} checks, "
          f"{_CHECKS.count('FAIL')} failures.")
    return 1 if "FAIL" in _CHECKS else 0


# ------------------------------------------------------------ streamlit UI

def render_ui() -> None:
    """The nested-groups panel in miniature, over the mock table."""
    from types import SimpleNamespace

    import streamlit as st

    st.set_page_config(page_title="Nested groups sandbox", layout="wide")
    st.markdown("# Nested condition groups — sandbox")
    st.caption("Real `_spec_widget`s and the real compiler over five mock "
               "players. Groups match all/any of their conditions and "
               "join with AND/OR; the compiled WHERE and its parameters "
               "print below, with a live count on every condition.")

    con = mock_connection()
    sport = SimpleNamespace(k=lambda *p: ":".join(("sandbox",)
                                                  + tuple(map(str, p))))
    profiles = {
        "player": {"kind": "text"},
        "club": {"kind": "text",
                 "values": sorted({r[1] for r in ROWS})},
        "drafted_by": {"kind": "text",
                       "values": sorted({r[2] for r in ROWS})},
        "career_games": {"kind": "integer", "lo": 40, "hi": 400},
        "career_goals": {"kind": "integer", "lo": 5, "hi": 275},
        "height_cm": {"kind": "float", "lo": 178.0, "hi": 191.0},
        "premierships": {"kind": "integer", "lo": 0, "hi": 4},
        "debut_date": {"kind": "date", "lo": "1990-05-05",
                       "hi": "2008-03-22"},
        "scraped_at": {"kind": "datetime", "lo": "2026-08-08T09:00:00",
                       "hi": "2026-08-09T23:59:59"},
    }
    by_name = {c.name: c for c in COLUMNS}
    ordered = QB.categorised_order(by_name)

    state = st.session_state.setdefault(
        "sandbox_groups", {"groups": [{"gid": 0}], "next": 1})

    bag = QB.ParamBag()
    pairs = []
    for gi, group in enumerate(list(state["groups"])):
        gid = group["gid"]
        base = f"sandbox:{gid}"
        joiner = "AND"
        if gi:
            joiner = st.segmented_control(
                "Joined with the groups above using", ["AND", "OR"],
                default="OR", key=f"{base}:joiner") or "OR"
        with st.container(border=True):
            head = st.container(horizontal=True,
                                vertical_alignment="center", gap="small")
            with head:
                with st.container(width="stretch"):
                    st.markdown(f"**Group {gi + 1}**")
                match_label = st.radio("Match", ["all (AND)", "any (OR)"],
                                       horizontal=True, key=f"{base}:match",
                                       label_visibility="collapsed")
                if len(state["groups"]) > 1 and st.button(
                        ":material/delete:", key=f"{base}:drop",
                        type="tertiary", help="Remove this group"):
                    state["groups"] = [g for g in state["groups"]
                                       if g["gid"] != gid]
                    st.rerun()
            chosen = st.multiselect(
                "Conditions — columns are grouped by category", ordered,
                key=f"{base}:cols",
                format_func=lambda n:
                    f"{n.replace('_', ' ')} — {QB.column_category(n)}")
            match = "AND" if (match_label or "").startswith("all") else "OR"
            clauses = []
            for name in chosen:
                box = st.container(border=True)
                spec = QB._spec_widget(box, f"{base}:{name}",
                                       by_name[name], profiles[name])
                if spec is None:
                    box.caption("No filter yet.")
                    continue
                solo = QB.ParamBag()
                clause = QB.compile_condition(spec, set(by_name), solo)
                if clause:
                    n = con.execute(
                        f"SELECT COUNT(*) FROM players WHERE {clause}",
                        solo.values).fetchone()[0]
                    box.caption(f":material/filter_alt: matches {n:,} "
                                f"row{'' if n == 1 else 's'} on its own")
                    clauses.append(QB.compile_condition(spec,
                                                        set(by_name), bag))
            pairs.append((joiner,
                          QB.combine_condition_clauses(clauses, match)))
    if st.button("Add a condition group", icon=":material/add:"):
        state["groups"].append({"gid": state["next"]})
        state["next"] += 1
        st.rerun()

    where = QB.combine_group_clauses(pairs)
    st.markdown("#### Compiled WHERE")
    st.code(where or "— nothing filters yet —", language="sql")
    st.code(repr(bag.values), language="python")
    if where:
        rows = con.execute(
            f"SELECT * FROM players WHERE {where}", bag.values).fetchall()
        st.dataframe(rows)


def _in_streamlit() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx() is not None
    except Exception:
        return False


if _in_streamlit():
    render_ui()
elif __name__ == "__main__":
    sys.exit(run_headless())
