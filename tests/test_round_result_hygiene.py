#!/usr/bin/env python3
"""The round/result hygiene test core._code promises.

Constraint SQL compares `round` and `result` bare -- `round = 'GF'` --
because wrapping the column as UPPER(TRIM(...)) defeats the
games(round, player_id) index and scans the whole table per square. That
is only sound while every build stores its codes already normalised, so
this test holds the invariant against every sport database that exists:
if a build ever writes ' gf ' or 'w', it fails here rather than as a
silently empty grid square.
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

import sports


@pytest.mark.parametrize("sport", list(sports.SPORTS.values()),
                         ids=list(sports.SPORTS))
def test_round_and_result_codes_are_stored_normalised(sport):
    if not sport.exists():
        pytest.skip(f"no {sport.key} database built")
    s = sport.schema
    con = sqlite3.connect(f"file:{sport.db}?mode=ro", uri=True)
    try:
        bad = con.execute(
            f"""SELECT COUNT(*) FROM {s.games}
                WHERE ({s.round} IS NOT NULL
                       AND {s.round} <> UPPER(TRIM({s.round})))
                   OR ({s.result} IS NOT NULL
                       AND {s.result} <> UPPER(TRIM({s.result})))"""
        ).fetchone()[0]
    finally:
        con.close()
    assert bad == 0, (
        f"{sport.key}: {bad} games rows carry an unnormalised round/result "
        f"code; bare comparisons like `{s.round} = 'GF'` would miss them. "
        f"Fix the build ({sport.build_cmd}) rather than re-wrapping the "
        f"column in UPPER(TRIM(...)).")
