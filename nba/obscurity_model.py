"""
nba/obscurity_model.py -- The NBA obscurity model.

The machinery is shared and lives in obscurity.py; only the terms and
weights are the sport's. Deliberately not the AFL weights renamed.
"""

from obscurity import Model, Term, span

#: Career games and minutes are the broad career-visibility measures and
#: carry 45% between them. Points sits at only 0.07 because scoring is the
#: most role-dependent counting stat in the sport -- weighting it like the
#: AFL weights goals would rank every defensive specialist and role player
#: as obscure for playing their position.
#:
#: Minutes are the one genuinely optional term: they are not recorded before
#: 1951-52, so a career wholly inside the 1940s has no minutes at all and
#: must drop the term rather than be scored as having played none.
#:
#: `teams` is the least settled term and the first to revisit once there is
#: any crowd data to calibrate against. A ten-team NBA journeyman is
#: genuinely hard to place, which is not true of the AFL, but the direction
#: is arguable: more teams also means more squares qualified for.
#:
#: Version 1, and it must stay distinct from the AFL's 2. A shared version
#: number across two different formulas would claim the scores are
#: comparable, which they are not.
MODEL = Model(version=1, terms=(
    Term("games", 0.30, column="career_games", fillna=0.0),
    Term("span", 0.15, derive=span),
    Term("minutes", 0.15, column="career_minutes", optional=True),
    Term("era", 0.15, column="final_season"),
    Term("playoffs", 0.13, column="playoffs_played", fillna=0.0),
    Term("points", 0.07, column="career_points", fillna=0.0),
    Term("teams", 0.05, column="n_clubs"),
))
