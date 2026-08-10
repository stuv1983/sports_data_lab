import datetime as dt
import json
import sqlite3

from afl import historic_grids as HG
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


def test_save_grid_keeps_gridleys_real_board_number(tmp_path):
    path = tmp_path / "sport.db"
    assert fetch_grids.save_grid(
        path, "2026-08-08", "Gridley", ["r1", "r2", "r3"],
        ["c1", "c2", "c3"], grid_num=1119,
    ) == "inserted"
    assert fetch_grids.save_grid(
        path, "2026-08-08", "Gridley", ["r1", "r2", "r3"],
        ["c1", "c2", "c3"], grid_num=1119,
    ) == "unchanged"

    with sqlite3.connect(path) as con:
        assert con.execute(
            "SELECT grid_num, date FROM historic_grids"
        ).fetchall() == [(1119, "2026-08-08")]


def test_a_fetched_board_becomes_a_grid_the_solver_can_analyse():
    """Grid Solver's "Today's Gridley" source opens the feed's own payload.

    The same dict scan_gridley saves has to survive the trip to a
    HistoricGrid unaltered: the criteria are Gridley's exact wording, which
    is what afl/parse_criteria.py reads.
    """
    board = {
        "grid_num": 1121,
        "rows": ["200+ GAMES PLAYED", "DANE RAMPE TEAMMATE",
                 "LOST 2+ GRAND FINALS"],
        "cols": ["Sydney Swans", "MINOR PREMIERSHIP WINNER",
                 "PLAYED IN 2010s"],
    }

    grid = HG.from_feed(board, "2026-08-10", "Gridley")

    assert grid.complete
    assert grid.number == 1121
    assert grid.source == "Gridley"
    assert grid.key == "#1121 (2026-08-10)"
    assert grid.rows == tuple(board["rows"])
    assert grid.cols == tuple(board["cols"])

    report = HG.analyse(grid, con=None, sport=None)
    assert not report.unsupported, [c.text for c in report.unsupported]


def test_an_unpublished_day_is_no_grid_rather_than_an_empty_one():
    assert HG.from_feed(None, "2999-01-01", "Gridley") is None
    assert HG.from_feed({}, "2999-01-01", "Gridley") is None


def test_a_short_feed_payload_is_kept_as_a_partial_capture():
    """A board is never padded to nine solvable squares it does not have."""
    grid = HG.from_feed(
        {"grid_num": 1122, "rows": ["150+ GAMES PLAYED"],
         "cols": ["Sydney Swans", "PLAYED IN 2010s"]},
        "2026-08-11", "Gridley")

    assert not grid.complete
    assert grid.rows == () and grid.cols == ()
    assert grid.partial_criteria == ("150+ GAMES PLAYED", "Sydney Swans",
                                     "PLAYED IN 2010s")
    assert "3 of six" in grid.note

    report = HG.analyse(grid, con=None, sport=None)
    assert len(report.loose) == 3
    assert report.status == "Partial capture — not playable"


def test_scan_gridley_starts_after_latest_saved_date(tmp_path):
    path = tmp_path / "sport.db"
    fetch_grids.save_grid(
        path, "2026-08-06", "Gridley", ["old1", "old2", "old3"],
        ["old4", "old5", "old6"], grid_num=1117,
    )
    requested = []

    def fake_fetch(date):
        requested.append(date)
        number = 1118 if date.endswith("07") else 1119
        return {
            "grid_num": number,
            "rows": ["r1", "r2", "r3"],
            "cols": ["c1", "c2", "c3"],
        }

    result = fetch_grids.scan_gridley(
        path, through=dt.date(2026, 8, 8), fetcher=fake_fetch
    )

    assert requested == ["2026-08-07", "2026-08-08"]
    assert result["inserted"] == 2
    assert [board["grid_num"] for board in result["boards"]] == [1118, 1119]
