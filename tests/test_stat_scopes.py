#!/usr/bin/env python3
"""Every statistic must be askable at every scope.

The reported gap was "x+ frees against a season", but the real hole was
wider: only goals and games had a career question, so '500+ career marks'
could not be expressed at all, and 'AVG 20+ DISPOSALS CAREER' silently
answered with a *season* average instead.

These tests pin the whole grid: statistic x scope x total-or-average, for
every stat in the schema and not just the ones someone happened to ask
about.
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

from afl import constraints as C
import core
from afl import parse_criteria as P
import sports


def fixture():
    """Three careers shaped so each scope gives a different answer."""
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE players (
          player_id INTEGER, player TEXT, debut_season INTEGER,
          final_season INTEGER, career_games INTEGER, career_goals INTEGER,
          finals_played INTEGER, clubs_hist TEXT, obscurity REAL,
          n_clubs INTEGER
        );
        CREATE TABLE games (
          player_id INTEGER, season INTEGER, club_now TEXT, club_hist TEXT,
          is_final INTEGER, frees_against REAL, disposals REAL, goals REAL
        );
        INSERT INTO players VALUES
          (1,'Spike',2000,2001,4,0,1,'A',50.0,1),
          (2,'Steady',2000,2001,6,0,0,'B',50.0,1),
          (3,'Ancient',1950,1950,3,0,0,'C',90.0,1);
    """)
    rows = [
        # Spike: one huge game, small otherwise. Career total 34.
        (1, 2000, "A", "A", 0, 30, 40, 1),
        (1, 2000, "A", "A", 0, 2, 10, 0),
        (1, 2001, "A", "A", 0, 1, 10, 0),
        (1, 2001, "A", "A", 1, 1, 12, 2),      # a final
        # Steady: six even games, career total 36, no big game.
        *[(2, 2000 + (i > 2), "B", "B", 0, 6, 20, 1) for i in range(6)],
        # Ancient: pre-recording era, frees NULL throughout.
        *[(3, 1950, "C", "C", 0, None, None, 2) for _ in range(3)],
    ]
    con.executemany("INSERT INTO games VALUES (?,?,?,?,?,?,?,?)", rows)
    return con


def G():
    return core.Generic(core.Schema(
        stats=["frees_against", "disposals", "goals"],
        required_games_cols=(), required_player_cols=()))


def ids(con, built):
    sql, params = built
    return {r[0] for r in con.execute(sql, params)}


# ------------------------------------------------- the scopes differ

def test_each_scope_answers_a_different_question():
    con, g = fixture(), G()
    # Spike has the biggest single game; Steady the biggest career total.
    assert ids(con, g.stat_in_a_game("frees_against", 30)) == {1}
    assert ids(con, g.career_stat_total_min("frees_against", 36)) == {2}
    # Season totals: Spike 32 in 2000, Steady 18 then 18.
    assert ids(con, g.season_stat_total_min("frees_against", 32)) == {1}
    assert ids(con, g.season_stat_total_min("frees_against", 18)) == {1, 2}


def test_career_total_sums_across_seasons():
    con, g = fixture(), G()
    assert ids(con, g.career_stat_total_min("frees_against", 34)) == {1, 2}
    assert ids(con, g.career_stat_total_min("frees_against", 37)) == set()


def test_career_average_respects_its_games_floor():
    con, g = fixture(), G()
    # Steady averages 6.0 over 6 games; Spike 8.5 over 4.
    assert ids(con, g.career_stat_average_min("frees_against", 6,
                                              min_games=4)) == {1, 2}
    assert ids(con, g.career_stat_average_min("frees_against", 6,
                                              min_games=5)) == {2}


def test_games_with_stat_counts_separate_games():
    con, g = fixture(), G()
    assert ids(con, g.games_with_stat_min("frees_against", 6, 6)) == {2}
    assert ids(con, g.games_with_stat_min("frees_against", 6, 7)) == set()
    assert ids(con, g.games_with_stat_min("frees_against", 30, 1)) == {1}


def test_finals_scope_only_counts_finals():
    con, g = fixture(), G()
    assert ids(con, g.stat_in_a_postseason_game("goals", 2)) == {1}
    assert ids(con, g.stat_in_a_postseason_game("goals", 3)) == set()


def test_unrecorded_stat_never_matches_at_any_scope():
    """The pre-recording career must not appear as a career total of 0."""
    con, g = fixture(), G()
    for built in (g.career_stat_total_min("frees_against", 0),
                  g.season_stat_total_min("frees_against", 0),
                  g.career_stat_average_min("frees_against", 0, min_games=1),
                  g.games_with_stat_min("frees_against", 0, 1)):
        assert 3 not in ids(con, built)


def test_unknown_stat_is_rejected_at_every_scope():
    g = G()
    for method in ("career_stat_total_min", "career_stat_average_min",
                   "games_with_stat_min", "stat_in_a_postseason_game",
                   "postseason_stat_average_min"):
        with pytest.raises(ValueError):
            getattr(g, method)("not_a_stat", 1)


# ------------------------------------------------------------ parsing

def parse(text):
    built, label = P.parse(text)
    assert built is not None, f"unparsed: {text} ({label})"
    return label


def test_frees_against_answers_at_every_scope():
    """The reported failure, at each scope it can be asked."""
    assert parse("30+ FREES AGAINST - SEASON") == \
        "30+ frees_against in a season"
    assert parse("30+ FREES AGAINST IN A SEASON") == \
        "30+ frees_against in a season"
    assert parse("5+ FREES AGAINST IN A GAME") == \
        "5+ frees_against in a game"
    assert parse("300+ CAREER FREES AGAINST") == \
        "300+ frees_against in a career"
    assert "avg in a season" in parse("AVG 2+ FREES AGAINST SEASON")
    assert "career" in parse("AVG 2+ FREES AGAINST CAREER")
    assert parse("3+ FREES AGAINST IN A FINAL") == \
        "3+ frees_against in a final"


def test_frees_for_answers_too():
    assert parse("30+ FREES FOR IN A SEASON") == "30+ frees_for in a season"
    assert parse("200+ CAREER FREES FOR") == "200+ frees_for in a career"
    assert parse("5+ FREES FOR IN A GAME") == "5+ frees_for in a game"


def test_career_scope_is_not_answered_with_a_season():
    """'AVG 20+ DISPOSALS CAREER' used to return a season average."""
    label = parse("AVG 20+ DISPOSALS CAREER")
    assert "career" in label and "in a season" not in label


def test_repeat_games_wording():
    assert parse("10+ GAMES WITH 30+ DISPOSALS") == \
        "10+ games with 30+ disposals"


def test_every_stat_parses_at_every_scope():
    """No statistic may be reachable at one scope and not another."""
    words = {v: k for k, v in
             sorted(P.STAT_WORDS.items(), key=lambda kv: -len(kv[0]))}
    missing = []
    for stat, word in words.items():
        for template, expect in (
            ("5+ {} IN A GAME", "in a game"),
            ("50+ {} IN A SEASON", "in a season"),
            ("500+ {} CAREER", "in a career"),
            ("AVG 2+ {} SEASON", "avg in a season"),
        ):
            text = template.format(word.upper())
            built, label = P.parse(text)
            if built is None or expect not in label or stat not in label:
                missing.append((text, label))
    assert not missing, missing


def test_cap_wording_is_not_answered_with_a_floor():
    """A cap means the opposite; answering it with a floor is a wrong
    answer, not a gap."""
    label = parse("LESS THAN 20 GOALS - CAREER")
    assert "fewer" in label
    assert parse("UNDER 50 GAMES").startswith("49 or fewer")


def test_existing_wording_still_routes_the_same_way():
    for text, expected in (
        ("40+ DISPOSALS IN A GAME", "40+ disposals in a game"),
        ("500+ DISPOSALS IN A SEASON", "500+ disposals in a season"),
        ("50+ GOALS IN A SEASON", "50+ goals in a season"),
        ("30+ CONTESTED MARKS SEASON", "30+ contested_marks in a season"),
        ("30+ DISPOSALS & 3+ GOALS GAME", "30+ disposals & 3+ goals"),
        ("100+ POINT WIN", "won by 100+ points"),
        ("100,000+ CROWD", "crowd of 100,000+"),
    ):
        assert parse(text) == expected, text


# --------------------------------------------------------- registration

def test_every_scope_is_in_the_manual_builder():
    for name in ("X+ of a stat in one game", "X+ of a stat in one season",
                 "X+ of a stat in a career", "Season average of a stat",
                 "Career average of a stat", "X+ games with Y+ of a stat",
                 "X+ of a stat in a final", "Finals average of a stat"):
        assert name in C.BUILDERS, name


#: Arguments axis_widget renders with an explicit branch, and arguments it
#: renders through the numeric fallback. Every builder argument in every
#: sport must be one or the other.
UI_HANDLED_ARGS = {"club", "venue", "state", "player_id", "kind", "source", "award",
                   "times", "avg", "player", "stat", "stat_a", "stat_b",
                   "min_games", "derby", "event", "rivalry",
                   "ground_status", "ground_metric",
                   "award_axis", "position", "aa_position", "average",
                   "min_plate_appearances", "war", "place", "votes",
                   "cm", "kg", "decade"}
UI_NUMERIC_ARGS = {"x", "y", "x_a", "x_b", "games", "goals", "clubs", "from",
                   "to", "season", "times", "points", "people", "round"}


@pytest.mark.parametrize("sport_key", ["afl", "mlb", "nfl", "nba"])
def test_every_builder_argument_is_renderable_by_the_ui(sport_key):
    """A builder whose argument the UI cannot render is unusable.

    app.py's axis_widget has a branch per argument name and a numeric
    fallback; anything not numeric needs an explicit branch, so this
    catches a new builder that quietly cannot be configured.

    Parameterised over every registered sport. It used to check the AFL
    alone, which is why MLB's `rivalry` argument went in unguarded: a
    missing branch there would have rendered a franchise-pair key as a
    number input and nobody would have found out until the board was open.
    """
    module = sports.get(sport_key).C
    for name, (_fn, argnames) in module.BUILDERS.items():
        for arg in argnames:
            assert arg in UI_HANDLED_ARGS or arg in UI_NUMERIC_ARGS, (
                sport_key, name, arg)


# ============================================================ live data

def live():
    from pathlib import Path

    from data_paths import default_db
    db = default_db("afl")
    if not Path(db).exists():
        return None
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def test_live_every_stat_builder_executes_for_every_stat():
    """198 combinations. Compiled, not counted: a full count of each is
    minutes of table scans and proves nothing extra about the SQL."""
    con = live()
    if con is None:
        pytest.skip("no built database")
    sample = {"x": 5, "y": 5, "avg": 1.0, "min_games": 10, "times": 2,
              "x_a": 5, "x_b": 1,
              # Builders that scope a statistic to a fixture or a ground.
              "derby": "showdown", "event": "Anzac Day", "venue": "M.C.G."}
    bad = []
    for name, (fn, argnames) in C.BUILDERS.items():
        if not any(a.startswith("stat") for a in argnames):
            continue
        for stat in sports.AFL_STATS:
            args = [stat if a.startswith("stat") else sample.get(a, 5)
                    for a in argnames]
            sql, params = fn(*args)
            try:
                con.execute(
                    f"EXPLAIN SELECT 1 FROM players p "
                    f"WHERE p.player_id IN ({sql})", params)
            except sqlite3.Error as exc:
                bad.append((name, stat, str(exc)))
    assert not bad, bad[:5]


def test_live_frees_against_returns_players_at_every_scope():
    con = live()
    if con is None:
        pytest.skip("no built database")
    for built in (C.stat_in_a_game("frees_against", 6),
                  C.season_stat_total_min("frees_against", 30),
                  C.career_stat_total_min("frees_against", 300),
                  C.season_stat_average_min("frees_against", 2),
                  C.career_stat_average_min("frees_against", 2),
                  C.games_with_stat_min("frees_against", 5, 10),
                  C.stat_in_a_final("frees_against", 4)):
        assert C.count(con, [built]) > 0


def run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:
                if exc.__class__.__name__ == "Skipped":
                    continue
                raise
    print("stat scope tests: passed")


if __name__ == "__main__":
    run()
