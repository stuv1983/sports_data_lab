"""The sport-agnostic Game Lab prototypes in explore.py.

These modes are the fallback for every sport without its own game module,
so they have to hold up on all four databases rather than only the AFL one
they were written against. Each test here pins a defect that made a mode
either crash or misjudge an answer.
"""

import sqlite3

import pytest

import explore
import sports
import ui_widgets

SPORTS = [sports.AFL, sports.NBA, sports.MLB, sports.NFL]


def _con(sport):
    if not sport.exists():
        pytest.skip(f"no built {sport.key} database")
    return sqlite3.connect(sport.db)


@pytest.mark.parametrize("sport", SPORTS, ids=lambda s: s.key)
def test_a_head_to_head_never_pits_a_player_against_themselves(sport):
    """Two draws from _new_game_target could land on the same player, which
    makes "who played more games?" unanswerable -- and, because the
    comparison is >=, scored *both* buttons as correct."""
    con = _con(sport)
    try:
        for _ in range(40):
            first, second = explore._new_game_pair(sport, con)
            assert first is not None and second is not None
            assert first != second
    finally:
        con.close()


@pytest.mark.parametrize("sport", SPORTS, ids=lambda s: s.key)
def test_the_threshold_target_is_reachable_in_every_sport(sport):
    """The mark was hard-coded at 300 games -- a fair AFL career and an
    impossible one in the NFL, leaving that sport's challenge with no
    correct answers at all."""
    con = _con(sport)
    try:
        sc = sport.schema
        target = explore._threshold_target(sport, con)
        assert target and target > 0

        above = con.execute(
            f"SELECT COUNT(*) FROM {sc.players} WHERE {sc.career_games} >= ?",
            (target,)).fetchone()[0]
        assert above >= 50, (
            f"{sport.key}: only {above} players clear {target}")
        # and it stays a challenge rather than admitting most of the league
        everyone = con.execute(
            f"SELECT COUNT(*) FROM {sc.players} "
            f"WHERE {sc.career_games} > 0").fetchone()[0]
        assert above < everyone * 0.5
    finally:
        con.close()


@pytest.mark.parametrize("sport", SPORTS, ids=lambda s: s.key)
def test_the_threshold_target_is_a_round_number(sport):
    """A target of 287 reads as a bug. Round marks read as a rule."""
    con = _con(sport)
    try:
        assert explore._threshold_target(sport, con) % 50 == 0
    finally:
        con.close()


@pytest.mark.parametrize("sport", SPORTS, ids=lambda s: s.key)
def test_a_game_target_has_the_career_the_clues_describe(sport):
    """Every clue reads a career column, so a target without one shows
    "None-None" and gives the guesser nothing to work with."""
    con = _con(sport)
    try:
        sc = sport.schema
        pid = explore._new_game_target(sport, con)
        assert pid is not None
        row = con.execute(
            f"SELECT {sc.debut_season}, {sc.final_season}, {sc.career_games}, "
            f"{sc.n_clubs} FROM {sc.players} WHERE {sc.player_id}=?",
            (pid,)).fetchone()
        debut, final, games, clubs = row
        assert debut and final and games and clubs
        assert games >= 100
    finally:
        con.close()


@pytest.mark.parametrize("sport", SPORTS, ids=lambda s: s.key)
def test_player_matches_yields_the_triples_the_threshold_game_unpacks(sport):
    """The Stat Threshold mode unpacked four values from these rows and
    raised ValueError on the first character typed into its search box."""
    if not sport.exists():
        pytest.skip(f"no built {sport.key} database")
    matches = ui_widgets.player_matches(
        "a", sport, explore._db_revision(sport.db), limit=5)
    assert matches, "no player anywhere matched 'a'"
    for match in matches:
        assert len(match) == 3
        # player_id is an int in AFL and NBA but a string in MLB ("acea01")
        # and NFL ("00-0035190"), so only its presence is asserted here.
        pid, name, label = match
        assert pid is not None
        assert name and label
