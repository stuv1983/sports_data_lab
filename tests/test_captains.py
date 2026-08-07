#!/usr/bin/env python3
"""Regression checks for captain parsing and conservative identity linking."""

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

from utils.afl import load_captains as captains
from afl import scrape_afl_captains as scraper


def make_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE players (
            player_id INTEGER PRIMARY KEY,
            player TEXT,
            debut_season INTEGER,
            final_season INTEGER,
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
    con.executemany("INSERT INTO players VALUES (?, ?, ?, ?, ?, ?)", [
        (1, "Paddy OBrien", 1918, 1925, "Carlton", "Carlton"),
        (2, "Gary Ablett", 2002, 2020, "Geelong|Gold Coast", "Geelong|Gold Coast"),
        (3, "Bill Twomey", 1953, 1958, "Collingwood", "Collingwood"),
        (4, "Sam Docherty", 2013, 2026, "Brisbane Lions|Carlton", "Brisbane Lions|Carlton"),
        (5, "Brendon Goddard", 2003, 2018, "St Kilda|Essendon", "St Kilda|Essendon"),
        (6, "John Smith", 1900, 1905, "Carlton", "Carlton"),
        (7, "John Smith", 1901, 1904, "Carlton", "Carlton"),
    ])
    con.executemany("INSERT INTO games VALUES (?, ?, ?, ?)", [
        (1, 1920, "Carlton", "Carlton"),
        (2, 2011, "Gold Coast", "Gold Coast"),
        (3, 1957, "Collingwood", "Collingwood"),
        # Docherty intentionally has no 2019 appearance.
        (4, 2018, "Carlton", "Carlton"),
        (4, 2020, "Carlton", "Carlton"),
        (5, 2016, "Essendon", "Essendon"),
        (6, 1902, "Carlton", "Carlton"),
        (7, 1902, "Carlton", "Carlton"),
    ])
    return con


def row(season: int, club: str, player: str, role: str = "Captain") -> dict:
    return {
        "source_row_id": f"{season}-{club}-{player}-{role}",
        "season": season,
        "club": club,
        "player": player,
        "role": role,
        "source_url": "",
        "player_url": "",
        "source_page": "test",
        "source_revision": None,
        "source_period": str(season),
        "source_notes": "",
        "source_name": "test.csv",
    }


def test_linking() -> None:
    con = make_db()
    rows = [
        row(1920, "Carlton", "Paddy O'Brien"),
        row(2011, "Gold Coast", "Gary Ablett Jr."),
        row(1957, "Collingwood", "Bill Twomey, Jr."),
        row(2019, "Carlton", "Sam Docherty"),
        row(2016, "Essendon", "Jobe Watson"),
        row(1902, "Carlton", "John Smith"),
        row(1920, "Carlton", "Example Vice", "Vice-captain"),
    ]
    totals = captains.import_rows(con, rows)
    assert totals["unique"] == 3, totals
    assert totals["resolved"] == 2, totals
    assert totals["ambiguous"] == 1, totals
    assert totals["unsupported_role"] == 1, totals
    linked = dict(con.execute(
        "SELECT player, player_id FROM captaincies WHERE player_id IS NOT NULL"
    ))
    assert linked["Paddy O'Brien"] == 1
    assert linked["Gary Ablett Jr."] == 2
    assert linked["Bill Twomey, Jr."] == 3
    assert linked["Sam Docherty"] == 4
    assert linked["Jobe Watson"] == 5
    con.close()


def test_csv_split() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        first = root / "one" / "captains.csv"
        second = root / "two" / "captains.csv"
        first.parent.mkdir()
        second.parent.mkdir()
        for path in (first, second):
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["season", "club", "player", "role"])
                writer.writerow([1928, "St Kilda", "Horrie Mason, Bill Cubbins", "Captain"])
                writer.writerow([1957, "Collingwood", "Bill Twomey, Jr.", "Captain"])
        rows = captains.read_csvs([first, second])
    names = [item["player"] for item in rows]
    assert names == ["Horrie Mason", "Bill Cubbins", "Bill Twomey, Jr."], names


def test_duplicate_rows_and_atomic_publish() -> None:
    con = make_db()
    original = row(1920, "Carlton", "Paddy O'Brien")
    totals = captains.import_rows(con, [original, dict(original)])
    assert totals["duplicate_source_rows_ignored"] == 1, totals
    assert con.execute("SELECT COUNT(*) FROM captaincies").fetchone()[0] == 1

    broken = row(2011, "Gold Coast", "Gary Ablett Jr.")
    del broken["source_url"]
    try:
        captains.import_rows(con, [broken])
    except KeyError:
        pass
    else:
        raise AssertionError("broken import unexpectedly succeeded")

    # The failed refresh must not publish an empty or partial replacement.
    assert con.execute("SELECT player FROM captaincies").fetchone()[0] == "Paddy O'Brien"
    con.close()


def test_scraper_helpers() -> None:
    assert scraper.expand_period("1914–15; 1917", 2026) == [1914, 1915, 1917]
    assert scraper.expand_period("2023–", 2026) == [2023, 2024, 2025, 2026]
    assert scraper._plain_names("Horrie Mason, Bill Cubbins") == [
        "Horrie Mason", "Bill Cubbins"
    ]
    assert scraper._plain_names("Bill Twomey, Jr.") == ["Bill Twomey, Jr."]


def main() -> None:
    test_linking()
    test_csv_split()
    test_duplicate_rows_and_atomic_publish()
    test_scraper_helpers()
    print("captain regression tests: passed")


if __name__ == "__main__":
    main()
