"""
afl/obscurity_model.py -- The AFL obscurity model.

The machinery -- Term, Model, ranking, the missing-is-not-zero rule -- is
shared and lives in obscurity.py. Only the terms and their weights are the
sport's, and they are here so that retuning the AFL cannot silently move an
NBA score.
"""

from obscurity import Model, Term, span

#: Weights are the whole of the tuning surface, and they are judgement, not
#: a fit: the only ground truth available offline is a handful of rarity
#: percentages read off finished puzzles, which is far too few to fit six
#: coefficients against without inventing precision. Career games stays the
#: single strongest fame proxy; career span is the term that separates "a
#: whole career inside one season" from the same game count spread over a
#: decade.
#:
#: Version history, stored alongside every score so a database can say which
#: model produced it:
#:   1: original. Missing Brownlow data counted as zero votes; the era term
#:      was clipped flat at 0 for every final season from 2000 on.
#:   2: missing Brownlow data drops the term and renormalises instead of
#:      being read as "never polled"; era ranks final seasons rather than
#:      clipping, so the last 25 years separate again.
#:
#: The version number is the AFL's alone. Another sport reaching version 2
#: says nothing about this one -- the formulas are different and the scores
#: are not comparable.
#:
#: Declaration order is the component-column order in the built database.
MODEL = Model(version=2, terms=(
    Term("games", 0.30, column="career_games", fillna=0.0),
    Term("span", 0.18, derive=span),
    Term("goals", 0.14, column="career_goals", fillna=0.0),
    Term("finals", 0.13, column="finals_played", fillna=0.0),
    # Recency: modern players are far more familiar to today's solvers.
    Term("era", 0.15, column="final_season"),
    Term("brownlow", 0.10, column="career_brownlow", optional=True),
))
