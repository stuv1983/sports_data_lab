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
The generic teammate matcher requires exact shared matches. This module
does not offer it until the weekly rows have a stable shared match identity.

Everything here reads `games`, whose weekly player statistics begin in 1999.
A square is therefore a question about a player's statistics-era career, not
his whole NFL career. sports.NFL.empty_hint says so on screen; the rosters
table reaches back to 1920 but a roster line is not an appearance.
"""

import core
import sports
import venue_states

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
played_with_id = _G.played_with_id

played_in_season_range = _G.played_in_season_range
debuted_between = _G.debuted_between

played_at_venue = _G.played_at_venue


def played_in_state(state):
    return _G.played_at_venues(venue_states.venues_for_state("nfl", state))

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

# Compared bare (`round = ?`), never as UPPER(TRIM(round)): wrapping the
# column blinds SQLite to the round indexes. The build stores codes already
# normalised, and the round/result hygiene test guards that invariant.
SUPER_BOWL_ROUND = "SB"
CONFERENCE_ROUND = "CON"


def played_in_the_super_bowl():
    """Appeared in a Super Bowl."""
    return ("SELECT DISTINCT player_id FROM games "
            "WHERE round = ?", [SUPER_BOWL_ROUND])


def won_the_super_bowl():
    """Appeared in a Super Bowl his team won.

    The eligibility rule is "appeared in the game", which is what the data
    can support. A ring earned from the inactive list is a different
    question and the database cannot see it.
    """
    return ("SELECT DISTINCT player_id FROM games "
            "WHERE round = ? AND result = 'W'",
            [SUPER_BOWL_ROUND])


def never_played_in_the_super_bowl():
    """Played, but never in a Super Bowl."""
    return ("SELECT DISTINCT player_id FROM games WHERE player_id NOT IN "
            "(SELECT player_id FROM games WHERE round = ?)",
            [SUPER_BOWL_ROUND])


def super_bowls_played_min(times):
    return _G.played_in_round_min(SUPER_BOWL_ROUND, times)


def super_bowls_won_min(times):
    return _G.round_outcome_min(SUPER_BOWL_ROUND, "W", times)


def super_bowls_lost_min(times):
    return _G.round_outcome_min(SUPER_BOWL_ROUND, "L", times)


def played_in_a_conference_championship():
    return ("SELECT DISTINCT player_id FROM games "
            "WHERE round = ?", [CONFERENCE_ROUND])


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


# --------------------------------------------------- extended layers
#
# Squares over the datasets `--extended` imports. Each is gated in
# LAYER_BUILDERS below, so a build without that dataset does not offer it
# rather than failing on a missing table.
#
# Two player keys are in play. depth_charts, rosters_weekly, injuries and
# contracts carry `gsis_id`, which is our player_id. snap_counts, combine
# and trades carry Pro-Football-Reference ids, which reach players through
# `players.pfr_id` -- 22,554 of 25,041 players have one, and the join
# resolves 324,384 of 324,611 snap rows.

_BY_PFR = ("JOIN players p ON p.pfr_id = {table}.{column}")


def offensive_snaps_in_a_game(n):
    """Took this many offensive snaps in one game. 2012 onward."""
    return (f"""SELECT DISTINCT p.player_id FROM snap_counts s
                {_BY_PFR.format(table='s', column='pfr_player_id')}
                WHERE s.offense_snaps >= ?""", [n])


def defensive_snaps_in_a_game(n):
    """Took this many defensive snaps in one game. 2012 onward."""
    return (f"""SELECT DISTINCT p.player_id FROM snap_counts s
                {_BY_PFR.format(table='s', column='pfr_player_id')}
                WHERE s.defense_snaps >= ?""", [n])


def special_teams_snaps_in_a_season(n):
    """Accumulated this many special-teams snaps within one season."""
    return (f"""SELECT player_id FROM (
                  SELECT p.player_id AS player_id, s.season AS season,
                         SUM(s.st_snaps) AS total
                  FROM snap_counts s
                  {_BY_PFR.format(table='s', column='pfr_player_id')}
                  GROUP BY p.player_id, s.season
                  HAVING total >= ?)""", [n])


def listed_as_a_starter():
    """Named first on a published depth chart. 2001 onward.

    Depth charts are published weekly and are not the same as starting the
    game: this is the club's stated intention, which is the only starter
    evidence nflverse carries.
    """
    return ("SELECT DISTINCT gsis_id FROM depth_charts "
            "WHERE TRIM(depth_team) = '1' AND gsis_id IS NOT NULL", [])


def listed_as_a_starter_for(club):
    """Named first on a depth chart for this franchise."""
    codes = nfl_reference.codes_for(club)
    marks = ",".join("?" for _ in codes) or "NULL"
    return (f"""SELECT DISTINCT gsis_id FROM depth_charts
                WHERE TRIM(depth_team) = '1' AND gsis_id IS NOT NULL
                  AND COALESCE(team, club_code) IN ({marks})""", codes)


def on_the_weekly_roster_for(club):
    """Held a place on this franchise's weekly roster. 2002 onward.

    Wider than "played for": a player can be rostered all season and never
    take a snap, and `games` cannot see him. That is the whole point of
    this square, and it is why it is worded as roster membership.
    """
    codes = nfl_reference.codes_for(club)
    marks = ",".join("?" for _ in codes) or "NULL"
    return (f"""SELECT DISTINCT gsis_id FROM rosters_weekly
                WHERE gsis_id IS NOT NULL AND team IN ({marks})""", codes)


def bench_press_at_the_combine(n):
    """Put up this many bench-press reps at the NFL combine."""
    return (f"""SELECT DISTINCT p.player_id FROM combine c
                {_BY_PFR.format(table='c', column='pfr_id')}
                WHERE c.bench >= ?""", [n])


def attended_the_combine():
    return (f"""SELECT DISTINCT p.player_id FROM combine c
                {_BY_PFR.format(table='c', column='pfr_id')}""", [])


def contract_worth_at_least(millions):
    """Signed a contract of this total value, in millions of dollars."""
    return ("SELECT DISTINCT gsis_id FROM contracts "
            "WHERE gsis_id IS NOT NULL AND value >= ?", [millions])


def was_traded():
    """Named in a trade. Over the Cap's trade history, keyed on PFR id."""
    return (f"""SELECT DISTINCT p.player_id FROM trades t
                {_BY_PFR.format(table='t', column='pfr_id')}""", [])


def listed_out_on_an_injury_report():
    """Ruled out on a weekly injury report. 2009 onward."""
    return ("SELECT DISTINCT gsis_id FROM injuries "
            "WHERE gsis_id IS NOT NULL AND report_status = 'Out'", [])


# ---------------------------------------------------------------- registry

def won_wiki_award(award):
    """Won the selected award in the normalized Wikipedia layer."""
    return ("SELECT DISTINCT player_id FROM wiki_awards "
            "WHERE award_key=? AND player_id IS NOT NULL "
            "AND match_status IN ('unique','resolved')", [award])


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
    "Played with…":               (played_with_id, ["player_id"]),
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
    "Played in state":            (played_in_state, ["state"]),
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
    "Played in X+ Super Bowls":   (super_bowls_played_min, ["times"]),
    "Won X+ Super Bowls":         (super_bowls_won_min, ["times"]),
    "Lost X+ Super Bowls":        (super_bowls_lost_min, ["times"]),
    "Never played in a Super Bowl": (never_played_in_the_super_bowl, []),
    "Played in a conference championship":
        (played_in_a_conference_championship, []),
    # Draft. Hidden by app.py when draft_available() is False.
    "Drafted in round X":         (drafted_in_round, ["round"]),
    "Draft pick between":         (draft_pick_between, ["from", "to"]),
    "Drafted by club":            (drafted_by, ["club"]),
    "Drafted between years":      (drafted_between, ["from", "to"]),
    "Undrafted (no draft record)": (undrafted, []),
    # Extended layers. Each is hidden unless its table was imported.
    "X+ offensive snaps in a game":   (offensive_snaps_in_a_game, ["x"]),
    "X+ defensive snaps in a game":   (defensive_snaps_in_a_game, ["x"]),
    "X+ special-teams snaps in a season":
        (special_teams_snaps_in_a_season, ["x"]),
    "Listed as a starter":            (listed_as_a_starter, []),
    "Listed as a starter for club":   (listed_as_a_starter_for, ["club"]),
    "On a club's weekly roster":      (on_the_weekly_roster_for, ["club"]),
    "X+ bench-press reps at the combine":
        (bench_press_at_the_combine, ["x"]),
    "Attended the NFL combine":       (attended_the_combine, []),
    "Contract worth $X million+":     (contract_worth_at_least, ["x"]),
    "Named in a trade":               (was_traded, []),
    "Ruled out on an injury report":  (listed_out_on_an_injury_report, []),
    "Won an NFL award":               (won_wiki_award, ["award"]),
}

#: The category shelves the criterion pickers arrange BUILDERS on --
#: the same names the AFL catalogue uses, so a reader who learned one
#: sport's picker can navigate every sport's. A builder named nowhere
#: here falls to the picker's "More" shelf.
BUILDER_GROUPS = {
    "Clubs & journeys": (
        "Played for club", "First career game for club", "One-club player",
        "Multi-club player", "Played for X+ clubs", "X+ goals at 2+ clubs",
        "X+ games at 2+ clubs", "On a club's weekly roster",
    ),
    "Career milestones": (
        "150+ / X+ career games", "Fewer than X career games",
        "X+ career goals", "X or fewer career goals",
        "X+ of a stat in a career", "Career average of a stat",
        "Contract worth $X million+", "Named in a trade",
    ),
    "Single-game feats": (
        "X+ of a stat in one game", "Two stats in the same game",
        "X+ games with Y+ of a stat", "X+ offensive snaps in a game",
        "X+ defensive snaps in a game",
    ),
    "Season & era": (
        "Played between seasons", "Debuted between seasons",
        "X+ of a stat in one season", "Season average of a stat",
        "X+ special-teams snaps in a season", "Listed as a starter",
        "Listed as a starter for club", "Ruled out on an injury report",
    ),
    "Finals & premierships": (
        "Played in a final", "Won a final", "X+ finals games",
        "X+ of a stat in a final", "Finals average of a stat",
        "Goal average in finals", "No finals wins (played finals)",
        "Never won a final", "Never played finals",
        "Played in a Super Bowl", "Won a Super Bowl",
        "Played in X+ Super Bowls", "Won X+ Super Bowls",
        "Lost X+ Super Bowls", "Never played in a Super Bowl",
        "Played in a conference championship",
    ),
    "Grounds & venues": (
        "Played at venue", "Played in state", "Won a final at venue",
    ),
    "Draft & recruitment": (
        "Drafted in round X", "Draft pick between", "Drafted by club",
        "Drafted between years", "Undrafted (no draft record)",
        "X+ bench-press reps at the combine", "Attended the NFL combine",
    ),
    "Awards & honours": ("Won an NFL award",),
    "Teammates": ("Played with…",),
}

#: Builder -> the availability probe app.py gates it on. A build without
#: `--extended` simply does not offer these; nothing here fails on a
#: missing table.
LAYER_BUILDERS = {
    "X+ offensive snaps in a game": "snap_counts_available",
    "X+ defensive snaps in a game": "snap_counts_available",
    "X+ special-teams snaps in a season": "snap_counts_available",
    "Listed as a starter": "depth_charts_available",
    "Listed as a starter for club": "depth_charts_available",
    "On a club's weekly roster": "rosters_weekly_available",
    "X+ bench-press reps at the combine": "combine_available",
    "Attended the NFL combine": "combine_available",
    "Contract worth $X million+": "contracts_available",
    "Named in a trade": "trades_available",
    "Ruled out on an injury report": "injuries_available",
}

#: Builders app.py hides when their layer is missing.
DRAFT_BUILDERS = {"Drafted in round X", "Draft pick between",
                  "Drafted by club", "Drafted between years",
                  "Undrafted (no draft record)"}
AWARD_BUILDER_NAMES = {"Won an NFL award"}
AWARD_SLUGS = {
    "AP Most Valuable Player": "nfl_mvp",
    "AP Offensive Player of the Year": "nfl_offensive_player_of_year",
    "AP Defensive Player of the Year": "nfl_defensive_player_of_year",
    "AP Rookie of the Year": "nfl_rookie_of_year",
}
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


def awards_available(con):
    if not core.have_tables(con, "wiki_awards"):
        return False
    return bool(con.execute(
        "SELECT 1 FROM wiki_awards WHERE player_id IS NOT NULL LIMIT 1"
    ).fetchone())


def awards_count(con):
    return con.execute("SELECT COUNT(*) FROM wiki_awards").fetchone()[0]


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
            f"step: run `python -m utils.nfl.patch_nfl_db`.") from exc


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
