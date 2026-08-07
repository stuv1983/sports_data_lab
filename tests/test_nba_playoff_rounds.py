#!/usr/bin/env python3
"""Playoff round classification: the data four criteria cannot work without.

A box-score source says a game was a playoff game and not which round. Until
the round is known, "played in the NBA Finals", "won a Finals game",
"appeared for the championship team" and the championship derivation itself
all answer nobody -- which reads on screen as "no player has ever won a
title" rather than as missing data.

Two properties matter more than the rest.

NOTHING IS GUESSED. A series name the classifier does not recognise gets no
round and says so. A playoff game with no matching series gets no round and
fails the strict build. The alternative -- defaulting to R1 -- files Finals
games in the first round and silently costs somebody a title.

THE SEASON CONVERSION IS THE DANGEROUS PART. Basketball-Reference labels a
series with the year it was played, the build uses start years, and getting
it backwards files every series one season late, where it matches no games
at all and looks exactly like a source that simply lacks playoff data.
"""

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

import pandas as pd
import pytest

from nba import build_nba_db
from utils.nba import load_nba_playoff_series as loader
import nba_fixture
from nba import nba_playoff_rounds as rounds
from nba import nba_source


# ------------------------------------------------------- classification

@pytest.mark.parametrize("name, code", [
    ("Finals", "F"),
    ("Eastern Conf Finals", "CF"),
    ("Western Div Finals", "CF"),
    ("Central Div Finals", "CF"),
    ("Eastern Conf Semifinals", "CSF"),
    ("Western Div Semifinals", "CSF"),
    ("Semifinals", "CSF"),
    ("Eastern Conf First Round", "R1"),
    ("First Round", "R1"),
    ("Quarterfinals", "QF"),
    ("Western Div Tiebreak", "TB"),
    ("Eastern Div 3rd Place Tiebreak", "TB"),
])
def test_every_series_name_the_source_uses_is_classified(name, code):
    assert loader.classify(name) == code


def test_an_unrecognised_series_name_is_not_guessed():
    """Defaulting to a round is how a Finals game ends up in round one."""
    assert loader.classify("Play-In Tournament") is None
    assert loader.classify("") is None


def test_a_conference_final_is_not_mistaken_for_the_finals():
    """'Eastern Conf Finals' contains 'Finals'. Anchoring matters."""
    assert loader.classify("Eastern Conf Finals") == "CF"
    assert loader.classify("Finals") == "F"


def test_the_official_seed_is_kept_rather_than_discarded():
    """The box-score sources do not carry seeding at all."""
    assert loader.split_seed("Boston Celtics (1)") == ("Boston Celtics", 1)
    assert loader.split_seed("Miami Heat (8)") == ("Miami Heat", 8)
    assert loader.split_seed("Chicago Stags") == ("Chicago Stags", None)
    assert loader.split_seed("") == (None, None)


# ------------------------------------------------------ the saved extract

SERIES_COLUMNS = ("season", "lg", "series", "winner", "loser", "wins_winner",
                  "wins_loser", "series_url", "source_url")


def write_extract(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(SERIES_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in SERIES_COLUMNS})
    return path


def series_row(season, series, winner, loser, lg="NBA", wins=(4, 1)):
    return {"season": season, "lg": lg, "series": series, "winner": winner,
            "loser": loser, "wins_winner": wins[0], "wins_loser": wins[1]}


def test_a_played_in_year_becomes_the_seasons_start_year(tmp_path):
    """The 2025-26 Finals are season=2026 upstream and 2025 here. Reversing
    this files every series where no game will ever look for it."""
    extract = write_extract(tmp_path / "series.csv", [
        series_row(2026, "Finals", "New York Knicks", "San Antonio Spurs")])
    parsed, problems = loader.parse(extract)
    assert problems == []
    assert parsed[0]["season"] == 2025
    assert parsed[0]["round"] == "F"


def test_repeated_header_rows_in_the_extract_are_skipped_quietly(tmp_path):
    """The saved page repeats its header every twenty rows. That is a fact
    about the page, not a defect to report."""
    extract = write_extract(tmp_path / "series.csv", [
        {"season": "Season", "lg": "Lg", "series": "Series"},
        series_row(2024, "Finals", "Boston Celtics", "Dallas Mavericks")])
    parsed, problems = loader.parse(extract)
    assert len(parsed) == 1
    assert problems == []


def test_an_unrecognised_series_is_written_without_a_round_and_reported(
        tmp_path):
    extract = write_extract(tmp_path / "series.csv", [
        series_row(2024, "Finals", "Boston Celtics", "Dallas Mavericks"),
        series_row(2024, "Play-In", "Miami Heat", "Chicago Bulls")])
    parsed, problems = loader.parse(extract)
    assert len(parsed) == 2
    assert any("Play-In" in p for p in problems)
    assert [r for r in parsed if r["series_name"] == "Play-In"][0]["round"] == ""


def test_a_season_without_a_finals_is_a_complaint(tmp_path):
    extract = write_extract(tmp_path / "series.csv", [
        series_row(2024, "Eastern Conf Finals", "Boston Celtics",
                   "Indiana Pacers")])
    parsed, _ = loader.parse(extract)
    assert any("no Finals series" in c for c in loader.check(parsed))


def test_two_finals_in_one_season_is_a_complaint(tmp_path):
    extract = write_extract(tmp_path / "series.csv", [
        series_row(2024, "Finals", "Boston Celtics", "Dallas Mavericks"),
        series_row(2024, "Finals", "Denver Nuggets", "Miami Heat")])
    parsed, _ = loader.parse(extract)
    assert any("exactly one is required" in c for c in loader.check(parsed))


def test_the_same_pair_meeting_twice_in_a_season_is_a_complaint(tmp_path):
    """A game is assigned its round by team pair, so a repeat makes the
    round ambiguous exactly where it is relied on."""
    extract = write_extract(tmp_path / "series.csv", [
        series_row(2024, "Finals", "Boston Celtics", "Dallas Mavericks"),
        series_row(2024, "Eastern Conf Finals", "Boston Celtics",
                   "Dallas Mavericks")])
    parsed, _ = loader.parse(extract)
    assert any("cannot be assigned a round" in c for c in loader.check(parsed))


def test_a_season_shifted_by_one_is_caught_by_the_league_cross_check(tmp_path):
    """The check that earns its keep: two independently parsed pages have to
    agree on who won, and an off-by-one conversion makes them disagree."""
    extract = write_extract(tmp_path / "series.csv", [
        series_row(2024, "Finals", "Boston Celtics", "Dallas Mavericks")])
    parsed, _ = loader.parse(extract)

    leagues = tmp_path / "leagues.csv"
    with open(leagues, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["season", "lg_id", "champion"])
        writer.writeheader()
        writer.writerow({"season": "2023-24", "lg_id": "NBA",
                         "champion": "Boston Celtics"})
    assert loader.cross_check(parsed, leagues) == []

    # Now claim a different champion for that season.
    with open(leagues, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["season", "lg_id", "champion"])
        writer.writeheader()
        writer.writerow({"season": "2023-24", "lg_id": "NBA",
                         "champion": "Dallas Mavericks"})
    assert any("league index says" in c
               for c in loader.cross_check(parsed, leagues))


def test_the_checked_in_reference_is_clean():
    """The real file, over the real 1946-2025 span: every series classified,
    every season with exactly one Finals, and both saved pages agreeing on
    all 89 champions."""
    extract = build_nba_db.Path("data/nba/output/playoff_series_rows.csv")
    if not extract.exists():
        pytest.skip("the saved Basketball-Reference extract is not present")
    parsed, problems = loader.parse(extract)
    assert problems == []
    assert loader.check(parsed) == []
    assert loader.cross_check(parsed, "data/nba/output/leagues_rows.csv") == []
    assert {r["round"] for r in parsed} == {"F", "CF", "CSF", "R1", "QF", "TB"}
    assert sum(1 for r in parsed if r["round"] == "F") == 89


# ------------------------------------------------------------ assignment

def write_reference(root, rows):
    path = root / "reference" / "playoff_series.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(loader.COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in loader.COLUMNS})
    return path


def reference_row(season, code, winner, loser, league="NBA", wins=(4, 1)):
    return {"season": season, "league": league, "round": code,
            "series_name": code, "winner": winner, "loser": loser,
            "wins_winner": wins[0], "wins_loser": wins[1]}


def match_frame(rows):
    return pd.DataFrame(rows)


def collect(issues):
    return lambda kind, detail, severity="warn", season=None: issues.append(
        (severity, kind, detail))


def test_a_relocated_team_is_matched_on_the_name_it_played_under(tmp_path):
    """The reference says Seattle SuperSonics. Matching on the modern
    franchise name fails for every relocated team."""
    write_reference(tmp_path, [
        reference_row(2004, "F", "Seattle SuperSonics", "Boston Celtics",
                      wins=(1, 0))])
    index = rounds.load(tmp_path)

    frame = match_frame([{
        "match_id": "m1", "season": 2004, "phase": "playoff", "round": None,
        "home_team": "Oklahoma City Thunder", "away_team": "Boston Celtics",
        "home_hist": "Seattle SuperSonics", "away_hist": "Boston Celtics"}])
    issues = []
    result = rounds.assign(frame, index, collect(issues))
    assert result["assigned"] == 1
    assert frame.at[0, "round"] == "F"
    assert issues == []


def test_a_playoff_game_with_no_series_is_left_alone_and_fails_the_build(
        tmp_path):
    write_reference(tmp_path, [
        reference_row(2004, "F", "Boston Celtics", "Los Angeles Lakers")])
    frame = match_frame([{
        "match_id": "m1", "season": 2004, "phase": "playoff", "round": None,
        "home_team": "Chicago Bulls", "away_team": "Miami Heat",
        "home_hist": "Chicago Bulls", "away_hist": "Miami Heat"}])
    issues = []
    result = rounds.assign(frame, rounds.load(tmp_path), collect(issues))

    assert result["unresolved"] == 1
    assert frame.at[0, "round"] is None
    assert [i for i in issues if i[0] == "error"
            and i[1] == "unresolved_playoff_round"]


def test_a_round_the_source_supplied_is_not_overwritten(tmp_path):
    """Reference data fills a gap. Rewriting a value the source carried
    would hide a real disagreement between the two."""
    write_reference(tmp_path, [
        reference_row(2004, "CF", "Boston Celtics", "Los Angeles Lakers")])
    frame = match_frame([{
        "match_id": "m1", "season": 2004, "phase": "playoff", "round": "F",
        "home_team": "Boston Celtics", "away_team": "Los Angeles Lakers",
        "home_hist": "Boston Celtics", "away_hist": "Los Angeles Lakers"}])
    issues = []
    result = rounds.assign(frame, rounds.load(tmp_path), collect(issues))

    assert result["already_set"] == 1
    assert frame.at[0, "round"] == "F"
    assert [i for i in issues if i[1] == "round_disagreement"]


def test_aba_series_are_not_read_as_nba_ones(tmp_path):
    """The ABA merged in; its nine championships are not NBA championships,
    and folding them in would make 'champion' wrong for nine seasons."""
    write_reference(tmp_path, [
        reference_row(1975, "F", "Kentucky Colonels", "Indiana Pacers",
                      league="ABA")])
    assert rounds.load(tmp_path) == {}
    assert len(rounds.load(tmp_path, leagues=("ABA",))) == 1


def test_baa_series_are_read_as_nba_ones(tmp_path):
    """The BAA is the NBA's own predecessor and the league counts its
    three titles."""
    write_reference(tmp_path, [
        reference_row(1946, "F", "Philadelphia Warriors", "Chicago Stags",
                      league="BAA")])
    assert len(rounds.load(tmp_path)) == 1


def test_a_series_shorter_than_the_reference_records_is_reported(tmp_path):
    write_reference(tmp_path, [
        reference_row(2004, "F", "Boston Celtics", "Los Angeles Lakers",
                      wins=(4, 3))])
    frame = match_frame([{
        "match_id": f"m{n}", "season": 2004, "phase": "playoff", "round": None,
        "home_team": "Boston Celtics", "away_team": "Los Angeles Lakers",
        "home_hist": "Boston Celtics", "away_hist": "Los Angeles Lakers"}
        for n in range(3)])
    issues = []
    rounds.assign(frame, rounds.load(tmp_path), collect(issues))
    complaint = [i for i in issues if i[1] == "series_length_disagreement"]
    assert complaint and "7 game(s)" in complaint[0][2]


# ----------------------------------------------------- through the build

def test_the_build_fails_when_a_playoff_game_has_no_round(tmp_path):
    """The fixture's source carries its own rounds. Strip them and the build
    has nothing to derive a championship from, and must say so."""
    root = tmp_path / "noround"
    nba_fixture.write(root / "csv")
    path = root / "csv" / "matches_2009.csv"
    path.write_text(path.read_text(encoding="utf-8").replace(",CF,", ",,")
                    .replace(",F,", ",,"), encoding="utf-8")

    with pytest.raises(build_nba_db.BuildError):
        build_nba_db.build(root / "nba.db",
                           nba_source.CsvNbaSource(root / "csv"),
                           verbose=False)
    kinds = {r[0] for r in sqlite3.connect(root / "nba.db").execute(
        "SELECT kind FROM source_issues")}
    assert "unresolved_playoff_round" in kinds


def test_a_reference_fills_the_rounds_a_source_does_not_carry(tmp_path):
    """The whole point: a box-score source with no round data still gets a
    Finals, a champion, and four criteria that answer somebody."""
    root = tmp_path / "filled"
    nba_fixture.write(root / "csv")
    path = root / "csv" / "matches_2009.csv"
    path.write_text(path.read_text(encoding="utf-8").replace(",CF,", ",,")
                    .replace(",F,", ",,"), encoding="utf-8")
    write_reference(root, [
        reference_row(2009, "CF", "Boston Celtics", "Oklahoma City Thunder",
                      wins=(1, 0)),
        reference_row(2009, "F", "Boston Celtics", "Los Angeles Lakers",
                      wins=(1, 0))])

    build_nba_db.build(root / "nba.db",
                       nba_source.CsvNbaSource(root / "csv"), verbose=False)
    con = sqlite3.connect(root / "nba.db")
    assert dict(con.execute(
        "SELECT round, COUNT(*) FROM matches WHERE phase='playoff' "
        "GROUP BY round")) == {"CF": 1, "F": 1}
    assert con.execute(
        "SELECT club_now FROM team_seasons WHERE champion=1").fetchall() == [
            ("Boston Celtics",)]


def test_a_season_whose_playoffs_have_no_finals_fails_the_build(tmp_path):
    """A conference final with no Finals behind it means no champion, which
    four criteria report as 'nobody has ever won a title'."""
    root = tmp_path / "nofinal"
    nba_fixture.write(root / "csv")
    path = root / "csv" / "matches_2009.csv"
    path.write_text(path.read_text(encoding="utf-8").replace(",F,", ",CF,"),
                    encoding="utf-8")

    with pytest.raises(build_nba_db.BuildError):
        build_nba_db.build(root / "nba.db",
                           nba_source.CsvNbaSource(root / "csv"),
                           verbose=False)
    kinds = {r[0] for r in sqlite3.connect(root / "nba.db").execute(
        "SELECT kind FROM source_issues")}
    assert "no_finals_round" in kinds or "season_without_finals" in kinds


def main():
    import subprocess
    return subprocess.call([_sys.executable, "-m", "pytest", __file__, "-q"])


if __name__ == "__main__":
    _sys.exit(main())
