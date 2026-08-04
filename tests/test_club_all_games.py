#!/usr/bin/env python3
"""Regression tests for the All Games source.

The fixture below is real AFL Tables markup, trimmed to a handful of rows and
kept byte-for-byte otherwise: the sortable-table wrapper, the ``colspan`` season
caption, the bare ``<tt>`` element around the opposition scoring, the blank
crowd cell, the trailing space inside score cells, and the ``tfoot`` totals row.
Hand-written fixtures hid a parser regression once already, so the shapes that
actually appear in downloaded HTML are the ones tested here.

Run:  python utils/test_club_all_games.py
"""
from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
#: The modules under test live in utils/, which is not a package on the path.
sys.path.insert(0, str(ROOT / "utils"))
sys.path.insert(0, str(ROOT))

from club_all_games import (ParseError, check_against_footers, parse_all_games,
                            parse_attendance, parse_date_text, parse_game_key,
                            parse_scoring, parse_season_footers, quarter_only,
                            score_to_points)
import load_club_all_games as loader

FIXTURE = """<html><head><title>AFL Tables - Richmond All Games - By Season</title></head>
<body><h1>Richmond - All Games - By Season</h1>
<table style="font: 12px Tahoma;" border="2" width="100%" class="sortable" id="sortableTable0">
<thead><tr><th colspan="13"> 2026</th></tr>
<tr><th width="3%"><a href="#" columnid="0" title="Click to sort">Rnd</a><span class="tableSortArrow">&nbsp;&nbsp;</span></th>
<th width="2%">T</th><th align="left" width="13%">Opponent</th><th width="13%">Scoring</th><th width="4%">F</th>
<th width="13%">Scoring</th><th width="4%">A</th><th width="2%">R</th><th width="5%">M</th><th width="6%">W-D-L</th>
<th width="11%">Venue</th><th width="7%">Crowd</th><th width="21%">Date</th></tr></thead><tbody>
<tr><td align="center"><a href="https://afltables.com/afl/stats/games/2026/031420260312.html">R2</a></td><td align="center">A</td><td>Carlton</td><td align="center">3.3 5.6 7.12 9.17 </td><td align="center">71</td><td align="center"><tt>6.4 9.9 9.13 10.15 </tt></td><td align="center">75</td><td align="center">L</td><td align="center">-4</td><td align="center">0-0-1</td><td align="center">M.C.G.</td><td align="center"> 74313</td><td align="center">Thu 12-Mar-2026 7:30 PM</td></tr>
<tr><td align="center"><a href="https://afltables.com/afl/stats/games/2026/142020260321.html">R3</a></td><td align="center">H</td><td>Gold Coast</td><td align="center">2.2 5.4 8.5 9.6 </td><td align="center">60</td><td align="center"><tt>2.2 10.5 14.9 19.14 </tt></td><td align="center">128</td><td align="center">L</td><td align="center">-68</td><td align="center">0-0-2</td><td align="center">M.C.G.</td><td align="center"> 30468</td><td align="center">Sat 21-Mar-2026 1:15 PM</td></tr>
<tr><td align="center"><a href="https://afltables.com/afl/stats/games/2026/091420260502.html">R9</a></td><td align="center">F</td><td>Geelong</td><td align="center">2.0 4.0 6.0 8.0 </td><td align="center">48</td><td align="center"><tt>1.0 3.0 5.0 7.0 </tt></td><td align="center">42</td><td align="center">W</td><td align="center">6</td><td align="center">1-0-2</td><td align="center">Gabba</td><td align="center"></td><td align="center">Sat 02-May-2026</td></tr>
</tbody>
<tfoot><tr><td colspan="3"><b>Totals</b></td><td align="center"><b>19.5</b></td><td align="center"><b> 179</b></td><td align="center"><b>36.29</b></td><td align="center"><b> 245</b></td><td align="center" colspan="3"><b>P:3 W:1 D:0 L:2</b></td><td>&nbsp;</td><td align="center"><b>104781</b></td><td>&nbsp;</td></tr></tfoot>
</table></body></html>
"""


class ScoringTests(unittest.TestCase):
    def test_score_to_points(self):
        self.assertEqual(score_to_points("9.17"), (9, 17, 71))
        self.assertEqual(score_to_points("0.0"), (0, 0, 0))

    def test_bad_score_raises(self):
        for value in ("9-17", "", "9.", "nine.one"):
            with self.assertRaises(ParseError):
                score_to_points(value)

    def test_scoring_is_cumulative_not_per_quarter(self):
        cumulative = parse_scoring("3.3 5.6 7.12 9.17")
        self.assertEqual([c[2] for c in cumulative], [21, 36, 54, 71])
        self.assertEqual(quarter_only(cumulative),
                         [(3, 3, 21), (2, 3, 15), (2, 6, 18), (2, 5, 17)])

    def test_wrong_quarter_count_raises(self):
        with self.assertRaises(ParseError):
            parse_scoring("3.3 5.6 7.12")


class KeyAndDateTests(unittest.TestCase):
    def test_game_key_is_order_independent(self):
        # Both club pages link the same key, with team codes in ascending
        # order. It identifies the match, never the home side.
        url, key, codes, date = parse_game_key(
            "https://afltables.com/afl/stats/games/2026/031420260312.html")
        self.assertEqual(key, "031420260312")
        self.assertEqual(codes, ("03", "14"))
        self.assertEqual(date, "2026-03-12")

    def test_bad_key_raises(self):
        with self.assertRaises(ParseError):
            parse_game_key("https://afltables.com/afl/stats/games/2026/abc.html")

    def test_date_with_time(self):
        self.assertEqual(parse_date_text("Thu 12-Mar-2026 7:30 PM"),
                         ("2026-03-12", "19:30", "2026-03-12T19:30"))

    def test_date_without_time_stays_valid(self):
        date, time, stamp = parse_date_text("Sat 02-May-2026")
        self.assertEqual(date, "2026-05-02")
        self.assertIsNone(time)
        self.assertIsNone(stamp)

    def test_blank_crowd_is_null_not_zero(self):
        self.assertIsNone(parse_attendance(""))
        self.assertIsNone(parse_attendance(" "))
        self.assertEqual(parse_attendance(" 74313"), 74313)
        self.assertEqual(parse_attendance("74,313"), 74313)


class HeadingTests(unittest.TestCase):
    def test_combined_heading_uses_last_segment_for_display(self):
        from club_all_games import clean_club_heading
        self.assertEqual(clean_club_heading("South Melbourne/Sydney"), "Sydney")
        self.assertEqual(clean_club_heading("Footscray/Western Bulldogs"),
                         "Western Bulldogs")
        self.assertEqual(clean_club_heading("Richmond"), "Richmond")


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.rows, self.errors = parse_all_games(FIXTURE, "richmond")

    def test_parses_every_row_without_error(self):
        self.assertEqual(len(self.rows), 3)
        self.assertEqual(self.errors, [])

    def test_tt_wrapped_opposition_scoring_is_read(self):
        self.assertEqual(self.rows[0].scoring_against_raw, "6.4 9.9 9.13 10.15")
        self.assertEqual(self.rows[0].points_against, 75)

    def test_away_row_orientation(self):
        row = self.rows[0]
        self.assertEqual((row.home_team_raw, row.away_team_raw),
                         ("Carlton", "Richmond"))

    def test_home_row_orientation(self):
        row = self.rows[1]
        self.assertEqual((row.home_team_raw, row.away_team_raw),
                         ("Richmond", "Gold Coast"))

    def test_finals_row_is_not_given_a_home_team(self):
        row = self.rows[2]
        self.assertEqual(row.team_position, "F")
        self.assertEqual(row.is_final, 1)
        self.assertIsNone(row.home_team_raw)
        self.assertIsNone(row.away_team_raw)
        self.assertIsNone(row.attendance)

    def test_flat_row_carries_cumulative_quarters(self):
        flat = self.rows[0].flat()
        self.assertEqual(flat["q1_for_points"], 21)
        self.assertEqual(flat["q4_for_points"], flat["points_for"])
        self.assertEqual(flat["q4_against_points"], flat["points_against"])

    def test_season_footer_reconciles(self):
        footers = parse_season_footers(FIXTURE)
        self.assertEqual(footers[2026]["played"], 3)
        self.assertEqual(check_against_footers(self.rows, footers), [])

    def test_footer_mismatch_is_reported(self):
        broken = dict(parse_season_footers(FIXTURE))
        broken[2026] = dict(broken[2026], played=4, wins=2)
        problems = check_against_footers(self.rows, broken)
        self.assertEqual(len(problems), 2)

    def test_row_level_arithmetic_is_enforced(self):
        corrupted = FIXTURE.replace(
            '<td align="center">-4</td>', '<td align="center">-5</td>', 1)
        with self.assertRaises(ParseError):
            parse_all_games(corrupted, "richmond")

    def test_lenient_mode_collects_instead_of_raising(self):
        corrupted = FIXTURE.replace(
            '<td align="center">-4</td>', '<td align="center">-5</td>', 1)
        rows, errors = parse_all_games(corrupted, "richmond", strict=False)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(errors), 1)


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        club_dir = root / "raw" / "richmond"
        club_dir.mkdir(parents=True)
        (club_dir / "afltables_all_games.html").write_text(
            FIXTURE, encoding="windows-1252")
        self.raw_dir = root / "raw"
        self.db = root / "test.db"
        con = sqlite3.connect(self.db)
        con.execute("""CREATE TABLE matches (
            match_id INTEGER, match_key TEXT, season INTEGER, round TEXT,
            match_date TEXT, venue TEXT, home_team TEXT, away_team TEXT,
            home_team_now TEXT, away_team_now TEXT,
            home_score INTEGER, away_score INTEGER, home_away_known INTEGER,
            attendance INTEGER, home_q1 INTEGER, home_q2 INTEGER,
            home_q3 INTEGER, home_q4 INTEGER, away_q1 INTEGER, away_q2 INTEGER,
            away_q3 INTEGER, away_q4 INTEGER)""")
        con.executemany(
            "INSERT INTO matches (match_id, season, round, match_date, venue, "
            "home_team, away_team, home_team_now, away_team_now, home_score, "
            "away_score, home_away_known) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
            [(1, 2026, "R2", "2026-03-12", "M.C.G.", "Carlton", "Richmond",
              "Carlton", "Richmond", 75, 71),
             (2, 2026, "R3", "2026-03-21", "M.C.G.", "Richmond", "Gold Coast",
              "Richmond", "Gold Coast", 60, 128),
             (3, 2026, "R9", "2026-05-02", "Gabba", "Geelong", "Richmond",
              "Geelong", "Richmond", 42, 48)])
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, **kwargs):
        return loader.run(self.db, self.raw_dir, **kwargs)

    def test_links_and_applies(self):
        self._run()
        con = sqlite3.connect(self.db)
        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM club_match_sources "
                        "WHERE match_status = 'unique'").fetchone()[0], 3)
        # Away-page orientation: Carlton hosted, so Carlton's quarters are the
        # source row's 'against' column.
        home_q1, away_q1, attendance = con.execute(
            "SELECT home_q1_points, away_q1_points, attendance "
            "FROM match_details WHERE match_id = 1").fetchone()
        self.assertEqual((home_q1, away_q1, attendance), (40, 21, 74313))
        con.close()

    def test_finals_row_oriented_from_the_match_database(self):
        self._run()
        con = sqlite3.connect(self.db)
        home_q4, away_q4, orientation = con.execute(
            "SELECT home_q4_points, away_q4_points, orientation "
            "FROM match_details WHERE match_id = 3").fetchone()
        # Geelong is the home side per matches; Richmond's page says only 'F'.
        self.assertEqual((home_q4, away_q4), (42, 48))
        self.assertEqual(orientation, "away_page")
        con.close()

    def test_blank_crowd_stays_null_on_matches(self):
        self._run()
        con = sqlite3.connect(self.db)
        self.assertIsNone(
            con.execute("SELECT attendance FROM matches WHERE match_id = 3"
                        ).fetchone()[0])
        con.close()

    def test_apply_only_refills_after_a_rebuild(self):
        self._run()
        con = sqlite3.connect(self.db)
        # afl/derive_matches.py replaces the table, clearing enrichment.
        con.execute("UPDATE matches SET attendance = NULL, home_q1 = NULL")
        con.commit()
        con.close()
        self._run(apply_only=True)
        con = sqlite3.connect(self.db)
        attendance, home_q1 = con.execute(
            "SELECT attendance, home_q1 FROM matches WHERE match_id = 1"
        ).fetchone()
        self.assertEqual((attendance, home_q1), (74313, 40))
        con.close()

    def test_score_mismatch_is_not_linked(self):
        con = sqlite3.connect(self.db)
        con.execute("UPDATE matches SET home_score = 99 WHERE match_id = 2")
        con.commit()
        con.close()
        self._run()
        con = sqlite3.connect(self.db)
        status = con.execute(
            "SELECT match_status FROM club_match_sources WHERE round = 'R3'"
        ).fetchone()[0]
        self.assertEqual(status, "score_mismatch")
        self.assertIsNone(
            con.execute("SELECT attendance FROM matches WHERE match_id = 2"
                        ).fetchone()[0])
        con.close()

    def test_combined_page_heading_still_links_by_club_now(self):
        # Reproduces the real bug: Sydney's actual All Games page is titled
        # "South Melbourne/Sydney - All Games - By Season", so every row's
        # parsed club identity is that literal combined string. A pre-1982
        # row plays as "South Melbourne" in `matches`; linking must not
        # depend on the page heading matching that text at all.
        sydney_page = """<html><body><h1>South Melbourne/Sydney - All Games - By Season</h1>
<table><thead><tr><th colspan="13"> 1980</th></tr></thead><tbody>
<tr><td align="center"><a href="https://afltables.com/afl/stats/games/1980/092019800412.html">R1</a></td><td align="center">H</td><td>Fitzroy</td><td align="center">2.0 4.0 6.0 8.0 </td><td align="center">48</td><td align="center"><tt>1.0 3.0 5.0 7.0 </tt></td><td align="center">42</td><td align="center">W</td><td align="center">6</td><td align="center">1-0-0</td><td align="center">Lake Oval</td><td align="center"> 12000</td><td align="center">Sat 12-Apr-1980</td></tr>
</tbody></table></body></html>
"""
        club_dir = self.raw_dir / "sydney"
        club_dir.mkdir()
        (club_dir / "afltables_all_games.html").write_text(
            sydney_page, encoding="windows-1252")
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO matches (match_id, season, round, match_date, venue, "
            "home_team, away_team, home_team_now, away_team_now, home_score, "
            "away_score, home_away_known) VALUES "
            "(4, 1980, 'R1', '1980-04-12', 'Lake Oval', 'South Melbourne', "
            "'Fitzroy', 'Sydney', 'Fitzroy', 48, 42, 1)")
        con.commit()
        con.close()

        self._run(club_ids=["sydney"])
        con = sqlite3.connect(self.db)
        status = con.execute(
            "SELECT match_status, match_id FROM club_match_sources "
            "WHERE source_club_id = 'sydney'").fetchone()
        self.assertEqual(status, ("unique", 4))
        home_source, orientation = con.execute(
            "SELECT home_source_club, orientation FROM match_details "
            "WHERE match_id = 4").fetchone()
        self.assertEqual(home_source, "Sydney")
        self.assertEqual(orientation, "home_page")
        con.close()

    def test_disputed_attendance_is_withheld(self):
        # Carlton's own page for the same match: sides swapped, and a crowd
        # that disagrees with Richmond's page by seven.
        mirror = """<html><body><h1>Carlton - All Games - By Season</h1>
<table><thead><tr><th colspan="13"> 2026</th></tr></thead><tbody>
<tr><td align="center"><a href="https://afltables.com/afl/stats/games/2026/031420260312.html">R2</a></td><td align="center">H</td><td>Richmond</td><td align="center">6.4 9.9 9.13 10.15 </td><td align="center">75</td><td align="center"><tt>3.3 5.6 7.12 9.17 </tt></td><td align="center">71</td><td align="center">W</td><td align="center">4</td><td align="center">1-0-0</td><td align="center">M.C.G.</td><td align="center"> 74320</td><td align="center">Thu 12-Mar-2026 7:30 PM</td></tr>
</tbody></table></body></html>
"""
        club_dir = self.raw_dir / "carlton"
        club_dir.mkdir()
        (club_dir / "afltables_all_games.html").write_text(
            mirror, encoding="windows-1252")
        self._run()
        con = sqlite3.connect(self.db)
        issues = con.execute(
            "SELECT field FROM club_match_source_issues").fetchall()
        self.assertIn(("attendance",), issues)
        self.assertIsNone(
            con.execute("SELECT attendance FROM match_details "
                        "WHERE match_id = 1").fetchone()[0])
        con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
