#!/usr/bin/env python3
"""Offline tests for the FootyWire Rising Star parser, linker and constraints."""

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
import tempfile
from pathlib import Path

from afl import fetch_footywire_rising_star as F
from afl import load_rising_star as L
from afl import rising_star as R

HTML = """
<html><body>
<div>1993 Rising Star winner is <a>Nathan Buckley</a> of the <a>Brisbane Bears</a>.</div>
<div>1993 AFL Rising Star Nominations</div>
<table>
<tr><th>Rd</th><th>Player</th><th>Team</th><th>Opp</th><th>K</th><th>H</th><th>D</th><th>M</th><th>G</th><th>B</th><th>T</th><th>HO</th><th>FF</th><th>FA</th><th>SC</th><th>AF</th></tr>
<tr><td>1</td><td><a href="/afl/footy/pp-st-kilda-saints--peter-everitt">P Everitt</a></td><td><a href="/afl/footy/ty-st-kilda-saints">Saints</a></td><td><a href="/afl/footy/ty-geelong-cats">Cats</a></td><td>12</td><td>5</td><td>17</td><td>6</td><td>1</td><td>1</td><td>1</td><td>12</td><td>0</td><td>0</td><td>0</td><td>0</td></tr>
<tr><td>2</td><td><a href="/afl/footy/pp-hawthorn-hawks--shane-crawford">S Crawford</a></td><td><a href="/afl/footy/ty-hawthorn-hawks">Hawks</a></td><td><a href="/afl/footy/ty-sydney-swans">Swans</a></td><td>19</td><td>4</td><td>23</td><td>9</td><td>5</td><td>2</td><td>3</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td></tr>
<tr><td>7</td><td><a href="/afl/footy/pp-brisbane-lions--nathan-buckley">N Buckley</a></td><td><a href="/afl/footy/ty-brisbane-lions">Bears</a></td><td><a href="/afl/footy/ty-hawthorn-hawks">Hawks</a></td><td>20</td><td>8</td><td>28</td><td>3</td><td>0</td><td>1</td><td>1</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td></tr>
</table></body></html>
"""


def build_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE players (
      player_id INTEGER PRIMARY KEY, player TEXT, name_key TEXT,
      debut_season INTEGER, final_season INTEGER, career_games INTEGER
    );
    CREATE TABLE games (
      player_id INTEGER, season INTEGER, club_now TEXT, club_hist TEXT
    );
    CREATE TABLE meta (key TEXT, value TEXT);
    """)
    players = [
        (1, "Peter Everitt", "peter everitt", 1993, 2008, 291),
        (2, "Shane Crawford", "shane crawford", 1993, 2008, 305),
        (3, "Nathan Buckley", "nathan buckley", 1993, 2007, 280),
        (4, "Michael OLoughlin", "michael oloughlin", 1995, 2009, 303),
        (5, "Mathew Capuano", "mathew capuano", 1994, 2003, 107),
        (6, "Ryan OConnor", "ryan oconnor", 1994, 2000, 87),
        (7, "Matt Rosa", "matt rosa", 2005, 2018, 207),
        (8, "Brad Moran", "brad moran", 2006, 2011, 21),
        (9, "Mark Smith", "mark smith", 2006, 2008, 10),
        (10, "Matt Smith", "matt smith", 2006, 2008, 8),
        (11, "Test Player", "test player", 2005, 2007, 3),
    ]
    con.executemany("INSERT INTO players VALUES (?,?,?,?,?,?)", players)
    games = [
        (1, 1993, "St Kilda", "St Kilda"),
        (2, 1993, "Hawthorn", "Hawthorn"),
        (3, 1993, "Brisbane Bears", "Brisbane Bears"),
        (4, 1995, "Sydney", "Sydney"),
        (5, 1995, "North Melbourne", "North Melbourne"),
        (6, 1995, "Essendon", "Essendon"),
        (7, 2006, "West Coast", "West Coast"),
        (8, 2006, "North Melbourne", "Kangaroos"),
        (9, 2006, "Adelaide", "Adelaide"),
        (10, 2006, "Adelaide", "Adelaide"),
        (11, 2006, "Carlton", "Carlton"),
    ]
    con.executemany("INSERT INTO games VALUES (?,?,?,?)", games)
    con.commit()
    con.close()


def source_row(player: str, display: str, season: int, club: str) -> dict:
    return {
        "player": player,
        "player_display": display,
        "name_key": L.normalise_name(player),
        "season": season,
        "club": club,
    }


def main() -> None:
    rows = F.parse_page(HTML, 1993)
    assert len(rows) == 3
    assert rows[0]["player"] == "Peter Everitt"
    assert rows[0]["club"] == "St Kilda"
    assert rows[0]["opponent"] == "Geelong"
    assert rows[2]["club"] == "Brisbane Bears"  # display beats bad modern slug
    assert rows[2]["is_season_winner"] == 1
    assert rows[1]["disposals"] == 23
    assert rows[0]["frees_for"] is None
    assert rows[0]["supercoach"] is None
    assert rows[0]["unavailable_stats"] == (
        "frees_for|frees_against|supercoach|afl_fantasy"
    )

    with tempfile.TemporaryDirectory() as folder:
        base = Path(folder)
        supplied = base / "browser_saved"
        supplied.mkdir()
        (supplied / "1993.html").write_text(HTML, encoding="utf-8")
        output = base / "archive"
        rc = F.main([
            "--html-dir", str(supplied),
            "--output-dir", str(output),
            "--from", "1993", "--to", "1993",
            "--save-html-only",
        ])
        assert rc == 0
        archived = output / "html" / "1993.html"
        assert archived.exists()
        assert "AFL Rising Star Nominations" in archived.read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory() as folder:
        base = Path(folder)
        source = base / "rising_star_nominees.csv"
        F.write_csv(source, rows)
        db = base / "test.db"
        build_db(db)
        result = L.load_sources(db, [source], verbose=False)
        assert result["rows"] == 3
        assert result["trusted"] == 3

        con = sqlite3.connect(db)
        assert R.rising_star_available(con)
        sql, params = R.rising_star_nominee_for("Hawthorn")
        assert con.execute(sql, params).fetchall() == [(2,)]
        linked = con.execute(
            "SELECT player_id, matched_player, match_status "
            "FROM rising_star_nominees ORDER BY round_number"
        ).fetchall()
        assert linked == [
            (1, "Peter Everitt", "unique"),
            (2, "Shane Crawford", "unique"),
            (3, "Nathan Buckley", "unique"),
        ]

        fallbacks = [
            (source_row("Michael O Loughlin", "M O'Loughlin", 1995, "Sydney"), 4, "Michael OLoughlin"),
            (source_row("Matthew Capuano", "M Capuano", 1995, "North Melbourne"), 5, "Mathew Capuano"),
            (source_row("Ryan O Connor", "R O'Connor", 1995, "Essendon"), 6, "Ryan OConnor"),
            (source_row("Matthew Rosa", "M Rosa", 2006, "West Coast"), 7, "Matt Rosa"),
            (source_row("Bradley Moran", "B Moran", 2006, "North Melbourne"), 8, "Brad Moran"),
        ]
        for row, expected_id, expected_name in fallbacks:
            player_id, status, count, _, matched, method = L.resolve_row(con, row)
            assert (player_id, status, count, matched, method) == (
                expected_id, "resolved", 1, expected_name,
                "initial_surname_season_club",
            )

        ambiguous = source_row("Michael Smith", "M Smith", 2006, "Adelaide")
        assert L.resolve_row(con, ambiguous)[1] == "ambiguous"

        mismatch = source_row("Test Player", "T Player", 2006, "Essendon")
        resolved = L.resolve_row(con, mismatch)
        assert resolved[0] == 11
        assert resolved[1] == "club_mismatch"
        con.close()

    print("[PASS] FootyWire page parser and invariants")
    print("[PASS] historical club identity overrides")
    print("[PASS] source-wide unavailable-stat handling")
    print("[PASS] conservative player-name fallback")
    print("[PASS] unsafe and ambiguous links remain untrusted")
    print("[PASS] CSV -> SQLite linking and Rising Star constraints")


if __name__ == "__main__":
    main()
