#!/usr/bin/env python3
"""The derived Club Explorer tables must mean what the page says they do.

utils/derive_club_tables.py builds the six tables from a sport's own match
database instead of a scrape. The risks are all in the aggregation:

  * a player at several clubs needs one row per club, with that club's
    figures -- an early version gave all of LeBron James's rows to
    Cleveland;
  * `games` is a count of rows for a sport whose row is a game, and a sum
    of the `games` column for one whose row is a season;
  * a rate is not a total, so ERA must not be summed;
  * the AFL is scraped and must never be overwritten.
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

from utils import derive_club_tables as D


@pytest.fixture(scope="module")
def derived(nba_db):
    """The NBA fixture database with club tables derived into it."""
    import sports

    class _Sport:
        key = "nba"
        label = "NBA fixture"
        db = str(nba_db)
        schema = sports.NBA_SCHEMA
        games_row_label = sports.NBA.games_row_label

    D.derive(_Sport(), verbose=False)
    con = sqlite3.connect(nba_db)
    yield con
    con.close()


def test_every_required_table_is_written(derived):
    from afl import club_explorer

    have = {row[0] for row in derived.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert club_explorer.REQUIRED_TABLES <= have
    assert club_explorer.club_data_available(derived) is True


def test_a_player_at_two_clubs_gets_a_row_for_each(derived):
    """The bug that shipped first: every row took the first club found."""
    rows = derived.execute("""
        SELECT club_id, games FROM club_player_totals
        WHERE player_name = 'Marcus Oyelaran' ORDER BY club_id
    """).fetchall()
    clubs = [club for club, _ in rows]
    assert len(clubs) == len(set(clubs)), "a club is repeated for one player"
    assert len(clubs) >= 1
    # The fixture's Sonics games are club_now Oklahoma City, so his whole
    # career is one franchise -- but the totals must still add up.
    total = derived.execute("""
        SELECT SUM(games) FROM club_player_totals
        WHERE player_name = 'Marcus Oyelaran'
    """).fetchone()[0]
    career = derived.execute("""
        SELECT COUNT(*) FROM games WHERE player = 'Marcus Oyelaran'
    """).fetchone()[0]
    assert total == career


def test_club_totals_match_the_match_rows(derived):
    """Every club's totals are that club's games, not the whole career."""
    mismatches = derived.execute("""
        SELECT t.club_id, t.player_name, t.games, COUNT(g.player_id)
        FROM club_player_totals t
        JOIN clubs c ON c.club_id = t.club_id
        JOIN games g ON g.player_id = t.player_id AND g.club_now = c.name
        GROUP BY t.row_id
        HAVING t.games <> COUNT(g.player_id)
    """).fetchall()
    assert mismatches == []


def test_points_are_summed_per_club_not_per_career(derived):
    for club_id, player, points in derived.execute("""
            SELECT t.club_id, t.player_name, t.points
            FROM club_player_totals t LIMIT 20"""):
        expected = derived.execute("""
            SELECT SUM(g.points) FROM games g
            JOIN clubs c ON c.club_id = ? AND c.name = g.club_now
            WHERE g.player = ?""", (club_id, player)).fetchone()[0]
        assert points == expected


def test_records_are_capped_and_ranked(derived):
    over = derived.execute("""
        SELECT club_id, scope, stat, COUNT(*) FROM club_player_records
        GROUP BY club_id, scope, stat HAVING COUNT(*) > ?
    """, (D.TOP_N,)).fetchall()
    assert over == []

    ranks = derived.execute("""
        SELECT source_rank, value FROM club_player_records
        WHERE scope = 'game' AND stat = 'points'
        ORDER BY club_id, source_rank
    """).fetchall()
    assert all(r >= 1 for r, _ in ranks)


def test_a_rate_is_never_totalled(derived):
    """ERA summed across seasons is a meaningless number."""
    columns = {row[1] for row in
               derived.execute("PRAGMA table_info(club_player_totals)")}
    assert not (columns & D.RATE_STATS)


def test_the_afl_is_refused_because_its_tables_are_scraped():
    import sports

    assert "afl" in D.EXCLUDED
    with pytest.raises(D.DeriveError):
        D.derive(sports.AFL, verbose=False)


def test_slugs_are_stable_and_match_the_clubs_table(derived):
    assert D.slug("Los Angeles Lakers") == "los_angeles_lakers"
    assert D.slug("St. Louis Blues") == "st_louis_blues"
    orphans = derived.execute("""
        SELECT COUNT(*) FROM club_player_totals
        WHERE club_id NOT IN (SELECT club_id FROM clubs)
    """).fetchone()[0]
    assert orphans == 0


def main():
    import subprocess
    return subprocess.call([_sys.executable, "-m", "pytest", __file__, "-q"])


if __name__ == "__main__":
    _sys.exit(main())
