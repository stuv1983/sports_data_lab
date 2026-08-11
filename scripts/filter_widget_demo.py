#!/usr/bin/env python3
"""Standalone harness for query_builder's _filter_widget.

    streamlit run scripts/filter_widget_demo.py

Renders the real widget -- not a copy -- over mock column profiles of
every kind it supports (low-cardinality text, free text, integer, float,
boolean, date, and a column with unreadable date bounds), with no
database and no sport loaded. The compiled predicate and its bound
parameters print live under each widget, and the assembled statements
(plain and grouped) at the bottom, so a change to the operator logic can
be exercised by eye in seconds and any value you type can be watched
arriving as a parameter, never as SQL text.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

import query_builder as QB

st.set_page_config(page_title="_filter_widget harness", layout="wide")
st.markdown("# `_filter_widget` harness")
st.caption(
    "The real widget over mock profiles — no database, no sport. Pick "
    "operators and values; the parameterised predicate each filter "
    "compiles to appears beneath it.")

#: A sport stand-in supplying the one thing the widget uses: namespaced keys.
SPORT = SimpleNamespace(k=lambda *parts: ":".join(("demo",) + tuple(
    str(part) for part in parts)))

#: (column, profile) pairs covering every kind the widget maps.
MOCKS = [
    (QB.Column("club", "TEXT", "text"),
     {"kind": "text", "values": ["Carlton", "Collingwood", "Fitzroy",
                                 "Geelong", "Hawthorn"]}),
    (QB.Column("player", "TEXT", "text"),
     {"kind": "text"}),                          # high-cardinality: free text
    (QB.Column("career_goals", "INTEGER", "integer"),
     {"kind": "integer", "lo": 0, "hi": 1360}),
    (QB.Column("disposal_average", "REAL", "float"),
     {"kind": "float", "lo": 0.0, "hi": 35.8}),
    (QB.Column("is_final", "BOOLEAN", "boolean"),
     {"kind": "boolean"}),
    (QB.Column("match_date", "DATE", "date"),
     {"kind": "date", "lo": "1897-05-08", "hi": "2026-08-01"}),
    (QB.Column("scraped_at", "DATE", "date"),
     {"kind": "date", "lo": "not-a-date", "hi": "also junk"}),
]

bag = QB.ParamBag()
predicates: list[str] = []
for column, profile in MOCKS:
    with st.container(border=True):
        st.caption(f"`{column.name}` — declared {column.declared}, "
                   f"kind **{column.kind}**"
                   + (", unparseable bounds" if column.name == "scraped_at"
                      else ""))
        predicate = QB._filter_widget(st, SPORT, "demo", column,
                                      profile, bag)
        if predicate:
            predicates.append(predicate)
            st.code(predicate, language="sql")
        else:
            st.caption("no filter")

st.markdown("#### Assembled statement")
combinator = st.radio("Combine with", ["AND", "OR"], horizontal=True)
group_by = st.multiselect("Group by (COUNT per group)",
                          [column.name for column, _ in MOCKS])
known = {column.name for column, _ in MOCKS}
if group_by:
    sql = QB.build_group_select("demo", group_by, predicates, combinator,
                                100, bag, known_tables={"demo"},
                                known_columns=known)
else:
    sql = QB.build_select("demo", sorted(known), predicates, combinator,
                          "career_goals", True, 100, bag,
                          known_tables={"demo"}, known_columns=known)
st.code(sql, language="sql")
st.markdown("#### Bound parameters")
st.code(repr(bag.values), language="python")
st.caption("Every typed value above appears here as a named parameter — "
           "none of them are ever spliced into the SQL text.")
