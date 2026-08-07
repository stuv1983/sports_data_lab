#!/usr/bin/env python3
"""Regression tests for family-draft parsing, linking and constraints."""

from __future__ import annotations

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---


import csv
import sqlite3
import tempfile
from pathlib import Path

from afl import family_draft as F
from utils.afl import load_family_draft as loader


def make_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE players (
            player_id INTEGER PRIMARY KEY,
            player TEXT,
            debut_season INTEGER,
            final_season INTEGER,
            career_games INTEGER,
            clubs_hist TEXT,
            clubs_now TEXT
        );
        CREATE TABLE games (
            player_id INTEGER,
            season INTEGER,
            club_now TEXT,
            club_hist TEXT
        );
    """)
    con.executemany("INSERT INTO players VALUES (?,?,?,?,?,?,?)", [
        (1, "Gary Ablett", 1982, 1997, 248, "Hawthorn|Geelong", "Hawthorn|Geelong"),
        (2, "Gary Ablett Jr.", 2002, 2020, 357, "Geelong|Gold Coast", "Geelong|Gold Coast"),
        (3, "Peter Daicos", 1979, 1993, 250, "Collingwood", "Collingwood"),
        (4, "Josh Daicos", 2017, 2026, 151, "Collingwood", "Collingwood"),
        (5, "Nick Daicos", 2022, 2026, 95, "Collingwood", "Collingwood"),
        (6, "Andrew McKay", 1993, 2003, 244, "Carlton", "Carlton"),
        (7, "John Smith", 1970, 1985, 100, "Carlton", "Carlton"),
        (8, "John Smith", 1972, 1986, 90, "Carlton", "Carlton"),
    ])
    con.executemany("INSERT INTO games VALUES (?,?,?,?)", [
        (1, 1990, "Geelong", "Geelong"),
        (2, 2002, "Geelong", "Geelong"),
        (3, 1988, "Collingwood", "Collingwood"),
        (4, 2017, "Collingwood", "Collingwood"),
        (5, 2022, "Collingwood", "Collingwood"),
        (6, 1999, "Carlton", "Carlton"),
        (7, 1980, "Carlton", "Carlton"),
        (8, 1980, "Carlton", "Carlton"),
    ])
    return con


def row(comp: str, year: int, child: str, club: str, father: str) -> dict:
    rule = "father-son" if comp == "AFL" else "father-daughter"
    base = {
        "competition": comp,
        "rule": rule,
        "draft_year": year,
        "drafted_player": child,
        "drafted_player_wikipedia_url": "",
        "club": club,
        "club_wikipedia_url": "",
        "father": father,
        "father_wikipedia_url": "",
        "selection_raw": "1",
        "selection_pick": 1,
        "selection_note": "",
        "games_played": 1,
        "father_games_raw": "",
        "father_games_played": None,
        "father_games_note": "",
        "current_player": 0,
        "changed_team": 0,
        "status_marker": "",
        "source_url": "https://example.test",
        "source_revision_id": 1,
        "scraped_at_utc": "2026-08-02T00:00:00+00:00",
        "source_name": "test.csv",
    }
    base["source_row_id"] = loader._source_row_id(base)
    return base


def ids(con, constraint) -> set[int]:
    sql, params = constraint
    return {item[0] for item in con.execute(sql, params)}


def test_linking_and_constraints() -> None:
    con = make_db()
    rows = [
        row("AFL", 2001, "Gary Ablett, Jr.", "Geelong", "Gary Ablett, Sr."),
        row("AFL", 2016, "Josh Daicos", "Collingwood", "Peter Daicos"),
        row("AFL", 2021, "Nick Daicos", "Collingwood", "Peter Daicos"),
        row("AFL", 2000, "Missing Child", "Carlton", "John Smith"),
        row("AFLW", 2018, "Abbie McKay", "Carlton", "Andrew McKay"),
    ]
    totals = loader.import_rows(con, rows)
    assert totals["child_unique"] == 2, totals
    assert totals["child_resolved"] == 1, totals
    assert totals["child_unmatched"] == 1, totals
    assert totals["child_out_of_scope"] == 1, totals
    assert totals["father_unique"] == 3, totals
    assert totals["father_resolved"] == 1, totals
    assert totals["father_ambiguous"] == 1, totals

    linked = con.execute("""
        SELECT drafted_player, drafted_player_id, father_player_id
        FROM family_draft ORDER BY draft_year, drafted_player
    """).fetchall()
    by_child = {name: (child_id, father_id) for name, child_id, father_id in linked}
    assert by_child["Gary Ablett, Jr."] == (2, 1), by_child
    assert by_child["Josh Daicos"] == (4, 3), by_child
    assert by_child["Nick Daicos"] == (5, 3), by_child
    assert by_child["Abbie McKay"] == (None, 6), by_child

    assert ids(con, F.father_son_selection()) == {2, 4, 5}
    assert ids(con, F.father_also_played_afl()) == {2, 4, 5}
    assert ids(con, F.father_played_for("Collingwood")) == {4, 5}
    assert ids(con, F.parent_child_pair()) == {1, 2, 3, 4, 5}
    assert ids(con, F.child_of_father_id(3)) == {4, 5}
    assert ids(con, F.child_of_father_name("Peter Daicos")) == {4, 5}
    assert F.family_draft_available(con)
    assert F.family_draft_count(con) == 3
    con.close()


def test_csv_reading() -> None:
    columns = [
        "competition", "rule", "year", "drafted_player",
        "drafted_player_wikipedia_url", "club", "club_wikipedia_url",
        "father", "father_wikipedia_url", "selection_raw", "selection_pick",
        "selection_note", "games_played", "father_games_raw",
        "father_games_played", "father_games_note", "current_player",
        "changed_team", "status_marker", "source_url", "source_revision_id",
        "scraped_at_utc",
    ]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "family.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerow({
                "competition": "AFL", "rule": "father-son", "year": 2021,
                "drafted_player": "Nick Daicos", "club": "Collingwood",
                "father": "Peter Daicos", "selection_raw": "4",
                "selection_pick": "4", "games_played": "95",
                "father_games_raw": "250", "father_games_played": "250",
                "current_player": "1", "changed_team": "0",
                "source_revision_id": "123", "source_url": "https://example.test",
                "scraped_at_utc": "2026-08-02T00:00:00+00:00",
            })
        rows = loader.read_csvs([path, path])
    assert len(rows) == 1
    assert rows[0]["draft_year"] == 2021
    assert rows[0]["selection_pick"] == 4
    assert rows[0]["current_player"] == 1


def test_placeholder() -> None:
    con = sqlite3.connect(":memory:")
    F.ensure_family_draft_table(con)
    assert not F.family_draft_available(con)
    assert ids(con, F.father_son_selection()) == set()
    con.close()


def main() -> None:
    test_linking_and_constraints()
    test_csv_reading()
    test_placeholder()
    print("family-draft regression tests: passed")


if __name__ == "__main__":
    main()
