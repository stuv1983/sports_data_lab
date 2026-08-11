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
        QB.build_select("players; DROP TABLE players", ["player"], [], "AND",
                        None, False, 10, QB.ParamBag(),
                        known_tables={"players"}, known_columns={"player"})
    with pytest.raises(ValueError):
        QB.build_select("players", ["player) FROM sqlite_master --"], [],
                        "AND", None, False, 10, QB.ParamBag(),
                        known_tables={"players"}, known_columns={"player"})


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


# ---------------------------------------------------------------- assembly

def test_build_select_assembles_and_the_statement_actually_runs():
    """The assembled SQL is executed against a real (in-memory) SQLite
    table, because a quoting bug survives string assertions but not the
    parser."""
    bag = QB.ParamBag()
    predicates = [QB.in_clause("club", ["Fitzroy"], bag),
                  QB.between_clause("games", 10, 300, bag)]
    sql = QB.build_select(
        "players", ["player", "games"], predicates, "AND",
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
    sql = QB.build_select("players", ["player"], [], "AND", None, False,
                          10 ** 9, bag, known_tables={"players"},
                          known_columns={"player"})
    assert "LIMIT :p0" in sql
    assert bag.values["p0"] == QB.MAX_ROWS


def test_or_combines_and_anything_else_is_refused():
    bag = QB.ParamBag()
    predicates = [QB.equals_clause("club", "Fitzroy", bag),
                  QB.equals_clause("club", "Carlton", bag)]
    sql = QB.build_select("players", ["player"], predicates, "OR", None,
                          False, 10, bag, known_tables={"players"},
                          known_columns={"player", "club"})
    assert "OR" in sql
    with pytest.raises(ValueError):
        QB.build_select("players", ["player"], predicates, "AND --", None,
                        False, 10, QB.ParamBag(), known_tables={"players"},
                        known_columns={"player", "club"})


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
    assert fields["games"]["fieldSettings"] == {"min": 0.0, "max": 400.0}
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
