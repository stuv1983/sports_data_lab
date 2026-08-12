#!/usr/bin/env python3
"""Canonical draft categories, decided at ingestion.

The constraints used to run LIKE '%national%' over Draftguru's raw label
per query -- correct, but a substring scan restated in five places, and
blind to indexes. afl/draft_kinds.py decides the category once, at load
time; these tests hold the classifier's rules, the migration for
databases built before the column existed, and the plan the equality
predicate now gets.
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

from afl import constraints as C
from afl.draft_kinds import draft_kind, ensure_draft_kind


@pytest.mark.parametrize("label,kind", [
    ("National", "national"),
    ("National Draft", "national"),      # the drift the LIKE existed for
    ("Rookie", "rookie"),
    ("Trade", "trade"),
    ("Pre-Season", "preseason"),
    ("Mid-Season", "midseason"),
    ("Free Agency", "free_agency"),
    ("Pre-Draft", "pre_draft"),          # unrecognised -> stable slug
    ("Training Squad Selection", "training_squad_selection"),
])
def test_real_draftguru_labels_map_to_their_category(label, kind):
    assert draft_kind(label) == kind


def test_blank_and_nan_labels_stay_null_not_a_nan_slug():
    assert draft_kind(None) is None
    assert draft_kind("") is None
    assert draft_kind("   ") is None
    assert draft_kind(float("nan")) is None, \
        "a pandas NaN must never become the category 'nan'"


def _db_without_kind():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE draft (player TEXT, draft_type TEXT, "
                "pick INTEGER)")
    con.executemany("INSERT INTO draft VALUES (?, ?, ?)", [
        ("Alpha", "National", 1),
        ("Beta", "National Draft", 2),
        ("Gamma", "Rookie", 1),
        ("Delta", None, None),
    ])
    return con


def test_the_migration_backfills_a_database_built_before_the_column():
    con = _db_without_kind()
    assert ensure_draft_kind(con) == 4
    kinds = dict(con.execute("SELECT player, draft_kind FROM draft"))
    assert kinds == {"Alpha": "national", "Beta": "national",
                     "Gamma": "rookie", "Delta": None}


def test_the_migration_is_idempotent():
    con = _db_without_kind()
    ensure_draft_kind(con)
    assert ensure_draft_kind(con) == 4      # reclassifies, never errors
    assert [r[1] for r in con.execute("PRAGMA table_info(draft)")].count(
        "draft_kind") == 1


def test_a_database_without_a_draft_table_is_left_alone():
    con = sqlite3.connect(":memory:")
    assert ensure_draft_kind(con) == 0
    assert con.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0


def test_the_draft_kind_predicate_drives_its_index():
    """The shape every rewritten site now emits -- equality on draft_kind
    plus a pick range -- must reach ix_draft_kind rather than scanning."""
    con = _db_without_kind()
    ensure_draft_kind(con)
    plan = " | ".join(row[3] for row in con.execute(
        "EXPLAIN QUERY PLAN SELECT rowid FROM draft d "
        "WHERE d.draft_kind = 'national' AND d.pick BETWEEN 1 AND 10"))
    assert "ix_draft_kind" in plan, plan
    assert "SCAN draft" not in plan, plan


def test_the_top_pick_constraint_names_the_canonical_kind():
    sql, _params = C.draft_pick_between(1, 10)
    assert "d.draft_kind = 'national'" in sql
    assert "LIKE" not in sql.upper()


def test_draft_of_type_maps_the_ui_label_to_the_canonical_kind():
    sql, params = C.draft_of_type("Free Agency")
    assert "d.draft_kind = ?" in sql
    assert params == ["free_agency"]
