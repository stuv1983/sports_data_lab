#!/usr/bin/env python3
"""Searching past games by the scoreline rather than by who played.

Three suites. The first runs the filters against a small fixture whose
right answers are countable by eye, and covers what is easy to get quietly
wrong: an absolute margin staying absolute once a club is selected, zero
being a real bound rather than an absent one, and 'both teams' meaning the
lower of the two scores rather than the losing side's.

The second renders the Past games page against that fixture, so a failure
means the controls changed and not that somebody loaded a season.

The third asks the real databases whether each sport's named scorelines
find anything at all -- a preset whose only possible answer is "no games"
is a preset that reads as the search being broken. It skips when a
database has not been built.
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

import registry
import sports
from afl import club_history as H
from afl import past_games as PG

COLUMNS = [
    "source_club_id", "season", "round", "is_final", "team_position",
    "opponent_raw", "points_for", "points_against", "result", "margin",
    "season_wins_after", "season_draws_after", "season_losses_after",
    "venue_raw", "attendance", "match_date", "source_game_key",
    "match_status", "match_id",
]


def _match(key, season, rnd, date, home, away, hp, ap, is_final=0):
    """Both sides of one match, as the source table stores them."""
    pos_h, pos_a = ("F", "F") if is_final else ("H", "A")
    verdict = (lambda mine, theirs:
               "W" if mine > theirs else ("L" if mine < theirs else "D"))
    return [
        (home, season, rnd, is_final, pos_h, away, hp, ap,
         verdict(hp, ap), hp - ap, 0, 0, 0, "Home Oval", 10000, date, key,
         "unique", None),
        (away, season, rnd, is_final, pos_a, home, ap, hp,
         verdict(ap, hp), ap - hp, 0, 0, 0, "Home Oval", 10000, date, key,
         "unique", None),
    ]


def source_rows():
    """Six matches, one for every shape of scoreline the filters name.

    g1  alpha 100  beta  96   four points in it, both in three figures
    g2  beta  120  alpha 118  two points, both past 100, 238 combined
    g3  alpha  45  gamma 44   one point, and nobody reached fifty
    g4  gamma  70  beta  70   a draw
    g5  alpha 150  beta  20   a 130-point beating, and a 20-point score
    g6  beta  105  alpha 101  four points, both past 100 -- and a final
    """
    return (
        _match("g1", 2000, "R1", "2000-04-01", "alpha", "beta", 100, 96)
        + _match("g2", 2000, "R2", "2000-04-08", "beta", "alpha", 120, 118)
        + _match("g3", 2000, "R3", "2000-04-15", "alpha", "gamma", 45, 44)
        + _match("g4", 2000, "R4", "2000-04-22", "gamma", "beta", 70, 70)
        + _match("g5", 2000, "R5", "2000-04-29", "alpha", "beta", 150, 20)
        + _match("g6", 2000, "GF", "2000-09-01", "beta", "alpha", 105, 101,
                 is_final=1)
    )


@pytest.fixture
def con():
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.execute(f"CREATE TABLE club_match_sources ({', '.join(COLUMNS)})")
    con.executemany(
        f"INSERT INTO club_match_sources VALUES "
        f"({','.join('?' * len(COLUMNS))})", source_rows())
    con.commit()
    return con


def keys(matches):
    return sorted(m.source_game_key for m in matches)


# ----------------------------------------------------------- the filters

def test_a_margin_ceiling_finds_the_close_games(con):
    assert keys(H.search_matches(con, max_margin=5)) == [
        "g1", "g2", "g3", "g4", "g6"]


def test_a_margin_floor_and_ceiling_compose(con):
    """Close but not drawn."""
    assert keys(H.search_matches(con, min_margin=1, max_margin=5)) == [
        "g1", "g2", "g3", "g6"]


def test_both_teams_scoring_is_the_lower_of_the_two_scores(con):
    """Not the loser's: in a draw there is no loser, and 'both sides made
    a hundred' still has an answer."""
    # g1 was won 100-96: one side made a hundred, both did not.
    assert keys(H.search_matches(con, min_low_score=100)) == ["g2", "g6"]
    assert keys(H.search_matches(con, min_high_score=100)) == [
        "g1", "g2", "g5", "g6"]


def test_the_question_that_prompted_all_this(con):
    """Decided by under a goal, with both sides past a hundred."""
    assert keys(H.search_matches(con, max_margin=5, min_low_score=100)) == [
        "g2", "g6"]
    # g5 is a 150-20 beating, so a floor on the higher score alone is not
    # this question; g1's 100-96 needs the floor down at 96 to qualify.
    assert keys(H.search_matches(con, max_margin=5, min_low_score=96)) == [
        "g1", "g2", "g6"]


def test_zero_is_a_bound_and_not_an_absence(con):
    """`max_margin=0` is how a drawn game is asked for. Reading a zero as
    'no bound' would answer it with every match ever played."""
    assert keys(H.search_matches(con, max_margin=0)) == ["g4"]


def test_a_ceiling_on_the_higher_score_finds_the_low_scoring_games(con):
    assert keys(H.search_matches(con, max_high_score=49)) == ["g3"]


def test_a_ceiling_on_the_lower_score_finds_the_beatings(con):
    assert keys(H.search_matches(con, max_low_score=20)) == ["g5"]


def test_the_combined_total_adds_the_two_sides(con):
    assert keys(H.search_matches(con, min_total=200)) == ["g2", "g6"]
    assert keys(H.search_matches(con, max_total=90)) == ["g3"]


def test_the_scoreline_stays_a_fact_about_the_match_not_the_club(con):
    """beta lost g1 by four and won g2 by two. Both are games decided by
    under a goal, and a club filter must not quietly turn the ceiling into
    'and beta was the one who lost by under a goal'."""
    found = H.search_matches(con, club_id="beta", max_margin=5)
    assert keys(found) == ["g1", "g2", "g4", "g6"]
    # Both directions survive: the four-point loss, the draw, and the two
    # wins by two and by four.
    assert sorted(m.margin for m in found) == [-4, 0, 2, 4]


def test_the_scoreline_composes_with_the_ordinary_filters(con):
    assert keys(H.search_matches(con, max_margin=5, scope="finals")) == ["g6"]


def test_an_unknown_scoreline_filter_is_refused(con):
    with pytest.raises(ValueError):
        H._scoreline({"min_crowd": 10000})
    with pytest.raises(ValueError):
        H._scoreline({"exactly_margin": 5})


# ------------------------------------------------------------- ordering

def test_a_club_less_search_ranks_margins_by_size_not_by_sign(con):
    """The listing shows an absolute margin, so 'biggest' has to mean
    biggest. Ordering by the signed margin ranks by how heavily the
    first-named side won, which files the biggest away wins under
    'smallest margin'."""
    widest = H.search_matches(con, order="margin_abs_desc")
    assert widest[0].source_game_key == "g5"
    closest = H.search_matches(con, order="margin_abs_asc")
    assert closest[0].source_game_key == "g4"        # the draw
    # g2 was won by the away side by two; a signed ascending sort would
    # rank it as the widest loss and put it first.
    assert closest[-1].source_game_key == "g5"


def test_the_total_orders_run_over_both_scores(con):
    assert H.search_matches(con, order="total_desc")[0].source_game_key == "g2"
    assert H.search_matches(con, order="total_asc")[0].source_game_key == "g3"


# ---------------------------------------------------------------- page

def _run(con):
    # Imported inside: AppTest re-executes this function's source in an
    # empty module namespace, so nothing at the top of this file is in
    # scope.
    import sports as _sports
    from afl import past_games as _pg
    _pg.past_games_page(_sports.AFL, con)


def page(con, **state):
    at = AppTest.from_function(_run, args=(con,))
    for name, value in state.items():
        at.session_state[f"afl:{name}"] = value
    at.run(timeout=30)
    assert not at.exception, at.exception
    return at


def captions(at):
    return [caption.value for caption in at.caption]


def test_the_page_filters_on_the_scoreline(con):
    at = page(con, pg_margin_max=5, pg_low_min=100)
    assert any(caption.startswith("2 games") for caption in captions(at))


def test_an_empty_box_is_no_bound_at_all(con):
    at = page(con)
    assert any(caption.startswith("6 games") for caption in captions(at))


def test_the_active_scoreline_is_visible_while_the_expander_is_shut(con):
    """Otherwise the page is showing three games out of six and the reason
    is folded away inside a collapsed expander."""
    at = page(con, pg_margin_max=5, pg_low_min=100)
    assert any("margin 5 or less" in expander.label
               and "both teams 100 or more" in expander.label
               for expander in at.get("expander"))


def test_choosing_a_named_scoreline_fills_the_boxes(con):
    at = page(con)
    at.selectbox(key="afl:pg_scoreline").select(
        "Under a goal, both past 100").run(timeout=30)
    assert not at.exception, at.exception
    assert at.session_state["afl:pg_margin_max"] == 5
    assert at.session_state["afl:pg_low_min"] == 100
    assert any(caption.startswith("2 games") for caption in captions(at))


def test_the_picker_returns_to_rest_so_the_numbers_are_the_search(con):
    """The boxes are what is searched, and widening one afterwards is an
    ordinary thing to do. A picker still reading 'Under a goal' would be
    describing a search nobody is running."""
    at = page(con)
    at.selectbox(key="afl:pg_scoreline").select("Drawn").run(timeout=30)
    assert at.session_state["afl:pg_scoreline"] == PG._NO_PRESET
    assert at.session_state["afl:pg_margin_max"] == 0
    assert any(caption.startswith("1 games") for caption in captions(at))


def test_a_named_scoreline_explains_itself_while_it_still_holds(con):
    at = page(con)
    at.selectbox(key="afl:pg_scoreline").select(
        "Decided by under a goal").run(timeout=30)
    assert any("A goal is six points" in caption for caption in captions(at))
    # Widen the margin and the caption stops claiming to be that preset.
    at.number_input(key="afl:pg_margin_max").set_value(50).run(timeout=30)
    assert not any("A goal is six points" in caption
                   for caption in captions(at))


# ------------------------------------------------- the declared presets

def every_scoreline():
    for sport in sports.SPORTS.values():
        for preset in sport.scorelines:
            yield sport, preset


def test_every_named_scoreline_actually_bounds_something():
    for sport, preset in every_scoreline():
        bounds = {name: value for name, value in preset.filters().items()
                  if value is not None}
        assert bounds, f"{sport.key}: {preset.label} bounds nothing"
        for subject in ("margin", "low_score", "high_score", "total"):
            lo = bounds.get(f"min_{subject}")
            hi = bounds.get(f"max_{subject}")
            if lo is not None and hi is not None:
                assert lo <= hi, f"{sport.key}: {preset.label} {subject}"


def test_every_named_scoreline_is_named_once_per_sport():
    for sport in sports.SPORTS.values():
        labels = [preset.label for preset in sport.scorelines]
        assert len(labels) == len(set(labels)), sport.key


def test_a_scoreline_only_speaks_in_filters_the_search_accepts():
    for sport, preset in every_scoreline():
        for name in preset.filters():
            assert name in registry.SCORELINE_FILTERS


def test_live_every_named_scoreline_finds_games():
    checked = 0
    for sport, preset in every_scoreline():
        if not _os.path.exists(sport.db):
            continue
        con = sqlite3.connect(f"file:{sport.db}?mode=ro", uri=True)
        try:
            if not H.club_history_available(con):
                continue
            found = H.search_matches(con, limit=1, **preset.filters())
        finally:
            con.close()
        assert found, f"{sport.key}: '{preset.label}' matches no game"
        checked += 1
    if not checked:
        pytest.skip("no built database")


if __name__ == "__main__":
    # Fixture-based, so pytest drives it rather than the bare run() loop
    # the older suites in this directory carry.
    raise SystemExit(pytest.main([__file__]))
