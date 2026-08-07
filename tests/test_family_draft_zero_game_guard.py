#!/usr/bin/env python3
"""Regression tests for historical zero-game family-draft identities."""

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
        (1, "Darren Walsh", 1992, 1998, 35, "Carlton", "Carlton"),
        (2, "Future Son", 2026, 2026, 1, "Carlton", "Carlton"),
    ])
    con.executemany("INSERT INTO games VALUES (?,?,?,?)", [
        (1, 1992, "Carlton", "Carlton"),
        (2, 2026, "Carlton", "Carlton"),
    ])
    return con


def row(name, current):
    return {
        "competition": "AFL",
        "drafted_player": name,
        "draft_year": 1992 if name == "Darren Walsh" else 2025,
        "club": "Carlton",
        "games_played": 0,
        "current_player": current,
    }


def main():
    con = make_db()
    refs = loader.load_reference_maps(con)

    # Historical zero-game draftees must never resolve to a same-name player.
    linked = loader._resolve_child(row("Darren Walsh", 0), refs)
    assert linked[0] is None, linked
    assert linked[1] == "unmatched", linked
    assert "zero senior AFL games" in linked[3], linked

    # A current player's published count can lag the newer local database.
    current = loader._resolve_child(row("Future Son", 1), refs)
    assert current[0] == 2, current
    assert current[1] in {"unique", "resolved"}, current

    con.close()
    print("family-draft zero-game guard tests: passed")


if __name__ == "__main__":
    main()
