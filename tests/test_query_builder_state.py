"""Durable state, atomic restore and the execution gate, headlessly.

Streamlit deletes a keyed widget's state when its widget stops rendering,
which is exactly what a mode switch does -- the defect that emptied a
populated filter panel after a trip through Grid mode. These tests drive
query_builder.page() through real widgets over a tiny synthetic database
(no production data), covering: state surviving mode switches, all-or-
nothing token restores, the explicit Run gate, the table allowlist, and
sport-declared date metadata.
"""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---

import pytest

import query_builder as QB

_sys.path.insert(0, _os.path.join(_ROOT, "tests"))
import synthetic_sport

APP = f"""
import sys
sys.path.insert(0, {_ROOT!r})
sys.path.insert(0, {_ROOT!r} + "/tests")
import streamlit as st
import sports
import synthetic_sport

sport = synthetic_sport.make_sport()
synthetic_sport.build_db(sport.db)
sports.SPORTS[sport.key] = sport
st.session_state.setdefault("AVAILABLE", list(sport.C.BUILDERS))

import query_builder
query_builder.page(sport, heading=False)
"""

MODE_KEY = "syn:qb_mode"
GRID, FILTERS = QB.MODE_CONSTRAINTS, QB.MODE_FILTERS


@pytest.fixture(autouse=True)
def _deregister_synthetic_sport():
    """The AppTest scripts run in this process and register the synthetic
    sport in the real sports.SPORTS; leaving it there fails every later
    test that enumerates the registry."""
    yield
    import sports

    sports.SPORTS.pop("syn", None)


def _app():
    from streamlit.testing.v1 import AppTest

    synthetic_sport.build_db()
    app = AppTest.from_string(APP, default_timeout=60)
    app.run()
    assert not app.exception, app.exception
    return app


def _to_filters(app):
    app.segmented_control(key=MODE_KEY).set_value(FILTERS)
    app.run()
    assert not app.exception, app.exception


def _root_gid(app):
    return app.session_state["syn:qbf_state:players"]["root"]["gid"]


def _condition_on_games(app, value=100):
    """Select career_games in the root group and give it a ≥ condition."""
    base = f"syn:qbf:players:{_root_gid(app)}"
    app.multiselect(key=f"{base}:cols").select("career_games")
    app.run()
    assert not app.exception, app.exception
    app.number_input(key=f"{base}:career_games:val").set_value(value)
    app.run()
    assert not app.exception, app.exception
    return base


def _filters_envelope(value=150, limit=50):
    sport = synthetic_sport.make_sport()
    return QB.serialize_state(QB.build_share_envelope(
        sport, "filters",
        {"type": "group", "op": "AND", "children": [
            {"column": "career_games", "kind": "integer", "op": "≥",
             "value": value}]},
        table="players",
        display={"columns": ["player", "career_games"],
                 "sort": "career_games", "descending": True,
                 "limit": limit, "group_by": []}))


# ----------------------------------------------------------- durability

def test_filter_state_survives_a_round_trip_through_grid_mode():
    """The headline defect: a populated panel came back empty after
    switching to Grid mode and back, because widget state died with the
    hidden widgets."""
    app = _app()
    _to_filters(app)
    base = _condition_on_games(app, 100)

    # Away to Grid mode (the filter widgets stop rendering)...
    app.segmented_control(key=MODE_KEY).set_value(GRID)
    app.run()
    assert not app.exception, app.exception
    # ...and back.
    app.segmented_control(key=MODE_KEY).set_value(FILTERS)
    app.run()
    assert not app.exception, app.exception

    assert app.session_state[f"{base}:cols"] == ["career_games"]
    assert app.session_state[f"{base}:career_games:val"] == 100
    sql_blocks = [c.value for c in app.code]
    assert any('"career_games" >=' in s for s in sql_blocks), \
        "the compiled WHERE lost the restored condition"


def test_subgroups_nest_and_compile_through_the_panel():
    """(root ANY) holding a nested ALL subgroup — the recursive editor."""
    app = _app()
    _to_filters(app)
    base = _condition_on_games(app, 100)

    app.button(key=f"{base}:subgroup").click()
    app.run()
    assert not app.exception, app.exception
    state = app.session_state["syn:qbf_state:players"]
    assert len(state["root"]["children"]) == 1
    child_gid = state["root"]["children"][0]["gid"]

    child_base = f"syn:qbf:players:{child_gid}"
    app.multiselect(key=f"{child_base}:cols").select("player")
    app.run()
    sel = app.selectbox(key=f"{child_base}:player:op")
    sel.select("equals")
    app.run()
    app.text_input(key=f"{child_base}:player:val").set_value("Alpha")
    app.run()
    assert not app.exception, app.exception

    sql_blocks = [c.value for c in app.code]
    assert any('"career_games" >=' in s and '"player" =' in s
               for s in sql_blocks), sql_blocks


# ------------------------------------------------------------- restores

def test_a_valid_token_restores_query_and_display_atomically():
    app = _app()
    _to_filters(app)
    app.session_state["syn:qbf_pending"] = _filters_envelope(150, 50)
    app.run()
    assert not app.exception, app.exception

    state = app.session_state["syn:qbf_state:players"]
    gid = state["root"]["gid"]
    assert app.session_state[f"syn:qbf:players:{gid}:cols"] == \
        ["career_games"]
    assert app.session_state[f"syn:qbf:players:{gid}:career_games:val"] == 150
    assert app.session_state["syn:qb_limit"] == 50
    assert app.session_state["syn:qb_sort:players"] == "career_games"
    assert app.session_state["syn:qb_cols:players"] == \
        ["player", "career_games"]


@pytest.mark.parametrize("break_it", [
    lambda env: env["query"]["children"][0].update(column="no_such_col"),
    lambda env: env["query"]["children"][0].update(op="explodes"),
    lambda env: env["query"]["children"][0].update(value="not-a-number"),
    lambda env: env["display"].update(limit=-1),
    lambda env: env.update(table="secret_staging"),
])
def test_a_broken_token_warns_and_leaves_existing_state_untouched(break_it):
    """Restore is all-or-nothing: unknown columns, foreign operators,
    bad values and disallowed tables are refused with a warning, and the
    query that was on screen stays exactly as it was."""
    app = _app()
    _to_filters(app)
    base = _condition_on_games(app, 100)
    before_state = app.session_state["syn:qbf_state:players"]
    next_gid = before_state["next"]

    sport = synthetic_sport.make_sport()
    envelope = QB.build_share_envelope(
        sport, "filters",
        {"type": "group", "op": "AND", "children": [
            {"column": "career_games", "kind": "integer", "op": "≥",
             "value": 150}]},
        table="players",
        display={"columns": ["player"], "sort": None, "descending": False,
                 "limit": 50, "group_by": []})
    break_it(envelope)
    try:
        token = QB.serialize_state(envelope)
    except ValueError:
        pytest.skip("serializer already refuses this shape")

    app.session_state["syn:qbf_pending"] = token
    app.run()
    assert not app.exception, app.exception
    assert app.warning, "a failed restore must say why"
    assert app.session_state["syn:qbf_state:players"] == before_state
    # A partial restore would have staged keys on the next fresh gid.
    for leaked in (f"syn:qbf:players:{next_gid}:cols",
                   f"syn:qbf:players:{next_gid}:match",
                   f"syn:qbf:players:{next_gid}:career_games:val"):
        assert leaked not in app.session_state, "restore leaked staged keys"
    assert app.session_state[f"{base}:cols"] == ["career_games"]
    assert app.session_state[f"{base}:career_games:val"] == 100


def test_a_url_qb_parameter_restores_once_and_only_once():
    app = _app()
    app.query_params["qb"] = _filters_envelope(175, 25)
    app.run()
    assert not app.exception, app.exception
    state = app.session_state["syn:qbf_state:players"]
    gid = state["root"]["gid"]
    assert app.session_state[f"syn:qbf:players:{gid}:career_games:val"] == 175
    assert app.session_state[MODE_KEY] == FILTERS

    # The same URL must not stamp over later edits on every rerun.
    app.number_input(key=f"syn:qbf:players:{gid}:career_games:val") \
       .set_value(60)
    app.run()
    assert app.session_state[f"syn:qbf:players:{gid}:career_games:val"] == 60


def test_a_grid_token_restores_criteria_and_counts():
    """Grid restores rebuild criteria through the server-owned builders
    and land in the recursive grid state."""
    sport = synthetic_sport.make_sport()
    token = QB.serialize_state(QB.build_share_envelope(
        sport, "grid",
        {"type": "group", "op": "OR", "children": [
            {"type": "criterion", "kind": "Played for club",
             "args": ["A"]},
            {"type": "group", "op": "AND", "children": [
                {"type": "criterion", "kind": "Played for club",
                 "args": ["B"]},
                {"type": "criterion",
                 "kind": "150+ / X+ career games", "args": [100]},
            ]},
        ]},
        display={"order": "Most obscure", "limit": 25}))

    app = _app()
    app.session_state["syn:qbf_pending"] = token
    app.run()
    assert not app.exception, app.exception
    assert app.session_state[MODE_KEY] == GRID
    root = app.session_state["syn:qbc_query"]["root"]
    assert root["op"] == "OR"
    kinds = [c.get("kind") for c in root["children"]]
    assert kinds[0] == "Played for club"
    assert root["children"][1]["op"] == "AND"
    # Alpha played A; Gamma played B with 120 games: count = 2.
    values = [m.value for m in app.metric]
    assert values and int(values[0].replace(",", "")) == 2


def test_a_doctored_grid_token_cannot_carry_sql():
    app = _app()
    sport = synthetic_sport.make_sport()
    token = QB.serialize_state(QB.build_share_envelope(
        sport, "grid",
        {"type": "group", "op": "AND", "children": [
            {"type": "fragment",
             "sql": "SELECT player_id FROM players", "params": []}]},
        display={"order": "Most obscure", "limit": 25}))
    app.session_state["syn:qbf_pending"] = token
    app.run()
    assert not app.exception, app.exception
    assert app.warning, "fragment leaves must be refused"
    assert app.session_state["syn:qbc_query"]["root"]["children"] == []


# ------------------------------------------------------------ run gating

def test_results_wait_for_an_explicit_run_and_invalidate_on_change():
    app = _app()
    _to_filters(app)
    base = _condition_on_games(app, 100)

    assert not app.dataframe, "results ran before Run was clicked"
    app.button(key="syn:qb_run:players").click()
    app.run()
    assert not app.exception, app.exception
    assert app.dataframe, "Run must execute the query"

    # Changing the condition invalidates the previous run's signature.
    app.number_input(key=f"{base}:career_games:val").set_value(60)
    app.run()
    assert not app.dataframe, "a changed query must not show stale rows"


# ------------------------------------------------- allowlist and metadata

def test_internal_tables_are_not_offered_even_though_they_exist():
    app = _app()
    _to_filters(app)
    options = app.selectbox(key="syn:qb_table").options
    assert "secret_staging" not in options
    assert set(options) == {"players", "games"}


def test_a_text_declared_date_column_gets_date_operators():
    """games.date is TEXT in the DDL; the sport's query_column_kinds
    override must hand it the date vocabulary, strict after/before
    included."""
    app = _app()
    _to_filters(app)
    app.selectbox(key="syn:qb_table").select("games")
    app.run()
    gid = app.session_state["syn:qbf_state:games"]["root"]["gid"]
    base = f"syn:qbf:games:{gid}"
    app.multiselect(key=f"{base}:cols").select("date")
    app.run()
    assert not app.exception, app.exception
    ops = app.selectbox(key=f"{base}:date:op").options
    assert list(ops) == list(QB._DATE_OPS)
    assert "after" in ops and "before" in ops


def test_boolean_overridden_flag_gets_true_false_controls():
    app = _app()
    _to_filters(app)
    app.selectbox(key="syn:qb_table").select("games")
    app.run()
    gid = app.session_state["syn:qbf_state:games"]["root"]["gid"]
    base = f"syn:qbf:games:{gid}"
    app.multiselect(key=f"{base}:cols").select("is_final")
    app.run()
    assert not app.exception, app.exception
    assert app.segmented_control(key=f"{base}:is_final:bool").value == "Any"


def test_profiles_are_measured_only_for_active_columns():
    """The old page profiled every column of the selected table before
    either builder rendered -- ~30 s on a 155-column table."""
    from streamlit.testing.v1 import AppTest

    synthetic_sport.build_db()
    probe = APP.replace(
        "import query_builder\n",
        "import query_builder\n"
        "_real = query_builder.column_profile\n"
        "def _counting(conn, db, revision, table, column, kind):\n"
        "    st.session_state.setdefault('profiled', []).append(column)\n"
        "    return _real(conn, db, revision, table, column, kind)\n"
        "query_builder.column_profile = _counting\n")
    app = AppTest.from_string(probe, default_timeout=60)
    app.run()
    assert not app.exception, app.exception
    app.segmented_control(key=MODE_KEY).set_value(FILTERS)
    app.run()
    assert "profiled" not in app.session_state \
        or app.session_state["profiled"] == [], \
        "no condition selected, so nothing should be profiled"

    gid = app.session_state["syn:qbf_state:players"]["root"]["gid"]
    app.multiselect(key=f"syn:qbf:players:{gid}:cols") \
       .select("career_games")
    app.run()
    assert set(app.session_state["profiled"]) == {"career_games"}
