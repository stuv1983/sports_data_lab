"""The rebuilt Grid-query mode, driven headlessly through real widgets.

The builder's promise is that the query is an object: criteria are added
through one searchable picker, appear as cards with live counts, and can
be edited in place, OR-chained, and removed -- all in the main column,
because the sidebar does not exist on a phone. These tests walk that
lifecycle against the live AFL database and skip without one.
"""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---

from pathlib import Path

import pytest

from data_paths import default_db

pytestmark = pytest.mark.skipif(
    not Path(default_db("afl")).exists(), reason="no built AFL database")

APP = """
import os
import streamlit as st
import db_pool
import sports
import query_builder

sport = sports.get("afl")
stat = os.stat(sport.db)
revision = (str(sport.db), stat.st_mtime_ns, stat.st_size)
st.session_state.SPORT = sport
st.session_state.DB_REVISION = revision
st.session_state.con = db_pool.get_con(sport.db, revision)
st.session_state.AVAILABLE = sport.layers(st.session_state.con).builders
query_builder.page(sport, heading=False)
"""


def _app():
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(APP, default_timeout=120)
    app.run()
    assert not app.exception, app.exception
    return app


def _panel_key(app):
    nonce = app.session_state["afl:qbc_query"]["nonce"]
    return f"afl:qbc_panel:{nonce}"


def _pick(app, formatted):
    app.selectbox(key=f"{_panel_key(app)}_kind").select(formatted)
    app.run()
    assert not app.exception, app.exception


def test_the_mcg_hundred_game_query_is_three_touches():
    """The user story this page exists for: question, ground, number."""
    app = _app()

    kind = app.selectbox(key=f"{_panel_key(app)}_kind")
    # One searchable picker over the whole catalogue, category-tagged.
    assert len(kind.options) > 100
    assert any(o.startswith("X+ games at venue") for o in kind.options)

    _pick(app, "X+ games at venue — Grounds & venues")
    # The venue list leads with the most-played ground -- the MCG.
    venue = app.selectbox(key=f"{_panel_key(app)}_args_venue")
    assert "M.C.G." in venue.options[0]
    app.number_input(key=f"{_panel_key(app)}_args_x").set_value(100)
    app.run()

    # A live count appears before anything is committed.
    assert any("players qualify" in c.value for c in app.caption)

    app.button(key=f"{_panel_key(app)}_commit").click()
    app.run()
    assert not app.exception, app.exception

    state = app.session_state["afl:qbc_query"]
    children = state["root"]["children"]
    assert [c["kind"] for c in children] == ["X+ games at venue"]
    assert children[0]["args"][1] == 100
    # The full-query count is the known answer for this square.
    values = [m.value for m in app.metric]
    assert values and 50 <= int(values[0].replace(",", "")) <= 400


def _add(app, formatted, numbers=None):
    _pick(app, formatted)
    for key, value in (numbers or {}).items():
        app.number_input(key=f"{_panel_key(app)}_args_{key}").set_value(value)
        app.run()
    app.button(key=f"{_panel_key(app)}_commit").click()
    app.run()
    assert not app.exception, app.exception


def test_criteria_chain_edit_and_remove_in_place():
    app = _app()
    _add(app, "X+ games at venue — Grounds & venues", {"x": 100})
    _add(app, "150+ / X+ career games — Career milestones", {"games": 150})

    state = app.session_state["afl:qbc_query"]
    root = state["root"]
    assert len(root["children"]) == 2, "second add is a new requirement"
    assert root["op"] == "AND"

    # Edit preloads the stored configuration into the panel.
    app.button(key="afl:qbc_edit_btn:1").click()
    app.run()
    games = app.number_input(key=f"{_panel_key(app)}_args_games")
    assert games.value == 150
    games.set_value(300)
    app.run()
    app.button(key=f"{_panel_key(app)}_commit").click()
    app.run()
    state = app.session_state["afl:qbc_query"]
    assert state["root"]["children"][1]["args"] == [300]

    # "or…" wraps the chip into an OR card and the next add lands inside
    # it, not as a new top-level requirement.
    app.button(key="afl:qbc_or_btn:0").click()
    app.run()
    state = app.session_state["afl:qbc_query"]
    group = state["root"]["children"][0]
    assert group["type"] == "group" and group["op"] == "OR"
    _add(app, "Played at venue — Grounds & venues")
    state = app.session_state["afl:qbc_query"]
    group = state["root"]["children"][0]
    assert len(group["children"]) == 2
    assert len(state["root"]["children"]) == 2

    # Removing a chip inside the card leaves the card's other member.
    app.button(key="afl:qbc_drop_btn:0:1").click()
    app.run()
    state = app.session_state["afl:qbc_query"]
    group = state["root"]["children"][0]
    assert [c["kind"] for c in group["children"]] == ["X+ games at venue"]


def test_or_of_ands_is_expressible_and_compiles():
    """(A AND B) OR (C AND D): the shape the old two-level model could
    not represent. Root set to ANY, two ALL subgroups."""
    app = _app()
    _add(app, "Played for club — Clubs & journeys")
    _add(app, "150+ / X+ career games — Career milestones", {"games": 150})

    root_nid = app.session_state["afl:qbc_query"]["root"]["nid"]
    app.segmented_control(key=f"afl:qbc_group_op:{root_nid}") \
       .set_value("any (OR)")
    app.run()
    assert not app.exception, app.exception
    state = app.session_state["afl:qbc_query"]
    assert state["root"]["op"] == "OR"
    # The whole query still counts and renders.
    values = [m.value for m in app.metric]
    assert values and int(values[0].replace(",", "")) > 0


def test_the_page_never_touches_the_sidebar():
    """The old mode kept its add/remove buttons in the sidebar, which a
    phone never shows. Everything must render in the main column."""
    app = _app()
    assert not app.sidebar.selectbox
    assert not app.sidebar.button
    assert not app.sidebar.number_input


# ------------------------------------------------- outer page deep links

PAGE_APP = """
import os
import streamlit as st
import db_pool
import sports

sport = sports.get("afl")
stat = os.stat(sport.db)
revision = (str(sport.db), stat.st_mtime_ns, stat.st_size)
st.session_state.SPORT = sport
st.session_state.DB_REVISION = revision
st.session_state.con = db_pool.get_con(sport.db, revision)
st.session_state.setdefault(
    "AVAILABLE", sport.layers(st.session_state.con).builders)

with open("app_pages/8_Advanced_Search.py", encoding="utf-8") as fh:
    exec(compile(fh.read(), "8_Advanced_Search.py", "exec"))
"""


def _page_app():
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(PAGE_APP, default_timeout=120)
    return app


def test_deep_links_route_and_reroute_the_outer_mode():
    """?q= opens Player search, ?qb= opens the Builder, and a *changed*
    URL re-routes an existing session -- while an unchanged URL never
    overrides a mode the reader picked by hand."""
    app = _page_app()
    app.query_params["q"] = "games>=300 sort:games"
    app.run()
    assert not app.exception, app.exception
    assert app.session_state["afl:advanced_mode"] == "Player search"
    assert app.session_state["afl:advanced_query"] == "games>=300 sort:games"

    # The reader switches to Builder by hand; the unchanged URL must not
    # bounce them back on the next rerun.
    app.segmented_control(key="afl:advanced_mode").set_value(
        "Query builder")
    app.run()
    assert not app.exception, app.exception
    assert app.session_state["afl:advanced_mode"] == "Query builder"

    # A *different* q in the same tab re-routes and updates the text.
    app.query_params["q"] = "club:Fitzroy games>=100"
    app.run()
    assert not app.exception, app.exception
    assert app.session_state["afl:advanced_mode"] == "Player search"
    assert app.session_state["afl:advanced_query"] == \
        "club:Fitzroy games>=100"


def test_a_builder_deep_link_selects_the_builder_and_restores():
    import query_builder as QB
    import sports

    sport = sports.get("afl")
    token = QB.serialize_state(QB.build_share_envelope(
        sport, "grid",
        {"type": "group", "op": "AND", "children": [
            {"type": "criterion", "kind": "150+ / X+ career games",
             "args": [300]}]},
        display={"order": "Most obscure", "limit": 25}))

    app = _page_app()
    app.query_params["qb"] = token
    app.run()
    assert not app.exception, app.exception
    assert app.session_state["afl:advanced_mode"] == "Query builder"
    root = app.session_state["afl:qbc_query"]["root"]
    assert [c["kind"] for c in root["children"]] == \
        ["150+ / X+ career games"]
    assert root["children"][0]["args"] == [300]
    # The grid compiled and counted: plenty of 300-game AFL careers.
    values = [m.value for m in app.metric]
    assert values and int(values[0].replace(",", "")) > 50
