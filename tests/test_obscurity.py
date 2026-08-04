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

from afl.build_db import (OBSCURITY_MODEL_VERSION, OBSCURITY_WEIGHTS,
                      obscurity_components, obscurity_score)


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
    """Isolated against the stored component, not the blended score.

    Span cannot be varied on its own through the score: lengthening a career
    moves final_season, which the era term also reads. Model 1 hid that,
    because era clipped both of these careers to the same 100. Asserting the
    component directly says what this test means to say.
    """
    c = obscurity_components(frame([
        (17, 0, 0, 0, 1899, 1899),
        (17, 0, 0, 0, 1899, 1909),
    ]))
    assert (c.span_component.iloc[0] - c.span_component.iloc[1]
            == pytest.approx(50, abs=0.2))


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


# ------------------------------------------------- model 2: missing vs zero

def test_missing_brownlow_data_is_not_read_as_never_polled():
    """Brownlow voting starts in 1931; before it, there is nothing to poll.

    Two identical careers, one with a recorded zero and one with no data at
    all. Model 1 scored them the same, crediting the pre-1931 player for
    failing to poll in a count that did not exist.
    """
    # A population, because every term here is a percentile, and the two
    # subjects must sit mid-field on the other five: if those are already
    # maxed there is no room for the Brownlow term to show a difference.
    rows = [(100, 50, 0, 5, 1935, 1945),        # recorded: polled no votes
            (100, 50, None, 5, 1935, 1945)]     # no data for this player
    rows += [(20 + i * 10, 10 + i * 5, 1 + i, i, 1930, 1940 + i % 10)
             for i in range(40)]
    s = obscurity_score(frame(rows))
    assert s.iloc[1] < s.iloc[0], (s.iloc[0], s.iloc[1])


def test_a_dropped_term_renormalises_rather_than_shrinking_the_score():
    """The remaining five weights must still sum to the full scale.

    Otherwise every player with a data gap is quietly capped below 100 and
    the missing term reads as "not obscure" instead of "not known".
    """
    rows = [(1, 0, None, 0, 1900, 1900)]
    rows += [(300 + i, 400, 100, 30, 1990, 2010) for i in range(40)]
    c = obscurity_components(frame(rows))
    assert c.obscurity_confidence.iloc[0] == pytest.approx(
        1.0 - OBSCURITY_WEIGHTS["brownlow"])
    assert c.brownlow_component.iloc[0] != c.brownlow_component.iloc[0]  # NaN
    # Still able to reach the top of the scale on the terms it does have.
    assert c.obscurity.iloc[0] > 95


def test_confidence_is_one_when_every_term_is_available():
    c = obscurity_components(frame([(100, 50, 3, 5, 1990, 2000)]))
    assert c.obscurity_confidence.iloc[0] == pytest.approx(1.0)


# ------------------------------------------------------- model 2: era shape

def test_the_era_term_separates_the_last_twenty_five_years():
    """Model 1 clipped every final season from 2000 on to a flat zero.

    A career that ended in 2001 and one still running in 2026 were equally
    "contemporary", which threw away the recency signal across a quarter of
    the league's history.
    """
    c = obscurity_components(frame([
        (100, 50, 3, 5, 1995, 2001),
        (100, 50, 3, 5, 2010, 2015),
        (100, 50, 3, 5, 2020, 2026),
    ]))
    era = c.era_component.tolist()
    assert era[0] > era[1] > era[2], era


def test_era_still_ranks_older_careers_as_more_obscure():
    c = obscurity_components(frame([
        (50, 10, 0, 0, 1900, 1905),
        (50, 10, 0, 0, 1960, 1965),
        (50, 10, 0, 0, 2015, 2020),
    ]))
    era = c.era_component.tolist()
    assert era[0] > era[1] > era[2], era


# --------------------------------------------------------- model provenance

def test_components_are_stored_for_every_weight():
    """A score you cannot break down is a score you cannot audit or retune."""
    c = obscurity_components(frame([(10, 2, 1, 0, 1950, 1955)]))
    for name in OBSCURITY_WEIGHTS:
        assert f"{name}_component" in c.columns


def test_the_model_version_is_recorded():
    c = obscurity_components(frame([(10, 2, 1, 0, 1950, 1955)]))
    assert (c.obscurity_model == OBSCURITY_MODEL_VERSION).all()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
