#!/usr/bin/env python3
"""A hand-entered round must land as if the dataset had published it.

utils/afl/load_round_csv.py reads AFL Tables match pages pasted into CSVs,
so the risks are all in reading text nobody validated and in deciding who a
name refers to:

  * a name is not a key -- 460 names in afltables_player_index belong to
    more than one player, and picking the first gives one man another's
    game;
  * the round summary and the match pages are two statements of the same
    scores, so a disagreement between them is a transcription error and must
    stop the load rather than be written;
  * the folder is assembled by hand, so it collects misnamed and duplicated
    copies, and pairing on filenames would load a match twice;
  * `games` is replaced wholesale by every rebuild, so anything written here
    has to survive in a table of its own or quietly disappear.
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
from pathlib import Path

import pytest

from utils.afl import load_round_csv as L

SUMMARY = (
    'Western Bulldogs,2.3   5.6  10.9 11.11,77,'
    '"Thu 06-Aug-2026 7:30 PM Att: 25,052 Venue: Docklands"\n'
    'North Melbourne,6.0  10.2  12.7 15.10,100,'
    'North Melbourne won by 23 pts [ Match stats ]\n'
    '\n'
    'Adelaide,1.3   4.5   7.7   9.9,63,'
    '"Sat 08-Aug-2026 7:05 PM (7:35 PM) Att: 42,762 Venue: Adelaide Oval"\n'
    'Richmond,2.2   4.4   6.9  7.12,54,'
    'Adelaide won by 9 pts [ Match stats ]\n'
)

STATS_HEADER = ("#,Player,KI,MK,HB,DI,GL,BH,HO,TK,RB,IF,CL,CG,FF,FA,BR,CP,UP,"
                "CM,MI,1%,BO,GA,%P")


def stat_row(number, name, goals=0, behinds=0):
    """One player's line, positioned against STATS_HEADER's 25 columns."""
    cells = [str(number), f'"{name}"'] + [""] * 23
    cells[2] = "10"                             # KI, so one stat is non-blank
    cells[6] = str(goals) if goals else ""      # GL
    cells[7] = str(behinds) if behinds else ""  # BH
    return ",".join(cells)


def game_file(home, away, home_goals, home_behinds, away_goals, away_behinds,
              home_players, away_players):
    """A match page: two Match Statistics tables, two Player Details tables."""
    lines = []
    for club, players, goals, behinds in (
            (home, home_players, home_goals, home_behinds),
            (away, away_players, away_goals, away_behinds)):
        lines.append(f"{club} Match Statistics [ Season ][ Game by Game ]")
        lines.append(STATS_HEADER)
        share = goals
        for index, (number, name) in enumerate(players):
            mine = share if index == 0 else 0
            lines.append(stat_row(number, name, mine,
                                  behinds if index == 0 else 0))
        lines.append(f"Totals,{len(players) * 10},,,,{goals}")
        lines.append("")
    for club, players in ((home, home_players), (away, away_players)):
        lines.append(f"{club} Player Details")
        lines.append("#,Player,Age,Career Games (W-D-L W%),Career Goals (Ave.),"
                     f"{club} Games (W-D-L W%),{club} Goals (Ave.)")
        for number, name in players:
            lines.append(f'{number},"{name}",25y 100d,50 (25-0-25 50.00%),'
                         f'20 (0.40),50 (25-0-25 50.00%),20 (0.40)')
        lines.append("Totals,25y 100d,500 (250-0-250 50.00%),200 (0.40),,")
        lines.append("")
    return "\n".join(lines)


BULLDOGS = [(1, "Liberatore, Tom"), (2, "Bontempelli, Marcus")]
NORTH = [(3, "Simpkin, Jy"), (4, "Larkey, Nick")]
ADELAIDE = [(5, "Ah Chee, Callum"), (6, "Berry, Sam")]
RICHMOND = [(7, "Short, Jayden"), (8, "Lynch, Tom")]


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "Rd23.csv").write_text(SUMMARY, encoding="utf-8")
    (tmp_path / "dogs v north.csv").write_text(
        game_file("Western Bulldogs", "North Melbourne", 11, 11, 15, 10,
                  BULLDOGS, NORTH), encoding="utf-8")
    (tmp_path / "crows v tigers.csv").write_text(
        game_file("Adelaide", "Richmond", 9, 9, 7, 12, ADELAIDE, RICHMOND),
        encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------
# reading the summary


def test_quarter_scores_are_cumulative_and_the_last_one_is_the_score():
    quarters, score = L.parse_score("2.3   5.6  10.9 11.11")
    assert quarters == [(2, 3), (5, 6), (10, 9), (11, 11)]
    assert score == 11 * 6 + 11 == 77


def test_the_fixture_line_gives_date_time_crowd_and_ground():
    parsed = L.parse_fixture_text(
        "Thu 06-Aug-2026 7:30 PM Att: 25,052 Venue: Docklands")
    assert parsed == {"match_date": "2026-08-06", "match_time": "19:30",
                      "attendance": 25052, "venue": "Docklands"}


def test_a_second_local_time_in_brackets_is_not_mistaken_for_the_venue():
    """Interstate games print the home time in brackets after the local one."""
    parsed = L.parse_fixture_text(
        "Sat 08-Aug-2026 7:05 PM (7:35 PM) Att: 42,762 Venue: Adelaide Oval")
    assert parsed["match_time"] == "19:05"
    assert parsed["venue"] == "Adelaide Oval"


def test_a_summary_reads_into_fixtures_with_both_sides(folder):
    fixtures = L.parse_round_summary(folder / "Rd23.csv")
    assert len(fixtures) == 2
    first = fixtures[0]
    assert first.home.club == "Western Bulldogs"
    assert first.away.club == "North Melbourne"
    assert (first.home.score, first.away.score) == (77, 100)
    assert first.winner == "North Melbourne" and first.margin == 23
    assert first.attendance == 25052


def test_a_result_that_contradicts_the_scores_is_refused(tmp_path):
    """A transcription slip in the result line must not be written."""
    path = tmp_path / "bad.csv"
    path.write_text(
        'Carlton,1.1 2.2 3.3 4.4,28,"Sat 08-Aug-2026 1:00 PM Venue: Docklands"\n'
        'Essendon,2.2 4.4 6.6 8.8,56,Carlton won by 28 pts [ Match stats ]\n',
        encoding="utf-8")
    with pytest.raises(L.LoadError, match="result says"):
        L.parse_round_summary(path)


def test_a_stated_score_that_contradicts_its_quarters_is_refused(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text(
        'Carlton,1.1 2.2 3.3 4.4,99,"Sat 08-Aug-2026 1:00 PM Venue: Docklands"\n'
        'Essendon,2.2 4.4 6.6 8.8,56,Essendon won by 43 pts [ Match stats ]\n',
        encoding="utf-8")
    with pytest.raises(L.LoadError, match="does not match the quarter scores"):
        L.parse_round_summary(path)


# --------------------------------------------------------------------------
# reading a match page


def test_a_match_page_yields_both_squads_with_their_stats(folder):
    game = L.parse_game_file(folder / "dogs v north.csv")
    assert set(game.clubs) == {"Western Bulldogs", "North Melbourne"}
    lines = game.players["Western Bulldogs"]
    assert [line.source_name for line in lines] == [n for _, n in BULLDOGS]
    assert lines[0].stats["kicks"] == 10.0


def test_totals_rushed_and_coach_rows_are_not_players(folder):
    game = L.parse_game_file(folder / "dogs v north.csv")
    for lines in game.players.values():
        names = [line.source_name for line in lines]
        assert not {"Totals", "Rushed", "Opposition"} & set(names)
        assert len(lines) == 2


def test_a_blank_counting_stat_is_zero_but_a_blank_brownlow_is_unknown(folder):
    """Votes are not published until the count; zero would be a claim."""
    line = L.parse_game_file(folder / "dogs v north.csv").players[
        "North Melbourne"][1]
    assert line.stats["tackles"] == 0.0
    assert line.stats["brownlow"] is None


def test_player_details_are_attached_to_the_stats_row(folder):
    line = L.parse_game_file(folder / "dogs v north.csv").players[
        "Western Bulldogs"][0]
    assert line.age_text == "25y 100d"
    assert line.career_games_text.startswith("50")


def test_a_summary_is_not_read_as_a_match_page(folder):
    assert L.parse_game_file(folder / "Rd23.csv") is None


# --------------------------------------------------------------------------
# pairing a hand-assembled folder


def test_files_are_paired_by_the_clubs_they_name_not_their_filenames(folder):
    fixtures, games, ignored = L.read_directory(folder, "Rd23.csv")
    paired, unused, duplicates = L.pair_games(fixtures, games)
    assert not ignored and not unused and not duplicates
    assert set(paired) == {frozenset({"Western Bulldogs", "North Melbourne"}),
                           frozenset({"Adelaide", "Richmond"})}


def test_an_identical_copy_under_another_name_is_dropped_not_loaded_twice(folder):
    """A stale rename is the normal state of a hand-assembled folder."""
    copy = folder / "dogs v north rd22-2026.csv"
    copy.write_text((folder / "dogs v north.csv").read_text(encoding="utf-8"),
                    encoding="utf-8")
    fixtures, games, _ = L.read_directory(folder, "Rd23.csv")
    paired, _unused, duplicates = L.pair_games(fixtures, games)
    assert len(paired) == 2
    assert len(duplicates) == 1


def test_two_different_files_for_one_match_is_an_error_not_a_choice(folder):
    other = folder / "dogs v north alternative.csv"
    other.write_text(
        game_file("Western Bulldogs", "North Melbourne", 11, 11, 15, 10,
                  [(1, "Liberatore, Tom"), (9, "Someone, Else")], NORTH),
        encoding="utf-8")
    fixtures, games, _ = L.read_directory(folder, "Rd23.csv")
    with pytest.raises(L.LoadError, match="two different files"):
        L.pair_games(fixtures, games)


def test_a_fixture_with_no_match_page_stops_the_load(folder):
    (folder / "crows v tigers.csv").unlink()
    fixtures, games, _ = L.read_directory(folder, "Rd23.csv")
    with pytest.raises(L.LoadError, match="no game file for"):
        L.pair_games(fixtures, games)


def test_player_goals_are_checked_against_the_summary(folder):
    fixtures, games, _ = L.read_directory(folder, "Rd23.csv")
    paired, _unused, _dupes = L.pair_games(fixtures, games)
    for fixture in fixtures:
        assert L.check_against_summary(fixture, paired[fixture.clubs]) == []


def test_a_squad_that_kicked_the_wrong_number_of_goals_is_reported(folder):
    (folder / "dogs v north.csv").write_text(
        game_file("Western Bulldogs", "North Melbourne", 3, 11, 15, 10,
                  BULLDOGS, NORTH), encoding="utf-8")
    fixtures, games, _ = L.read_directory(folder, "Rd23.csv")
    paired, _unused, _dupes = L.pair_games(fixtures, games)
    notes = L.check_against_summary(fixtures[0], paired[fixtures[0].clubs])
    assert any("players kicked 3 goals" in note for note in notes)


# --------------------------------------------------------------------------
# deciding who a name refers to


@pytest.fixture
def roster():
    """Two namesake pairs: contemporaries, and a modern player's forebear."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE afltables_player_index "
                "(source_name TEXT, player_id INTEGER)")
    con.execute("CREATE TABLE players (player_id INTEGER, player TEXT, "
                "clubs_hist TEXT, clubs_now TEXT, debut_season INTEGER, "
                "final_season INTEGER)")
    people = [
        (12441, "Bailey Williams", "Western Bulldogs", "Western Bulldogs",
         2016, 2026),
        (12836, "Bailey Williams", "West Coast", "West Coast", 2020, 2026),
        (3597, "Archie Roberts", "Essendon|Melbourne", "Essendon|Melbourne",
         1932, 1937),
        (13168, "Archie Roberts", "Essendon", "Essendon", 2024, 2026),
        (11898, "Tom Liberatore", "Western Bulldogs", "Western Bulldogs",
         2011, 2026),
    ]
    for pid, name, hist, now, debut, final in people:
        surname, given = name.rsplit(" ", 1)[1], name.rsplit(" ", 1)[0]
        con.execute("INSERT INTO afltables_player_index VALUES (?, ?)",
                    (f"{surname}, {given}", pid))
        con.execute("INSERT INTO players VALUES (?,?,?,?,?,?)",
                    (pid, name, hist, now, debut, final))
    con.commit()
    return L.player_lookup(con)


def test_two_players_of_the_same_name_are_told_apart_by_club(roster):
    """Both Bailey Williamses are playing now, at different clubs."""
    dogs = L.resolve(roster, "Williams, Bailey", "Western Bulldogs",
                     "Western Bulldogs", 2026)
    eagles = L.resolve(roster, "Williams, Bailey", "West Coast", "West Coast",
                       2026)
    assert dogs[0] == 12441
    assert eagles[0] == 12836
    assert dogs[2].endswith("club") and eagles[2].endswith("club")


def test_a_namesake_from_another_era_is_ruled_out_by_the_season(roster):
    """Both Archie Robertses played for Essendon, so only the era separates."""
    player_id, _display, how = L.resolve(roster, "Roberts, Archie", "Essendon",
                                         "Essendon", 2026)
    assert player_id == 13168
    assert "era" in how


def test_an_unmistakable_name_needs_no_tie_breaking(roster):
    player_id, display, how = L.resolve(roster, "Liberatore, Tom",
                                        "Western Bulldogs",
                                        "Western Bulldogs", 2026)
    assert (player_id, display, how) == (11898, "Tom Liberatore", "index")


def test_a_name_nobody_has_is_a_debut(roster):
    player_id, display, how = L.resolve(roster, "Howes, Noah", "Collingwood",
                                        "Collingwood", 2026)
    assert player_id is None and how == "debut"
    assert display == "Noah Howes"


def test_a_name_that_survives_both_filters_twice_over_is_refused(roster):
    """Guessing would credit a stranger with someone else's game."""
    roster.indexed["williams, bailey"] = {12441, 12836}
    player_id, _display, how = L.resolve(roster, "Williams, Bailey",
                                         "Geelong", "Geelong", 2026)
    assert player_id is None and how == "ambiguous"


# --------------------------------------------------------------------------
# turning an age into a birthday


@pytest.mark.parametrize("played, age, born", [
    ("2026-08-08", "28y 303d", "1997-10-09"),
    ("2026-08-06", "25y 0d", "2001-08-06"),
])
def test_the_stated_age_and_the_match_date_give_an_exact_birthday(played, age,
                                                                  born):
    assert L.date_minus_age(played, age) == born


def test_no_age_means_no_invented_birthday():
    assert L.date_minus_age("2026-08-08", None) is None
    assert L.date_minus_age("2026-08-08", "unknown") is None


# --------------------------------------------------------------------------
# the shape written for the fixture


def test_a_match_is_written_from_both_clubs_points_of_view(folder):
    """club_match_sources is keyed per club, and club history reads it that
    way: one row would drop the match from the away club's history."""
    fixtures = L.parse_round_summary(folder / "Rd23.csv")
    rows = L._source_rows(2026, "23", fixtures[0], "now")
    assert len(rows) == 2
    home, away = rows
    assert (home["source_club_id"], home["team_position"]) == (
        "western_bulldogs", "H")
    assert (away["source_club_id"], away["team_position"]) == (
        "north_melbourne", "A")
    assert home["source_game_key"] == away["source_game_key"]
    assert home["points_for"] == away["points_against"] == 77
    assert home["q4_for_points"] == away["q4_against_points"] == 77


def test_the_fixture_row_claims_no_page_was_fetched(folder):
    """The key is derived; no URL was visited and none may be recorded."""
    fixtures = L.parse_round_summary(folder / "Rd23.csv")
    for row in L._source_rows(2026, "23", fixtures[0], "now"):
        assert row["source_game_url"] is None


def test_the_game_key_is_the_afl_tables_one_and_ignores_which_side_is_home():
    low, high, key = L.game_key("Western Bulldogs", "Fremantle", "2026-05-01")
    assert (low, high, key) == ("07", "08", "070820260501")
    assert L.game_key("Fremantle", "Western Bulldogs", "2026-05-01")[2] == key


# --------------------------------------------------------------------------
# surviving a rebuild


@pytest.fixture
def stored():
    """A database holding one stored round with a debutant in it."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE players (player_id INTEGER PRIMARY KEY, "
                "player TEXT, dob TEXT, birth_year INTEGER, "
                "birth_year_min INTEGER, birth_year_max INTEGER, "
                "debut_season INTEGER, final_season INTEGER, "
                "career_games INTEGER, career_goals REAL, "
                "career_brownlow REAL, finals_played INTEGER, "
                "clubs_hist TEXT, clubs_now TEXT, n_clubs INTEGER, "
                "name_key TEXT, club_path_hist TEXT, club_path_now TEXT)")
    con.execute("CREATE TABLE manual_round_games (season INTEGER, round TEXT, "
                "match_date TEXT, club_hist TEXT, club_now TEXT, "
                "source_name TEXT, player TEXT, player_id INTEGER, "
                "age_text TEXT, career_goals_text TEXT)")
    con.execute("INSERT INTO players (player_id, player) VALUES (500, 'Someone')")
    con.execute(
        "INSERT INTO manual_round_games (season, round, match_date, club_hist, "
        "club_now, source_name, player, player_id, age_text, "
        "career_goals_text) VALUES "
        "(2026, '23', '2026-08-09', 'Collingwood', 'Collingwood', "
        "'Howes, Noah', 'Noah Howes', NULL, '20y 285d', '2 (0.50)')")
    con.commit()
    return con


def test_a_debutant_is_created_with_a_birthday_derived_from_their_age(stored):
    created = L.create_debutants(stored, 2026, "23")
    assert created == [(501, "Noah Howes")]
    row = stored.execute(
        "SELECT dob, debut_season, clubs_now, name_key, career_goals "
        "FROM players WHERE player_id = 501").fetchone()
    assert row == ("28-Oct-2005", 2026, "Collingwood", "noah howes", 2.0)
    assert stored.execute(
        "SELECT player_id FROM manual_round_games").fetchone()[0] == 501


def test_a_rebuild_that_drops_a_debutant_gets_them_back_under_the_same_id(stored):
    """A new id would orphan the games rows and break saved references."""
    L.create_debutants(stored, 2026, "23")
    stored.execute("DELETE FROM players WHERE player_id = 501")   # the rebuild
    stored.commit()

    recreated = L.create_debutants(stored, 2026, "23")
    assert recreated == [(501, "Noah Howes")]
    assert stored.execute(
        "SELECT COUNT(*) FROM players WHERE player_id = 501").fetchone()[0] == 1


def test_a_debutant_already_present_is_not_created_twice(stored):
    L.create_debutants(stored, 2026, "23")
    assert L.create_debutants(stored, 2026, "23") == []
    assert stored.execute(
        "SELECT COUNT(*) FROM players").fetchone()[0] == 2


# --------------------------------------------------------------------------
# saying what the load decided


def test_findings_are_grouped_with_the_unfixed_ones_first(capsys):
    """What still needs a person must not be buried under what does not."""
    found = L.Findings()
    found.note("rushed behinds", "44 belong to no player")
    found.fixed("duplicate file", "a.csv is identical to b.csv")
    found.not_fixed("career total", "does not follow on")

    found.report()
    printed = capsys.readouterr().out.splitlines()
    statuses = [line.split("]")[0] + "]" for line in printed
                if line.strip().startswith("[")]
    assert statuses == [f"  {L.NOT_FIXED}", f"  {L.FIXED}", f"  {L.NOTE}"]
    assert "1 not fixed, 1 fixed, 1 noted" in printed[-1]


def test_a_clean_load_says_so_rather_than_printing_nothing(capsys):
    L.Findings().report()
    assert "nothing to report" in capsys.readouterr().out


def test_a_shared_name_between_two_current_players_is_reported_singly():
    """The risky ones are named; the routine ones are counted."""
    roster = L.Roster(indexed={"williams, bailey": {1, 2}}, named={},
                      clubs={}, span={})
    found = L.Findings()
    L.describe_identities(roster, {
        ("West Coast", "Williams, Bailey"): (2, "Bailey Williams",
                                             "index+era+club"),
        ("Carlton", "Cameron, Charlie"): (5, "Charlie Cameron", "index+era"),
        ("Geelong", "Smith, Old"): (9, "Old Smith", "index+era"),
    }, found)

    named = found.of(L.FIXED)
    assert len(named) == 1 and "two clubs this season" in named[0].detail
    counted = found.of(L.NOTE)
    assert len(counted) == 1 and "2 names" in counted[0].detail


def test_a_debut_and_an_ambiguous_name_are_reported_differently():
    roster = L.Roster(indexed={"jones, sam": {1, 2}}, named={}, clubs={},
                      span={})
    found = L.Findings()
    L.describe_identities(roster, {
        ("Carlton", "Jones, Sam"): (None, "Sam Jones", "ambiguous"),
        ("Essendon", "New, Player"): (None, "Player New", "debut"),
    }, found)

    assert len(found.of(L.NOT_FIXED)) == 1
    assert "could be any of 2 players" in found.of(L.NOT_FIXED)[0].detail
    assert len(found.of(L.FIXED)) == 1
    assert "first game" in found.of(L.FIXED)[0].detail


def _behind_only(club, behinds):
    line = L.PlayerLine(jumper="1", source_name="Someone, One",
                        stats={"behinds": float(behinds)})
    return club, [line]


def test_rushed_behinds_are_one_line_for_the_round_not_one_per_club():
    """Every side has some; eighteen lines would bury the real findings."""
    # Each side is credited 1 behind but the summary states 3, so two per
    # side were rushed -- four across the two matches.
    fixtures, paired = [], {}
    for home, away in (("Carlton", "Essendon"), ("Geelong", "Hawthorn")):
        fixture = L.Fixture(
            home=L.Side(home, [(0, 3)], 3), away=L.Side(away, [(0, 3)], 3),
            match_date="2026-08-08", match_time="19:30", attendance=1,
            venue="Docklands", winner=None, margin=0, source_line=1)
        fixtures.append(fixture)
        club_a, lines_a = _behind_only(home, 1)
        club_b, lines_b = _behind_only(away, 1)
        paired[fixture.clubs] = L.GameFile(
            path=Path("x.csv"), clubs=[home, away],
            players={club_a: lines_a, club_b: lines_b})

    found = L.Findings()
    L.describe_rushed_behinds(fixtures, paired, found)
    notes = found.of(L.NOTE)
    assert len(notes) == 1
    assert "8 of the round's 12 behinds" in notes[0].detail


def test_no_rushed_behinds_produces_no_note():
    fixture = L.Fixture(
        home=L.Side("Carlton", [(0, 1)], 1), away=L.Side("Essendon", [(0, 1)], 1),
        match_date="2026-08-08", match_time="19:30", attendance=1,
        venue="Docklands", winner=None, margin=0, source_line=1)
    club_a, lines_a = _behind_only("Carlton", 1)
    club_b, lines_b = _behind_only("Essendon", 1)
    paired = {fixture.clubs: L.GameFile(
        path=Path("x.csv"), clubs=["Carlton", "Essendon"],
        players={club_a: lines_a, club_b: lines_b})}

    found = L.Findings()
    L.describe_rushed_behinds([fixture], paired, found)
    assert found.items == []


def test_a_summary_missing_a_crowd_or_venue_is_noted_not_refused():
    fixture = L.Fixture(
        home=L.Side("Carlton", [(1, 1)], 7), away=L.Side("Essendon", [(0, 1)], 1),
        match_date="2026-08-08", match_time=None, attendance=None,
        venue=None, winner="Carlton", margin=6, source_line=1)
    found = L.Findings()
    L.describe_fixture_gaps(fixture, found)
    assert found.of(L.NOT_FIXED) == []
    assert "venue, crowd, start time" in found.of(L.NOTE)[0].detail


# --------------------------------------------------------------------------
# career totals as a check on the identity decision


@pytest.fixture
def careers():
    """A database holding 114 earlier games for one player and none for another."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE games (player_id INTEGER, season INTEGER, "
                "round TEXT)")
    con.executemany("INSERT INTO games VALUES (?, ?, ?)",
                    [(770, 2025, str(n)) for n in range(1, 115)])
    con.execute("INSERT INTO games VALUES (770, 2026, '23')")  # this round
    con.commit()
    return con


def _one_player(name, career_games_text):
    line = L.PlayerLine(jumper="7", source_name=name, stats={},
                        career_games_text=career_games_text)
    game = L.GameFile(path=Path("x.csv"), clubs=["Collingwood"],
                      players={"Collingwood": [line]})
    return {frozenset({"Collingwood", "West Coast"}): game}


def test_an_established_players_next_game_number_follows_on(careers):
    """114 games held, so AFL Tables must be calling this their 115th."""
    notes = L.check_career_totals(
        careers, 2026, "23", _one_player("Daicos, Nick", "115 (80-0-35)"),
        {("Collingwood", "Daicos, Nick"): (770, "Nick Daicos", "index")})
    assert notes == []


def test_a_debut_must_be_the_players_first_game(careers):
    notes = L.check_career_totals(
        careers, 2026, "23", _one_player("Howes, Noah", "1 (1-0-0 100.00%)"),
        {("Collingwood", "Howes, Noah"): (None, "Noah Howes", "debut")})
    assert notes == []


def test_a_debut_with_a_career_behind_it_is_a_failed_match_not_a_debut(careers):
    """Creating a player here would split a real career in two."""
    notes = L.check_career_totals(
        careers, 2026, "23", _one_player("Dicaos, Nick", "115 (80-0-35)"),
        {("Collingwood", "Dicaos, Nick"): (None, "Nick Dicaos", "debut")})
    assert len(notes) == 1
    assert "career game 115" in notes[0]
    assert "no player of that name" in notes[0]


def test_a_total_that_does_not_follow_on_is_reported(careers):
    """What matching the wrong namesake looks like from the other side."""
    notes = L.check_career_totals(
        careers, 2026, "23", _one_player("Roberts, Archie", "46 (20-0-26)"),
        {("Collingwood", "Roberts, Archie"): (770, "Archie Roberts", "index")})
    assert len(notes) == 1
    assert "holds 114 earlier game(s)" in notes[0]


def test_a_player_whose_career_total_is_absent_is_not_guessed_at(careers):
    notes = L.check_career_totals(
        careers, 2026, "23", _one_player("Someone, New", None),
        {("Collingwood", "Someone, New"): (None, "New Someone", "debut")})
    assert notes == []


# --------------------------------------------------------------------------
# remembering where the CSVs live


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "SETTINGS", tmp_path / "round_loader.json")
    return tmp_path


def test_a_folder_is_remembered_and_read_back(settings, tmp_path):
    assert L.remembered_dir() is None
    L.remember_dir(tmp_path)
    assert L.remembered_dir() == tmp_path


def test_a_remembered_folder_that_has_gone_is_not_offered(settings, tmp_path):
    """The CSVs live on removable and synced drives; the path can vanish."""
    gone = tmp_path / "on a memory stick"
    gone.mkdir()
    L.remember_dir(gone)
    gone.rmdir()
    assert L.remembered_dir() is None


def test_unreadable_settings_are_ignored_rather_than_fatal(settings):
    L.SETTINGS.write_text("{not json", encoding="utf-8")
    assert L.remembered_dir() is None


@pytest.mark.parametrize("text, expected", [
    ("Rd23.csv", "23"),
    ("rd22.csv", "22"),
    ("round 7", "7"),
    ("R5-2026", "5"),
    ("western bulldogs vs north mel rd23-2026.csv", "23"),
    ("2026 season notes", None),
    ("", None),
])
def test_a_round_number_is_recognised_in_a_name(text, expected):
    assert L.guess_round(text) == expected


# --------------------------------------------------------------------------
# what the browse window shows about a folder


def test_a_folder_is_described_without_loading_any_of_it(folder):
    from utils.afl import load_round_gui as G

    text, summaries, season, round_name = G.describe(folder)
    assert text.startswith("2 game file(s)")
    assert summaries == ["Rd23.csv"]
    assert (season, round_name) == (2026, "23")


def test_the_summary_matching_the_folders_round_is_offered_first(folder):
    """Last round's summary is usually still sitting in the folder."""
    from utils.afl import load_round_gui as G

    (folder / "rd22.csv").write_text(SUMMARY, encoding="utf-8")
    named = folder.rename(folder.parent / "rd23")
    text, summaries, season, round_name = G.describe(named)
    assert summaries[0] == "Rd23.csv"
    assert round_name == "23"


def test_a_folder_with_nothing_in_it_is_reported_not_raised(tmp_path):
    from utils.afl import load_round_gui as G

    assert G.describe(tmp_path)[0] == "no .csv files in this folder"
    assert G.describe(tmp_path / "nowhere")[0] == "that folder does not exist"


# --------------------------------------------------------------------------
# taking a round back out


def _loaded_db(tmp_path, *, published=False, fetched=False):
    """A database holding one hand-entered round and one published one.

    Small on purpose: these tests are about which rows `remove` decides to
    touch, not about the rebuild it delegates to.
    """
    path = tmp_path / "afl.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE games (player_id INTEGER, season INTEGER, round TEXT,
                            goals REAL, match_id INTEGER);
        CREATE TABLE manual_round_games (season INTEGER, round TEXT,
                            player_id INTEGER, source_file TEXT);
        CREATE TABLE matches (match_id INTEGER, season INTEGER, round TEXT);
        CREATE TABLE match_details (match_id INTEGER);
        CREATE TABLE club_match_sources (season INTEGER, round TEXT,
                            source_game_url TEXT);
        CREATE TABLE afltables_match_scores (season INTEGER, round TEXT);
        CREATE TABLE players (player_id INTEGER PRIMARY KEY, player TEXT,
                            career_games INTEGER, career_goals REAL,
                            final_season INTEGER);
        INSERT INTO games VALUES (1, 2026, '23', 2, 900), (2, 2026, '23', 1, 900),
                                 (1, 2026, '22', 3, 800);
        INSERT INTO manual_round_games VALUES (2026, '23', 1, 'rd23.csv'),
                                              (2026, '23', 2, 'rd23.csv');
        INSERT INTO matches VALUES (900, 2026, '23'), (800, 2026, '22');
        INSERT INTO match_details VALUES (900), (800);
        INSERT INTO club_match_sources VALUES (2026, 'R23', NULL),
                                              (2026, 'R22', 'https://afltables');
        INSERT INTO players VALUES (1, 'A Player', 2, 5, 2026),
                                   (2, 'B Player', 1, 1, 2026);
        """)
    if published:
        con.execute("INSERT INTO afltables_match_scores VALUES (2026, '23')")
    if fetched:
        con.execute("INSERT INTO club_match_sources VALUES "
                    "(2026, 'R23', 'https://afltables/r23')")
    con.commit()
    con.close()
    return path


def test_the_footprint_counts_every_table_the_load_wrote(tmp_path):
    con = sqlite3.connect(_loaded_db(tmp_path))
    counts = L._round_footprint(con, 2026, "23")
    assert counts == {"games": 2, "manual_round_games": 2, "matches": 1,
                      "club_match_sources": 1, "match_details": 1}
    # The published round beside it is not part of the hand-entered one.
    assert L._round_footprint(con, 2026, "22")["games"] == 1
    con.close()


def test_a_round_the_source_has_since_published_is_not_removed(tmp_path):
    """Those rows are the scrape's. --forget is the tool for that, not this.

    Removing here would delete real data to no purpose, so it takes an
    explicit --force rather than happening because the round number matched.
    """
    path = _loaded_db(tmp_path, published=True)
    assert L.remove(path, 2026, "23") == 1

    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM games WHERE round='23'"
                       ).fetchone()[0] == 2
    con.close()


def test_a_round_the_scrape_has_replaced_is_not_removed(tmp_path):
    """A fetched source_game_url means the All Games scrape owns the row."""
    path = _loaded_db(tmp_path, fetched=True)
    assert L.remove(path, 2026, "23") == 1
    assert "fetched fixture" in (
        L._upstream_owns(sqlite3.connect(path), 2026, "23") or "")


def test_a_hand_entered_round_is_removable(tmp_path):
    con = sqlite3.connect(_loaded_db(tmp_path))
    assert L._upstream_owns(con, 2026, "23") is None
    con.close()


def test_a_dry_run_writes_nothing(tmp_path):
    path = _loaded_db(tmp_path)
    assert L.remove(path, 2026, "23", dry_run=True, keep_backups=0) == 0

    con = sqlite3.connect(path)
    for table, column in (("games", "round"), ("manual_round_games", "round"),
                          ("matches", "round")):
        assert con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column}='23'"
        ).fetchone()[0] > 0, f"{table} was written during a dry run"
    con.close()


def test_a_round_that_was_never_loaded_is_not_a_silent_success(tmp_path):
    assert L.remove(_loaded_db(tmp_path), 2026, "19", dry_run=True) == 1


def test_a_backup_is_taken_before_anything_is_deleted(tmp_path):
    path = _loaded_db(tmp_path)
    backup = L._backup(path, keep=3)
    assert backup is not None and backup.exists()
    assert backup.parent == path.parent / "backups"
    assert backup.stat().st_size == path.stat().st_size


def test_older_backups_are_rotated_out(tmp_path):
    """Same folder and retention as the nightly promote step uses."""
    path = _loaded_db(tmp_path)
    folder = path.parent / "backups"
    folder.mkdir()
    for day in range(1, 5):
        stale = folder / f"afl-2026010{day}-000000.db"
        stale.write_bytes(b"old")
        _os.utime(stale, (day, day))

    kept = L._backup(path, keep=3)
    remaining = sorted(folder.glob("afl-*.db"))
    assert len(remaining) == 3
    assert kept in remaining
    # The oldest go first, newest kept.
    assert folder / "afl-20260101-000000.db" not in remaining


def test_a_backup_can_be_declined(tmp_path):
    assert L._backup(_loaded_db(tmp_path), keep=0) is None


# --------------------------------------------------------------------------
# a round loaded out of order


@pytest.fixture
def out_of_order():
    """114 games before round 22, and a round 23 already entered by hand.

    The real shape this hit: round 23 was entered while round 22 was still
    missing upstream, so the database held a game played *after* the round
    being loaded.
    """
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE games (player_id INTEGER, season INTEGER, "
                "round TEXT, date TEXT)")
    con.executemany(
        "INSERT INTO games VALUES (?, ?, ?, ?)",
        [(770, 2025, str(n), f"2025-{(n % 12) + 1:02d}-01")
         for n in range(1, 115)])
    # The round after the one being loaded, already in the database.
    con.execute("INSERT INTO games VALUES (770, 2026, '23', '2026-08-08')")
    con.commit()
    return con


def test_a_later_round_already_held_is_not_counted_as_an_earlier_game(
        out_of_order):
    """The failure that refused 366 of 414 players on a correct round.

    Counting "every game that is not this round" quietly includes the
    round played the following week, so every player in both looks one
    game ahead of himself and the whole round is refused.
    """
    player = _one_player("Daicos, Nick", "115 (80-0-35)")
    resolved = {("Collingwood", "Daicos, Nick"): (770, "Nick Daicos", "index")}

    # Counting up to the round's first day is the right question.
    assert L.check_career_totals(
        out_of_order, 2026, "22", player, resolved,
        before="2026-08-01") == []

    # Without it, the round-23 game is miscounted and the load is refused.
    notes = L.check_career_totals(out_of_order, 2026, "22", player, resolved)
    assert len(notes) == 1
    assert "holds 115 earlier game(s)" in notes[0]


def test_counting_up_to_a_date_still_catches_the_wrong_namesake(out_of_order):
    """The check must keep failing what it was built to fail."""
    notes = L.check_career_totals(
        out_of_order, 2026, "22", _one_player("Daicos, Nick", "46 (20-0-26)"),
        {("Collingwood", "Daicos, Nick"): (770, "Nick Daicos", "index")},
        before="2026-08-01")
    assert len(notes) == 1
    assert "career game 46" in notes[0] and "holds 114" in notes[0]


def test_counting_up_to_a_date_still_catches_a_false_debut(out_of_order):
    notes = L.check_career_totals(
        out_of_order, 2026, "22", _one_player("Someone, Else", "175 (90-0-85)"),
        {("Collingwood", "Someone, Else"): (None, "Else Someone", "debut")},
        before="2026-08-01")
    assert len(notes) == 1
    assert "no player of that name" in notes[0]
