#!/usr/bin/env python3
"""Obscurity must use its whole scale and must count career shape.

Two defects, both of which made the ranking blunter than it looked:

1. Ties took the midpoint rank. 82% of players never polled a Brownlow
   vote, 65% never played a final, 26% never kicked a goal. Under pandas'
   default `method="average"` every one of them scored the *middle* of
   their tie -- 58.8/100 for "no Brownlow votes" -- so the most anonymous
   career possible reached only 84.9 and the top sixth of the scale was
   unreachable by anybody.

2. Career span was not an input at all. 17 games inside a single season
   and 17 games strung across a decade scored identically, though the
   first is a far more obscure career.

These tests pin both, plus the invariant that matters most on a board: a
strictly smaller career footprint must never rank as less obscure.
"""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---


import pandas as pd
import pytest

from build_db import OBSCURITY_WEIGHTS, obscurity_score


def frame(rows):
    """rows: (games, goals, brownlow, finals, debut, final)."""
    return pd.DataFrame(rows, columns=[
        "career_games", "career_goals", "career_brownlow",
        "finals_played", "debut_season", "final_season"])


def test_weights_sum_to_one():
    assert round(sum(OBSCURITY_WEIGHTS.values()), 6) == 1.0


def test_the_most_anonymous_career_reaches_the_top_of_the_scale():
    """A one-game 1900 nobody among stars must score ~100, not ~85."""
    rows = [(1, 0, 0, 0, 1900, 1900)]
    rows += [(300 + i, 400, 100, 30, 1990, 2010) for i in range(40)]
    s = obscurity_score(frame(rows))
    assert s.iloc[0] > 95, s.iloc[0]


def test_a_tied_group_takes_its_best_rank_not_the_midpoint():
    """Never polling a Brownlow vote is maximal anonymity on that term.

    Thirty players share zero votes and one has many. The thirty must all
    score at the top of the Brownlow term, not at the middle of their tie.
    """
    rows = [(20, 5, 0, 0, 1950, 1952) for _ in range(30)]
    rows += [(20, 5, 200, 0, 1950, 1952)]
    s = obscurity_score(frame(rows))
    tied, famous = s.iloc[:30], s.iloc[30]
    assert tied.nunique() == 1
    assert tied.iloc[0] > famous
    # The tied group is identical on every other term, so the whole gap is
    # the Brownlow weight -- proof the term is not being averaged away.
    # A percentile rank floors at 1/N rather than 0, so the best achievable
    # score on a term is (1 - 1/N) * 100, not a flat 100.
    best = (1 - 1 / len(s)) * 100
    assert tied.iloc[0] - famous == pytest.approx(
        OBSCURITY_WEIGHTS["brownlow"] * best, abs=0.2)


def test_career_span_separates_otherwise_identical_careers():
    """Same games, goals and finals; one season versus a decade."""
    s = obscurity_score(frame([
        (17, 0, 0, 0, 1899, 1899),      # all inside one season
        (17, 0, 0, 0, 1899, 1909),      # the same 17 games over 11 years
    ]))
    assert s.iloc[0] > s.iloc[1], (s.iloc[0], s.iloc[1])


def test_span_has_the_weight_it_claims():
    s = obscurity_score(frame([
        (17, 0, 0, 0, 1899, 1899),
        (17, 0, 0, 0, 1899, 1909),
    ]))
    assert s.iloc[0] - s.iloc[1] == pytest.approx(
        OBSCURITY_WEIGHTS["span"] * 50, abs=0.2)


def test_a_strictly_smaller_footprint_is_never_less_obscure():
    """The invariant a solver relies on when reading a ranked square."""
    small = (5, 1, 0, 0, 1975, 1976)
    big = (250, 300, 90, 25, 1975, 1990)
    s = obscurity_score(frame([small, big]))
    assert s.iloc[0] > s.iloc[1]


def test_scores_stay_inside_zero_and_one_hundred():
    rows = [(1, 0, 0, 0, 1897, 1897), (439, 1360, 262, 40, 1990, 2026),
            (20, 4, 0, 0, 1950, 1955), (100, 50, 10, 5, 2000, 2010)]
    s = obscurity_score(frame(rows))
    assert s.min() >= 0 and s.max() <= 100


def test_era_term_survives_a_missing_final_season():
    """final_season NULL must not poison the whole column with NaN."""
    s = obscurity_score(frame([
        (10, 0, 0, 0, 1950, 1951),
        (10, 0, 0, 0, 1950, None),
    ]))
    assert s.iloc[0] == s.iloc[0]          # not NaN


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
