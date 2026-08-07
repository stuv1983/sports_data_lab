#!/usr/bin/env python3
"""Duplicate player-game rows: resolution, refusal, and the career recount.

The case this protects against is real. AFL Tables carries the 1909-07-03
St Kilda-Essendon match twice for each of two players both named Jim Stewart
(IDs 5230 and 5685), with the two players' career_game_no/birth_year_est pairs
crossed between them. Career totals are a row count, so each duplicate added a
phantom appearance: 86 games instead of 85, and 4 instead of 3.
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
import tempfile
from pathlib import Path

from utils.shared import dedupe_games, repair_database


def _rows(*specs):
    """(player_id, date, career_game_no, ref) tuples as resolver input."""
    return [{"player_id": p, "date": d, "career_game_no": n, "ref": r,
             "fields": (p, d, n)}
            for p, d, n, r in specs]


def test_resolves_by_career_sequence():
    """The Jim Stewart shape: two candidates, one fits the gap."""
    # Player 5230's real appearances run 67, 68, 69. The intruder is game 1.
    drop, unresolved = dedupe_games.resolve(_rows(
        (5230, "1909-06-19", 67, "a"),
        (5230, "1909-07-03", 68, "keep"),
        (5230, "1909-07-03", 1, "drop"),
        (5230, "1909-09-04", 69, "b"),
    ))
    assert not unresolved
    assert drop == ["drop"]


def test_resolves_a_debut_duplicate():
    """The other side of the crossing: the first appearance must be game 1."""
    drop, unresolved = dedupe_games.resolve(_rows(
        (5685, "1909-07-03", 68, "drop"),
        (5685, "1909-07-03", 1, "keep"),
        (5685, "1909-07-10", 2, "b"),
    ))
    assert not unresolved
    assert drop == ["drop"]


def test_collapses_exact_duplicates():
    """Identical rows are one row recorded twice; no evidence needed."""
    rows = _rows((7, "2000-05-01", 1, "a"), (7, "2000-05-01", 1, "b"))
    drop, unresolved = dedupe_games.resolve(rows)
    assert not unresolved
    assert len(drop) == 1


def test_refuses_when_both_candidates_fit():
    """A guess here produces a clean-looking database that is wrong."""
    drop, unresolved = dedupe_games.resolve(_rows(
        (9, "2000-05-01", 5, "a"),
        (9, "2000-05-01", 6, "b"),
        (9, "2000-06-01", 9, "c"),
    ))
    assert not drop
    assert len(unresolved) == 1
    assert unresolved[0].player_id == 9
    assert sorted(unresolved[0].candidates) == [5, 6]


def test_refuses_when_no_candidate_fits():
    drop, unresolved = dedupe_games.resolve(_rows(
        (9, "2000-05-01", 1, "a"),
        (9, "2000-06-01", 40, "b"),
        (9, "2000-06-01", 50, "c"),
    ))
    assert not drop
    assert len(unresolved) == 1


def test_unnumbered_appearances_are_not_evidence():
    """A missing career_game_no is no evidence, not a matching one.

    The two rows here conflict on club, so they are not the same row twice --
    and with neither numbered, nothing can say which one belongs.
    """
    rows = _rows((9, "2000-05-01", None, "a"), (9, "2000-05-01", None, "b"),
                 (9, "2000-06-01", 2, "c"))
    rows[0]["fields"] = (9, "2000-05-01", None, "Carlton")
    rows[1]["fields"] = (9, "2000-05-01", None, "Geelong")
    drop, unresolved = dedupe_games.resolve(rows)
    assert not drop
    assert len(unresolved) == 1


def test_leaves_clean_players_alone():
    drop, unresolved = dedupe_games.resolve(_rows(
        (1, "2000-05-01", 1, "a"), (1, "2000-06-01", 2, "b"),
    ))
    assert not drop and not unresolved


def test_repair_drops_duplicates_and_recounts_careers():
    """End to end: the row goes, and every total derived from it follows."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "fixture.db"
        con = sqlite3.connect(path)
        con.executescript("""
          CREATE TABLE players (
            player_id INTEGER, player TEXT, career_games INTEGER,
            career_goals INTEGER, finals_played INTEGER,
            debut_season INTEGER, final_season INTEGER
          );
          CREATE TABLE games (
            player_id INTEGER, season INTEGER, date TEXT, career_game_no INTEGER,
            goals INTEGER, is_final INTEGER, birth_year_est INTEGER
          );
          -- Career totals below are the inflated ones a build without the
          -- gate produces, so the recount has something to correct.
          INSERT INTO players VALUES (5230,'Jim Stewart',4,6,0,1909,1909);
          INSERT INTO games VALUES
            (5230,1909,'1909-06-19',67,2,0,1884),
            (5230,1909,'1909-07-03',68,2,0,1884),
            (5230,1909,'1909-07-03', 1,2,0,1888),
            (5230,1909,'1909-09-04',69,0,0,1884);
        """)
        con.commit()

        dropped, unresolved = repair_database.drop_duplicate_games(con)
        assert dropped == 1
        assert not unresolved

        assert con.execute(
            "SELECT career_game_no, birth_year_est FROM games "
            "WHERE player_id=5230 AND date='1909-07-03'"
        ).fetchall() == [(68, 1884)]

        assert con.execute(
            "SELECT career_games, career_goals FROM players WHERE player_id=5230"
        ).fetchone() == (3, 4)
        con.close()


def run():
    for name, function in sorted(globals().items()):
        if name.startswith("test_"):
            function()
    print("duplicate player-game tests: passed")


if __name__ == "__main__":
    run()
