import sqlite3

import core
import venue_states


def test_selector_has_50_states_dc_and_puerto_rico():
    assert len(venue_states.US_JURISDICTIONS) == 52
    assert "Washington, D.C." in venue_states.STATE_NAMES
    assert "Puerto Rico" in venue_states.STATE_NAMES


def test_known_venues_are_assigned_to_the_right_state():
    assert "Lambeau Field" in venue_states.venues_for_state("nfl", "Wisconsin")
    assert "Fenway Park II" in venue_states.venues_for_state(
        "mlb", "Massachusetts")
    assert "Crypto.com Arena" in venue_states.venues_for_state(
        "nba", "California")
    assert "Scotiabank Arena" not in venue_states.venues_for_state(
        "nba", "New York")


def test_grouped_venue_constraint_is_parameterised_and_matches_players():
    schema = core.Schema(career_score="career_goals", career_postseason="finals_played", game_score="goals")
    generic = core.Generic(schema)
    sql, params = generic.played_at_venues(("Fenway Park", "TD Garden"))
    assert params == ["Fenway Park", "TD Garden"]

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE games(player_id INTEGER, venue TEXT)")
    con.executemany("INSERT INTO games VALUES (?, ?)", [
        (1, "Fenway Park"), (2, "TD Garden"), (3, "Dodger Stadium")])
    assert {row[0] for row in con.execute(sql, params)} == {1, 2}


def test_empty_state_is_a_valid_no_match_query():
    sql, params = core.Generic(core.Schema(career_score="career_goals", career_postseason="finals_played", game_score="goals")).played_at_venues(())
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE games(player_id INTEGER, venue TEXT)")
    assert con.execute(sql, params).fetchall() == []
