"""
nfl/constraints_nfl.py -- NFL square descriptions, compiled to SQL.

Every constraint compiles to a fragment selecting DISTINCT player_id, and
intersecting two of them solves one square. Same contract as
afl/constraints.py and nba/constraints_nba.py; the machinery is core.py.

Most of this file is re-exports bound to NFL_SCHEMA. BUILDERS reuses the
AFL display keys wherever the semantics match, because app.axis_widget
renders those keys through the sport's Vocab -- "X+ career goals" reads as
"X+ career touchdowns" on an NFL board without any UI code knowing.

WHAT IS DELIBERATELY MISSING
---------------------------
`teammate_of_id` matches players sharing a (club, season), which the NFL's
53-man rosters and mid-season trades make far too loose to answer honestly.

Everything here reads `games`, whose weekly player statistics begin in 1999.
A square is therefore a question about a player's statistics-era career, not
his whole NFL career. sports.NFL.empty_hint says so on screen; the rosters
table reaches back to 1920 but a roster line is not an appearance.
"""

import core
import sports

from . import nfl_reference

# Declared in sports.py because the sport picker needs them before this
# module is imported. STATS and CLUBS come from the reference file
# nfl/patch_nfl_db.py writes, so they are measured rather than assumed.
SCHEMA = sports.NFL_SCHEMA
STATS = sports.NFL_STATS
CLUBS = SCHEMA.clubs
VENUE_ALIASES = SCHEMA.venue_aliases

_G = core.Generic(SCHEMA)


# ------------------------------------------------- generic, bound to NFL

played_for = _G.played_for
debut_club = _G.debut_club
one_club_player = _G.one_club_player
played_for_n_clubs = _G.played_for_n_clubs
multi_club_player = _G.multi_club_player

career_games_min = _G.career_games_min
career_games_max = _G.career_games_max
career_touchdowns_min = _G.career_score_min
career_touchdowns_max = _G.career_score_max
career_touchdowns_between = _G.career_score_between

stat_in_a_game = _G.stat_in_a_game
two_stats_same_game = _G.two_stats_same_game
season_stat_average_min = _G.season_stat_average_min
season_stat_total_min = _G.season_stat_total_min
career_stat_total_min = _G.career_stat_total_min
career_stat_average_min = _G.career_stat_average_min
games_with_stat_min = _G.games_with_stat_min
CAREER_AVG_MIN_GAMES = _G.CAREER_AVG_MIN_GAMES
SEASON_AVG_MIN_GAMES = _G.SEASON_AVG_MIN_GAMES

touchdowns_at_multiple_teams = _G.score_at_multiple_clubs
games_at_multiple_teams = _G.games_at_multiple_clubs

played_in_season_range = _G.played_in_season_range
debuted_between = _G.debuted_between

played_at_venue = _G.played_at_venue

# The playoffs are the NFL's post-season; only the wording differs.
stat_in_a_playoff_game = _G.stat_in_a_postseason_game
playoff_stat_average_min = _G.postseason_stat_average_min
playoff_games_min = _G.postseason_games_min
played_in_the_playoffs = _G.played_postseason
never_played_playoffs = _G.never_played_postseason
no_playoff_wins = _G.no_postseason_wins
never_won_a_playoff_game = _G.never_won_postseason
won_a_playoff_game = _G.won_postseason
won_a_playoff_game_at = _G.won_postseason_at
touchdowns_average_in_playoffs = _G.score_average_in_postseason


# --------------------------------------------------------- NFL-specific
#
# The Super Bowl is one nominated round, not simply the last playoff game,
# so these read `round` -- the game type patch_nfl_db.py copies from
# `matches` -- rather than the is_playoff flag.

SUPER_BOWL_ROUND = "SB"
CONFERENCE_ROUND = "CON"


def played_in_the_super_bowl():
    """Appeared in a Super Bowl."""
    return ("SELECT DISTINCT player_id FROM games "
            "WHERE UPPER(TRIM(round)) = ?", [SUPER_BOWL_ROUND])


def won_the_super_bowl():
    """Appeared in a Super Bowl his team won.

    The eligibility rule is "appeared in the game", which is what the data
    can support. A ring earned from the inactive list is a different
    question and the database cannot see it.
    """
    return ("SELECT DISTINCT player_id FROM games "
            "WHERE UPPER(TRIM(round)) = ? AND result = 'W'",
            [SUPER_BOWL_ROUND])


def never_played_in_the_super_bowl():
    """Played, but never in a Super Bowl."""
    return ("SELECT DISTINCT player_id FROM games WHERE player_id NOT IN "
            "(SELECT player_id FROM games WHERE UPPER(TRIM(round)) = ?)",
            [SUPER_BOWL_ROUND])


def played_in_a_conference_championship():
    return ("SELECT DISTINCT player_id FROM games "
            "WHERE UPPER(TRIM(round)) = ?", [CONFERENCE_ROUND])


# ------------------------------------------------------------------ draft
#
# These read the draft columns nflverse puts on the player list, not the
# `draft_picks` table: both come from the same source, but the player list
# carries 12,229 drafted players against draft_picks' 10,824 linked ones,
# and needs no join.
#
# Coverage: draft_picks starts in 1980. A player drafted earlier has no
# draft year here, which is why `undrafted` is worded as it is -- it means
# "no draft record", and for a 1970s career that is a gap, not a fact.

def drafted_in_round(n):
    """Taken in this round of the draft."""
    return ("SELECT player_id FROM players WHERE draft_round = ?", [int(n)])


def draft_pick_between(lo, hi):
    """Overall pick number between lo and hi, inclusive."""
    return ("SELECT player_id FROM players "
            "WHERE draft_pick BETWEEN ? AND ?", [int(lo), int(hi)])


def drafted_by(club):
    """Drafted by this franchise, its earlier identities included."""
    codes = nfl_reference.codes_for(club)
    if not codes:
        return ("SELECT player_id FROM players WHERE 0", [])
    marks = ",".join("?" for _ in codes)
    return (f"SELECT player_id FROM players WHERE draft_team IN ({marks})",
            codes)


def drafted_between(lo, hi):
    return ("SELECT player_id FROM players "
            "WHERE draft_year BETWEEN ? AND ?", [int(lo), int(hi)])


def undrafted():
    """Played, with no draft record.

    True of an undrafted free agent, and equally true of anyone drafted
    before 1980, which is where nflverse's draft data starts. Both are
    playing careers the draft layer cannot account for.
    """
    return ("SELECT player_id FROM players "
            "WHERE draft_year IS NULL AND career_games >= 1", [])


# ---------------------------------------------------------------- registry

BUILDERS = {
    "Played for club":            (played_for, ["club"]),
    "150+ / X+ career games":     (career_games_min, ["games"]),
    "Fewer than X career games":  (career_games_max, ["games"]),
    "X+ career goals":            (career_touchdowns_min, ["goals"]),
    "X or fewer career goals":    (career_touchdowns_max, ["goals"]),
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
    "X+ goals at 2+ clubs":       (touchdowns_at_multiple_teams,
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
    "Goal average in finals":     (touchdowns_average_in_playoffs, ["avg"]),
    # NFL-only keys. The AFL and NBA have no equivalent question, and these
    # are named for what a `games` row can prove: appearing in the game,
    # not being on the roster when it was won.
    "Played in a Super Bowl":     (played_in_the_super_bowl, []),
    "Won a Super Bowl":           (won_the_super_bowl, []),
    "Never played in a Super Bowl": (never_played_in_the_super_bowl, []),
    "Played in a conference championship":
        (played_in_a_conference_championship, []),
    # Draft. Hidden by app.py when draft_available() is False.
    "Drafted in round X":         (drafted_in_round, ["round"]),
    "Draft pick between":         (draft_pick_between, ["from", "to"]),
    "Drafted by club":            (drafted_by, ["club"]),
    "Drafted between years":      (drafted_between, ["from", "to"]),
    "Undrafted (no draft record)": (undrafted, []),
}

#: Builders app.py hides when their layer is missing.
DRAFT_BUILDERS = {"Drafted in round X", "Draft pick between",
                  "Drafted by club", "Drafted between years",
                  "Undrafted (no draft record)"}
AWARD_BUILDER_NAMES = set()
AWARD_SLUGS = {}
#: The NFL has one draft, so there is no type to choose between.
DRAFT_TYPES = ()
TEAM_SEASON_BUILDERS = set()


def draft_available(con):
    """True when the player list carries draft years (1980 onward)."""
    if not core.have_tables(con, "players"):
        return False
    try:
        return bool(con.execute(
            "SELECT 1 FROM players WHERE draft_year IS NOT NULL "
            "LIMIT 1").fetchone())
    except Exception:
        return False


def draft_count(con):
    return con.execute(
        "SELECT COUNT(*) FROM players WHERE draft_year IS NOT NULL"
    ).fetchone()[0]


# ------------------------------------------------------- source layers
#
# Everything the build imports and no square reads yet. They are reported
# rather than hidden: `--extended` loads eight datasets, any of which can
# fail without failing the build, and a status panel that says nothing
# about them cannot be told apart from a build that skipped them.
#
# The probes are generated because they are all the same probe -- does the
# table exist, and how many rows -- and sixteen hand-written copies of it
# would be sixteen places for one to drift.

SOURCE_LAYERS = {
    "rosters": "Rosters (1920 on)",
    "rosters_weekly": "Weekly rosters (2002 on)",
    "snap_counts": "Snap counts (2012 on)",
    "injuries": "Injuries (2009 on)",
    "depth_charts": "Depth charts (2001 on)",
    "officials": "Officials (2015 on)",
    "combine": "Combine",
    "contracts": "Contracts",
    "trades": "Trades",
}


def _layer_probes(table):
    def available(con):
        return core.have_tables(con, table)

    def count(con):
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    available.__name__ = f"{table}_available"
    count.__name__ = f"{table}_count"
    return available, count


for _table in SOURCE_LAYERS:
    globals()[f"{_table}_available"], globals()[f"{_table}_count"] = \
        _layer_probes(_table)
del _table


# ------------------------------------------------- engine, bound to NFL

def require_schema(con):
    """Fail loudly at startup if the database has not been patched."""
    try:
        core.require_schema(con, SCHEMA)
    except RuntimeError as exc:
        raise RuntimeError(
            f"{exc}\n\nIf the build itself succeeded, this is the adapter "
            f"step: run `python -m nfl.patch_nfl_db`.") from exc


def solve(con, constraints, limit=25, order="obscurity"):
    return core.solve(con, constraints, SCHEMA, limit=limit, order=order)


def count(con, constraints):
    return core.count(con, constraints, SCHEMA)


def square(con, constraints, order="obscurity"):
    return core.square(con, constraints, SCHEMA, order=order)


def to_standalone_sql(constraints, limit=25):
    return core.to_standalone_sql(constraints, SCHEMA, limit=limit)


star_value = core.star_value
stars_text = core.stars_text
stars_html = core.stars_html
STAR_DISCLAIMER = sports.NFL.star_disclaimer
