"""
nba/constraints_nba.py -- NBA square descriptions, compiled to SQL.

Every constraint compiles to a fragment selecting DISTINCT player_id.
Intersecting two of them solves one square of the grid. Same contract as
afl/constraints.py; the machinery is shared and lives in core.py.

Most of this file is re-exports. That is the point of core.Generic: a
constraint that differs between the AFL and the NBA only by column name and
wording is written once, and this module names it in NBA terms. The
genuinely NBA-specific builders start at "NBA-specific" below, and there
are only five of them.

BUILDERS reuses the AFL display keys wherever the semantics match. That is
not laziness: app.axis_widget renders those keys through the sport's Vocab,
so "X+ career goals" appears as "X+ career points" on an NBA board without
a line of UI code caring which sport it is. A new key would have needed a
new label entry and a new scope-label entry to say the same thing.

TEAMMATES ARE DELIBERATELY ABSENT
---------------------------------
core.Generic.teammate_of_id matches players who share a (club, season), and
in the NBA that is wrong often enough to matter: a player traded in
February and a player traded away in December share a team and a season and
were never on the floor together. The AFL's trade window makes this a rare
edge case; the NBA's does not. Answering it properly needs shared *matches*,
which is a later milestone, so "Teammate of…" is missing from BUILDERS
rather than present and wrong.
"""

import core
import sports

# Declared in sports.py because the sport picker needs them before this
# module is imported. CLUBS and VENUE_ALIASES come from the reference file
# the build writes, so they are measured rather than hand-maintained.
SCHEMA = sports.NBA_SCHEMA
STATS = sports.NBA_STATS
CLUBS = SCHEMA.clubs
VENUE_ALIASES = SCHEMA.venue_aliases

_G = core.Generic(SCHEMA)


# ------------------------------------------------- generic, bound to NBA

played_for = _G.played_for
debut_club = _G.debut_club
one_club_player = _G.one_club_player
played_for_n_clubs = _G.played_for_n_clubs
multi_club_player = _G.multi_club_player

career_games_min = _G.career_games_min
career_games_max = _G.career_games_max
career_points_min = _G.career_score_min
career_points_max = _G.career_score_max
career_points_between = _G.career_score_between

stat_in_a_game = _G.stat_in_a_game
two_stats_same_game = _G.two_stats_same_game
season_stat_average_min = _G.season_stat_average_min
season_stat_total_min = _G.season_stat_total_min
career_stat_total_min = _G.career_stat_total_min
career_stat_average_min = _G.career_stat_average_min
games_with_stat_min = _G.games_with_stat_min
CAREER_AVG_MIN_GAMES = _G.CAREER_AVG_MIN_GAMES
SEASON_AVG_MIN_GAMES = _G.SEASON_AVG_MIN_GAMES

points_at_multiple_teams = _G.score_at_multiple_clubs
games_at_multiple_teams = _G.games_at_multiple_clubs

played_in_season_range = _G.played_in_season_range
debuted_between = _G.debuted_between

played_at_venue = _G.played_at_venue

# The playoffs are the NBA's post-season. The semantics are the generic
# ones; only the wording differs.
stat_in_a_playoff_game = _G.stat_in_a_postseason_game
playoff_stat_average_min = _G.postseason_stat_average_min
playoff_games_min = _G.postseason_games_min
played_in_the_playoffs = _G.played_postseason
never_played_playoffs = _G.never_played_postseason
no_playoff_wins = _G.no_postseason_wins
never_won_a_playoff_game = _G.never_won_postseason
won_a_playoff_game = _G.won_postseason
won_a_playoff_game_at = _G.won_postseason_at
points_average_in_playoffs = _G.score_average_in_postseason


def _venue(name):
    """Resolve an arena alias to the name the database stores."""
    return SCHEMA.canonical_venue(name)


# --------------------------------------------------------- NBA-specific
#
# The Finals are one nominated round, not simply the last playoff series,
# so these read `round` rather than the generic is_playoff flag -- exactly
# as constraints.premiership_player() reads 'GF' for the AFL.
#
# `champion` and `made_playoffs_season` read team_seasons rather than
# scanning box scores, which is what that table is for.

FINALS_ROUND = "F"


def played_in_the_finals():
    """Appeared in an NBA Finals game."""
    return ("SELECT DISTINCT player_id FROM games "
            "WHERE UPPER(TRIM(round)) = ?", [FINALS_ROUND])


def won_the_finals():
    """Won an NBA Finals game. Not the same as winning the title."""
    return ("SELECT DISTINCT player_id FROM games "
            "WHERE UPPER(TRIM(round)) = ? AND result = 'W'", [FINALS_ROUND])


def never_made_the_finals():
    """Played, but never in a Finals game."""
    return ("SELECT DISTINCT player_id FROM games WHERE player_id NOT IN "
            "(SELECT player_id FROM games WHERE UPPER(TRIM(round)) = ?)",
            [FINALS_ROUND])


def champion():
    """
    Played for a team in a season it won the championship.

    The eligibility rule is "appeared in a game for the team that season",
    which is the one the data can actually support. It is deliberately not
    "was on the roster when the title was won": a player traded away in
    December is in neither the playoff roster nor, under most people's
    reading, the answer -- but he did appear for the champions, and the
    database cannot see roster status on a given date. Stated here so the
    square means something specific rather than something plausible.
    """
    return ("""SELECT DISTINCT g.player_id FROM games g
               JOIN team_seasons t
                 ON t.season = g.season AND t.club_now = g.club_now
                AND t.phase = 'regular'
               WHERE t.champion = 1""", [])


def made_playoffs_season():
    """Played for a team in a season it reached the playoffs."""
    return ("""SELECT DISTINCT g.player_id FROM games g
               JOIN team_seasons t
                 ON t.season = g.season AND t.club_now = g.club_now
                AND t.phase = 'regular'
               WHERE t.made_playoffs = 1""", [])


def missed_playoffs_season():
    """Played for a team in a season it missed the playoffs."""
    return ("""SELECT DISTINCT g.player_id FROM games g
               JOIN team_seasons t
                 ON t.season = g.season AND t.club_now = g.club_now
                AND t.phase = 'regular'
               WHERE t.made_playoffs = 0""", [])


# ---------------------------------------------------------------- registry

BUILDERS = {
    "Played for club":            (played_for, ["club"]),
    "150+ / X+ career games":     (career_games_min, ["games"]),
    "Fewer than X career games":  (career_games_max, ["games"]),
    "X+ career goals":            (career_points_min, ["goals"]),
    "X or fewer career goals":    (career_points_max, ["goals"]),
    "X+ of a stat in one game":   (stat_in_a_game, ["stat", "x"]),
    "X+ of a stat in one season": (season_stat_total_min, ["stat", "x"]),
    "X+ of a stat in a career":   (career_stat_total_min, ["stat", "x"]),
    "Season average of a stat":   (season_stat_average_min, ["stat", "avg"]),
    "Career average of a stat":   (career_stat_average_min,
                                   ["stat", "avg", "min_games"]),
    "X+ games with Y+ of a stat": (games_with_stat_min,
                                   ["stat", "y", "times"]),
    "X+ of a stat in a final":    (stat_in_a_playoff_game, ["stat", "x"]),
    "Finals average of a stat":   (playoff_stat_average_min, ["stat", "avg"]),
    "Two stats in the same game": (two_stats_same_game,
                                   ["stat_a", "x_a", "stat_b", "x_b"]),
    "X+ goals at 2+ clubs":       (points_at_multiple_teams,
                                   ["goals", "clubs"]),
    "X+ games at 2+ clubs":       (games_at_multiple_teams,
                                   ["games", "clubs"]),
    "No finals wins (played finals)": (no_playoff_wins, []),
    "Never won a final":          (never_won_a_playoff_game, []),
    "Never played finals":        (never_played_playoffs, []),
    "Played between seasons":     (played_in_season_range, ["from", "to"]),
    "Debuted between seasons":    (debuted_between, ["from", "to"]),
    "One-club player":            (one_club_player, []),
    "Played for X+ clubs":        (played_for_n_clubs, ["clubs"]),
    "Multi-club player":          (multi_club_player, []),
    "First career game for club": (debut_club, ["club"]),
    "Played at venue":            (played_at_venue, ["venue"]),
    "Won a final at venue":       (won_a_playoff_game_at, ["venue"]),
    "X+ finals games":            (playoff_games_min, ["x"]),
    "Played in a final":          (played_in_the_playoffs, []),
    "Won a final":                (won_a_playoff_game, []),
    "Goal average in finals":     (points_average_in_playoffs, ["avg"]),
    # NBA-only. These need their own keys because the AFL has no equivalent
    # question -- a premiership is decided by one game, a championship by a
    # series, and "made the playoffs" is not a meaningful AFL square.
    # Named for what the games can prove, not for what a grid square would
    # like to say. See champion() -- "NBA champion" will be a different
    # criterion once roster stints land, and reusing the label now would
    # make the two indistinguishable in a saved board.
    "Appeared for championship team": (champion, []),
    "Made the playoffs":          (made_playoffs_season, []),
    "Missed the playoffs":        (missed_playoffs_season, []),
    "Played in the Finals":       (played_in_the_finals, []),
    "Won a Finals game":          (won_the_finals, []),
    "Never played in the Finals": (never_made_the_finals, []),
}

#: Builders needing an optional layer. Both empty for these milestones --
#: app.py filters BUILDERS by these sets, and an empty set means nothing is
#: hidden rather than everything.
DRAFT_BUILDERS = set()
AWARD_BUILDER_NAMES = set()
AWARD_SLUGS = {}

#: Draft categories offered in the UI. Empty because no NBA draft layer is
#: loaded; app.py reads this instead of hardcoding the AFL's eight.
DRAFT_TYPES = ()

#: Team-season criteria depend on team_seasons, which the core build writes.
TEAM_SEASON_BUILDERS = {"Appeared for championship team", "Made the playoffs",
                        "Missed the playoffs"}


def draft_available(con):
    """True when an NBA draft layer has been imported. Not in this build."""
    return core.have_tables(con, "nba_draft", "nba_draft_links")


def awards_available(con):
    """True when an NBA award layer has been imported. Not in this build."""
    return core.have_tables(con, "nba_awards")


def team_seasons_available(con):
    """True when team_seasons carries a decided championship."""
    if not core.have_tables(con, "team_seasons"):
        return False
    try:
        return bool(con.execute(
            "SELECT 1 FROM team_seasons WHERE champion = 1 LIMIT 1").fetchone())
    except Exception:
        return False


# ------------------------------------------------- engine, bound to NBA
# Thin wrappers so the UI calls the same names for either sport. No
# ensure_*_table calls: the NBA has no optional connection-local tables to
# stand in for, and inventing empty ones would only hide a missing layer.

def require_schema(con):
    """Fail loudly at startup if the database predates a migration."""
    core.require_schema(con, SCHEMA)


def solve(con, constraints, limit=25, order="obscurity"):
    """Intersect constraints and return ranked players."""
    return core.solve(con, constraints, SCHEMA, limit=limit, order=order)


def count(con, constraints):
    """How many players satisfy every constraint."""
    return core.count(con, constraints, SCHEMA)


def square(con, constraints, order="obscurity"):
    """Eligible count plus the single best answer, for a prefilled board."""
    return core.square(con, constraints, SCHEMA, order=order)


def to_standalone_sql(constraints, limit=25):
    """Render an intersection as a single pasteable SQL statement."""
    return core.to_standalone_sql(constraints, SCHEMA, limit=limit)


# Star-rating helpers, re-exported so the UI never imports core directly.
# STAR_DISCLAIMER is generated from the NBA obscurity model rather than
# taken from core: core's names goals, finals and Brownlow votes, none of
# which the NBA model uses, and it denies including club spread, which the
# NBA model does include.
star_value = core.star_value
stars_text = core.stars_text
stars_html = core.stars_html
STAR_DISCLAIMER = sports.NBA.star_disclaimer
