"""What analysing a captured grid is allowed to cost.

The Grid Solver lists every captured board in its picker and analyses one.
Those are different jobs with different budgets: the listing runs on every
page load whichever source is chosen, while the counts and the
nine-intersection check belong to the single board a solver opens.

Analysing the whole AFL library the expensive way is over two minutes --
fifteen boards, six criterion counts and nine square counts each, every one
of them a sweep of 694k player-games. That was being spent before the page
drew anything. These tests hold the two jobs apart by counting the
player-counting queries each one issues.
"""

import sqlite3

import sports
from afl import historic_grids as HG


class Recorder:
    """A connection that remembers every statement run through it."""

    def __init__(self, con):
        self._con = con
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append(" ".join(sql.split()))
        return self._con.execute(sql, params)

    @property
    def counts(self):
        """Statements that count eligible players -- the expensive ones."""
        return [s for s in self.statements if s.startswith("SELECT COUNT(*)")]


def _con():
    """A database with just enough of the AFL shape to answer the grid."""
    con = sqlite3.connect(":memory:")
    con.execute("""CREATE TABLE players (
        player_id INTEGER PRIMARY KEY, player TEXT, career_games INTEGER,
        career_goals INTEGER, n_clubs INTEGER, debut_season INTEGER,
        final_season INTEGER, obscurity REAL)""")
    con.execute("""CREATE TABLE games (
        player_id INTEGER, player TEXT, season INTEGER, date TEXT,
        round TEXT, venue TEXT, club_now TEXT, club_hist TEXT,
        opponent TEXT, career_game_no INTEGER, goals INTEGER,
        disposals INTEGER, is_final INTEGER, result TEXT)""")
    con.executemany(
        "INSERT INTO players VALUES (?,?,?,?,?,?,?,?)",
        [(1, "Ann Alpha", 200, 40, 1, 2010, 2021, 50.0),
         (2, "Bo Beta", 40, 5, 2, 2015, 2019, 80.0)])
    con.executemany(
        "INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(1, "Ann Alpha", 2015, "2015-04-01", "1", "MCG", "Carlton",
          "Carlton", "Geelong", 1, 3, 25, 0, "W"),
         (2, "Bo Beta", 2015, "2015-04-01", "1", "MCG", "Carlton",
          "Carlton", "Geelong", 1, 0, 12, 0, "L")])
    return con


GRID = HG.HistoricGrid(
    number=9001, date="2026-08-10", source="test",
    cols=("Carlton", "PLAYED IN 2010s", "150+ GAMES PLAYED"),
    rows=("3+ GOALS GAME", "20+ DISPOSALS IN A GAME", "ONE-CLUB PLAYER"))


def test_the_picker_listing_counts_nothing():
    """Six criteria and nine squares of counting, skipped for a listing."""
    con = Recorder(_con())

    report = HG.analyse(GRID, con, sports.get("afl"),
                        check_squares=False, count_eligible=False)

    assert con.counts == []
    assert report.supported_count == 6
    assert all(c.eligible is None for c in report.all_criteria)
    assert report.squares_ok is None


def test_the_listing_still_reads_the_database_for_support():
    """Skipping the counts must not turn it into a parse-only guess.

    A criterion whose data layer is not loaded has to stay declined, or the
    picker says "Ready" for a board that cannot be played.
    """
    con = Recorder(_con())          # no awards or person_links tables
    grid = HG.HistoricGrid(
        number=9002, date="2026-08-10", source="test",
        cols=("Carlton", "PLAYED IN 2010s", "BEST & FAIREST"),
        rows=("3+ GOALS GAME", "20+ DISPOSALS IN A GAME", "ONE-CLUB PLAYER"))

    report = HG.analyse(grid, con, sports.get("afl"),
                        check_squares=False, count_eligible=False)

    assert con.counts == []
    declined = {c.text: c.reason for c in report.unsupported}
    assert "BEST & FAIREST" in declined
    assert "Award data is not loaded" in declined["BEST & FAIREST"]


def test_opening_one_board_counts_its_criteria_and_its_squares():
    con = Recorder(_con())

    report = HG.analyse(GRID, con, sports.get("afl"))

    assert len(con.counts) == 15            # six criteria, nine squares
    assert [c.eligible for c in report.all_criteria] == [1, 1, 1, 2, 2, 1]
    assert report.squares_ok is True
