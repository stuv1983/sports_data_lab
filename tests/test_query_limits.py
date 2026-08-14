#!/usr/bin/env python3
"""Server-side resource bounds for both search systems.

Widget bounds are advice to a browser. Every ceiling here -- row limits,
per-condition value counts, scalar sizes, the global parameter budget,
free-form query size, numeric exactness -- must live in compiler code,
where a forged request cannot step around it.
"""

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

import core
import query_builder as QB
import query_filters as Q


# ------------------------------------------------------- parameter budget

def test_the_param_bag_enforces_the_global_budget():
    bag = QB.ParamBag()
    for i in range(QB.MAX_QUERY_PARAMS):
        bag.add(i)
    with pytest.raises(ValueError):
        bag.add("one too many")


def test_many_individually_legal_conditions_cannot_exceed_the_budget():
    """Each IN list obeys MAX_RULE_VALUES, but ten of them together must
    still hit the *global* wall inside the one shared bag."""
    bag = QB.ParamBag()
    spec = {"column": "goals", "kind": "integer", "op": "one of",
            "values": list(range(QB.MAX_RULE_VALUES))}
    with pytest.raises(ValueError):
        for _ in range(10):
            QB.compile_condition(spec, {"goals"}, bag)


def test_oversized_in_lists_fail_inside_the_compiler():
    """The cap must hold for token/component payloads that never touched
    a widget."""
    spec = {"column": "goals", "kind": "integer", "op": "one of",
            "values": list(range(QB.MAX_RULE_VALUES + 1))}
    with pytest.raises(ValueError):
        QB.compile_condition(spec, {"goals"}, QB.ParamBag())
    spec = {"column": "club", "kind": "text", "op": "one of",
            "values": [str(i) for i in range(QB.MAX_RULE_VALUES + 1)]}
    with pytest.raises(ValueError):
        QB.compile_condition(spec, {"club"}, QB.ParamBag())


def test_nested_and_hostile_value_shapes_are_refused():
    for values in ("stringy", {"a": 1}, [["nested"]], [{"d": 1}],
                   [float("inf")], [float("nan")],
                   ["A" * (QB.MAX_SCALAR_CHARS + 1)]):
        spec = {"column": "club", "kind": "text", "op": "one of",
                "values": values}
        with pytest.raises(ValueError):
            QB.compile_condition(spec, {"club"}, QB.ParamBag())


def test_oversized_scalars_are_refused_in_text_conditions():
    spec = {"column": "club", "kind": "text", "op": "contains",
            "value": "A" * (QB.MAX_SCALAR_CHARS + 1)}
    with pytest.raises(ValueError):
        QB.compile_condition(spec, {"club"}, QB.ParamBag())


def test_the_numeric_comma_list_is_capped():
    with pytest.raises(ValueError):
        QB.parse_number_list(",".join(
            str(i) for i in range(QB.MAX_RULE_VALUES + 2)))


# ------------------------------------------------------ numeric exactness

def test_integer_conditions_refuse_fractions_instead_of_truncating():
    """int(1.9) == 1 silently changed the question being asked."""
    spec = {"column": "games", "kind": "integer", "op": "=", "value": 1.9}
    with pytest.raises(ValueError):
        QB.compile_condition(spec, {"games"}, QB.ParamBag())
    # 1.0 is a whole number and passes; the bound value is exact.
    bag = QB.ParamBag()
    QB.compile_condition({"column": "games", "kind": "integer", "op": "=",
                          "value": 1.0}, {"games"}, bag)
    assert bag.values["p0"] == 1


@pytest.mark.parametrize("value,integer,expected", [
    (1, True, 1),
    ("1", True, 1),
    (1.0, True, 1),
    (2 ** 53 + 1, True, 2 ** 53 + 1),         # float would round this
    ("9007199254740993", False, 9007199254740993),
    (1.5, False, 1.5),
])
def test_coerce_number_is_exact(value, integer, expected):
    assert Q.coerce_number(value, integer=integer) == expected


@pytest.mark.parametrize("value,integer", [
    (1.9, True),
    ("abc", True),
    (float("nan"), False),
    (float("inf"), False),
    (float("-inf"), False),
    (2 ** 63, True),                 # one past SQLite's signed 64-bit
    (-(2 ** 63) - 1, True),
    ("1e100", False),                # would bind as an unbindable bigint
    (True, False),                   # a bool is not a number here
])
def test_coerce_number_refuses_the_unbindable(value, integer):
    with pytest.raises(ValueError):
        Q.coerce_number(value, integer=integer)


def test_free_form_numbers_ride_through_the_same_gate():
    con, schema = None, core.Schema(
        career_score="career_goals", career_postseason="finals_played",
        game_score="goals", stats=("goals",), clubs=("A",))
    with pytest.raises(Q.QuerySyntaxError):
        Q.compile_query(schema, "games>=1e100", con=con)
    sql, params, _ = Q.compile_query(schema,
                                     f"games>={2 ** 53 + 1}", con=con)
    assert 2 ** 53 + 1 in params      # exact, not float-rounded


# ------------------------------------------------- free-form query bounds

def test_the_free_form_parser_bounds_query_and_token_sizes():
    with pytest.raises(Q.QuerySyntaxError):
        Q.tokenize("x" * (Q.MAX_QUERY_CHARS + 1))
    with pytest.raises(Q.QuerySyntaxError):
        Q.tokenize(" ".join(f"a:{i}" for i in range(Q.MAX_QUERY_TOKENS + 1)))
    with pytest.raises(Q.QuerySyntaxError):
        Q.tokenize("club:" + "A" * (Q.MAX_TOKEN_CHARS + 1))
    with pytest.raises(Q.QuerySyntaxError):
        Q.tokenize(12345)             # not text at all
    # Malformed quoting stays a QuerySyntaxError, never a raw shlex error.
    with pytest.raises(Q.QuerySyntaxError):
        Q.tokenize('name:"unclosed')


def test_legitimate_queries_still_fit_the_bounds():
    tokens = Q.tokenize('club:"St Kilda" games>=100 sort:obscurity')
    assert tokens == ['club:St Kilda', 'games>=100', 'sort:obscurity']


# ------------------------------------------- the shared parameter budget

def _lineage_fixture():
    """A sport whose clubs carry lineage names, so one club token expands
    to several bound values -- the amplifier the character and token caps
    say nothing about."""
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE players (player_id INTEGER, player TEXT,
          debut_season INTEGER, final_season INTEGER, career_games INTEGER,
          career_goals INTEGER, finals_played INTEGER, clubs_hist TEXT,
          obscurity REAL);
        CREATE TABLE games (player_id INTEGER, season INTEGER,
          club_now TEXT, club_hist TEXT, goals INTEGER);
        INSERT INTO players VALUES (1,'A',1990,2000,10,5,0,'Bears',50.0);
    """)
    schema = core.Schema(
        career_score="career_goals", career_postseason="finals_played",
        game_score="goals", stats=("goals",), clubs=("Bears",),
        club_lineage={"Bears": ("Old Bears", "Older Bears")},
        required_games_cols=(), required_player_cols=())
    return con, schema


def test_the_free_form_language_enforces_the_parameter_budget():
    """Character and token caps do not imply a parameter cap: a club
    token binds two values per lineage identity, so a query inside every
    other bound compiled to far more values than one query may use --
    and the builder had refused that same count all along."""
    con, schema = _lineage_fixture()
    query = " ".join(['club:"Bears"'] * Q.MAX_QUERY_TOKENS)

    assert len(query) <= Q.MAX_QUERY_CHARS
    assert len(Q.tokenize(query)) <= Q.MAX_QUERY_TOKENS

    with pytest.raises(Q.QuerySyntaxError, match="bound values"):
        Q.compile_query(schema, query, con=con)


def test_a_query_just_inside_the_budget_still_compiles():
    """The bound refuses the excess, not the feature: a query that fits
    must still compile and carry every value it needs."""
    con, schema = _lineage_fixture()
    # Six bound values per token (3 identities, named twice for the UNION).
    tokens = Q.MAX_QUERY_PARAMS // 6 - 1
    sql, params, _ = Q.compile_query(
        schema, " ".join(['club:"Bears"'] * tokens), con=con)
    assert len(params) <= Q.MAX_QUERY_PARAMS
    assert params.count("Old Bears") == tokens * 2
    con.execute(sql, params).fetchall()      # SQLite accepts it


def test_both_search_systems_share_one_parameter_budget():
    """Two copies of a limit are two limits waiting to disagree, which is
    exactly what happened: the builder capped at 900 while the query
    language had no total bound at all."""
    assert QB.MAX_QUERY_PARAMS is Q.MAX_QUERY_PARAMS

    bag = QB.ParamBag()
    for _ in range(Q.MAX_QUERY_PARAMS):
        bag.add(1)
    with pytest.raises(ValueError):
        bag.add(1)
