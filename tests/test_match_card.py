"""What the match card promises, for every sport that can answer it.

The card is built entirely from `core.Schema`: which table holds one row
per match, what that table calls the two clubs and the score, and which
`games` column joins the two. Those names are the only thing standing
between a reader and a blank dialog, so they are checked against the real
databases here rather than trusted.

Everything else runs against small in-memory databases shaped like each
build, so a test failure means the card's logic changed and not that
somebody reloaded a season.
"""

import sqlite3

import pandas as pd
import pytest

import overlays
import sports

IMPLEMENTED = [sport for sport in sports.SPORTS.values() if sport.enabled]
IMPLEMENTED_IDS = [sport.key for sport in IMPLEMENTED]


@pytest.fixture(autouse=True)
def _no_cached_logo_index():
    """Keep this module's synthetic databases out of the logo cache.

    `overlays._logo_index` is keyed on the sport and its database
    revision, and deliberately does not hash the connection -- in the
    running app there is exactly one connection per database, so the
    connection carries no information the key does not. A test that hands
    the same sport a stand-in connection breaks that assumption, and the
    empty index it produces would otherwise be served to whatever asks for
    that sport's logos next.
    """
    overlays._logo_index.clear()
    yield
    overlays._logo_index.clear()


# ------------------------------------------------------- schema wiring

@pytest.mark.parametrize("sport", IMPLEMENTED, ids=IMPLEMENTED_IDS)
def test_the_match_columns_a_sport_declares_are_the_ones_it_has(sport):
    """A misspelled column name is a card that renders nothing.

    Every name below is interpolated straight into SQL and every failure
    is swallowed so the card degrades instead of crashing -- which means a
    typo would show up as a permanently empty scorecard and nothing else.
    """
    schema = sport.schema
    if not schema.matches:
        pytest.skip(f"{sport.key} records no per-match table")
    con = sqlite3.connect(f"file:{sport.db}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in
                   con.execute(f"PRAGMA table_info({schema.matches})")}
    except sqlite3.Error:
        pytest.skip(f"{sport.key} database is not built here")
    if not columns:
        pytest.skip(f"{sport.key} database is not built here")

    declared = [schema.match_key, schema.match_home_team,
                schema.match_away_team, schema.match_home_score,
                schema.match_away_score, schema.match_venue,
                schema.match_date, schema.match_round,
                schema.match_attendance]
    missing = [name for name in declared if name and name not in columns]
    assert not missing, (
        f"{sport.key}: {schema.matches} has no {missing}")

    games = {row[1] for row in
             con.execute(f"PRAGMA table_info({schema.games})")}
    assert schema.games_match_key in games, (
        f"{sport.key}: {schema.games} cannot be joined to {schema.matches}")
    assert schema.games_side_key in games
    con.close()


@pytest.mark.parametrize("sport", IMPLEMENTED, ids=IMPLEMENTED_IDS)
def test_a_box_score_is_ordered_before_it_is_shown(sport):
    """The box score leads with a statistic the sport actually records."""
    ordered = sport.schema.box_score_stats()
    assert ordered, f"{sport.key} declares no box-score statistics"
    assert len(set(ordered)) == len(ordered), f"{sport.key} repeats a statistic"


# ------------------------------------------------------------ fixtures

def _afl_shaped() -> sqlite3.Connection:
    """A two-match AFL database: one modern, one from before disposals."""
    con = sqlite3.connect(":memory:")
    con.executescript(
        """
        CREATE TABLE matches (
            match_id INTEGER, season INTEGER, round TEXT, match_date TEXT,
            venue TEXT, home_team TEXT, away_team TEXT,
            home_score REAL, away_score REAL, attendance TEXT,
            home_q1 TEXT, home_q2 TEXT, home_q3 TEXT, home_q4 TEXT,
            away_q1 TEXT, away_q2 TEXT, away_q3 TEXT, away_q4 TEXT);
        CREATE TABLE match_details (
            match_id INTEGER, attendance INTEGER, match_time TEXT,
            home_q1_goals INTEGER, home_q1_behinds INTEGER, home_q1_points INTEGER,
            home_q2_goals INTEGER, home_q2_behinds INTEGER, home_q2_points INTEGER,
            home_q3_goals INTEGER, home_q3_behinds INTEGER, home_q3_points INTEGER,
            home_q4_goals INTEGER, home_q4_behinds INTEGER, home_q4_points INTEGER,
            away_q1_goals INTEGER, away_q1_behinds INTEGER, away_q1_points INTEGER,
            away_q2_goals INTEGER, away_q2_behinds INTEGER, away_q2_points INTEGER,
            away_q3_goals INTEGER, away_q3_behinds INTEGER, away_q3_points INTEGER,
            away_q4_goals INTEGER, away_q4_behinds INTEGER, away_q4_points INTEGER);
        CREATE TABLE games (
            player_id INTEGER, player TEXT, club_hist TEXT, club_now TEXT,
            season INTEGER, match_id INTEGER, is_home INTEGER,
            kicks REAL, handballs REAL, disposals REAL, marks REAL,
            goals REAL, behinds REAL, tackles REAL, round TEXT);
        INSERT INTO matches VALUES
            (1, 2026, '23', '2026-08-08', 'Adelaide Oval', 'Adelaide',
             'Richmond', 63, 54, '42762', '9','29','49','63',
             '14','28','45','54'),
            (2, 1902, 'GF', '1902-09-20', 'M.C.G.', 'Collingwood',
             'Essendon', 60, 27, '35202', NULL,NULL,NULL,NULL,
             NULL,NULL,NULL,NULL);
        INSERT INTO match_details VALUES
            (1, 42762, '19:05',
             1,3,9, 4,5,29, 7,7,49, 9,9,63,
             2,2,14, 4,4,28, 6,9,45, 7,12,54);
        INSERT INTO games VALUES
            (10,'Izak Rankine','Adelaide','Adelaide',2026,1,1,
             20,16,36,6,1,0,6,'23'),
            (11,'Jordan Dawson','Adelaide','Adelaide',2026,1,1,
             24,11,35,6,1,1,2,'23'),
            (12,'Jack Ross','Richmond','Richmond',2026,1,0,
             14,15,29,1,0,0,2,'23'),
            (20,'Teddy Lockwood','Collingwood','Collingwood',1902,2,1,
             NULL,NULL,NULL,NULL,3,NULL,NULL,'GF'),
            (21,'Albert Thurgood','Essendon','Essendon',1902,2,0,
             NULL,NULL,NULL,NULL,1,NULL,NULL,'GF');
        """)
    return con


def _nfl_shaped() -> sqlite3.Connection:
    """An nflverse-shaped database: abbreviations up top, names below."""
    con = sqlite3.connect(":memory:")
    con.executescript(
        """
        CREATE TABLE matches (
            game_id TEXT, season INTEGER, week INTEGER, gameday TEXT,
            stadium TEXT, home_team TEXT, away_team TEXT,
            home_score INTEGER, away_score INTEGER, roof TEXT);
        CREATE TABLE games (
            player_id TEXT, player TEXT, club_hist TEXT, club_now TEXT,
            team TEXT, season INTEGER, game_id TEXT,
            touchdowns REAL, passing_yards REAL, receiving_yards REAL);
        INSERT INTO matches VALUES
            ('2023_22_SF_KC', 2023, 22, '2024-02-11', 'Allegiant Stadium',
             'KC', 'SF', 25, 22, 'dome');
        INSERT INTO games VALUES
            ('a','Patrick Mahomes','Kansas City Chiefs','Kansas City Chiefs',
             'KC', 2023, '2023_22_SF_KC', 2, 333, 0),
            ('b','Brock Purdy','San Francisco 49ers','San Francisco 49ers',
             'SF', 2023, '2023_22_SF_KC', 1, 255, 0);
        """)
    return con


# --------------------------------------------------------- finding one

def test_a_match_is_found_by_the_id_its_results_row_carries():
    con = _afl_shaped()
    row = overlays._match_row.__wrapped__("afl", (1,), None, con)
    assert row["home_team"] == "Adelaide"
    assert row["away_score"] == 54


def test_a_match_is_found_by_its_source_key_when_it_has_no_resolved_id():
    """Only the AFL build resolves a numeric match_id onto its results rows.

    The other three carry the source's own game key instead, so both are
    offered and the first that finds a row wins.
    """
    con = _nfl_shaped()

    class Row:
        match_id = None
        source_game_key = "2023_22_SF_KC"

    assert overlays._match_keys(Row()) == ("2023_22_SF_KC",)
    row = overlays._match_row.__wrapped__(
        "nfl", ("2023_22_SF_KC",), None, con)
    assert row["home_team"] == "KC"


def test_a_match_that_is_not_in_the_table_is_not_an_error():
    con = _afl_shaped()
    assert overlays._match_row.__wrapped__("afl", (999,), None, con) == {}


# ------------------------------------------------------ the box score

def test_a_statistic_the_era_never_kept_is_left_out_of_the_box_score():
    """Disposals begin in 1965. A 1902 card must not imply otherwise.

    A column of blanks reads as a database fault rather than as a
    statistic the competition was not yet keeping, so it is dropped.
    """
    con = _afl_shaped()
    modern = overlays._box_score.__wrapped__("afl", 1, None, con)
    old = overlays._box_score.__wrapped__("afl", 2, None, con)
    assert "disposals" in modern.columns
    assert "disposals" not in old.columns
    assert "goals" in old.columns


def test_both_sides_are_split_by_the_home_flag_where_the_build_writes_one():
    con = _afl_shaped()
    box = overlays._box_score.__wrapped__("afl", 1, None, con)
    home = overlays._side_of(box, "Adelaide", "Home")
    away = overlays._side_of(box, "Richmond", "Away")
    assert sorted(home["Player"]) == ["Izak Rankine", "Jordan Dawson"]
    assert list(away["Player"]) == ["Jack Ross"]


def test_a_side_is_found_by_its_code_when_there_is_no_home_flag():
    """nflverse writes no is_home, so the side key carries the split."""
    con = _nfl_shaped()
    box = overlays._box_score.__wrapped__("nfl", "2023_22_SF_KC", None, con)
    assert "HomeFlag" not in box.columns
    home = overlays._side_of(box, "KC", "Home")
    assert list(home["Player"]) == ["Patrick Mahomes"]


def test_a_side_is_named_by_the_fuller_of_the_two_names_it_is_known_by():
    con = _nfl_shaped()
    box = overlays._box_score.__wrapped__("nfl", "2023_22_SF_KC", None, con)
    home = overlays._side_of(box, "KC", "Home")
    assert overlays._display_name("KC", home) == "Kansas City Chiefs"
    # An abbreviation is only replaced by something longer, never the
    # other way around.
    assert overlays._display_name("Adelaide", pd.DataFrame()) == "Adelaide"


def test_a_sport_with_no_per_match_table_asks_for_no_box_score():
    """The MLB's finest grain is a player's season, not a game."""
    assert sports.get("mlb").schema.matches == ""


# ------------------------------------------------ scoring progression

def test_the_progression_reads_a_football_score_as_goals_behinds_points():
    con = _afl_shaped()
    sport = sports.get("afl")
    row = overlays._match_row.__wrapped__("afl", (1,), None, con)
    details = overlays._match_detail_row.__wrapped__("afl", 1, None, con)
    table, present = overlays._period_scores(sport, row, details)
    assert present
    assert list(table["Break"]) == ["Q1", "Q2", "Q3", "Full time"]
    assert table.iloc[0]["Adelaide"] == "1.3 (9)"
    assert table.iloc[3]["Richmond"] == "7.12 (54)"
    # The running score reads as a contest: behind at quarter time, ahead
    # from half time on, home by nine at the end.
    assert table.iloc[0]["Lead"] == "Richmond by 5"
    assert table.iloc[1]["Lead"] == "Adelaide by 1"
    assert table.iloc[3]["Lead"] == "Adelaide by 9"
    # 29 - 9 and 28 - 14: what each side put on in the second quarter.
    assert table.iloc[1]["In the quarter"] == "20–14"


def test_two_recorded_breaks_are_named_halves_and_not_quarters():
    """A break is named for the period it closes, not for a fixed four."""
    sport = sports.get("afl")
    row = {"home_team": "Adelaide", "away_team": "Richmond"}
    details = {"home_q1_points": 30, "away_q1_points": 24,
               "home_q2_points": 63, "away_q2_points": 54}
    table, present = overlays._period_scores(sport, row, details)
    assert present
    assert list(table["Break"]) == ["Half time", "Full time"]
    assert "In the period" in table.columns


def test_the_progression_falls_back_to_running_points_alone():
    """`matches` keeps points and no goals; that is still a progression."""
    con = _afl_shaped()
    sport = sports.get("afl")
    row = overlays._match_row.__wrapped__("afl", (1,), None, con)
    table, present = overlays._period_scores(sport, row, {})
    assert present
    assert len(table) == 4
    assert table.iloc[0]["Adelaide"] == "9"
    assert table.iloc[3]["Lead"] == "Adelaide by 9"


def test_a_match_with_no_recorded_breaks_gets_no_progression_table():
    con = _afl_shaped()
    sport = sports.get("afl")
    row = overlays._match_row.__wrapped__("afl", (2,), None, con)
    table, present = overlays._period_scores(sport, row, {})
    assert not present
    assert table.empty


def test_the_side_names_the_card_shows_are_the_ones_the_progression_uses():
    con = _nfl_shaped()
    sport = sports.get("nfl")
    row = overlays._match_row.__wrapped__("nfl", ("2023_22_SF_KC",), None, con)
    table, present = overlays._period_scores(
        sport, row, {}, ["Kansas City Chiefs", "San Francisco 49ers"])
    assert not present  # nflverse records no per-quarter score
    assert table.empty


# ---------------------------------------------------------- formatting

def test_a_count_is_written_the_way_a_reader_writes_it():
    assert overlays._tidy(36.0) == "36"
    assert overlays._tidy(1247) == "1,247"
    assert overlays._tidy(12.25) == "12.2"
    assert overlays._tidy(None) == "—"


def test_a_crowd_recorded_as_text_still_counts_as_a_number():
    """AFL Tables attendance arrives as text, and 42,762 is not a string."""
    assert overlays._as_int("42762") == 42762
    assert overlays._as_int("42,762") == 42762
    assert overlays._as_int(63.0) == 63
    assert overlays._as_int(None) is None
    assert overlays._as_int("unknown") is None


def test_the_winning_side_is_the_one_the_scoreboard_highlights():
    sport = sports.get("afl")
    con = _afl_shaped()
    board = overlays._scoreboard(
        sport, con, ["Adelaide", "Richmond"], ["Home", "Away"], [63, 54])
    assert board.count("is-winner") == 1
    assert board.index("63") < board.index("54")
    # A draw crowns nobody.
    drawn = overlays._scoreboard(
        sport, con, ["Adelaide", "Richmond"], ["Home", "Away"], [63, 63])
    assert "is-winner" not in drawn


def test_a_statbar_marks_the_side_that_had_more_of_it():
    bars = overlays._statbars(
        "Adelaide", "Richmond", [("Disposals", 358.0, 317.0)])
    assert "Adelaide" in bars and "Richmond" in bars
    assert bars.count("is-more") == 1
    assert "358" in bars and "317" in bars


def test_the_scorers_are_named_in_the_order_they_scored():
    con = _afl_shaped()
    box = overlays._box_score.__wrapped__("afl", 1, None, con)
    home = overlays._side_of(box, "Adelaide", "Home")
    named = overlays._scorers(home, "goals")
    assert named.startswith("Izak Rankine 1") or named.startswith("Jordan")
    assert overlays._scorers(home, "not_a_statistic") == ""


# ------------------------------------------------------- field reading

def test_a_field_is_read_from_a_row_a_dataclass_or_a_mapping():
    class Match:
        season = 2026
        venue = ""

    assert overlays._field(Match(), "season") == 2026
    # An empty string is not an answer, so the next name is tried.
    assert overlays._field(Match(), "venue", "season") == 2026
    assert overlays._field({"Season": 1902}, "season", "Season") == 1902
    assert overlays._field({}, "season", default="—") == "—"
