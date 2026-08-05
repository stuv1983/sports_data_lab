"""
constraints_mlb.py -- MLB square descriptions, compiled to SQL.

Every constraint compiles to a fragment selecting DISTINCT player_id.
Intersecting two of them solves one square of the grid. Same contract as
afl/constraints.py and nba/constraints_nba.py; the machinery is shared and
lives in core.py.

Like the NBA module, most of this file is re-exports of core.Generic under
MLB names, and it reuses the AFL display keys wherever the semantics match
so app.axis_widget renders them through the sport's Vocab -- "X+ career
goals" appears as "X+ career home runs" on an MLB board without a line of
UI code caring which sport it is.

WHAT IS DELIBERATELY MISSING, AND WHY
-------------------------------------
Lahman's finest grain is a player's season with one team, so a row of
`games` is a season, not a game (build_mlb_db.py's docstring has the
detail). Three families of square are therefore absent rather than present
and wrong:

  * per-game squares -- "X+ of a stat in one game", "Two stats in the same
    game", "X+ games with Y+ of a stat". A per-season total answered under
    a per-game label would silently turn "40 home runs in a game" into a
    routine season.
  * per-game averages -- core.Generic's average builders divide a total by
    a row count, and a row here is a season.
  * teammates -- core.Generic.teammate_of_id matches players sharing a
    (club, season), which in baseball's trade market means two players who
    were never in the same clubhouse. The NBA module declines this for the
    same reason.

The postseason squares are real: BattingPost, PitchingPost and SeriesPost
give the build a per-round `result`, so "won a final" means won a
postseason series, not merely appeared in October.
"""

import core
import sports

from . import mlb_reference

# Declared in sports.py because the sport picker needs them before this
# module is imported. CLUBS comes from the reference file the build writes,
# so it is measured rather than hand-maintained.
SCHEMA = sports.MLB_SCHEMA
STATS = sports.MLB_STATS
CLUBS = SCHEMA.clubs
VENUE_ALIASES = SCHEMA.venue_aliases

_G = core.Generic(SCHEMA)


# ------------------------------------------------- generic, bound to MLB

played_for = _G.played_for
debut_club = _G.debut_club
one_club_player = _G.one_club_player
played_for_n_clubs = _G.played_for_n_clubs
multi_club_player = _G.multi_club_player

career_games_min = _G.career_games_min
career_games_max = _G.career_games_max
career_home_runs_min = _G.career_score_min
career_home_runs_max = _G.career_score_max
career_home_runs_between = _G.career_score_between

season_stat_total_min = _G.season_stat_total_min
career_stat_total_min = _G.career_stat_total_min
CAREER_AVG_MIN_GAMES = _G.CAREER_AVG_MIN_GAMES
SEASON_AVG_MIN_GAMES = _G.SEASON_AVG_MIN_GAMES

home_runs_at_multiple_clubs = _G.score_at_multiple_clubs
games_at_multiple_clubs = _G.games_at_multiple_clubs

played_in_season_range = _G.played_in_season_range
debuted_between = _G.debuted_between

played_at_venue = _G.played_at_venue

# The postseason is baseball's finals. The semantics are the generic ones;
# only the wording differs.
postseason_games_min = _G.postseason_games_min
played_in_the_postseason = _G.played_postseason
never_played_postseason = _G.never_played_postseason
no_postseason_wins = _G.no_postseason_wins
never_won_postseason = _G.never_won_postseason
won_postseason = _G.won_postseason
won_postseason_at = _G.won_postseason_at


# --------------------------------------------------------- MLB-specific
#
# The World Series is one nominated round, not simply the last series
# played, so these read `round` rather than the generic is_postseason flag
# -- exactly as constraints.premiership_player() reads 'GF' for the AFL and
# constraints_nba.played_in_the_finals() reads 'F'.

WORLD_SERIES_ROUND = "WS"


def played_in_the_world_series():
    """Appeared in a World Series game."""
    return ("SELECT DISTINCT player_id FROM games "
            "WHERE UPPER(TRIM(round)) = ?", [WORLD_SERIES_ROUND])


def won_the_world_series():
    """Played for the team that won the World Series that season.

    `result` on a postseason row is the *series* outcome from SeriesPost,
    so this is the title rather than a single game. The eligibility rule is
    "appeared in the Series", which is the one the data can support: a
    player on the roster who never left the bench is not in BattingPost.
    """
    return ("SELECT DISTINCT player_id FROM games "
            "WHERE UPPER(TRIM(round)) = ? AND result = 'W'",
            [WORLD_SERIES_ROUND])


def never_played_the_world_series():
    """Played, but never in a World Series game."""
    return ("SELECT DISTINCT player_id FROM games WHERE player_id NOT IN "
            "(SELECT player_id FROM games WHERE UPPER(TRIM(round)) = ?)",
            [WORLD_SERIES_ROUND])


def career_hits_min(hits):
    """3,000 hits is the milestone the sport actually talks about, and it
    is not the schema's headline stat, so it needs its own builder."""
    return ("SELECT player_id FROM players WHERE career_hits >= ?", [hits])


# --------------------------------------------------- rivalry (Retrosheet)
#
# Lahman has no box scores, so "won more Yankees-Red Sox games than lost"
# cannot come from the tables build_mlb_db.py writes. mlb/load_retrosheet.py
# fills mlb_player_rivalry_games separately, from Retrosheet's game logs --
# see that module's docstring for why the squares below are gated on it
# rather than always offered.

#: rivalry_key -> display label, for the axis picker.
RIVALRY_LABELS = {key: r["label"] for key, r in mlb_reference.rivalries().items()}
RIVALRY_CHOICES = list(RIVALRY_LABELS.items())

#: Rivalry appearances below this are a cameo, not a rivalry record.
RIVALRY_MIN_GAMES = 5


def rivalry_winning_record(rivalry):
    """Started more wins than losses in this rivalry, min RIVALRY_MIN_GAMES."""
    return ("""
        SELECT player_id FROM mlb_player_rivalry_games
        WHERE rivalry_key = ?
        GROUP BY player_id
        HAVING SUM(CASE WHEN is_win = 1 THEN 1 ELSE 0 END)
             > SUM(CASE WHEN is_win = 0 THEN 1 ELSE 0 END)
           AND COUNT(*) >= ?
    """, [rivalry, RIVALRY_MIN_GAMES])


def rivalry_games_min(rivalry, games):
    """Started X+ games in this rivalry, win or lose."""
    return ("""
        SELECT player_id FROM mlb_player_rivalry_games
        WHERE rivalry_key = ? GROUP BY player_id HAVING COUNT(*) >= ?
    """, [rivalry, games])


# ---------------------------------------------------------------- registry

BUILDERS = {
    "Played for club":            (played_for, ["club"]),
    "150+ / X+ career games":     (career_games_min, ["games"]),
    "Fewer than X career games":  (career_games_max, ["games"]),
    "X+ career goals":            (career_home_runs_min, ["goals"]),
    "X or fewer career goals":    (career_home_runs_max, ["goals"]),
    "X+ of a stat in one season": (season_stat_total_min, ["stat", "x"]),
    "X+ of a stat in a career":   (career_stat_total_min, ["stat", "x"]),
    "X+ goals at 2+ clubs":       (home_runs_at_multiple_clubs,
                                   ["goals", "clubs"]),
    "X+ games at 2+ clubs":       (games_at_multiple_clubs,
                                   ["games", "clubs"]),
    "No finals wins (played finals)": (no_postseason_wins, []),
    "Never won a final":          (never_won_postseason, []),
    "Never played finals":        (never_played_postseason, []),
    "Played in a final":          (played_in_the_postseason, []),
    "Won a final":                (won_postseason, []),
    "X+ finals games":            (postseason_games_min, ["x"]),
    "Played between seasons":     (played_in_season_range, ["from", "to"]),
    "Debuted between seasons":    (debuted_between, ["from", "to"]),
    "One-club player":            (one_club_player, []),
    "Played for X+ clubs":        (played_for_n_clubs, ["clubs"]),
    "Multi-club player":          (multi_club_player, []),
    "First career game for club": (debut_club, ["club"]),
    "Played at venue":            (played_at_venue, ["venue"]),
    "Won a final at venue":       (won_postseason_at, ["venue"]),
    # MLB-only. The World Series is a named round the other two sports have
    # no equivalent of, and 3,000 hits is a milestone the schema's headline
    # stat (home runs) cannot express.
    "Played in the World Series": (played_in_the_world_series, []),
    "Won the World Series":       (won_the_world_series, []),
    "Never played in the World Series": (never_played_the_world_series, []),
    "X+ career hits":             (career_hits_min, ["x"]),
    "Winning record in a rivalry": (rivalry_winning_record, ["rivalry"]),
    "X+ games in a rivalry":      (rivalry_games_min, ["rivalry", "games"]),
}

#: Builders needing an optional layer. app.py filters BUILDERS by these
#: sets, and an empty set means nothing is hidden rather than everything.
DRAFT_BUILDERS = set()
AWARD_BUILDER_NAMES = set()
CAPTAIN_BUILDER_NAMES = set()
RISING_STAR_BUILDER_NAMES = set()
FAMILY_RELATIONSHIP_BUILDER_NAMES = set()
AWARD_SLUGS = {}

#: Draft categories offered in the UI. Empty because there is no MLB draft
#: layer -- Lahman ships no draft file. app.py reads this instead of
#: hardcoding the AFL's eight.
DRAFT_TYPES = ()


# --------------------------------------------------------- capability probes

def draft_available(con):
    """True when an MLB draft layer has been imported. Not in this build."""
    return core.have_tables(con, "mlb_draft")


def awards_available(con):
    """True when AwardsPlayers.csv was loaded. build_mlb_db.py writes it
    whenever the file is present in the Lahman export."""
    return core.have_tables(con, "awards")


def awards_count(con):
    """Rows behind the "Awards data" status line."""
    try:
        return con.execute("SELECT COUNT(*) FROM awards").fetchone()[0]
    except Exception:                                       # noqa: BLE001
        return None


def hall_of_fame_available(con):
    return core.have_tables(con, "hall_of_fame")


def captain_available(con):
    """Baseball has captains; Lahman does not record them."""
    return False


def rising_star_available(con):
    return False


def family_relationships_available(con):
    return False


def rivalry_available(con):
    """True once mlb/load_retrosheet.py has populated the rivalry table."""
    if not core.have_tables(con, "mlb_player_rivalry_games"):
        return False
    return con.execute(
        "SELECT 1 FROM mlb_player_rivalry_games LIMIT 1").fetchone() is not None


#: Builders gated on a layer with no dedicated probe slot in registry.py's
#: five (draft/awards/captain/rising_star/family). registry.Sport.layers()
#: reads this generically: {builder_name: probe_function_name}.
LAYER_BUILDERS = {
    "Winning record in a rivalry": "rivalry_available",
    "X+ games in a rivalry": "rivalry_available",
}


# ------------------------------------------------- engine, bound to MLB
# Thin wrappers so the UI calls the same names for every sport.

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
star_value = core.star_value
stars_text = core.stars_text
stars_html = core.stars_html
STAR_DISCLAIMER = sports.MLB.star_disclaimer
