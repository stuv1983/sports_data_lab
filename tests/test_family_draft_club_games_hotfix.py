#!/usr/bin/env python3
"""Regression test for family-draft qualifying-club game disambiguation."""

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


def source_row():
    row = {
        "competition": "AFL",
        "rule": "father-son",
        "draft_year": 1992,
        "drafted_player": "Darren Walsh",
        "drafted_player_wikipedia_url": "",
        "club": "Carlton",
        "club_wikipedia_url": "",
        "father": "Brian Walsh",
        "father_wikipedia_url": "",
        "selection_raw": "N/A",
        "selection_pick": None,
        "selection_note": "",
        "games_played": 0,
        "father_games_raw": "64",
        "father_games_played": 64,
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

    # The correct Brian Walsh has a 115-game total but exactly 64 for Carlton.
    # The namesake has a misleading 64-game career total at another club.
    con.executemany("INSERT INTO players VALUES (?,?,?,?,?,?,?)", [
        (1, "Brian Walsh", 1970, 1978, 115,
         "Carlton|Essendon", "Carlton|Essendon"),
        (2, "Brian Walsh", 1955, 1962, 64,
         "Richmond", "Richmond"),
    ])
    rows = []
    rows.extend((1, 1970 + (i % 9), "Carlton", "Carlton") for i in range(64))
    rows.extend((1, 1975 + (i % 4), "Essendon", "Essendon") for i in range(51))
    rows.extend((2, 1955 + (i % 8), "Richmond", "Richmond") for i in range(64))
    con.executemany("INSERT INTO games VALUES (?,?,?,?)", rows)
    return con


def main():
    con = make_db()
    totals = loader.import_rows(con, [source_row()])
    assert totals["father_resolved"] == 1, totals
    linked = con.execute(
        "SELECT father_player_id, father_match_status, father_notes "
        "FROM family_draft"
    ).fetchone()
    assert linked[0] == 1, linked
    assert linked[1] == "resolved", linked
    assert "source qualifying-club game total" in linked[2], linked
    con.close()
    print("family-draft qualifying-club games tests: passed")


if __name__ == "__main__":
    main()
