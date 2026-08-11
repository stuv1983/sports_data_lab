"""The season and round pickers on the Past games page.

A round is the one filter on this page whose right answer is not typeable:
'R23' is what the database stores, "round 23" is what a reader means, and
from 2024 the AFL's own fixture calls that same round 22. So the picker
offers what was actually played, in the order it was played, and names the
AFL's number beside ours once a season is chosen.

Rendered through AppTest against a small in-memory source table rather than
the real database, so a failure means the page changed and not that
somebody loaded a round.
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
from streamlit.testing.v1 import AppTest

import sports
from afl import past_games as PG

COLUMNS = [
    "source_club_id", "season", "round", "is_final", "team_position",
    "opponent_raw", "points_for", "points_against", "result", "margin",
    "season_wins_after", "season_draws_after", "season_losses_after",
    "venue_raw", "attendance", "match_date", "source_game_key",
    "match_status", "match_id",
]


def _match(key, season, rnd, date, home, away, is_final=0):
    pos_h, pos_a = ("F", "F") if is_final else ("H", "A")
    return [
        (home, season, rnd, is_final, pos_h, away, 100, 50, "W", 50,
         0, 0, 0, "Adelaide Oval", 42762, date, key, "unique", None),
        (away, season, rnd, is_final, pos_a, home, 50, 100, "L", -50,
         0, 0, 0, "Adelaide Oval", 42762, date, key, "unique", None),
    ]


def source_rows():
    """Two seasons whose round lists differ, plus a finals series.

    1897 is the shape that makes a per-season round list worth building:
    fourteen rounds and no round 23 anywhere, so a picker left showing
    2026's rounds would offer a filter that cannot match.
    """
    rows = []
    for n in range(1, 15):
        rows += _match(f"old{n}", 1897, f"R{n}", f"1897-05-{n + 1:02d}",
                       "geelong", "essendon")
    rows += _match("oldsf", 1897, "SF", "1897-09-04", "essendon", "geelong",
                   is_final=1)
    for n in (21, 23):
        rows += _match(f"new{n}", 2026, f"R{n}", f"2026-08-{n - 15:02d}",
                       "adelaide", "richmond")
    return rows


@pytest.fixture
def con():
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.execute(f"CREATE TABLE club_match_sources ({', '.join(COLUMNS)})")
    con.executemany(
        f"INSERT INTO club_match_sources VALUES "
        f"({','.join('?' * len(COLUMNS))})", source_rows())
    con.commit()
    return con


def _run(con):
    # Imported inside: AppTest re-executes this function's source in an
    # empty module namespace, so nothing at the top of this file is in scope.
    import sports as _sports
    from afl import past_games as _pg
    _pg.past_games_page(_sports.AFL, con)


def page(con, **state):
    # AppTest re-executes the function's source with no closure, so the
    # connection travels as an argument and the widget values as session
    # state seeded before the run -- which is also how the running app
    # arrives at them.
    at = AppTest.from_function(_run, args=(con,))
    for name, value in state.items():
        at.session_state[name] = value
    at.run(timeout=30)
    assert not at.exception, at.exception
    return at


def picker(at, key):
    for box in at.get("selectbox"):
        if box.proto.id.endswith(key) or box.key == key:
            return box
    raise AssertionError(f"no selectbox {key}: "
                         f"{[b.key for b in at.get('selectbox')]}")


# ------------------------------------------------------------- labelling

def test_a_numbered_round_is_spoken_as_a_round():
    assert PG._round_name("R23") == "Round 23"
    assert PG._round_name("R1") == "Round 1"


def test_a_numbered_week_is_spoken_as_a_week():
    """The NFL stores 'W3'; nobody calls it round 3."""
    assert PG._round_name("W3") == "Week 3"


def test_a_finals_code_is_spoken_in_full():
    assert PG._round_name("PF") == "Preliminary final"
    assert PG._round_name("WS") == "World Series"


def test_an_unknown_code_is_left_exactly_as_the_source_wrote_it():
    """Better an unfamiliar code than a wrong expansion of one."""
    assert PG._round_name("ZZ") == "ZZ"


# --------------------------------------------------------- the pickers

def test_the_round_picker_offers_the_rounds_that_were_played(con):
    at = page(con)
    options = picker(at, "pg_round").options
    assert options[0] == "Any round"
    assert "Round 21" in options and "Round 23" in options
    assert "Semi final" in options
    # Playing order, not alphabetical: R21 before R23 before the final.
    assert (options.index("Round 21") < options.index("Round 23")
            < options.index("Semi final"))


def test_choosing_a_season_narrows_the_rounds_to_the_ones_it_had(con):
    at = page(con, pg_season=1897)
    options = picker(at, "pg_round").options
    assert "Round 14" in options
    assert "Round 21" not in options and "Round 23" not in options


def test_a_round_the_newly_chosen_season_never_played_is_dropped(con):
    """Otherwise the page holds a filter that can only return nothing."""
    at = page(con, pg_round="R23", pg_season=1897)
    assert at.session_state["pg_round"] == "Any"


def test_the_round_picker_names_the_afls_own_number_beside_ours(con):
    """Our round 23 is the AFL's round 22, and 2026 is a season it applies
    to. Saying so in the picker is the whole reason the note exists."""
    at = page(con, pg_season=2026)
    options = picker(at, "pg_round").options
    assert "Round 23 · AFL Round 22" in options
    assert "Round 21 · AFL Round 20" in options


def test_before_2024_our_round_number_is_the_afls_and_nothing_is_added(con):
    at = page(con, pg_season=1897)
    assert "Round 14" in picker(at, "pg_round").options


def test_without_a_season_the_picker_stays_neutral_about_whose_number(con):
    """Round 14 is ours and the AFL's in 1897 and not in 2026; with every
    season in scope the caption carries the caveat instead."""
    at = page(con)
    assert all(" · AFL " not in option
               for option in picker(at, "pg_round").options)
    assert any("Opening Round" in caption.value for caption in at.caption)


def test_a_chosen_season_switches_the_years_slider_off(con):
    """Two controls both setting the years is how a search silently
    returns nothing."""
    at = page(con, pg_season=2026)
    assert at.get("select_slider")[0].disabled is True
    at = page(con)
    assert at.get("select_slider")[0].disabled is False


def test_the_season_and_round_pickers_actually_filter(con):
    at = page(con, pg_season=2026, pg_round="R23")
    assert any("1 games" in caption.value for caption in at.caption)


def test_a_season_and_round_with_no_match_says_so_rather_than_erroring(con):
    # Every match here is won by 50; nothing survives a 200-point floor.
    at = page(con, pg_season=2026, pg_round="R23", pg_margin=200)
    assert at.info
