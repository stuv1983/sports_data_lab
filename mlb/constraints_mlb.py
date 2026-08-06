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
  * per-game averages via core.Generic, whose average builders divide a
    total by a row count -- a season here. The per-game squares below
    divide by SUM(games) instead, which is the real denominator and is a
    column Lahman does give us.
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
import venue_states

from . import mlb_reference

# Declared in sports.py because the sport picker needs them before this
# module is imported. CLUBS comes from the reference file the build writes,
# so it is measured rather than hand-maintained.
SCHEMA = sports.MLB_SCHEMA
STATS = sports.MLB_STATS
CLUBS = SCHEMA.clubs
VENUE_ALIASES = SCHEMA.venue_aliases

_G = core.Generic(SCHEMA)


# --------------------------------------- the Immaculate Grid pairing rule
#
# "When paired with a team, 100 RBI must be achieved with that team in a
# single season. When paired with a non-team category, it does not need to
# be in the same season."
#
# A row of this sport's `games` is a (player, season, team) carrying that
# season's totals, so both halves of "100 RBI for Cleveland in one season"
# live on one row. These two builders emit core's row-scoped fragments so
# that core._where can merge them into a single EXISTS when a team square
# meets a season-stat square; every other pairing is left alone, which is
# what the second sentence of the rule asks for.
#
# The predicates are written against the `g` alias core._row_exists opens.

def played_for(club):
    """Played at least one season for this club, predecessors included."""
    names = SCHEMA.club_identities(club)
    marks = ",".join("?" for _ in names)
    return (f"{core.ROW_MARKER}team@g.{SCHEMA.club_now} IN ({marks})",
            list(names))


def season_stat_total_min(stat, n):
    """Accumulated `n` or more of `stat` in a single season.

    No GROUP BY, unlike the generic builder: a row is already one player's
    season with one team, so the row's own value is the season total. That
    is also what makes it mergeable with a team predicate -- the season and
    the team are the same fact here.
    """
    if stat not in SCHEMA.stats:
        raise ValueError(f"unknown statistic: {stat!r}")
    return (f"{core.ROW_MARKER}stat@g.{stat} >= ?", [n])


# ------------------------------------------------- generic, bound to MLB

_played_for_any_season = _G.played_for
debut_club = _G.debut_club
one_club_player = _G.one_club_player
played_for_n_clubs = _G.played_for_n_clubs
multi_club_player = _G.multi_club_player

career_games_min = _G.career_games_min
career_games_max = _G.career_games_max
career_home_runs_min = _G.career_score_min
career_home_runs_max = _G.career_score_max
career_home_runs_between = _G.career_score_between

career_stat_total_min = _G.career_stat_total_min
CAREER_AVG_MIN_GAMES = _G.CAREER_AVG_MIN_GAMES
SEASON_AVG_MIN_GAMES = _G.SEASON_AVG_MIN_GAMES

home_runs_at_multiple_clubs = _G.score_at_multiple_clubs
games_at_multiple_clubs = _G.games_at_multiple_clubs

played_in_season_range = _G.played_in_season_range
debuted_between = _G.debuted_between

played_at_venue = _G.played_at_venue


def played_in_state(state):
    return _G.played_at_venues(venue_states.venues_for_state("mlb", state))

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


# ------------------------------------------------ Immaculate Grid categories
#
# The non-team, non-stat axes the real puzzle uses. Each is gated on the
# table behind it (see LAYER_BUILDERS), so a database built before the
# layer existed offers the square not at all rather than answering it "no".

#: Awards that appear as their own grid axis, label -> the `awards.award`
#: value. Lahman names them exactly as the puzzle does.
AWARD_AXES = {
    "Gold Glove": "Gold Glove",
    "Silver Slugger": "Silver Slugger",
    "MVP": "Most Valuable Player",
    "Cy Young": "Cy Young Award",
    "Rookie of the Year": "Rookie of the Year",
}
AWARD_AXIS_CHOICES = list(AWARD_AXES.items())

#: Fielding positions offered as an axis, from `player_positions`.
POSITIONS = ("Pitcher", "Catcher", "First Base", "Second Base", "Third Base",
             "Shortstop", "Left Field", "Center Field", "Right Field",
             "Designated Hitter")


def won_award(award):
    """Won this award at least once, in any season."""
    return ("SELECT DISTINCT player_id FROM awards WHERE award = ?",
            [AWARD_AXES.get(award, award)])


def selected_all_star():
    """Named to an All-Star team.

    Selection, not appearance: Lahman records GP=0 for a player picked who
    did not get into the game, and the puzzle counts him as an All-Star.
    """
    return ("SELECT DISTINCT player_id FROM all_star", [])


def in_the_hall_of_fame():
    """Inducted into the Hall of Fame.

    `hall_of_fame` is a ballot table -- most of its rows are a player
    appearing on a ballot and missing out -- so this filters on the
    induction rather than on being listed.
    """
    return ("SELECT DISTINCT player_id FROM hall_of_fame "
            "WHERE UPPER(TRIM(inducted)) = 'Y'", [])


def played_position(position):
    """Appeared at this position at least once.

    'min. 1 game', as the puzzle's own subtitle says: player_positions
    holds career totals per position and only rows above zero are written.
    """
    return ("SELECT DISTINCT player_id FROM player_positions "
            "WHERE position = ? AND games >= 1", [position])


def born_outside_the_us():
    """Born outside the 50 states and DC.

    Not the same as "born outside the USA", which is why this reads both
    columns. Lahman files Puerto Rico, the US Virgin Islands and Guam as
    their own birthCountry, so they fall outside by that test -- correctly,
    since the puzzle's wording is "50 states and DC". A player with no
    recorded birth country is excluded rather than assumed foreign.
    """
    return ("SELECT player_id FROM players "
            "WHERE birth_country IS NOT NULL AND TRIM(birth_country) <> '' "
            "AND UPPER(TRIM(birth_country)) NOT IN ('USA', 'US', 'U.S.A.')",
            [])


#: Games a career must cover before a per-game rate is offered, so a
#: three-game call-up cannot top a rate leaderboard.
CAREER_PER_GAME_MIN_GAMES = core.Generic.CAREER_AVG_MIN_GAMES


def career_stat_per_game_min(stat, avg, min_games=None):
    """Averaged `avg` or more of `stat` per game across a career.

    Divides by SUM(games), not by row count: a row is a season with one
    team, so core.Generic's average builder would return a per-season rate
    under a per-game label. Seasons where the stat was not recorded are
    excluded from both halves of the fraction.
    """
    floor = (CAREER_PER_GAME_MIN_GAMES if min_games is None
             else int(min_games))
    return (f"""SELECT player_id FROM games
                WHERE {stat} IS NOT NULL AND games > 0
                GROUP BY player_id
                HAVING SUM(games) >= ?
                   AND CAST(SUM({stat}) AS REAL) / SUM(games) >= ?""",
            [floor, avg])


def season_stat_per_game_min(stat, avg, min_games=1):
    """A single season averaging `avg` or more of `stat` per game.

    Grouped by season rather than by row so a player traded mid-year is
    judged on the whole season, not on each stint separately.
    """
    return (f"""SELECT player_id FROM games
                WHERE {stat} IS NOT NULL AND games > 0
                  AND is_postseason = 0
                GROUP BY player_id, season
                HAVING SUM(games) >= ?
                   AND CAST(SUM({stat}) AS REAL) / SUM(games) >= ?""",
            [int(min_games), avg])


def season_batting_average_min(average, min_plate_appearances=502):
    """A qualifying season batting at least this average.

    A rate, not a total, so it needs a floor: without one a 1-for-2
    September call-up bats .500 for the season. The floor is 502 *plate
    appearances*, the modern qualification rule, estimated as at-bats plus
    walks.

    Not at-bats alone, which is the obvious shortcut and is wrong in the
    one case everybody checks: Ted Williams hit .406 in 1941 from 456
    at-bats, because he also walked 147 times. An at-bats floor of 502
    threw out the most famous batting season in the sport. AB+BB puts him
    at 603 and comfortably in.

    Still an estimate -- hit-by-pitch and sacrifices are plate appearances
    too and this build does not import them -- so it errs a little strict
    at the margin rather than letting a short season in.
    """
    return ("SELECT player_id FROM games "
            "WHERE at_bats > 0 "
            "AND (at_bats + COALESCE(walks, 0)) >= ? "
            "AND (CAST(hits AS REAL) / at_bats) >= ?",
            [min_plate_appearances, average])


def season_two_stats_min(stat_a, x_a, stat_b, x_b):
    """Both thresholds reached in the *same* season -- the 30/30 square.

    One row is one player's season with one team, so this is a single-row
    test. It is the one place a season square is conjunctive on its own,
    independent of whether a team is on the other axis.
    """
    for stat in (stat_a, stat_b):
        if stat not in SCHEMA.stats:
            raise ValueError(f"unknown statistic: {stat!r}")
    return (f"SELECT player_id FROM games "
            f"WHERE {stat_a} >= ? AND {stat_b} >= ?", [x_a, x_b])


# -------------------------------------------------------------------- WAR
#
# Wins Above Replacement is the one statistic on this page that Lahman
# cannot supply at all -- see mlb/load_war.py for what it is and where the
# figures come from. Both squares are gated on `war_available`, so they
# stay hidden until those files are loaded rather than returning nobody.

def season_war_min(war):
    """A single season worth `war` or more Wins Above Replacement.

    A row is one player's season with one team, so a player traded
    mid-year has two rows and clears this only if one half does on its
    own. That is the rule every other season square here follows, and it
    is why `career_war_min` reads the career column instead of summing
    this one.
    """
    return (f"{core.ROW_MARKER}stat@g.war >= ?", [war])


def career_war_min(war):
    """Career WAR of at least `war`, from the total load_war.py wrote.

    Read off `players` rather than summed here, so the number a square
    selects on is the same one the player's profile displays.
    """
    return ("SELECT player_id FROM players WHERE career_war >= ?", [war])


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
    "Played in state":            (played_in_state, ["state"]),
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
    # Immaculate Grid's own non-team categories.
    "Won an award":               (won_award, ["award_axis"]),
    "All-Star selection":         (selected_all_star, []),
    "Hall of Fame":               (in_the_hall_of_fame, []),
    "Played a position":          (played_position, ["position"]),
    "Born outside the US":        (born_outside_the_us, []),
    ".300+ batting average season": (season_batting_average_min,
                                     ["average", "min_plate_appearances"]),
    "Two stats in the same season": (season_two_stats_min,
                                     ["stat_a", "x_a", "stat_b", "x_b"]),
    "Career average of a stat":   (career_stat_per_game_min,
                                   ["stat", "avg", "min_games"]),
    "Season average of a stat":   (season_stat_per_game_min,
                                   ["stat", "avg", "min_games"]),
    # Named squares of their own rather than left to the generic stat
    # builders: WAR is the category Immaculate Grid actually uses, and
    # "5+ WAR season" is how a solver says it.
    "X+ WAR in a season":         (season_war_min, ["war"]),
    "X+ career WAR":              (career_war_min, ["war"]),
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


def all_star_available(con):
    """True once build_mlb_db.write_layers has written AllstarFull.csv."""
    return core.have_tables(con, "all_star")


def positions_available(con):
    """True once the per-position appearance totals are built."""
    return core.have_tables(con, "player_positions")


def birthplace_available(con):
    """True when `players` carries the birth columns the build now writes.

    A database built before they existed has the table but not the column,
    and the square would fail on execution rather than be hidden.
    """
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(players)")}
    except Exception:                                       # noqa: BLE001
        return False
    return "birth_country" in columns


def rivalry_available(con):
    """True once mlb/load_retrosheet.py has populated the rivalry table."""
    if not core.have_tables(con, "mlb_player_rivalry_games"):
        return False
    return con.execute(
        "SELECT 1 FROM mlb_player_rivalry_games LIMIT 1").fetchone() is not None


def war_available(con):
    """True once mlb/load_war.py has loaded Baseball-Reference's WAR files.

    Checks for a value, not just the column: `load_war` adds `games.war`
    before it fills it, so a run that failed part-way would otherwise
    advertise squares that no player can satisfy.
    """
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(games)")}
        if "war" not in columns:
            return False
        return con.execute(
            "SELECT 1 FROM games WHERE war IS NOT NULL LIMIT 1"
        ).fetchone() is not None
    except Exception:                                       # noqa: BLE001
        return False


def war_count(con):
    """Player-seasons carrying a WAR figure, for the status panel."""
    try:
        return con.execute(
            "SELECT COUNT(*) FROM games WHERE war IS NOT NULL").fetchone()[0]
    except Exception:                                       # noqa: BLE001
        return None




#: Builders gated on a layer with no dedicated probe slot in registry.py's
#: five (draft/awards/captain/rising_star/family). registry.Sport.layers()
#: reads this generically: {builder_name: probe_function_name}.
LAYER_BUILDERS = {
    "Winning record in a rivalry": "rivalry_available",
    "X+ games in a rivalry": "rivalry_available",
    "Won an award": "awards_available",
    "All-Star selection": "all_star_available",
    "Hall of Fame": "hall_of_fame_available",
    "Played a position": "positions_available",
    "Born outside the US": "birthplace_available",
    # Hidden until the Baseball-Reference files are loaded, because WAR is
    # the one statistic here that no amount of Lahman can supply.
    "X+ WAR in a season": "war_available",
    "X+ career WAR": "war_available",
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
