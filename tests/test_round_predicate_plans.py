#!/usr/bin/env python3
"""The title constraints must be able to drive a round index.

The round/result hygiene test guarantees every build stores its codes
already UPPER(TRIM)-normal, which is what lets the constraint SQL compare
the bare column. These tests hold the other half of that bargain: the
generated predicates actually reach the index. A reintroduced
UPPER(TRIM(round)) wrapper would pass every behavioural test and quietly
turn each title square back into a full games scan -- measured at ~0.3 to
0.4 seconds a square on the real NBA and NFL databases against under a
millisecond indexed.
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

from mlb import constraints_mlb
from nba import constraints_nba
from nfl import constraints_nfl


def _con():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE games (player_id INTEGER, season INTEGER, "
                "round TEXT, result TEXT)")
    con.execute("CREATE INDEX ix_games_round ON games(round, player_id)")
    con.executemany(
        "INSERT INTO games VALUES (?, ?, ?, ?)",
        [(i, 2020, code, "W" if i % 2 else "L")
         for i, code in enumerate(["1", "2", "F", "WS", "SB", "CON"] * 20)])
    return con


def _plan(con, built):
    sql, params = built
    return " | ".join(row[3] for row in con.execute(
        f"EXPLAIN QUERY PLAN {sql}", params))


@pytest.mark.parametrize("built", [
    constraints_nba.played_in_the_finals(),
    constraints_nba.won_the_finals(),
    constraints_mlb.played_in_the_world_series(),
    constraints_mlb.won_the_world_series(),
    constraints_nfl.played_in_the_super_bowl(),
    constraints_nfl.won_the_super_bowl(),
    constraints_nfl.played_in_a_conference_championship(),
])
def test_a_title_predicate_drives_the_round_index(built):
    con = _con()
    plan = _plan(con, built)
    assert "USING" in plan and "INDEX ix_games_round" in plan, plan
    assert "SCAN games" not in plan, plan


def test_the_wrapped_form_this_replaces_really_did_scan():
    """The counterexample that motivates the assertion above: the exact
    predicate shape these constraints used before."""
    con = _con()
    plan = _plan(con, (
        "SELECT DISTINCT player_id FROM games "
        "WHERE UPPER(TRIM(round)) = ?", ["F"]))
    assert "SCAN games" in plan, plan
