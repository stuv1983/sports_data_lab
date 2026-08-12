#!/usr/bin/env python3
"""The recursive Boolean model both builders compile through.

The old grid mode was structurally two-level -- AND between cards, OR
inside one -- and its fast path applied the Immaculate Grid same-row
pairing only when *every* card was a singleton, so one unrelated OR card
silently split "100 RBIs for Cleveland" into two independent facts. These
tests hold the replacement: arbitrary bounded nesting for grid criteria
and table filters alike, pairing preserved inside every AND branch, and
no serialized payload able to smuggle SQL through a leaf.
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


def _schema():
    return core.Schema(career_score="career_goals",
                       career_postseason="finals_played",
                       game_score="goals")


def _con():
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE players (
          player_id INTEGER, player TEXT, career_games INTEGER
        );
        CREATE TABLE games (player_id INTEGER, club_now TEXT, goals INT);
        INSERT INTO players VALUES (1,'Alpha',200), (2,'Beta',50),
                                   (3,'Gamma',120), (4,'Delta',300);
        INSERT INTO games VALUES (1,'A',120), (2,'A',10), (3,'B',50),
                                 (4,'C',200);
    """)
    return con


class _FakeConstraints:
    """A builder catalogue shaped exactly like a sport's BUILDERS."""

    BUILDERS = {
        "played for": (
            lambda club: ("SELECT player_id FROM games "
                          "WHERE club_now = ?", [club]), ["club"]),
        "games min": (
            lambda n: ("SELECT player_id FROM players "
                       "WHERE career_games >= ?", [n]), ["n"]),
        "row team": (
            lambda club: (core.ROW_MARKER + "team@g.club_now = ?",
                          [club]), ["club"]),
        "row stat": (
            lambda n: (core.ROW_MARKER + "stat@g.goals >= ?", [n]), ["n"]),
    }


class _FakeSport:
    C = _FakeConstraints()

    class vocab:                       # builder_label only reads attributes
        games = "games"
        score = "goals"
        title = "premiership"
        club = "club"
        postseason_one = "final"


def _resolve(kind, args):
    return QB._resolve_criterion(_FakeSport, kind, args)


def _crit(kind, *args):
    return {"type": "criterion", "kind": kind, "args": list(args)}


def _group(op, *children):
    return {"type": "group", "op": op, "children": list(children)}


# --------------------------------------------------------- Boolean shapes

def test_or_of_ands_compiles_and_answers_correctly():
    """(played A AND 100+ games) OR (played C AND 250+ games)."""
    con = _con()
    ast = _group("OR",
                 _group("AND", _crit("played for", "A"),
                        _crit("games min", 100)),
                 _group("AND", _crit("played for", "C"),
                        _crit("games min", 250)))
    where, params = QB.compile_constraint_ast(_schema(), ast, _resolve)
    rows = con.execute(
        f"SELECT player FROM players p WHERE {where} ORDER BY player",
        params).fetchall()
    assert [r[0] for r in rows] == ["Alpha", "Delta"]


def test_three_level_nesting_compiles():
    """A AND (B OR (C AND D)) -- nesting is arbitrary, not two-level."""
    con = _con()
    ast = _group("AND",
                 _crit("games min", 100),
                 _group("OR",
                        _crit("played for", "B"),
                        _group("AND", _crit("played for", "C"),
                               _crit("games min", 250))))
    where, params = QB.compile_constraint_ast(_schema(), ast, _resolve)
    rows = con.execute(
        f"SELECT player FROM players p WHERE {where} ORDER BY player",
        params).fetchall()
    assert [r[0] for r in rows] == ["Delta", "Gamma"]


def test_same_row_pairing_survives_an_unrelated_or_group():
    """The regression: row-marked team+stat criteria must stay one
    games-row predicate even when another card holds OR alternatives."""
    ast = _group("AND",
                 _crit("row team", "A"),
                 _crit("row stat", 100),
                 _group("OR", _crit("games min", 1),
                        _crit("games min", 2)))
    where, params = QB.compile_constraint_ast(_schema(), ast, _resolve)
    assert core.ROW_MARKER not in where
    assert "g.club_now = ? AND g.goals >= ?" in where, where

    con = _con()
    rows = con.execute(
        f"SELECT player FROM players p WHERE {where} ORDER BY player",
        params).fetchall()
    # Alpha kicked 120 for A; Beta played A but only kicked 10 there.
    assert [r[0] for r in rows] == ["Alpha"]


def test_pairing_applies_per_and_branch_not_globally():
    """Each AND branch pairs its own row-marked leaves independently."""
    ast = _group("OR",
                 _group("AND", _crit("row team", "A"),
                        _crit("row stat", 100)),
                 _group("AND", _crit("row team", "C"),
                        _crit("row stat", 150)))
    where, params = QB.compile_constraint_ast(_schema(), ast, _resolve)
    con = _con()
    rows = con.execute(
        f"SELECT player FROM players p WHERE {where} ORDER BY player",
        params).fetchall()
    assert [r[0] for r in rows] == ["Alpha", "Delta"]


def test_flat_compatibility_wrapper_keeps_pairing_with_or_groups():
    """compile_constraint_sets is the two-level case of the AST; its old
    fast path lost pairing the moment any group held alternatives."""
    schema = _schema()
    team = (core.ROW_MARKER + "team@g.club_now = ?", ["A"])
    stat = (core.ROW_MARKER + "stat@g.goals >= ?", [100])
    other = ("SELECT player_id FROM players WHERE career_games >= ?", [1])
    other2 = ("SELECT player_id FROM players WHERE career_games >= ?", [2])

    where, params = QB.compile_constraint_sets(
        schema, [[team], [stat], [other, other2]])
    assert "g.club_now = ? AND g.goals >= ?" in where, where

    con = _con()
    rows = con.execute(
        f"SELECT player FROM players p WHERE {where}", params).fetchall()
    assert [r[0] for r in rows] == ["Alpha"]


# ------------------------------------- pairing across nested boundaries

def _crossrow_con():
    """Beta is the DNF regression's false positive: an A row with 10
    goals and a separate C row with 150, so any compilation that lets the
    team and the statistic come from different rows accepts Beta."""
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE players (player_id INTEGER, player TEXT,
                              career_games INTEGER);
        CREATE TABLE games (player_id INTEGER, club_now TEXT, goals INT);
        INSERT INTO players VALUES (1,'Alpha',200), (2,'Beta',50);
        INSERT INTO games VALUES (1,'A',120), (2,'A',10), (2,'C',150);
    """)
    return con


def _players(con, where, params):
    return [r[0] for r in con.execute(
        f"SELECT player FROM players p WHERE {where} ORDER BY player",
        params)]


def test_pairing_holds_when_stats_sit_inside_a_nested_or():
    """team A AND (stat >= 100 OR stat >= 200): the audit's case. Before
    DNF this compiled the OR subgroup standalone, so Beta's 150 goals for
    C satisfied the statistic while the A membership came from another
    row entirely."""
    ast = _group("AND", _crit("row team", "A"),
                 _group("OR", _crit("row stat", 100),
                        _crit("row stat", 200)))
    where, params = QB.compile_constraint_ast(_schema(), ast, _resolve)
    assert core.ROW_MARKER not in where
    # Both DNF branches carry the pairing inside one row predicate.
    assert where.count("g.club_now = ? AND g.goals >= ?") == 2, where
    assert _players(_crossrow_con(), where, params) == ["Alpha"]


def test_pairing_holds_when_teams_sit_inside_a_nested_or():
    """(team A OR team C) AND stat >= 140 distributes to two paired
    branches: Beta's 150 for C matches, Alpha's 120 for A does not."""
    ast = _group("AND",
                 _group("OR", _crit("row team", "A"),
                        _crit("row team", "C")),
                 _crit("row stat", 140))
    where, params = QB.compile_constraint_ast(_schema(), ast, _resolve)
    assert _players(_crossrow_con(), where, params) == ["Beta"]


def test_career_condition_distributes_over_row_scoped_branches():
    """career AND (team/stat OR team/stat): the plain career predicate is
    copied into each branch and pairing survives in both."""
    ast = _group("AND",
                 _crit("games min", 100),
                 _group("OR",
                        _group("AND", _crit("row team", "A"),
                               _crit("row stat", 100)),
                        _group("AND", _crit("row team", "C"),
                               _crit("row stat", 140))))
    where, params = QB.compile_constraint_ast(_schema(), ast, _resolve)
    assert where.count("g.club_now = ? AND g.goals >= ?") == 2, where
    # Alpha: 200 games and 120 for A. Beta: 150 for C but only 50 games.
    assert _players(_crossrow_con(), where, params) == ["Alpha"]


def test_dnf_expansion_is_bounded():
    """Two OR groups of 17 alternatives mean 289 conjunctions -- past the
    ceiling, and refused rather than truncated."""
    alts = [_crit("games min", n) for n in range(17)]
    ast = _group("AND", _group("OR", *alts), _group("OR", *alts))
    with pytest.raises(ValueError, match="expands past"):
        QB.compile_constraint_ast(_schema(), ast, _resolve)


def test_empty_subgroup_stays_a_placeholder_noop():
    """"Add a subgroup" appends an empty group and reruns into a compile:
    it must contribute nothing, in AND and OR positions alike, and a
    wholly empty root stays the no-op query."""
    empty = {"type": "group", "op": "AND", "children": []}
    ast = _group("AND", _crit("row team", "A"), _crit("row stat", 100),
                 dict(empty))
    where, params = QB.compile_constraint_ast(_schema(), ast, _resolve)
    assert _players(_crossrow_con(), where, params) == ["Alpha"]

    ast = _group("OR", _crit("games min", 100), dict(empty))
    where, params = QB.compile_constraint_ast(_schema(), ast, _resolve)
    assert _players(_crossrow_con(), where, params) == ["Alpha"]

    where, params = QB.compile_constraint_ast(_schema(), dict(empty),
                                              _resolve)
    assert (where, params) == ("1=1", [])


# ------------------------------------------------------------- validation

def test_criterion_leaves_are_resolved_by_the_catalogue_only():
    with pytest.raises(ValueError):
        _resolve("no such builder", [])
    with pytest.raises(ValueError):
        _resolve("played for", "not-a-list")
    with pytest.raises(ValueError):
        _resolve("played for", [{"nested": "value"}])
    with pytest.raises(ValueError):
        _resolve("played for", ["x"] * (QB.MAX_CRITERION_ARGS + 1))
    with pytest.raises(ValueError):
        _resolve("played for", ["A" * (QB.MAX_SCALAR_CHARS + 1)])
    with pytest.raises(ValueError):
        _resolve("games min", [float("inf")])


def test_tokens_cannot_smuggle_sql_through_fragment_leaves():
    """The internal fragment leaf exists for in-process compatibility
    only; a restored payload carrying one must be refused outright."""
    hostile = _group("AND", {"type": "fragment",
                             "sql": "SELECT player_id FROM players",
                             "params": []})
    with pytest.raises(ValueError):
        QB._validated_grid_query(_FakeSport, hostile, [1])


def test_grid_ast_bounds_depth_children_and_shape():
    deep = _crit("games min", 1)
    for _ in range(QB.MAX_GROUP_DEPTH + 1):
        deep = _group("AND", deep)
    with pytest.raises(ValueError):
        QB.compile_constraint_ast(_schema(), deep, _resolve)

    wide = _group("AND", *[_crit("games min", 1)
                           for _ in range(QB.MAX_GROUP_CHILDREN + 1)])
    with pytest.raises(ValueError):
        QB.compile_constraint_ast(_schema(), wide, _resolve)

    with pytest.raises(ValueError):
        QB.compile_constraint_ast(_schema(), _group("XOR"), _resolve)
    with pytest.raises(ValueError):
        QB.compile_constraint_ast(
            _schema(), {"type": "group", "op": "AND", "children": "xx"},
            _resolve)
    with pytest.raises(ValueError):
        QB.compile_constraint_ast(_schema(), ["list"], _resolve)


def test_validated_grid_query_rebuilds_labels_server_side():
    """A token's label/defaults are never trusted: the rebuilt node
    carries the server's own label for the kind and args."""
    node = QB._validated_grid_query(
        _FakeSport,
        _group("OR",
               {"type": "criterion", "kind": "played for", "args": ["A"],
                "label": "TOTALLY FAKE LABEL"},
               _crit("games min", 100)),
        [7])
    assert node["op"] == "OR"
    leaf = node["children"][0]
    assert leaf["kind"] == "played for"
    assert leaf["label"] != "TOTALLY FAKE LABEL"
