import json
import sqlite3

from afl import parse_criteria
from utils import fetch_grids


def test_gridley_label_preserves_the_subtitle_that_defines_the_criterion():
    examples = [
        ({"id": "grandfinals3", "title": "3+ GRAND", "subtitle": "FINALS"},
         "3+ GRAND FINALS"),
        ({"id": "luke-hodge-teammate-4900", "title": "LUKE HODGE",
          "subtitle": "LUKE HODGE TEAMMATE"}, "LUKE HODGE TEAMMATE"),
        ({"id": "brownlowTop10", "title": "TOP 10",
          "subtitle": "BROWNLOW FINISH"}, "TOP 10 BROWNLOW FINISH"),
        ({"id": "debut-team-hawthorn", "title": "HAWTHORN",
          "subtitle": "FIRST CAREER GAME"}, "HAWTHORN FIRST CAREER GAME"),
    ]

    for payload, expected in examples:
        label = fetch_grids.gridley_label(payload)
        assert label == expected
        constraint, _display = parse_criteria.parse(label)
        assert constraint is not None

    _constraint, display = parse_criteria.parse("3+ GRAND FINALS")
    assert display == "played in 3+ grand finals"


def test_save_grid_is_idempotent_and_refreshes_existing_capture(tmp_path):
    path = tmp_path / "sport.db"
    date = "2026-08-07"

    assert fetch_grids.save_grid(
        path, date, "Gridley", ["r1", "r2", "r3"], ["c1", "c2", "c3"]
    ) == "inserted"
    assert fetch_grids.save_grid(
        path, date, "Gridley", ["R1", "R2", "R3"], ["C1", "C2", "C3"]
    ) == "updated"

    with sqlite3.connect(path) as con:
        con.execute(
            "INSERT INTO historic_grids (date, source, rows_json, cols_json) "
            "VALUES (?, ?, '[]', '[]')",
            (date, "Gridley"),
        )
    assert fetch_grids.save_grid(
        path, date, "Gridley", ["R1", "R2", "R3"], ["C1", "C2", "C3"]
    ) == "updated"

    with sqlite3.connect(path) as con:
        rows = con.execute(
            "SELECT rows_json, cols_json FROM historic_grids"
        ).fetchall()

    assert len(rows) == 1
    assert json.loads(rows[0][0]) == ["R1", "R2", "R3"]
    assert json.loads(rows[0][1]) == ["C1", "C2", "C3"]


def test_save_grid_propagates_database_failures(tmp_path):
    missing_parent = tmp_path / "missing" / "sport.db"

    try:
        fetch_grids.save_grid(
            missing_parent, "2026-08-07", "Gridley",
            ["r1", "r2", "r3"], ["c1", "c2", "c3"],
        )
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("database write failure was reported as success")
