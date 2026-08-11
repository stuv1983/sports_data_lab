#!/usr/bin/env python3
"""Regression tests for the club history layer (build plan step 4).

Two suites. The first runs against a small hand-built fixture whose right
answers are countable by eye, and covers the semantics that are easy to get
quietly wrong: finals as their own category, opponents by self-join rather
than by name, streaks crossing a season boundary, NULL attendance not being
a crowd of zero, and the trust boundary actually excluding something.

The second runs against the real database when it is present, and asserts a
handful of published historical facts plus the two source disagreements the
1924 finals round-robin causes. It skips rather than fails when the
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

from afl import club_history as H


COLUMNS = [
    "source_club_id", "season", "round", "is_final", "team_position",
    "opponent_raw", "points_for", "points_against", "result", "margin",
    "season_wins_after", "season_draws_after", "season_losses_after",
    "venue_raw", "attendance", "match_date", "source_game_key",
    "match_status",
]


def _match(key, season, rnd, date, home, away, hp, ap, venue, crowd,
           is_final=0, status="unique"):
    """Both sides of one match, as the source table stores them."""
    if is_final:
        pos_h = pos_a = "F"
    else:
        pos_h, pos_a = "H", "A"
    result_h = "W" if hp > ap else ("L" if hp < ap else "D")
    result_a = "W" if ap > hp else ("L" if ap < hp else "D")
    return [
        (home, season, rnd, is_final, pos_h, away, hp, ap, result_h, hp - ap,
         0, 0, 0, venue, crowd, date, key, status),
        (away, season, rnd, is_final, pos_a, home, ap, hp, result_a, ap - hp,
         0, 0, 0, venue, crowd, date, key, status),
    ]


def fixture():
    """Three clubs over two seasons.

    alpha: beats beta twice and gamma once in 2000, loses the final to beta,
           then opens 2001 with another win -- a four-match winning run that
           crosses the season boundary and is broken by the final only if
           finals are in scope.
    """
    con = sqlite3.connect(":memory:")
    con.execute(f"CREATE TABLE club_match_sources ({', '.join(COLUMNS)})")
    rows = []
    rows += _match("g1", 2000, "R1", "2000-04-01", "alpha", "beta",
                   100, 50, "Home Oval", 10000)
    rows += _match("g2", 2000, "R2", "2000-04-08", "beta", "alpha",
                   60, 90, "Away Oval", 20000)
    rows += _match("g3", 2000, "R3", "2000-04-15", "alpha", "gamma",
                   80, 79, "Home Oval", None)          # crowd not recorded
    rows += _match("g4", 2000, "R4", "2000-04-22", "gamma", "beta",
                   70, 70, "Neutral Oval", 5000)       # a draw
    rows += _match("g5", 2000, "GF", "2000-09-01", "alpha", "beta",
                   40, 95, "Neutral Oval", 50000, is_final=1)
    rows += _match("g6", 2001, "R1", "2001-04-01", "alpha", "gamma",
                   120, 30, "Home Oval", 15000)
    # An untrusted observation: excluded by default, visible on request.
    rows += _match("g7", 2001, "R2", "2001-04-08", "beta", "gamma",
                   200, 10, "Away Oval", 1000, status="ambiguous")
    con.executemany(
        f"INSERT INTO club_match_sources VALUES "
        f"({','.join('?' * len(COLUMNS))})", rows)
    return con


# ------------------------------------------------------ season records

def test_season_record_excludes_finals_by_default():
    con = fixture()
    rec, = H.season_records(con, "alpha", 2000)
    assert (rec.played, rec.wins, rec.draws, rec.losses) == (3, 3, 0, 0)
    assert rec.points_for == 270                     # 100 + 90 + 80
    assert rec.points_against == 189                 # 50 + 60 + 79
    assert rec.premiership_points == 12


def test_season_record_scope_all_includes_the_final():
    con = fixture()
    rec, = H.season_records(con, "alpha", 2000, scope="all")
    assert (rec.played, rec.wins, rec.losses) == (4, 3, 1)


def test_season_record_finals_only():
    con = fixture()
    rec, = H.season_records(con, "alpha", 2000, scope="finals")
    assert (rec.played, rec.wins, rec.losses) == (1, 0, 1)


def test_percentage_is_points_ratio_and_survives_zero_against():
    con = fixture()
    rec, = H.season_records(con, "alpha", 2000)
    assert round(rec.percentage, 2) == round(100 * 270 / 189, 2)
    assert H.SeasonRecord("x", 1, 1, 1, 0, 0, 50, 0).percentage is None


def test_a_draw_is_counted_as_a_draw_not_a_loss():
    con = fixture()
    rec, = H.season_records(con, "gamma", 2000)
    assert (rec.wins, rec.draws, rec.losses) == (0, 1, 1)


def test_unknown_scope_is_rejected():
    con = fixture()
    with pytest.raises(ValueError):
        H.season_records(con, "alpha", scope="regular_season")


# -------------------------------------------------------------- streaks

def test_winning_streak_crosses_a_season_boundary():
    """R1-R3 of 2000 plus R1 of 2001, with the lost final excluded."""
    con = fixture()
    best = H.streaks(con, "alpha", kind="winning",
                     scope="home_and_away", limit=1)[0]
    assert best.length == 4
    assert best.spans_seasons is True
    assert (best.from_season, best.to_season) == (2000, 2001)


def test_within_season_breaks_the_run_at_the_boundary():
    con = fixture()
    best = H.streaks(con, "alpha", kind="winning", scope="home_and_away",
                     within_season=True, limit=1)[0]
    assert best.length == 3
    assert best.spans_seasons is False


def test_a_lost_final_breaks_the_streak_when_finals_are_in_scope():
    con = fixture()
    best = H.streaks(con, "alpha", kind="winning", scope="all", limit=1)[0]
    assert best.length == 3


def test_unbeaten_counts_draws_but_winning_does_not():
    """gamma never wins, so the drawn match is a streak of one or of none."""
    con = fixture()
    unbeaten = H.streaks(con, "gamma", kind="unbeaten", limit=1)
    assert unbeaten[0].length == 1          # the drawn match
    assert H.streaks(con, "gamma", kind="winning") == []


def test_unknown_streak_kind_is_rejected():
    con = fixture()
    with pytest.raises(ValueError):
        H.streaks(con, "alpha", kind="glorious")


# ---------------------------------------------------------- head to head

def test_head_to_head_resolves_the_opponent_by_self_join():
    con = fixture()
    h, = H.head_to_head(con, "alpha", "beta", scope="home_and_away")
    assert (h.played, h.wins, h.losses) == (2, 2, 0)
    assert h.opponent_id == "beta"


def test_head_to_head_mirrors_exactly():
    con = fixture()
    a, = H.head_to_head(con, "alpha", "beta")
    b, = H.head_to_head(con, "beta", "alpha")
    assert (a.played, a.wins, a.draws, a.losses) == \
           (b.played, b.losses, b.draws, b.wins)
    assert a.points_for == b.points_against


def test_head_to_head_can_be_restricted_to_finals_and_to_an_era():
    con = fixture()
    f, = H.head_to_head(con, "alpha", "beta", scope="finals")
    assert (f.played, f.wins, f.losses) == (1, 0, 1)
    assert H.head_to_head(con, "alpha", "beta", season_from=2001) == []


def test_head_to_head_without_an_opponent_lists_every_opponent():
    con = fixture()
    rows = H.head_to_head(con, "alpha")
    assert {r.opponent_id for r in rows} == {"beta", "gamma"}


# ------------------------------------------------------- home/away/finals

def test_finals_are_their_own_split_not_folded_into_home_or_away():
    con = fixture()
    splits = {r.split: r for r in H.home_away_splits(con, "alpha")}
    # All-time, not per season: alpha is home in g1 and g3 (2000) and g6 (2001).
    assert set(splits) == {"home", "away", "finals"}
    assert splits["home"].played == 3       # g1, g3, g6
    assert splits["away"].played == 1       # g2
    assert splits["finals"].played == 1     # g5
    # The final is in none of the two positional splits, which is the point.
    assert splits["home"].played + splits["away"].played == 4


# --------------------------------------------------------------- venues

def test_venue_records_group_by_venue():
    con = fixture()
    rows = {r.venue: r for r in H.venue_records(con, "alpha")}
    assert rows["Home Oval"].played == 3    # g1, g3, g6
    assert rows["Home Oval"].wins == 3
    assert rows["Neutral Oval"].played == 1  # the final


def test_venue_alias_map_merges_two_names_into_one_record():
    """Two source spellings of one ground must aggregate, not split."""
    con = fixture()
    con.executemany(
        f"INSERT INTO club_match_sources VALUES "
        f"({','.join('?' * len(COLUMNS))})",
        _match("g8", 2001, "R3", "2001-04-15", "alpha", "beta",
               10, 5, "Home Park", 100))

    class _Schema:
        venue_aliases = {"home oval": "The Ground", "home park": "The Ground"}

        def canonical_venue(self, name):
            return self.venue_aliases.get(str(name).strip().lower(), name)

    class _Sport:
        schema = _Schema()

    rows = {r.venue: r for r in H.venue_records(con, "alpha", sport=_Sport())}
    assert rows["The Ground"].played == 4   # 3 at Home Oval + 1 at Home Park
    assert "Home Oval" in rows["The Ground"].venue_raw
    assert "Home Park" in rows["The Ground"].venue_raw


# --------------------------------------------------------------- margins

def test_biggest_win_and_biggest_loss_are_not_the_same_ordering():
    con = fixture()
    win = H.margins(con, "alpha", kind="win", limit=1)[0]
    assert win.margin == 90 and win.opponent_id == "gamma"
    loss = H.margins(con, "alpha", kind="loss", limit=1)[0]
    assert loss.margin == -55 and loss.is_final is True


def test_margin_kind_is_validated():
    con = fixture()
    with pytest.raises(ValueError):
        H.margins(con, kind="draw")


# ---------------------------------------------------------------- crowds

def test_unrecorded_attendance_is_not_a_crowd_of_zero():
    """g3 has no attendance and must not take the smallest-crowd place."""
    con = fixture()
    smallest = H.crowds(con, kind="smallest", limit=1)[0]
    assert smallest.attendance == 5000
    assert all(c.attendance is not None for c in H.crowds(con, limit=99))


def test_match_level_crowd_list_reports_each_match_once():
    con = fixture()
    rows = H.crowds(con, kind="largest", limit=99)
    dates = [r.match_date for r in rows]
    assert len(dates) == len(set(dates)), rows
    assert rows[0].attendance == 50000      # the final


def test_crowd_average_reports_how_much_it_is_based_on():
    con = fixture()
    by_club = {a.key: a for a in H.crowd_averages(con, by="club")}
    alpha = by_club["alpha"]
    assert alpha.matches == 5               # g1, g2, g3, g5, g6
    assert alpha.with_attendance == 4       # g3 has none
    assert alpha.average == (10000 + 20000 + 50000 + 15000) / 4


def test_crowd_grouping_is_validated():
    con = fixture()
    with pytest.raises(ValueError):
        H.crowd_averages(con, by="umpire")


# -------------------------------------------------------- trust boundary

def test_untrusted_rows_are_excluded_by_default_and_reported():
    con = fixture()
    assert H.excluded(con) == {"ambiguous": 2}
    assert H.season_records(con, "beta", 2001) == []


def test_include_unlinked_shows_them():
    con = fixture()
    rec, = H.season_records(con, "beta", 2001, include_unlinked=True)
    assert rec.wins == 1


def test_head_to_head_also_honours_the_trust_boundary():
    con = fixture()
    assert H.head_to_head(con, "beta", "gamma", season_from=2001) == []
    rows = H.head_to_head(con, "beta", "gamma", season_from=2001,
                          include_unlinked=True)
    assert rows and rows[0].played == 1


def test_coverage_reports_the_excluded_rows():
    con = fixture()
    cov = H.coverage(con)
    assert cov["observations"] == 14 and cov["matches"] == 7
    assert cov["with_attendance"] == 12     # g3's two rows have none
    assert cov["excluded"] == {"ambiguous": 2}


def test_availability_probe_is_false_without_the_table():
    con = sqlite3.connect(":memory:")
    assert H.club_history_available(con) is False


# ----------------------------------------------------- rounds available

def test_rounds_are_listed_in_playing_order_not_alphabetical_order():
    """R10 after R2, and the final last.

    Sorted as text this reads R1, R10, R2, R3, R4, GF -- which is the bug
    a round picker exists to avoid.
    """
    con = fixture()
    con.executemany(
        f"INSERT INTO club_match_sources VALUES "
        f"({','.join('?' * len(COLUMNS))})",
        _match("g8", 2000, "R10", "2000-06-10", "alpha", "gamma",
               60, 55, "Home Oval", 8000))
    assert H.rounds_available(con) == ["R1", "R2", "R3", "R4", "R10", "GF"]


def test_a_finals_code_is_placed_by_when_it_was_played():
    """Nothing in the label says a grand final comes after a semi final.

    Alphabetically 'GF' sorts before 'SF'; only the position each held in
    its own season says otherwise.
    """
    con = fixture()
    con.executemany(
        f"INSERT INTO club_match_sources VALUES "
        f"({','.join('?' * len(COLUMNS))})",
        _match("g8", 2000, "SF", "2000-08-25", "alpha", "gamma",
               60, 55, "Neutral Oval", 30000, is_final=1))
    assert H.rounds_available(con)[-2:] == ["SF", "GF"]


def test_rounds_can_be_narrowed_to_one_season():
    con = fixture()
    assert H.rounds_available(con, season=2000) == [
        "R1", "R2", "R3", "R4", "GF"]
    # 2001's other match is the ambiguous one, which the trust boundary
    # drops here exactly as it drops it everywhere else.
    assert H.rounds_available(con, season=2001) == ["R1"]


def test_rounds_can_be_narrowed_to_a_span_of_seasons():
    con = fixture()
    assert H.rounds_available(con, season_from=2001) == ["R1"]
    assert H.rounds_available(con, season_to=1999) == []


def test_a_season_that_records_no_round_offers_nothing_to_pick():
    """The MLB and NBA shape: a round for the postseason and nothing else."""
    con = fixture()
    con.executemany(
        f"INSERT INTO club_match_sources VALUES "
        f"({','.join('?' * len(COLUMNS))})",
        _match("g8", 2002, None, "2002-04-01", "alpha", "gamma",
               60, 55, "Home Oval", 8000))
    assert H.rounds_available(con, season=2002) == []
    assert H.matches_without_a_round(con, season=2002) == 1
    assert H.matches_without_a_round(con, season=2000) == 0


def test_matches_without_a_round_counts_matches_not_rows():
    con = fixture()
    con.executemany(
        f"INSERT INTO club_match_sources VALUES "
        f"({','.join('?' * len(COLUMNS))})",
        _match("g8", 2002, None, "2002-04-01", "alpha", "gamma",
               60, 55, "Home Oval", 8000)
        + _match("g9", 2002, None, "2002-04-08", "beta", "gamma",
                 60, 55, "Away Oval", 8000))
    # Four source rows, two matches.
    assert H.matches_without_a_round(con) == 2


# ============================================================ live data

def live():
    """The real database, or None when it has not been built."""
    from pathlib import Path

    from data_paths import default_db
    db = default_db("afl")
    if not Path(db).exists():
        return None
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def test_live_reconciles_against_the_independent_ladder_source():
    """team_seasons comes from the player-stats source, not this one.

    Only the 1924 running-total disagreements are expected. Anything else
    means the two sources have diverged on a club's season and the derived
    records cannot be trusted until it is explained.
    """
    con = live()
    if con is None:
        pytest.skip("no built database")
    problems = H.reconcile(con)
    unexpected = [p for p in problems if "1924" not in p]
    assert not unexpected, unexpected
    assert len(problems) == 2, problems
    assert all("running total" in p for p in problems)


def test_live_known_historical_records():
    con = live()
    if con is None:
        pytest.skip("no built database")

    # Geelong's 23-match run across 1952-53, the longest in the competition.
    best = H.streaks(con, kind="winning", limit=1)[0]
    assert best.club_id == "geelong" and best.length == 23
    assert (best.from_season, best.to_season) == (1952, 1953)

    # University's 51-match losing run to the end of their 1914 last season.
    worst = H.streaks(con, kind="losing", limit=1)[0]
    assert worst.club_id == "university" and worst.length == 51

    # The 1970 Grand Final, the largest crowd recorded.
    biggest = H.crowds(con, kind="largest", limit=1)[0]
    assert biggest.attendance == 121696
    assert biggest.season == 1970 and biggest.is_final is True

    # Geelong 2022: 18-4 home and away, then three finals for the flag.
    rec, = H.season_records(con, "geelong", 2022)
    assert (rec.played, rec.wins, rec.losses) == (22, 18, 4)
    finals, = H.season_records(con, "geelong", 2022, scope="finals")
    assert (finals.played, finals.wins) == (3, 3)


def test_live_head_to_head_mirrors_across_a_century():
    con = live()
    if con is None:
        pytest.skip("no built database")
    a, = H.head_to_head(con, "carlton", "collingwood")
    b, = H.head_to_head(con, "collingwood", "carlton")
    assert a.played == b.played
    assert (a.wins, a.draws, a.losses) == (b.losses, b.draws, b.wins)
    assert a.points_for == b.points_against


def test_live_every_match_has_exactly_two_sides():
    """The self-join opponent resolution depends on this holding."""
    con = live()
    if con is None:
        pytest.skip("no built database")
    odd = con.execute(
        "SELECT COUNT(*) FROM (SELECT source_game_key, COUNT(*) n "
        "FROM club_match_sources GROUP BY 1 HAVING n <> 2)").fetchone()[0]
    assert odd == 0


def test_live_finals_never_carry_a_home_side():
    con = live()
    if con is None:
        pytest.skip("no built database")
    bad = con.execute(
        "SELECT COUNT(*) FROM club_match_sources "
        "WHERE is_final = 1 AND team_position <> 'F'").fetchone()[0]
    assert bad == 0


def run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:                    # pytest.skip included
                if exc.__class__.__name__ == "Skipped":
                    continue
                raise
    print("club history tests: passed")


if __name__ == "__main__":
    run()
