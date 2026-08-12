#!/usr/bin/env python3
"""The query builder's promise is that nothing a reader supplies becomes
SQL text: identifiers must come from discovery, values ride as bound
parameters, and the visual mode's structured tree is compiled server-side
rather than its browser-built WHERE string being trusted. These tests hold
that promise with no database and no browser -- everything here is the
pure string-plus-params layer the page composes."""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---

import sqlite3

import pytest

import query_builder as QB


# ------------------------------------------------------------ type mapping

def test_declared_types_map_to_the_widget_kinds_that_edit_them():
    """The mapping follows SQLite affinity, with boolean and date claimed
    before the numeric substrings can grab them."""
    assert QB.type_kind("INTEGER") == "integer"
    assert QB.type_kind("BIGINT") == "integer"
    assert QB.type_kind("REAL") == "float"
    assert QB.type_kind("DECIMAL(10,2)") == "float"
    assert QB.type_kind("BOOLEAN") == "boolean"
    assert QB.type_kind("DATE") == "date"
    assert QB.type_kind("TIMESTAMP") == "datetime"
    assert QB.type_kind("DATETIME") == "datetime"
    assert QB.type_kind("VARCHAR(80)") == "text"
    assert QB.type_kind("") == "text"
    assert QB.type_kind(None) == "text"


# ------------------------------------------------------------- identifiers

def test_identifiers_are_quoted_and_embedded_quotes_doubled():
    assert QB._quote_ident("season") == '"season"'
    assert QB._quote_ident('we"ird') == '"we""ird"'


def test_unknown_identifiers_are_refused_not_quoted():
    """Quoting an attacker-chosen name would still leak it into SQL; the
    gate is membership in what discovery returned."""
    with pytest.raises(ValueError):
        QB.build_select("players; DROP TABLE players", ["player"], None,
                        None, False, 10, QB.ParamBag(),
                        known_tables={"players"}, known_columns={"player"})
    with pytest.raises(ValueError):
        QB.build_select("players", ["player) FROM sqlite_master --"], None,
                        None, False, 10, QB.ParamBag(),
                        known_tables={"players"}, known_columns={"player"})


def test_duplicate_display_and_group_columns_are_refused():
    """Duplicates can only come from forged widget state; ambiguity is an
    error, not a shrug."""
    with pytest.raises(ValueError):
        QB.build_select("players", ["player", "player"], None, None,
                        False, 10, QB.ParamBag(),
                        known_tables={"players"}, known_columns={"player"})
    with pytest.raises(ValueError):
        QB.build_group_select("players", ["club", "club"], None, 10,
                              QB.ParamBag(), known_tables={"players"},
                              known_columns={"club"})


# ------------------------------------------------------------------ values

def test_every_value_rides_as_a_bound_parameter_never_as_sql_text():
    bag = QB.ParamBag()
    clause = QB.in_clause("club", ["Fitzroy", "x' OR '1'='1"], bag)
    assert clause == '"club" IN (:p0, :p1)'
    assert bag.values == {"p0": "Fitzroy", "p1": "x' OR '1'='1"}


def test_like_escapes_the_readers_wildcards_but_keeps_ours():
    """A search for '100%' means the literal string, so % and _ are
    escaped; the surrounding wildcard is the builder's own."""
    bag = QB.ParamBag()
    clause = QB.like_clause("player", "100%_a", "contains", bag)
    assert clause == '"player" LIKE :p0 ESCAPE \'\\\''
    assert bag.values["p0"] == "%100\\%\\_a%"
    bag = QB.ParamBag()
    QB.like_clause("player", "Ab", "starts with", bag)
    assert bag.values["p0"] == "Ab%"


def test_between_binds_both_ends():
    bag = QB.ParamBag()
    clause = QB.between_clause("games", 50, 100, bag)
    assert clause == '"games" BETWEEN :p0 AND :p1'
    assert bag.values == {"p0": 50, "p1": 100}


def test_the_full_operator_set_compiles_parameterised():
    """=, !=, >, >=, <, <=: the operator comes from a fixed map and the
    value always rides as a parameter."""
    for label, sql_op in [("=", "="), ("!=", "<>"), (">", ">"),
                          (">=", ">="), ("<", "<"), ("<=", "<=")]:
        bag = QB.ParamBag()
        clause = QB.comparison_clause("goals", label, 500, bag)
        assert clause == f'"goals" {sql_op} :p0'
        assert bag.values == {"p0": 500}
    with pytest.raises(KeyError):
        QB.comparison_clause("goals", "; DROP TABLE", 1, QB.ParamBag())


def test_null_operators_take_no_parameters():
    assert QB.null_clause("height_cm", missing=True) == '"height_cm" IS NULL'
    assert QB.null_clause("height_cm", missing=False) == \
        '"height_cm" IS NOT NULL'


def test_ends_with_and_deliberate_wildcards():
    """"ends with" escapes the reader's wildcards like every LIKE mode;
    the pattern operator is the one place % and _ stay live -- and even
    there the pattern is a bound parameter, never SQL text."""
    bag = QB.ParamBag()
    QB.like_clause("player", "50%", "ends with", bag)
    assert bag.values["p0"] == "%50\\%"
    bag = QB.ParamBag()
    clause = QB.pattern_clause("player", "sm_th%", bag)
    assert clause == '"player" LIKE :p0'
    assert bag.values["p0"] == "sm_th%"


def test_a_numeric_list_parses_forgivingly():
    assert QB.parse_number_list("1, 5,10") == [1, 5, 10]
    assert QB.parse_number_list("2.5, junk, 7") == [2.5, 7]
    assert QB.parse_number_list("") == []


def test_group_select_counts_per_group_and_still_gates_identifiers():
    bag = QB.ParamBag()
    where = QB.comparison_clause("games", ">=", 100, bag)
    sql = QB.build_group_select(
        "players", ["club"], where, 50, bag,
        known_tables={"players"}, known_columns={"club", "games"})
    assert 'COUNT(*) AS total' in sql and 'GROUP BY "club"' in sql

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE players (player TEXT, club TEXT, games INT)")
    con.executemany("INSERT INTO players VALUES (?,?,?)",
                    [("A", "Fitzroy", 150), ("B", "Fitzroy", 200),
                     ("C", "Carlton", 250), ("D", "Carlton", 50)])
    rows = con.execute(sql, bag.values).fetchall()
    assert rows == [("Fitzroy", 2), ("Carlton", 1)]

    with pytest.raises(ValueError):
        QB.build_group_select("players", ["club) --"], None, 10,
                              QB.ParamBag(), known_tables={"players"},
                              known_columns={"club"})
    with pytest.raises(ValueError):
        QB.build_group_select("players", [], None, 10, QB.ParamBag(),
                              known_tables={"players"},
                              known_columns={"club"})


# ---------------------------------------------------------------- assembly

def test_build_select_assembles_and_the_statement_actually_runs():
    """The assembled SQL is executed against a real (in-memory) SQLite
    table, because a quoting bug survives string assertions but not the
    parser."""
    bag = QB.ParamBag()
    where = QB.compile_condition_node(
        {"type": "group", "op": "AND", "children": [
            {"column": "club", "kind": "text", "op": "one of",
             "values": ["Fitzroy"]},
            {"column": "games", "kind": "integer", "op": "between",
             "lo": 10, "hi": 300},
        ]}, {"club", "games"}, bag)
    sql = QB.build_select(
        "players", ["player", "games"], where,
        "games", True, 5, bag,
        known_tables={"players"}, known_columns={"player", "games", "club"})

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE players (player TEXT, club TEXT, games INT)")
    con.executemany("INSERT INTO players VALUES (?,?,?)",
                    [("A", "Fitzroy", 150), ("B", "Carlton", 200),
                     ("C", "Fitzroy", 5)])
    rows = con.execute(sql, bag.values).fetchall()
    assert rows == [("A", 150)]           # club and range filters both bit


def test_the_row_limit_is_bound_and_capped():
    bag = QB.ParamBag()
    sql = QB.build_select("players", ["player"], None, None, False,
                          10 ** 9, bag, known_tables={"players"},
                          known_columns={"player"})
    assert "LIMIT :p0" in sql
    assert bag.values["p0"] == QB.MAX_ROWS


def test_a_negative_or_malformed_limit_cannot_reach_sqlite():
    """SQLite reads LIMIT -1 as *unlimited*: the exact opposite of a
    ceiling. The widget's min is browser advice; the compiler clamps."""
    for hostile in (-1, 0):
        bag = QB.ParamBag()
        QB.build_select("players", ["player"], None, None, False,
                        hostile, bag,
                        known_tables={"players"}, known_columns={"player"})
        assert bag.values["p0"] == 1
    assert QB._bounded_limit(-1) == 1
    assert QB._bounded_limit(0) == 1
    assert QB._bounded_limit(250) == 250
    assert QB._bounded_limit(QB.MAX_ROWS + 1) == QB.MAX_ROWS
    assert QB._bounded_limit(10 ** 30) == QB.MAX_ROWS
    for junk in ("abc", None, 1.5, float("nan"), float("inf"), [10], True):
        with pytest.raises(ValueError):
            QB._bounded_limit(junk)


def test_nested_condition_groups_compile_with_explicit_parentheses():
    """(A AND B) OR (C AND D) and deeper shapes are first-class."""
    bag = QB.ParamBag()
    clause = QB.compile_condition_node(
        {"type": "group", "op": "OR", "children": [
            {"type": "group", "op": "AND", "children": [
                {"column": "club", "kind": "text", "op": "equals",
                 "value": "Fitzroy"},
                {"column": "games", "kind": "integer", "op": "≥",
                 "value": 150},
            ]},
            {"type": "group", "op": "AND", "children": [
                {"column": "club", "kind": "text", "op": "equals",
                 "value": "Carlton"},
                {"column": "games", "kind": "integer", "op": "≥",
                 "value": 200},
            ]},
        ]}, {"club", "games"}, bag)
    assert clause == ('(("club" = :p0 AND "games" >= :p1) OR '
                      '("club" = :p2 AND "games" >= :p3))')

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE players (player TEXT, club TEXT, games INT)")
    con.executemany("INSERT INTO players VALUES (?,?,?)",
                    [("A", "Fitzroy", 150), ("B", "Carlton", 200),
                     ("C", "Fitzroy", 5), ("D", "Carlton", 50)])
    rows = con.execute(
        f"SELECT player FROM players WHERE {clause} ORDER BY player",
        bag.values).fetchall()
    assert [r[0] for r in rows] == ["A", "B"]


def test_condition_groups_nest_three_deep_and_are_bounded():
    """A AND (B OR (C AND D)) compiles; hostile shapes are ValueError."""
    bag = QB.ParamBag()
    clause = QB.compile_condition_node(
        {"type": "group", "op": "AND", "children": [
            {"column": "games", "kind": "integer", "op": "≥", "value": 1},
            {"type": "group", "op": "OR", "children": [
                {"column": "club", "kind": "text", "op": "equals",
                 "value": "A"},
                {"type": "group", "op": "AND", "children": [
                    {"column": "club", "kind": "text", "op": "equals",
                     "value": "B"},
                    {"column": "games", "kind": "integer", "op": "≥",
                     "value": 100},
                ]},
            ]},
        ]}, {"club", "games"}, bag)
    assert clause == ('("games" >= :p0 AND ("club" = :p1 OR '
                      '("club" = :p2 AND "games" >= :p3)))')

    with pytest.raises(ValueError):
        QB.compile_condition_node({"type": "group", "op": "NAND",
                                   "children": []}, {"club"}, QB.ParamBag())
    with pytest.raises(ValueError):
        QB.compile_condition_node({"type": "group", "op": "AND",
                                   "children": "xx"}, {"club"},
                                  QB.ParamBag())
    with pytest.raises(ValueError):
        QB.compile_condition_node(["not", "a", "node"], {"club"},
                                  QB.ParamBag())

    deep = {"column": "games", "kind": "integer", "op": "≥", "value": 1}
    for _ in range(QB.MAX_GROUP_DEPTH + 1):
        deep = {"type": "group", "op": "AND", "children": [deep]}
    with pytest.raises(ValueError):
        QB.compile_condition_node(deep, {"games"}, QB.ParamBag())

    wide = {"type": "group", "op": "AND", "children": [
        {"column": "games", "kind": "integer", "op": "≥", "value": 1}
        for _ in range(QB.MAX_GROUP_CHILDREN + 1)]}
    with pytest.raises(ValueError):
        QB.compile_condition_node(wide, {"games"}, QB.ParamBag())


# ------------------------------------------------------------- visual mode

def test_the_tree_config_is_generated_from_discovery_not_hardcoded():
    cols = (QB.Column("games", "INTEGER", "integer"),
            QB.Column("club", "TEXT", "text"),
            QB.Column("weird name", "TEXT", "text"))
    profiles = {"games": {"lo": 0, "hi": 400},
                "club": {"values": ["Carlton", "Fitzroy"]}}
    config = QB.condition_tree_config(cols, profiles)
    fields = config["fields"]
    assert fields["games"]["type"] == "number"
    # No min/max on numeric fields: the observed extremes are a fact about
    # yesterday's data, not a cap on what may be asked -- bounding at
    # max(goals) forbade the question "what if someone kicks more".
    assert "fieldSettings" not in fields["games"]
    # Low-cardinality text upgrades to a select of the real values.
    assert fields["club"]["type"] == "select"
    assert {v["value"] for v in fields["club"]["fieldSettings"]["listValues"]} \
        == {"Carlton", "Fitzroy"}
    # A name the component would emit unquotable stays out of the config.
    assert "weird name" not in fields


def _rule(field, operator, *values):
    return {"type": "rule", "properties": {
        "field": field, "operator": operator, "value": list(values)}}


def _group(conjunction, *children, negate=False):
    return {"type": "group",
            "properties": {"conjunction": conjunction, "not": negate},
            "children1": list(children)}


def test_the_tree_is_compiled_server_side_with_every_value_bound():
    """The component's own SQL string is never executed; its structured
    tree is walked here, and even a hostile value stays a parameter."""
    bag = QB.ParamBag()
    tree = _group("AND",
                  _rule("games", "greater_or_equal", 100),
                  _rule("club", "equal", "x'; DROP TABLE players; --"))
    clause = QB.compile_tree_node(tree, {"games", "club"}, bag)
    assert clause == '("games" >= :p0 AND "club" = :p1)'
    assert bag.values == {"p0": 100, "p1": "x'; DROP TABLE players; --"}

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE players (player TEXT, club TEXT, games INT)")
    con.execute("INSERT INTO players VALUES ('A', 'Fitzroy', 150)")
    # The clause runs, binds, and the hostile text matched nothing.
    assert con.execute(f"SELECT * FROM players WHERE {clause}",
                       bag.values).fetchall() == []


def test_tree_operators_cover_ranges_lists_likes_and_negation():
    bag = QB.ParamBag()
    tree = _group("OR",
                  _rule("games", "between", 50, 100),
                  _rule("club", "select_any_in", ["Fitzroy", "Carlton"]),
                  _rule("player", "starts_with", "100%"),
                  _group("AND", _rule("club", "is_null"), negate=True))
    clause = QB.compile_tree_node(tree, {"games", "club", "player"}, bag)
    assert '"games" BETWEEN :p0 AND :p1' in clause
    assert '"club" IN (:p2, :p3)' in clause
    assert "ESCAPE" in clause and bag.values["p4"] == "100\\%%"
    assert 'NOT ("club" IS NULL)' in clause


def test_a_tree_field_outside_discovery_is_refused_not_quoted():
    with pytest.raises(ValueError):
        QB.compile_tree_node(
            _group("AND", _rule("games) FROM sqlite_master --", "equal", 1)),
            {"games"}, QB.ParamBag())


def test_an_operator_the_walker_does_not_know_is_refused():
    """An unknown operator must become a red box, never a pass-through."""
    with pytest.raises(ValueError):
        QB.compile_tree_node(
            _group("AND", _rule("games", "proximity", 1)),
            {"games"}, QB.ParamBag())
    with pytest.raises(ValueError):
        QB.compile_tree_node(
            {"type": "group",
             "properties": {"conjunction": "AND; DROP TABLE players"},
             "children1": [_rule("games", "equal", 1)]},
            {"games"}, QB.ParamBag())


def test_a_half_built_tree_filters_nothing_instead_of_erroring():
    bag = QB.ParamBag()
    incomplete = _group("AND",
                        {"type": "rule", "properties": {"field": "games"}},
                        _rule("games", "equal", None))
    assert QB.compile_tree_node(incomplete, {"games"}, bag) is None
    assert bag.values == {}


def test_the_wire_format_the_component_actually_sends_compiles():
    """streamlit_condition_tree 0.3 does not hand Python the builder's own
    export: its frontend strips node ids and renames `children1` to
    `children` before setComponentValue. A walker that only reads
    `children1` sees every group as empty and silently compiles no WHERE
    at all -- the regression where the visual tree filtered nothing."""
    tree = {  # verbatim shape from st.session_state[key], v0.3.0
        "type": "group",
        "properties": {"conjunction": "AND", "not": True},
        "children": [
            {"type": "rule", "properties": {
                "field": "birth_year", "operator": "between",
                "value": [1983, 1985],
                "valueSrc": ["value", "value"],
                "valueType": ["number", "number"]}},
        ],
    }
    QB.validate_tree(tree)
    bag = QB.ParamBag()
    clause = QB.compile_tree_node(tree, {"birth_year"}, bag)
    assert clause == 'NOT ("birth_year" BETWEEN :p0 AND :p1)'
    assert bag.values == {"p0": 1983, "p1": 1985}

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE players (player TEXT, birth_year INT)")
    con.executemany("INSERT INTO players VALUES (?, ?)",
                    [("Scott Lee", 1963), ("Hayden Skipworth", 1983),
                     ("Ryan Lonie", 1983), ("Journey Mann", 1990)])
    rows = con.execute(
        f"SELECT player FROM players WHERE {clause} ORDER BY player",
        bag.values).fetchall()
    assert [r[0] for r in rows] == ["Journey Mann", "Scott Lee"]


def test_wire_format_groups_nest_and_id_keyed_children_still_compile():
    """Nested groups arrive in the same renamed shape; older component
    versions ship raw exports where children1 is an id-keyed object.
    Both must keep compiling."""
    bag = QB.ParamBag()
    nested = {"type": "group",
              "properties": {"conjunction": "OR"},
              "children": [
                  {"type": "rule", "properties": {
                      "field": "goals", "operator": "greater",
                      "value": [50]}},
                  {"type": "group",
                   "properties": {"conjunction": "AND"},
                   "children": [
                       {"type": "rule", "properties": {
                           "field": "club", "operator": "select_equals",
                           "value": ["Fitzroy"]}},
                   ]},
              ]}
    clause = QB.compile_tree_node(nested, {"goals", "club"}, bag)
    assert clause == '("goals" > :p0 OR ("club" = :p1))'

    keyed = {"type": "group", "properties": {"conjunction": "AND"},
             "children1": {"a1b2": {"type": "rule", "properties": {
                 "field": "goals", "operator": "equal", "value": [3]}}}}
    assert QB.compile_tree_node(keyed, {"goals"}, QB.ParamBag()) \
        == '("goals" = :p0)'


# ------------------------------------------------- grid-constraint mode

def _constraint_fixture():
    import core

    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE players (
          player_id INTEGER, player TEXT, career_games INTEGER
        );
        CREATE TABLE games (player_id INTEGER, club_now TEXT);
        INSERT INTO players VALUES (1,'Alpha',200), (2,'Beta',50),
                                   (3,'Gamma',120), (4,'Delta',300);
        INSERT INTO games VALUES (1,'A'), (2,'A'), (3,'B'), (4,'C');
    """)
    return con, core.Schema(career_score="career_goals", career_postseason="finals_played", game_score="goals")


def _played(club):
    return ("SELECT player_id FROM games WHERE club_now = ?", [club])


def _games_min(n):
    return ("SELECT player_id FROM players WHERE career_games >= ?", [n])


def test_constraint_sets_or_within_a_group_and_between_groups():
    """(played A OR played B) AND 100+ games -- the use case the mode
    exists for, with parameter order following clause order."""
    con, schema = _constraint_fixture()
    where, params = QB.compile_constraint_sets(
        schema, [[_played("A"), _played("B")], [_games_min(100)]])
    rows = con.execute(
        f"SELECT p.player FROM players p WHERE {where} ORDER BY p.player",
        params).fetchall()
    assert [r[0] for r in rows] == ["Alpha", "Gamma"]


def test_all_and_constraint_sets_use_the_engine_intersection():
    """Single-member groups compile through core._where, so the MLB
    team-and-season pairing rule keeps applying to plain AND chains."""
    con, schema = _constraint_fixture()
    where, params = QB.compile_constraint_sets(
        schema, [[_played("A")], [_games_min(100)]])
    rows = con.execute(
        f"SELECT p.player FROM players p WHERE {where}", params).fetchall()
    assert [r[0] for r in rows] == ["Alpha"]


def test_a_row_marked_constraint_survives_an_or_group():
    """MLB's played_for emits a row-scoped fragment; inside an OR group it
    must compile to the EXISTS-style membership, not raw marker text."""
    import core

    con, schema = _constraint_fixture()
    marked = (f"{core.ROW_MARKER}team@g.club_now = ?", ["C"])
    where, params = QB.compile_constraint_sets(
        schema, [[marked, _played("B")]])
    assert core.ROW_MARKER not in where
    rows = con.execute(
        f"SELECT p.player FROM players p WHERE {where} ORDER BY p.player",
        params).fetchall()
    assert [r[0] for r in rows] == ["Delta", "Gamma"]


# ------------------------------------------------- share-token and tree bounds
#
# The token rides in a URL and the visual tree rides in a websocket message,
# so both are attacker-shaped inputs. These tests hold the resource bounds:
# no decompression bomb, no deep recursion, no node flood, and no scalar
# that downstream iteration could amplify into millions of objects.

def test_a_share_token_round_trips():
    payload = {"table": "players",
               "groups": [{"match": "AND", "conditions": [
                   {"column": "goals", "kind": "integer", "op": ">=",
                    "value": 30}]}]}
    assert QB.deserialize_state(QB.serialize_state(payload)) == payload


def test_a_decompression_bomb_dies_as_a_value_error_not_as_memory():
    import base64
    import zlib

    bomb = base64.urlsafe_b64encode(zlib.compress(
        b'{"a":"' + b"A" * (30 * QB.MAX_STATE_BYTES) + b'"}', 9)).decode()
    with pytest.raises(ValueError):
        QB.deserialize_state(bomb)


def test_an_oversized_compressed_token_is_refused_before_zlib_sees_it():
    with pytest.raises(ValueError):
        QB.deserialize_state("A" * (QB.MAX_TOKEN_CHARS + 1))


def test_a_deeply_nested_payload_is_refused():
    deep = node = {"table": "t"}
    for _ in range(QB.MAX_TREE_DEPTH + 5):
        node["groups"] = [{}]
        node = node["groups"][0]
    with pytest.raises(ValueError):
        QB.deserialize_state(QB.serialize_state(deep))


def test_a_node_flood_is_refused():
    with pytest.raises(ValueError):
        QB.validate_tree({"values": list(range(QB.MAX_TREE_NODES + 1))})


def test_an_oversized_scalar_is_refused_even_as_a_single_node():
    """One 100 MB string is one 'node'; a node count alone would pass it,
    and list()/join()/LIKE-escaping downstream would amplify it."""
    with pytest.raises(ValueError):
        QB.validate_tree({"x": "A" * (QB.MAX_SCALAR_CHARS + 1)})


def test_string_children_are_refused_not_exploded_into_characters():
    """list("x" * N) is N single-character nodes -- the amplification the
    review demonstrated. The shape is enforced, never coerced."""
    hostile = {"type": "group", "children1": "x" * 1000}
    with pytest.raises(ValueError):
        QB.compile_tree_node(hostile, {"player"}, QB.ParamBag())
    # The component's renamed key is bound by the same wall.
    hostile = {"type": "group", "children": "x" * 1000}
    with pytest.raises(ValueError):
        QB.compile_tree_node(hostile, {"player"}, QB.ParamBag())


def test_a_string_rule_value_is_refused_not_exploded():
    hostile = {"type": "rule", "properties": {
        "field": "player", "operator": "select_any_in",
        "value": "not-a-list"}}
    with pytest.raises(ValueError):
        QB.compile_tree_node(hostile, {"player"}, QB.ParamBag())


def test_a_rule_value_flood_is_refused():
    hostile = {"type": "rule", "properties": {
        "field": "player", "operator": "select_any_in",
        "value": [list(range(QB.MAX_RULE_VALUES + 1))]}}
    with pytest.raises(ValueError):
        QB.compile_tree_node(hostile, {"player"}, QB.ParamBag())


# ------------------------------------------------------------ date operators
#
# Every build stores dates as TEXT; ISO-8601 text compares correctly as
# plain strings, so the compiled predicates keep the bare column on the
# left and each operator means exactly what it says -- including the
# strict after/before the vocabulary used to lack entirely.

def _date_rows():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE games (player TEXT, date TEXT)")
    con.executemany("INSERT INTO games VALUES (?, ?)", [
        ("early", "2020-06-01"), ("on_day", "2020-06-15"),
        ("late", "2020-06-30"), ("missing", None)])
    return con


def _date_matches(con, op, value=None, lo=None, hi=None, ceiling=False):
    spec = {"column": "date", "kind": "date", "op": op,
            "day_ceiling": ceiling}
    if value is not None:
        spec["value"] = value
    if lo is not None:
        spec.update(lo=lo, hi=hi)
    bag = QB.ParamBag()
    clause = QB.compile_condition(spec, {"date"}, bag)
    rows = con.execute(
        f"SELECT player FROM games WHERE {clause} ORDER BY player",
        bag.values).fetchall()
    return [r[0] for r in rows]


def test_text_backed_iso_dates_answer_every_operator():
    con = _date_rows()
    assert _date_matches(con, "on", "2020-06-15") == ["on_day"]
    assert _date_matches(con, "after", "2020-06-15") == ["late"]
    assert _date_matches(con, "before", "2020-06-15") == ["early"]
    assert _date_matches(con, "on or after", "2020-06-15") == \
        ["late", "on_day"]
    assert _date_matches(con, "on or before", "2020-06-15") == \
        ["early", "on_day"]
    assert _date_matches(con, "between", lo="2020-06-10",
                         hi="2020-06-20") == ["on_day"]
    assert _date_matches(con, "is missing") == ["missing"]
    assert _date_matches(con, "is present") == \
        ["early", "late", "on_day"]


def test_strict_after_and_before_shift_correctly_on_datetime_columns():
    """"after the 15th" must exclude 14:30 *on* the 15th; "before" already
    excludes every moment of the day by plain string comparison."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE games (player TEXT, date TEXT)")
    con.executemany("INSERT INTO games VALUES (?, ?)", [
        ("mid_day", "2020-06-15T14:30:00"),
        ("next_day", "2020-06-16T09:00:00")])
    assert _date_matches(con, "after", "2020-06-15",
                         ceiling=True) == ["next_day"]
    assert _date_matches(con, "before", "2020-06-16",
                         ceiling=True) == ["mid_day"]
    assert _date_matches(con, "on", "2020-06-15",
                         ceiling=True) == ["mid_day"]


def test_no_date_predicate_wraps_the_column_in_a_function():
    bag = QB.ParamBag()
    for op in ("on", "after", "before", "on or after", "on or before"):
        clause = QB.compile_condition(
            {"column": "date", "kind": "date", "op": op,
             "value": "2020-06-15"}, {"date"}, bag)
        assert "DATE(" not in clause and clause.startswith('"date"')


def test_kind_overrides_reach_discovery():
    """A sport's declared truth beats the DDL's TEXT/INTEGER claims."""
    assert QB.type_kind("TEXT") == "text"
    # The override tuple shape discover_schema consumes:
    overrides = dict((("games.date", "date"), ("games.is_final",
                                               "boolean")))
    assert overrides.get("games.date", QB.type_kind("TEXT")) == "date"
    assert overrides.get("games.other", QB.type_kind("TEXT")) == "text"


# --------------------------------------------- hostile tree shapes / kinds

def test_hostile_tree_shapes_become_value_errors_not_type_errors():
    """A list-valued properties/field used to leak AttributeError and
    TypeError past page()'s ValueError handling."""
    with pytest.raises(ValueError):
        QB.compile_tree_node([1, 2], {"games"}, QB.ParamBag())
    with pytest.raises(ValueError):
        QB.compile_tree_node({"type": "rule", "properties": ["x"]},
                             {"games"}, QB.ParamBag())
    with pytest.raises(ValueError):
        QB.compile_tree_node(
            {"type": "rule", "properties": {
                "field": ["games"], "operator": "equal", "value": [1]}},
            {"games"}, QB.ParamBag())
    with pytest.raises(ValueError):
        QB.compile_tree_node(
            {"type": "rule", "properties": {
                "field": "games", "operator": ["equal"], "value": [1]}},
            {"games"}, QB.ParamBag())


def test_tree_operators_are_gated_by_column_kind_when_metadata_given():
    """LIKE on a number or arithmetic on text is a doctored payload."""
    columns = {"games": QB.Column("games", "INTEGER", "integer"),
               "club": QB.Column("club", "TEXT", "text")}
    with pytest.raises(ValueError):
        QB.compile_tree_node(_rule("games", "like", "abc"), columns,
                             QB.ParamBag())
    with pytest.raises(ValueError):
        QB.compile_tree_node(_rule("club", "greater_or_equal", 5),
                             columns, QB.ParamBag())
    # The same operators stay legal on the kinds they belong to.
    assert QB.compile_tree_node(_rule("club", "like", "abc"), columns,
                                QB.ParamBag())
    assert QB.compile_tree_node(_rule("games", "greater_or_equal", 5),
                                columns, QB.ParamBag())


def test_is_empty_no_longer_wraps_the_column_in_coalesce():
    bag = QB.ParamBag()
    clause = QB.compile_tree_node(_rule("club", "is_empty"), {"club"}, bag)
    assert "COALESCE" not in clause
    assert clause == '("club" IS NULL OR "club" = :p0)'

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE players (player TEXT, club TEXT)")
    con.executemany("INSERT INTO players VALUES (?, ?)",
                    [("A", "Fitzroy"), ("B", ""), ("C", None)])
    rows = con.execute(
        f"SELECT player FROM players WHERE {clause} ORDER BY player",
        bag.values).fetchall()
    assert [r[0] for r in rows] == ["B", "C"]

    bag = QB.ParamBag()
    clause = QB.compile_tree_node(_rule("club", "is_not_empty"),
                                  {"club"}, bag)
    rows = con.execute(
        f"SELECT player FROM players WHERE {clause}", bag.values).fetchall()
    assert [r[0] for r in rows] == ["A"]


def test_a_legitimate_tree_still_compiles_under_the_bounds():
    tree = {"type": "group", "properties": {"conjunction": "AND"},
            "children1": [
                {"type": "rule", "properties": {
                    "field": "player", "operator": "select_any_in",
                    "value": [["Alpha", "Beta"]]}},
                {"type": "rule", "properties": {
                    "field": "goals", "operator": "greater",
                    "value": [5]}},
            ]}
    QB.validate_tree(tree)
    bag = QB.ParamBag()
    clause = QB.compile_tree_node(tree, {"player", "goals"}, bag)
    assert '"player" IN' in clause and '"goals" >' in clause
    assert len(bag.values) == 3


# ------------------------------------------------- fail-closed tree values

@pytest.mark.parametrize("bad_member", [
    {"bad": 1}, ["nested"], ("nested",), None, float("inf"),
])
def test_tree_in_rejects_a_malformed_member_outright(bad_member):
    """["A", <bad>] refuses the whole rule. The predecessor filtered the
    bad member out, quietly turning the payload into IN ("A") -- a
    different query than the one specified -- and no parameter may be
    left behind by the failed rule."""
    bag = QB.ParamBag()
    tree = _rule("player", "select_any_in", ["A", bad_member])
    with pytest.raises(ValueError):
        QB.compile_tree_node(tree, {"player"}, bag)
    assert bag.values == {}


def _kinded_columns():
    return {"games": QB.Column("games", "INTEGER", "integer"),
            "average": QB.Column("average", "REAL", "float"),
            "club": QB.Column("club", "TEXT", "text"),
            "is_final": QB.Column("is_final", "INTEGER", "boolean"),
            "date": QB.Column("date", "TEXT", "date"),
            "updated_at": QB.Column("updated_at", "TEXT", "datetime")}


@pytest.mark.parametrize("tree", [
    _rule("games", "equal", 1.9),               # fraction into integer
    _rule("games", "greater", "abc"),           # text into a number
    _rule("games", "equal", True),              # bool is not a count
    _rule("average", "between", "low", "high"),  # text range on a float
    _rule("date", "equal", "15/06/2020"),       # not ISO
    _rule("date", "equal", "2020-13-45"),       # impossible ISO
    _rule("updated_at", "greater", "not a time"),
    _rule("is_final", "equal", "yes"),          # non-boolean flag value
    _rule("is_final", "equal", 1),              # even 1: true/false only
    _rule("club", "equal", "A" * (QB.MAX_SCALAR_CHARS + 1)),
])
def test_tree_values_are_typed_by_column_kind(tree):
    """The operator allowlist gates the operator; these gate the value.
    A value that cannot mean what the column stores is refused, never
    bound to compare wrongly -- and the failed rule leaves nothing in
    the bag."""
    bag = QB.ParamBag()
    with pytest.raises(ValueError):
        QB.compile_tree_node(tree, _kinded_columns(), bag)
    assert bag.values == {}


def test_tree_values_bind_in_storage_form_once_coerced():
    columns = _kinded_columns()

    bag = QB.ParamBag()
    QB.compile_tree_node(_rule("is_final", "equal", True), columns, bag)
    assert bag.values == {"p0": 1}      # flags are stored as 0/1

    bag = QB.ParamBag()
    QB.compile_tree_node(_rule("games", "equal", 100.0), columns, bag)
    assert bag.values == {"p0": 100}    # integral float -> exact int

    bag = QB.ParamBag()
    QB.compile_tree_node(_rule("date", "between", "2020-06-01",
                               "2020-06-30"), columns, bag)
    assert bag.values == {"p0": "2020-06-01", "p1": "2020-06-30"}

    bag = QB.ParamBag()
    QB.compile_tree_node(
        _rule("updated_at", "greater", "2020-06-15T14:30:00Z"),
        columns, bag)
    assert bag.values == {"p0": "2020-06-15T14:30:00Z"}
