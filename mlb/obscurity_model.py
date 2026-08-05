"""
mlb/obscurity_model.py -- The MLB obscurity model.

The machinery is shared and lives in obscurity.py; only the terms and
weights are the sport's, and they are not the AFL's or the NBA's renamed.
"""

from obscurity import Model, Term, span

#: Career games carries the most weight because Lahman's Appearances table
#: counts every player the same way whatever position they played, which is
#: the one visibility measure that is fair to a relief pitcher and a
#: shortstop at once.
#:
#: Hits outweigh home runs (0.11 to 0.07) for the reason points sit low in
#: the NBA model: the home-run leaderboard is the most role-dependent
#: counting stat baseball has, and weighting it heavily would score every
#: contact hitter and every pitcher as obscure for the position they played.
#: Both terms use `fillna=0.0` -- batting lines exist for every player-season
#: in the file, so a zero is a real zero.
#:
#: The postseason term is `optional`. There was no postseason at all in
#: several early seasons and no Division Series before 1969, so a career
#: wholly inside a year with no October cannot be read as having failed to
#: reach one. The build leaves it NULL for those players and the term drops.
#:
#: Version 1, distinct from the AFL's 2 and from the NBA's 1-for-a-different-
#: formula: no score here is comparable with a score there.
MODEL = Model(version=1, terms=(
    Term("games", 0.32, column="career_games", fillna=0.0),
    Term("span", 0.16, derive=span),
    Term("era", 0.16, column="final_season"),
    Term("postseason", 0.13, column="postseason_played", optional=True),
    Term("hits", 0.11, column="career_hits", fillna=0.0),
    Term("home_runs", 0.07, column="career_home_runs", fillna=0.0),
    Term("franchises", 0.05, column="n_clubs"),
))
