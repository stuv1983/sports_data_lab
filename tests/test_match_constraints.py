#!/usr/bin/env python3
"""Regression tests for match-context constraints and past-games search.

Margin, team score and drawn-match constraints read `games` directly and so
always work. Crowd constraints need the optional all-games layer and must
degrade to unavailable rather than raise. Both paths are covered here, plus
the parser wording that routes grid squares onto them.
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

from afl import club_history as CH
from afl import constraints as C
import match_constraints as M
from afl import parse_criteria as P


def fixture():
    """Two matches: a 90-point win and a draw, with a crowd on each."""
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE games (
          player_id INTEGER, season INTEGER, round TEXT, club_now TEXT,
          result TEXT, points_for INTEGER, points_against INTEGER,
          is_final INTEGER, match_id INTEGER
        );
        CREATE TABLE club_match_sources (
          source_club_id TEXT, season INTEGER, round TEXT, is_final INTEGER,
          team_position TEXT, points_for INTEGER, points_against INTEGER,
          result TEXT, margin INTEGER, venue_raw TEXT, attendance INTEGER,
          match_date TEXT, source_game_key TEXT, match_id INTEGER,
          match_status TEXT
        );
        INSERT INTO games VALUES
          (1, 2000, 'R1', 'Alpha', 'W', 120,  30, 0, 101),
          (2, 2000, 'R1', 'Beta',  'L',  30, 120, 0, 101),
          (1, 2000, 'GF', 'Alpha', 'D',  70,  70, 1, 102),
          (3, 2000, 'GF', 'Beta',  'D',  70,  70, 1, 102);
        INSERT INTO club_match_sources VALUES
          ('alpha',2000,'R1',0,'H',120,30,'W', 90,'Oval',  5000,
           '2000-04-01','g1',101,'unique'),
          ('beta', 2000,'R1',0,'A', 30,120,'L',-90,'Oval',  5000,
           '2000-04-01','g1',101,'unique'),
          ('alpha',2000,'GF',1,'F', 70, 70,'D',  0,'Big',  90000,
           '2000-09-01','g2',102,'unique'),
          ('beta', 2000,'GF',1,'F', 70, 70,'D',  0,'Big',  90000,
           '2000-09-01','g2',102,'unique');
    """)
    return con


def ids(con, built):
    sql, params = built
    return {r[0] for r in con.execute(sql, params)}


# ------------------------------------------------------ margin and result

def test_won_by_min_selects_only_the_winning_side():
    con = fixture()
    assert ids(con, M.won_by_min(90)) == {1}
    assert ids(con, M.won_by_min(91)) == set()


def test_lost_by_min_selects_only_the_losing_side():
    con = fixture()
    assert ids(con, M.lost_by_min(90)) == {2}


def test_won_by_max_is_inclusive():
    """Matches the career_score_max convention the parser relies on."""
    con = fixture()
    assert ids(con, M.won_by_max(90)) == {1}
    assert ids(con, M.won_by_max(89)) == set()


def test_a_draw_is_neither_a_win_nor_a_loss():
    con = fixture()
    assert ids(con, M.played_in_a_draw()) == {1, 3}
    assert 3 not in ids(con, M.won_by_min(0))
    assert 3 not in ids(con, M.lost_by_min(0))


def test_team_scored_min():
    con = fixture()
    assert ids(con, M.team_scored_min(120)) == {1}
    # Player 2's only match is the 30-point losing side, so 70 excludes them.
    assert ids(con, M.team_scored_min(70)) == {1, 3}
    assert ids(con, M.team_scored_min(30)) == {1, 2, 3}


def test_margin_builders_do_not_need_the_optional_layer():
    """They read `games`, so they work with no all-games rows at all."""
    con = fixture()
    con.execute("DROP TABLE club_match_sources")
    assert M.match_history_available(con) is False
    assert ids(con, M.won_by_min(90)) == {1}


# ------------------------------------------------------------ attendance

def test_crowd_min_reaches_through_match_id():
    con = fixture()
    assert ids(con, M.crowd_min(90000)) == {1, 3}
    assert ids(con, M.crowd_min(5000)) == {1, 2, 3}


def test_crowd_max_excludes_unrecorded_attendance():
    """A NULL crowd is unknown, not a crowd of zero."""
    con = fixture()
    con.execute("UPDATE club_match_sources SET attendance=NULL "
                "WHERE source_game_key='g1'")
    assert ids(con, M.crowd_max(10000)) == set()
    assert ids(con, M.crowd_min(0)) == {1, 3}


def test_crowd_in_a_final():
    con = fixture()
    assert ids(con, M.crowd_min_in_final(90000)) == {1, 3}
    assert ids(con, M.crowd_min_in_final(90001)) == set()


def test_two_source_rows_per_match_do_not_duplicate_players():
    con = fixture()
    sql, params = M.crowd_min(0)
    rows = con.execute(sql, params).fetchall()
    assert len(rows) == len(set(rows))


def test_availability_probe_is_false_without_the_table():
    con = sqlite3.connect(":memory:")
    assert M.match_history_available(con) is False
    assert M.match_history_count(con) == 0


def test_untrusted_source_rows_are_not_counted():
    con = fixture()
    con.execute("UPDATE club_match_sources SET match_status='ambiguous'")
    assert ids(con, M.crowd_min(0)) == set()


# ---------------------------------------------------------- registration

def test_builders_are_registered_for_the_manual_grid():
    for name in M.MATCH_BUILDERS:
        assert name in C.BUILDERS, name
    assert M.CROWD_BUILDER_NAMES <= set(M.MATCH_BUILDERS)


# --------------------------------------------------------------- parsing

def parse(text):
    result = P.parse(text)
    assert result[0] is not None, f"unparsed: {text} ({result[1]})"
    return result


def test_margin_wording():
    for text, expected in (
        ("100+ POINT WIN", "won by 100+ points"),
        ("WON BY 100+ POINTS", "won by 100+ points"),
        ("50+ POINT LOSS", "lost by 50+ points"),
        ("LOST BY 80 POINTS", "lost by 80+ points"),
    ):
        _built, label = parse(text)
        assert label == expected, (text, label)


def test_crowd_wording():
    for text, expected in (
        ("100,000+ CROWD", "crowd of 100,000+"),
        ("CROWD OF 90000+", "crowd of 90,000+"),
        ("50,000+ CROWD IN A FINAL", "crowd of 50,000+ at a final"),
    ):
        _built, label = parse(text)
        assert label == expected, (text, label)


def test_drawn_match_wording():
    _built, label = parse("PLAYED IN A DRAWN MATCH")
    assert label == "played in a drawn match"


def test_match_rules_do_not_swallow_stat_squares():
    """The new rules carry a number and a noun, like every stat square."""
    for text, expected in (
        ("40+ DISPOSALS IN A GAME", "40+ disposals in a game"),
        ("500+ DISPOSALS IN A SEASON", "500+ disposals in a season"),
        ("30+ FREES AGAINST - SEASON", "30+ frees_against in a season"),
        ("10+ GOALS IN A GAME", "10+ goals in a game"),
    ):
        _built, label = parse(text)
        assert label == expected, (text, label)


# ------------------------------------------------------- past-games search

def test_search_filters_compose():
    con = fixture()
    assert len(CH.search_matches(con, "alpha")) == 2
    assert len(CH.search_matches(con, "alpha", result="W")) == 1
    assert len(CH.search_matches(con, "alpha", scope="finals")) == 1
    assert len(CH.search_matches(con, "alpha", venue="Oval")) == 1
    assert len(CH.search_matches(con, "alpha", opponent_id="beta")) == 2
    assert len(CH.search_matches(con, "alpha", min_attendance=90000)) == 1


def test_search_is_from_the_searching_clubs_point_of_view():
    con = fixture()
    win, = CH.search_matches(con, "alpha", result="W")
    assert win.margin == 90 and win.opponent_id == "beta"
    loss, = CH.search_matches(con, "beta", result="L")
    assert loss.margin == -90 and loss.opponent_id == "alpha"


def test_search_without_a_club_returns_one_row_per_match():
    con = fixture()
    rows = CH.search_matches(con)
    assert len(rows) == 2
    assert len({r.source_game_key for r in rows}) == 2


def test_search_rejects_bad_arguments():
    con = fixture()
    with pytest.raises(ValueError):
        CH.search_matches(con, order="by_vibes")
    with pytest.raises(ValueError):
        CH.search_matches(con, result="X")


def test_club_id_mapping_covers_the_historical_clubs():
    """`clubs` has no Fitzroy row, but the source rows do."""
    con = fixture()
    con.execute("CREATE TABLE clubs (club_id TEXT, db_club_now TEXT)")
    con.execute("INSERT INTO clubs VALUES ('alpha','Alpha')")
    con.execute("INSERT INTO club_match_sources (source_club_id, match_status) "
                "VALUES ('fitzroy','unique')")
    mapping = CH.club_id_by_db_name(con)
    assert mapping["Alpha"] == "alpha"
    assert mapping["Fitzroy"] == "fitzroy"


# ============================================================= live data

def live():
    from pathlib import Path

    from data_paths import default_db
    db = default_db("afl")
    if not Path(db).exists():
        return None
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def test_live_every_club_now_value_maps_to_a_source_club():
    """Joining through `clubs` alone drops 44,002 Fitzroy/Bears/Uni rows."""
    con = live()
    if con is None:
        pytest.skip("no built database")
    mapping = CH.club_id_by_db_name(con)
    unmapped = [r[0] for r in con.execute(
        "SELECT DISTINCT club_now FROM games WHERE club_now IS NOT NULL")
        if r[0] not in mapping]
    assert not unmapped, unmapped


def test_live_match_constraints_return_plausible_counts():
    con = live()
    if con is None:
        pytest.skip("no built database")
    assert M.match_history_available(con)
    for built in (M.won_by_min(100), M.lost_by_min(100),
                  M.played_in_a_draw(), M.crowd_min(100000)):
        assert C.count(con, [built]) > 0


def test_live_crowd_constraint_is_a_subset_of_everyone():
    con = live()
    if con is None:
        pytest.skip("no built database")
    big = C.count(con, [M.crowd_min(100000)])
    bigger = C.count(con, [M.crowd_min(120000)])
    assert bigger < big


def run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:
                if exc.__class__.__name__ == "Skipped":
                    continue
                raise
    print("match constraint tests: passed")


if __name__ == "__main__":
    run()
