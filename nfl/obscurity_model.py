"""
nfl/obscurity_model.py -- The NFL obscurity model.

The machinery is shared and lives in obscurity.py; only the terms and
weights are the sport's.
"""

from obscurity import Model, Term, span

#: Career games carries more weight here than anywhere else because it is
#: the only visibility measure that is fair across positions: nflverse's
#: weekly statistics give an offensive lineman almost nothing else, and any
#: model leaning on production would rank every lineman as anonymous for
#: playing his position. Touchdowns sit at 0.10 for the same reason points
#: sit low in the NBA model.
#:
#: `touchdowns` is total touchdowns responsible for, passing included --
#: that is how build_nfl_db computes career_touchdowns, and mixing a
#: quarterback's passing scores into the same term as a receiver's is a
#: known compromise of it.
#:
#: One caveat runs through every term: the weekly data begins in 1999, so a
#: career that started earlier is scored on the part of it the database can
#: see. `fillna=0.0` on games and touchdowns is still right -- a player in
#: the table played and scored what the table says -- but a 1994 debut is
#: measured short, and the `era` term then reads him as more recent than he
#: was. Fixing that needs pre-1999 appearances, not a different weight.
#:
#: Version 1. It shares that number with the NBA and MLB models and means
#: nothing across them: three different formulas, three incomparable scales.
MODEL = Model(version=1, terms=(
    Term("games", 0.35, column="career_games", fillna=0.0),
    Term("span", 0.17, derive=span),
    Term("era", 0.16, column="final_season"),
    Term("postseason", 0.14, column="career_postseason_games", fillna=0.0),
    Term("touchdowns", 0.10, column="career_touchdowns", fillna=0.0),
    Term("teams", 0.08, column="n_teams"),
))
