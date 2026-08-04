#!/usr/bin/env python3
"""Club lineage expansion and per-square star scaling.

Two changes that alter existing answers, so both get live-data assertions
as well as fixture ones.
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

import core
import sports


# ------------------------------------------------------------- lineage

def schema_with_lineage():
    return core.Schema(
        clubs=["Brisbane Lions", "Carlton"],
        club_lineage={"Brisbane Lions":
                      ["Brisbane Lions", "Brisbane Bears", "Fitzroy"]},
        required_games_cols=(), required_player_cols=())


def lineage_fixture():
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE games (
          player_id INTEGER, season INTEGER, club_now TEXT, club_hist TEXT,
          career_game_no INTEGER
        );
        INSERT INTO games VALUES
          (1, 2000, 'Brisbane Lions',  'Brisbane Lions', 1),
          (2, 1990, 'Brisbane Bears',  'Brisbane Bears', 1),
          (3, 1980, 'Fitzroy',         'Fitzroy',        1),
          (4, 1990, 'Carlton',         'Carlton',        1);
    """)
    return con


def ids(con, built):
    sql, params = built
    return {r[0] for r in con.execute(sql, params)}


def test_current_club_includes_its_predecessors():
    con, G = lineage_fixture(), core.Generic(schema_with_lineage())
    assert ids(con, G.played_for("Brisbane Lions")) == {1, 2, 3}


def test_a_predecessor_named_directly_returns_only_itself():
    """Expansion is one-directional. A Fitzroy square is about Fitzroy."""
    con, G = lineage_fixture(), core.Generic(schema_with_lineage())
    assert ids(con, G.played_for("Fitzroy")) == {3}
    assert ids(con, G.played_for("Brisbane Bears")) == {2}


def test_a_club_with_no_lineage_is_unaffected():
    con, G = lineage_fixture(), core.Generic(schema_with_lineage())
    assert ids(con, G.played_for("Carlton")) == {4}


def test_debut_club_expands_too():
    con, G = lineage_fixture(), core.Generic(schema_with_lineage())
    assert ids(con, G.debut_club("Brisbane Lions")) == {1, 2, 3}


def test_club_identities_defaults_to_the_name_itself():
    s = schema_with_lineage()
    assert s.club_identities("Carlton") == ["Carlton"]
    assert s.club_identities("Brisbane Lions")[0] == "Brisbane Lions"


def test_afl_lineage_shape():
    """Only Brisbane changes an answer; the rest state an existing rule."""
    lineage = sports.AFL_CLUB_LINEAGE
    assert lineage["Brisbane Lions"] == [
        "Brisbane Lions", "Brisbane Bears", "Fitzroy"]
    for current in ("Sydney", "Western Bulldogs", "North Melbourne"):
        assert lineage[current][0] == current
    # University folded with no successor and must not be swept into anyone.
    assert not any("University" in names for names in lineage.values())


def test_picker_offers_the_lions_not_the_bears():
    assert "Brisbane Lions" in sports.AFL_CLUBS
    assert "Brisbane Bears" not in sports.AFL_CLUBS
    # Fitzroy is a club in its own right and stays selectable.
    assert "Fitzroy" in sports.AFL_CLUBS
    assert "University" in sports.AFL_CLUBS


# --------------------------------------------------------------- stars

def test_absolute_scale_is_unchanged_without_a_range():
    assert core.star_value(100) == 5.0
    assert core.star_value(50) == 2.5
    assert core.star_value(0) == 0.0
    assert core.star_value(None) is None


def test_rescaling_puts_the_rarest_answer_at_five():
    assert core.star_value(40, lo=0.4, hi=40) == 5.0
    assert core.star_value(0.4, lo=0.4, hi=40) == 0.0
    assert core.star_value(20.2, lo=0.4, hi=40) == 2.5


def test_a_flat_square_scores_five():
    """One answer, or several sharing an obscurity, is trivially rarest."""
    assert core.star_value(30, lo=30, hi=30) == 5.0
    assert core.star_value(30, lo=40, hi=30) == 5.0


def test_values_outside_the_range_are_clamped():
    assert core.star_value(90, lo=0, hi=50) == 5.0
    assert core.star_value(-5, lo=0, hi=50) == 0.0


def test_half_star_steps_are_preserved():
    for value in (core.star_value(o, lo=0, hi=100) for o in range(0, 101, 7)):
        assert (value * 2) % 1 == 0, value


def star_fixture():
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE players (
          player_id INTEGER, player TEXT, debut_season INTEGER,
          final_season INTEGER, career_games INTEGER, career_goals INTEGER,
          obscurity REAL, name_key TEXT
        );
        INSERT INTO players VALUES
          (1,'Obscure',1900,1905,10,2,40.0,'obscure'),
          (2,'Middling',1950,1960,100,50,20.0,'middling'),
          (3,'Famous',2000,2015,300,400,0.4,'famous');
    """)
    return con


def test_square_reports_the_obscurity_range():
    con = star_fixture()
    schema = core.Schema(required_games_cols=(), required_player_cols=())
    sq = core.square(
        con, [("SELECT player_id FROM players WHERE career_games >= ?", [1])],
        schema)
    assert sq.eligible == 3
    assert sq.obscurity_min == 0.4
    assert sq.obscurity_max == 40.0
    # Ordered by obscurity, the shown answer is the rarest, so five stars.
    assert sq.best_name == "Obscure"
    assert sq.stars == 5.0
    # The absolute scale still says what it always said.
    assert sq.absolute_stars == 2.0


def test_square_stars_are_not_trivially_five_under_other_orderings():
    """The face rating is informative whenever ranking is not by obscurity."""
    con = star_fixture()
    schema = core.Schema(required_games_cols=(), required_player_cols=())
    sq = core.square(
        con, [("SELECT player_id FROM players WHERE career_games >= ?", [1])],
        schema, order="newest")
    assert sq.best_name == "Famous"
    assert sq.stars == 0.0


def test_empty_square_has_no_range():
    con = star_fixture()
    schema = core.Schema(required_games_cols=(), required_player_cols=())
    sq = core.square(
        con, [("SELECT player_id FROM players WHERE career_games > ?", [999])],
        schema)
    assert sq.eligible == 0
    assert sq.stars is None


def test_stars_text_and_html_accept_a_range():
    assert "5/5" in core.stars_text(40, lo=0.4, hi=40)
    assert "0/5" in core.stars_text(0.4, lo=0.4, hi=40)
    assert "5/5" in core.stars_html(40, lo=0.4, hi=40)


# ============================================================ live data

def live():
    from pathlib import Path

    from data_paths import default_db
    db = default_db("afl")
    if not Path(db).exists():
        return None
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def test_live_brisbane_lions_includes_bears_and_fitzroy():
    """The reported bug: a Lions square answered 249 and missed ~1,300."""
    con = live()
    if con is None:
        pytest.skip("no built database")
    from afl import constraints as C

    lions = C.count(con, [C.played_for("Brisbane Lions")])
    bears = C.count(con, [C.played_for("Brisbane Bears")])
    fitzroy = C.count(con, [C.played_for("Fitzroy")])

    assert lions > 1400, lions
    assert lions > bears + fitzroy - 500      # deduped, but far above 249
    for club in ("Brisbane Bears", "Fitzroy"):
        both = C.count(con, [C.played_for("Brisbane Lions"),
                             C.played_for(club)])
        assert both == C.count(con, [C.played_for(club)]), club


def test_live_already_merged_clubs_are_unchanged():
    """club_now already folds these three, so lineage must be a no-op."""
    con = live()
    if con is None:
        pytest.skip("no built database")
    from afl import constraints as C

    for club, predecessor in (("Sydney", "South Melbourne"),
                              ("Western Bulldogs", "Footscray"),
                              ("North Melbourne", "Kangaroos")):
        total = C.count(con, [C.played_for(club)])
        assert total > 900, (club, total)
        # The predecessor is reachable through the current name.
        assert C.count(con, [C.played_for(club),
                             C.played_for(predecessor)]) == \
            C.count(con, [C.played_for(predecessor)])


def test_live_university_is_not_folded_into_anyone():
    con = live()
    if con is None:
        pytest.skip("no built database")
    from afl import constraints as C
    uni = C.count(con, [C.played_for("University")])
    assert 0 < uni < 200
    for club in sports.AFL_CLUBS:
        if club == "University":
            continue
        overlap = C.count(con, [C.played_for("University"),
                                C.played_for(club)])
        assert overlap < uni, club


def test_live_square_stars_spread_across_the_range():
    """On the absolute scale a real square's answers were nearly flat."""
    con = live()
    if con is None:
        pytest.skip("no built database")
    from afl import parse_criteria as P

    schema = sports.AFL_SCHEMA
    cs = [P.parse("ST KILDA")[0], P.parse("150+ GAMES PLAYED")[0]]
    sq = core.square(con, cs, schema)
    rows = core.solve(con, cs, schema, limit=25)

    absolute = {core.star_value(r[-1]) for r in rows}
    scaled = {core.star_value(r[-1], lo=sq.obscurity_min,
                              hi=sq.obscurity_max) for r in rows}
    assert len(scaled) > len(absolute), (sorted(absolute), sorted(scaled))
    assert max(scaled) == 5.0


def run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:
                if exc.__class__.__name__ == "Skipped":
                    continue
                raise
    print("club lineage and star tests: passed")


if __name__ == "__main__":
    run()
