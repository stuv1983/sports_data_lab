#!/usr/bin/env python3
"""Focused regression tests for family-draft father disambiguation."""

from __future__ import annotations

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---


import sqlite3

from utils.afl import load_family_draft as loader


def source_row(year, child, club, father, father_games):
    row = {
        "competition": "AFL",
        "rule": "father-son",
        "draft_year": year,
        "drafted_player": child,
        "drafted_player_wikipedia_url": "",
        "club": club,
        "club_wikipedia_url": "",
        "father": father,
        "father_wikipedia_url": "",
        "selection_raw": "N/A",
        "selection_pick": None,
        "selection_note": "",
        "games_played": 0,
        "father_games_raw": str(father_games),
        "father_games_played": father_games,
        "father_games_note": "",
        "current_player": 0,
        "changed_team": 0,
        "status_marker": "",
        "source_url": "https://example.test",
        "source_revision_id": 1,
        "scraped_at_utc": "2026-08-02T00:00:00+00:00",
        "source_name": "test.csv",
    }
    row["source_row_id"] = loader._source_row_id(row)
    return row


def make_db():
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
        (1, "Brian Walsh", 1956, 1963, 64, "Carlton", "Carlton"),
        (2, "Brian Walsh", 1969, 1976, 92, "Essendon", "Essendon"),
        (3, "Bill Brownless", 1986, 1997, 198, "Geelong", "Geelong"),
    ])
    con.executemany("INSERT INTO games VALUES (?,?,?,?)", [
        (1, 1960, "Carlton", "Carlton"),
        (2, 1972, "Essendon", "Essendon"),
        (3, 1990, "Geelong", "Geelong"),
    ])
    return con


def main():
    con = make_db()
    rows = [
        source_row(1992, "Missing Child A", "Carlton", "Brian Walsh", 64),
        source_row(2018, "Oscar Brownless", "Geelong", "Billy Brownless", 198),
    ]
    totals = loader.import_rows(con, rows)
    assert totals["father_resolved"] == 2, totals
    linked = dict(con.execute(
        "SELECT father, father_player_id FROM family_draft"
    ).fetchall())
    assert linked["Brian Walsh"] == 1, linked
    assert linked["Billy Brownless"] == 3, linked
    notes = dict(con.execute(
        "SELECT father, father_notes FROM family_draft"
    ).fetchall())
    assert "source father-game total" in notes["Brian Walsh"], notes
    assert "reviewed name alias" in notes["Billy Brownless"], notes
    con.close()
    print("family-draft linker hotfix tests: passed")


if __name__ == "__main__":
    main()
