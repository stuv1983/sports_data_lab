#!/usr/bin/env python3
"""Regression tests for season-total constraints and the frees columns.

Covers step 1 of the build plan:

  * the four stats build_db.py has always loaded but sports.AFL_STATS never
    listed (frees_for, frees_against, clangers, uncontested) now pass
    core._check() and reach the query layer;
  * season_stat_total_min sums across a season rather than per game, and
    never treats an unrecorded (NULL) stat as a zero;
  * the parser routes a bare season qualifier to the total builder and an
    explicit avg/average to the existing average builder;
  * STAT_WORDS is matched longest-key-first, so a longer criterion word is
    never shadowed by a shorter one that is a prefix of it.
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

import core
import parse_criteria
import sports


NEW_STATS = ("frees_for", "frees_against", "clangers", "uncontested")


def fixture():
    """A games table with three deliberately-shaped careers.

    player 1  one 30-free game, 12 frees across the rest of that season
    player 2  no big game, but 30 frees accumulated over the season
    player 3  a pre-recording season: every frees_against value is NULL
    """
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE players (
          player_id INTEGER, player TEXT, debut_season INTEGER,
          final_season INTEGER, career_games INTEGER, career_goals INTEGER,
          finals_played INTEGER, clubs_hist TEXT, obscurity REAL
        );
        CREATE TABLE games (
          player_id INTEGER, player TEXT, season INTEGER, round TEXT,
          club_now TEXT, club_hist TEXT, venue TEXT, is_final INTEGER,
          goals REAL, marks REAL, contested_marks REAL, disposals REAL,
          frees_for REAL, frees_against REAL, clangers REAL,
          uncontested REAL
        );
    """)
    rows = []
    # Player 1: a single 30-free game, then six 2-free games. Season total 42
    # but no single game reaches 30 twice -- the point is that a per-game
    # threshold and a season threshold are different questions.
    rows.append((1, "Spike", 2000, "1", "A", "A", "V", 0,
                 1, 5, 1, 20, 3, 30, 2, 10))
    for r in range(2, 8):
        rows.append((1, "Spike", 2000, str(r), "A", "A", "V", 0,
                     0, 4, 0, 18, 2, 2, 1, 9))
    # Player 2: fifteen 2-free games. Season total 30, best game only 2.
    for r in range(1, 16):
        rows.append((2, "Steady", 2000, str(r), "B", "B", "V", 0,
                     1, 6, 2, 22, 1, 2, 3, 11))
    # Player 3: a season before frees were recorded. goals are known; every
    # frees_against is NULL, which must never be summed as zero.
    for r in range(1, 21):
        rows.append((3, "Ancient", 1950, str(r), "C", "C", "V", 0,
                     2, None, None, None, None, None, None, None))
    con.executemany(
        "INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.executemany(
        "INSERT INTO players VALUES (?,?,?,?,?,?,?,?,?)",
        [(1, "Spike", 2000, 2000, 7, 1, 0, "A", 50.0),
         (2, "Steady", 2000, 2000, 15, 15, 0, "B", 50.0),
         (3, "Ancient", 1950, 1950, 20, 40, 0, "C", 90.0)])
    return con


def ids(con, built):
    sql, params = built
    return {r[0] for r in con.execute(sql, params)}


def grammar():
    """A Generic bound to the AFL schema, as constraints.py builds one."""
    return core.Generic(sports.AFL_SCHEMA)


# ------------------------------------------------------ the stat allowlist

def test_new_stats_pass_check():
    """The four loaded-but-unlisted stats reach the query layer at all."""
    C = grammar()
    for stat in NEW_STATS:
        C._check(stat)                      # raises ValueError if unlisted
        assert stat in sports.AFL_STATS, stat


def test_per_game_constraint_on_frees_returns_rows():
    con = fixture()
    C = grammar()
    assert ids(con, C.stat_in_a_game("frees_against", 30)) == {1}
    assert ids(con, C.stat_in_a_game("frees_against", 31)) == set()


# ------------------------------------------------------- season semantics

def test_season_total_sums_across_a_season():
    """Both players reach 30 for the season, by different routes."""
    con = fixture()
    C = grammar()
    assert ids(con, C.season_stat_total_min("frees_against", 30)) == {1, 2}


def test_one_big_game_is_not_a_qualifying_season():
    """A 30-free game inside a 12-free remainder is a 42-free season.

    Raise the bar above that season total and the spike player drops out,
    which is the distinction a per-game builder cannot express.
    """
    con = fixture()
    C = grammar()
    assert ids(con, C.season_stat_total_min("frees_against", 43)) == set()
    assert ids(con, C.season_stat_total_min("frees_against", 42)) == {1}


def test_null_season_never_matches_at_any_threshold():
    """An unrecorded stat is not a season total of zero.

    Player 3 played twenty games in a season predating frees entirely.
    Summing NULL as 0 would give a real-looking total of 0 and assert
    something the source never said, so even a threshold of 0 must not
    match.
    """
    con = fixture()
    C = grammar()
    for n in (0, 1, 30):
        assert 3 not in ids(con, C.season_stat_total_min("frees_against", n)), n


def test_no_minimum_games_floor():
    """Unlike an average, a total is not distorted by a short season."""
    con = fixture()
    C = grammar()
    # Player 1 played 7 games; the average builder's floor is 5, but the
    # total builder must not impose one at all.
    assert 1 in ids(con, C.season_stat_total_min("frees_against", 42))


# ------------------------------------------------------------- the parser

def parse(text):
    result = parse_criteria.parse(text)
    assert result is not None, f"unparsed: {text}"
    return result


def test_season_phrase_routes_to_total_builder():
    con = fixture()
    built, label = parse("30+ FREES AGAINST - SEASON")
    assert label == "30+ frees_against in a season", label
    assert ids(con, built) == {1, 2}


def test_average_phrase_still_routes_to_average_builder():
    """3d-bis must keep its wording; 3d-ter must not swallow it."""
    _built, label = parse("AVG 2+ FREES AGAINST - SEASON")
    assert "avg in a season" in label, label
    assert "frees_against" in label, label


def test_season_totals_generalise_beyond_frees():
    """The rule's real value: every season-total square, not just this one."""
    for text, expected in (
        ("500+ DISPOSALS IN A SEASON", "500+ disposals in a season"),
        ("50+ GOALS IN A SEASON", "50+ goals in a season"),
        ("100+ TACKLES IN A SEASON", "100+ tackles in a season"),
    ):
        _built, label = parse(text)
        assert label == expected, (text, label)


def test_career_phrase_is_not_a_season_total():
    """The career guard keeps 3d-ter off career squares."""
    _built, label = parse("100+ GOALS CAREER")
    assert "season" not in label, label


# ------------------------------------------- longest-key-first resolution

def test_frees_against_does_not_resolve_to_frees_for():
    for text in ("30+ FREES AGAINST - SEASON", "5+ FREES AGAINST IN A GAME"):
        _built, label = parse(text)
        assert "frees_against" in label, (text, label)
        assert "frees_for" not in label, (text, label)


def test_contested_marks_does_not_resolve_to_marks():
    """The ordering bug the plan predicted, in both rules that can hit it."""
    _built, label = parse("30+ CONTESTED MARKS SEASON")
    assert label == "30+ contested_marks in a season", label
    _built, label = parse("5+ CONTESTED MARKS IN A GAME")
    assert label == "5+ contested_marks in a game", label


def test_uncontested_does_not_resolve_to_contested():
    """"contested possession" is a literal substring of the uncontested key."""
    _built, label = parse("400+ UNCONTESTED POSSESSIONS IN A SEASON")
    assert "uncontested" in label, label


def test_stat_words_are_iterated_longest_first():
    lengths = [len(w) for w in parse_criteria.STAT_WORDS_BY_LENGTH]
    assert lengths == sorted(lengths, reverse=True)
    assert set(parse_criteria.STAT_WORDS_BY_LENGTH) == set(
        parse_criteria.STAT_WORDS)


# ------------------------------------------------------- builder registry

def test_total_builder_is_registered():
    import constraints
    assert "X+ of a stat in one season" in constraints.BUILDERS
    fn, args = constraints.BUILDERS["X+ of a stat in one season"]
    assert args == ["stat", "x"], args
    assert fn is constraints.season_stat_total_min


# ---------------------------------------------------------- era coverage

def test_new_stats_have_an_era_caption():
    """A season-total square must be able to say where coverage starts."""
    for stat in NEW_STATS:
        assert sports.AFL.stat_available_from(stat) is not None, stat
        warning = sports.AFL.stat_era_warning(stat)
        assert warning and str(sports.AFL.stat_eras[stat]) in warning, stat


def test_every_listed_stat_has_an_era():
    """No stat may be offered in the UI without a coverage answer."""
    missing = [s for s in sports.AFL_STATS
               if sports.AFL.stat_available_from(s) is None]
    assert not missing, f"no stat_eras entry: {missing}"


def run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("season total tests: passed")


if __name__ == "__main__":
    run()
