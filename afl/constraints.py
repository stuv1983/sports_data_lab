"""
afl/constraints.py -- AFL square descriptions, compiled to SQL.

Every constraint compiles to a fragment selecting DISTINCT player_id.
Intersecting two of them solves one square of the grid.

Used by app.py (the Streamlit grid), afl/make_sql.py (the .sql files),
afl/parse_criteria.py and afl/gridley.py.

WHAT CHANGED
------------
The sport-agnostic half of this module now lives in core.py: the solver,
the standalone-SQL renderer, the schema check, and every builder that
differed between sports only by column name. This file keeps its full
public surface -- every function, BUILDERS entry and constant that other
modules imported before is still importable from here, with identical
behaviour -- but the shared machinery is written once.

The AFL-specific parts that stay here are the ones with no honest NBA
counterpart: grand finals, premierships, wooden spoons, leading
goalkickers, and the Draftguru draft layer.
"""

import core
import sports

# The schema and vocabulary lists are declared once, in sports.py, because
# the sport picker needs them before this module is imported.
SCHEMA = sports.AFL_SCHEMA
STATS = sports.AFL_STATS
CLUBS = sports.AFL_CLUBS
VENUE_ALIASES = sports.AFL_VENUE_ALIASES

_G = core.Generic(SCHEMA)


# ------------------------------------------------- generic, bound to AFL
# Re-exported under the names callers already use.

played_for = _G.played_for
debut_club = _G.debut_club
one_club_player = _G.one_club_player
played_for_n_clubs = _G.played_for_n_clubs
multi_club_player = _G.multi_club_player

career_games_min = _G.career_games_min
career_games_max = _G.career_games_max
career_goals_min = _G.career_score_min
career_goals_max = _G.career_score_max
career_goals_between = _G.career_score_between

stat_in_a_game = _G.stat_in_a_game
two_stats_same_game = _G.two_stats_same_game
season_stat_average_min = _G.season_stat_average_min
season_stat_total_min = _G.season_stat_total_min
career_stat_total_min = _G.career_stat_total_min
career_stat_average_min = _G.career_stat_average_min
games_with_stat_min = _G.games_with_stat_min
stat_in_a_final = _G.stat_in_a_postseason_game
finals_stat_average_min = _G.postseason_stat_average_min
CAREER_AVG_MIN_GAMES = _G.CAREER_AVG_MIN_GAMES

#: Exposed so the UI and the tests can state the threshold rather than
#: leaving it buried in a default argument.
SEASON_AVG_MIN_GAMES = _G.SEASON_AVG_MIN_GAMES

goals_at_multiple_clubs = _G.score_at_multiple_clubs
games_at_multiple_clubs = _G.games_at_multiple_clubs

played_in_season_range = _G.played_in_season_range
debuted_between = _G.debuted_between

teammate_of = _G.teammate_of
teammate_of_id = _G.teammate_of_id

played_at_venue = _G.played_at_venue

# Finals are the AFL's name for the post-season. The semantics are the
# generic ones; only the wording differs.
finals_games_min = _G.postseason_games_min
played_in_a_final = _G.played_postseason
never_played_finals = _G.never_played_postseason
no_finals_wins = _G.no_postseason_wins
never_won_a_final = _G.never_won_postseason
won_a_final = _G.won_postseason
won_a_final_at = _G.won_postseason_at
goal_average_in_finals = _G.score_average_in_postseason


def _venue(name):
    """Kept for callers that resolved a venue alias directly."""
    return SCHEMA.canonical_venue(name)


# A compound ground criterion lets a grid square express much more than
# merely appearing there: e.g. 10 wins at the MCG or 50 career marks at
# Kardinia Park.  The values, rather than free-form SQL, are exposed to the
# UI so saved grids remain deterministic and safe.
GROUND_STATUS_CHOICES = (
    ("all", "All games"),
    ("wins", "Wins"),
    ("losses", "Losses"),
    ("draws", "Draws"),
    ("finals", "Finals"),
    ("final_wins", "Finals wins"),
)
GROUND_METRIC_CHOICES = (
    ("games", "Games played"),
    ("wins", "Games won"),
    ("goals", "Goals"),
    ("behinds", "Behinds"),
    ("score", "Player score (points)"),
    ("marks", "Marks"),
    ("disposals", "Disposals"),
    ("kicks", "Kicks"),
    ("handballs", "Handballs"),
    ("tackles", "Tackles"),
    ("brownlow", "Brownlow votes"),
)

_GROUND_STATUS_SQL = {
    "all": "1=1",
    "wins": "g.result = 'W'",
    "losses": "g.result = 'L'",
    "draws": "g.result = 'D'",
    "finals": "g.is_final = 1",
    "final_wins": "g.is_final = 1 AND g.result = 'W'",
}
_GROUND_METRIC_SQL = {
    "games": "COUNT(*)",
    "wins": "SUM(g.result = 'W')",
    "goals": "SUM(g.goals)",
    "behinds": "SUM(g.behinds)",
    "score": "SUM(COALESCE(g.goals, 0) * 6 + COALESCE(g.behinds, 0))",
    "marks": "SUM(g.marks)",
    "disposals": "SUM(g.disposals)",
    "kicks": "SUM(g.kicks)",
    "handballs": "SUM(g.handballs)",
    "tackles": "SUM(g.tackles)",
    "brownlow": "SUM(g.brownlow)",
}


def ground_performance(venue, ground_status, ground_metric, minimum):
    """Players reaching a cumulative performance threshold at one ground."""
    if ground_status not in _GROUND_STATUS_SQL:
        raise ValueError(f"Unknown ground status: {ground_status}")
    if ground_metric not in _GROUND_METRIC_SQL:
        raise ValueError(f"Unknown ground metric: {ground_metric}")
    ground = SCHEMA.canonical_venue(venue)
    expression = _GROUND_METRIC_SQL[ground_metric]
    stat_column = "goals" if ground_metric == "score" else ground_metric
    availability = ("" if ground_metric in {"games", "wins"} else
                    f"AND g.{stat_column} IS NOT NULL")
    return (f"""SELECT g.player_id FROM games g
                 WHERE g.venue = ? AND {_GROUND_STATUS_SQL[ground_status]}
                   {availability}
                 GROUP BY g.player_id
                 HAVING {expression} >= ?""", [ground, minimum])


# -------------------------------------------------------- AFL-specific
# A grand final is one nominated round, not simply the last post-season
# game, so these read `round` rather than the generic is_final flag.

def premiership_player():
    return ("""SELECT DISTINCT player_id FROM games
               WHERE UPPER(TRIM(round)) = 'GF' AND result = 'W'""", [])


def no_grand_finals():
    """Never played in a grand final."""
    return ("""SELECT player_id FROM players WHERE player_id NOT IN
               (SELECT player_id FROM games
                WHERE UPPER(TRIM(round)) = 'GF')""", [])


def played_a_grand_final():
    return ("""SELECT DISTINCT player_id FROM games
               WHERE UPPER(TRIM(round)) = 'GF'""", [])


def leading_goalkicker():
    """Led their club's goalkicking in at least one season."""
    return ("""SELECT DISTINCT player_id FROM season_goals
               WHERE is_club_leading = 1""", [])


def wooden_spoon_player():
    """Played for a club in a season it finished last."""
    return ("""SELECT DISTINCT g.player_id FROM games g
               JOIN team_seasons t
                 ON t.season = g.season AND t.club_now = g.club_now
               WHERE t.wooden_spoon = 1""", [])


# ----------------------------------------------------- draft constraints
# These read draft_links, and deliberately only trust rows that
# afl/link_draft.py resolved unambiguously. Ambiguous rows are excluded.

_LINKED = ("SELECT player_id FROM draft_links "
           "WHERE match_status IN ('unique','resolved') AND player_id IS NOT NULL")

_DRAFT_JOIN = """SELECT l.player_id FROM draft_links l JOIN draft d
                   ON d.rowid = l.draft_rowid
                 WHERE l.match_status IN ('unique','resolved')
                   AND l.player_id IS NOT NULL"""


def draft_pick_between(lo, hi):
    """National Draft selection between two pick numbers, inclusive.

    Draftguru restarts pick numbering for the Rookie, Pre-Season and
    Mid-Season drafts. Without the draft-type predicate, a normal "top-10
    draft pick" square silently includes all of those separate pick 1-10s.
    """
    return (f"""{_DRAFT_JOIN}
                  AND LOWER(d.draft_type) LIKE '%national%'
                  AND d.pick BETWEEN ? AND ?""", [lo, hi])


def draft_of_type(kind):
    """National / Rookie / Pre-Season / Mid-Season / Trade / Free Agency."""
    return (f"""{_DRAFT_JOIN}
                  AND LOWER(d.draft_type) LIKE ?""", [f"%{kind.lower()}%"])


def drafted_by(club):
    return (f"""{_DRAFT_JOIN}
                  AND LOWER(d.club) LIKE ?""", [f"%{club.lower()}%"])


def drafted_by_but_never_played_for(club):
    """Drafted by a club, never played a senior game for it."""
    return (f"""{_DRAFT_JOIN}
                  AND LOWER(d.club) LIKE ?
                  AND l.player_id NOT IN (
                      SELECT player_id FROM games
                      WHERE club_now = ? OR club_hist = ?)""",
            [f"%{club.lower()}%", club, club])


def recruited_from(source):
    """Original club / junior club substring, e.g. 'Glenelg', 'Oakleigh'."""
    return (f"""{_DRAFT_JOIN}
                  AND LOWER(d.original_club) LIKE ?""",
            [f"%{source.lower()}%"])


def drafted_between(lo, hi):
    return (f"""{_DRAFT_JOIN}
                  AND d.draft_year BETWEEN ? AND ?""", [lo, hi])


# ---------------------------------------------------------------- registry

BUILDERS = {
    "Played for club":            (played_for, ["club"]),
    "150+ / X+ career games":     (career_games_min, ["games"]),
    "Fewer than X career games":  (career_games_max, ["games"]),
    "X+ career goals":            (career_goals_min, ["goals"]),
    "X or fewer career goals":    (career_goals_max, ["goals"]),
    # Every statistic, at every scope a square can ask about. Before this
    # only goals had a career question, so "500+ career marks" could not
    # be expressed at all.
    "X+ of a stat in one game":   (stat_in_a_game, ["stat", "x"]),
    "X+ of a stat in one season": (season_stat_total_min, ["stat", "x"]),
    "X+ of a stat in a career":   (career_stat_total_min, ["stat", "x"]),
    "Season average of a stat":   (season_stat_average_min, ["stat", "avg"]),
    "Career average of a stat":   (career_stat_average_min,
                                   ["stat", "avg", "min_games"]),
    "X+ games with Y+ of a stat": (games_with_stat_min,
                                   ["stat", "y", "times"]),
    "X+ of a stat in a final":    (stat_in_a_final, ["stat", "x"]),
    "Finals average of a stat":   (finals_stat_average_min, ["stat", "avg"]),
    "Two stats in the same game": (two_stats_same_game,
                                   ["stat_a", "x_a", "stat_b", "x_b"]),
    "X+ goals at 2+ clubs":       (goals_at_multiple_clubs, ["goals", "clubs"]),
    "X+ games at 2+ clubs":       (games_at_multiple_clubs, ["games", "clubs"]),
    "Teammate of…":               (teammate_of_id, ["player_id"]),
    "No finals wins (played finals)": (no_finals_wins, []),
    "Never won a final":          (never_won_a_final, []),
    "Never played finals":        (never_played_finals, []),
    "Premiership player":         (premiership_player, []),
    "Played between seasons":     (played_in_season_range, ["from", "to"]),
    "Debuted between seasons":    (debuted_between, ["from", "to"]),
    "One-club player":            (one_club_player, []),
    "Played for X+ clubs":        (played_for_n_clubs, ["clubs"]),
    "Multi-club player":          (multi_club_player, []),
    "First career game for club": (debut_club, ["club"]),
    "Played at venue":            (played_at_venue, ["venue"]),
    "Ground performance":         (ground_performance,
                                    ["venue", "ground_status",
                                     "ground_metric", "x"]),
    "Won a final at venue":       (won_a_final_at, ["venue"]),
    "X+ finals games":            (finals_games_min, ["x"]),
    "Played in a final":          (played_in_a_final, []),
    "Won a final":                (won_a_final, []),
    "No grand finals":            (no_grand_finals, []),
    "Played a grand final":       (played_a_grand_final, []),
    "Goal average in finals":     (goal_average_in_finals, ["avg"]),
    "Club leading goalkicker":    (leading_goalkicker, []),
    "Wooden spoon season":        (wooden_spoon_player, []),
    "Draft pick between":         (draft_pick_between, ["from", "to"]),
    "Draft type (National/Rookie…)": (draft_of_type, ["kind"]),
    "Drafted by club":            (drafted_by, ["club"]),
    "Drafted by club, never played there": (drafted_by_but_never_played_for,
                                            ["club"]),
    "Recruited from…":            (recruited_from, ["source"]),
    "Drafted between years":      (drafted_between, ["from", "to"]),
}

#: Draft categories offered in the UI. Here rather than in app.py because
#: they are a property of the Draftguru layer this module reads, and no
#: other sport's draft has the same list.
DRAFT_TYPES = ("National", "Rookie", "Pre-Season", "Mid-Season",
               "Trade", "Free Agency", "Pre-Draft", "Post-Draft")

DRAFT_BUILDERS = {"Draft pick between", "Draft type (National/Rookie…)",
                  "Drafted by club", "Drafted by club, never played there",
                  "Recruited from…", "Drafted between years"}


def draft_available(con):
    """True when the Draftguru draft import and linker are loaded."""
    return core.have_tables(con, "draft", "draft_links")


# ------------------------------------------------- engine, bound to AFL
# Thin wrappers so existing callers keep their signatures. The bodies are
# in core.py and shared with every other sport.

def require_schema(con):
    """Fail loudly at startup if the database predates a migration."""
    core.require_schema(con, SCHEMA)
    # A connection-local empty table keeps optional captain constraints safe
    # before the separate captaincy dataset has been imported.
    ensure_captain_table(con)
    ensure_rising_star_table(con)
    ensure_brownlow_table(con)
    ensure_family_relationship_tables(con)


def solve(con, constraints, limit=25, order="obscurity"):
    """Intersect constraints and return ranked players."""
    ensure_captain_table(con)
    ensure_rising_star_table(con)
    ensure_brownlow_table(con)
    ensure_family_relationship_tables(con)
    return core.solve(con, constraints, SCHEMA, limit=limit, order=order)


def count(con, constraints):
    """How many players satisfy every constraint."""
    ensure_captain_table(con)
    ensure_rising_star_table(con)
    ensure_brownlow_table(con)
    ensure_family_relationship_tables(con)
    return core.count(con, constraints, SCHEMA)


def square(con, constraints, order="obscurity"):
    """Eligible count plus the single best answer, for a prefilled board."""
    ensure_captain_table(con)
    ensure_rising_star_table(con)
    ensure_brownlow_table(con)
    ensure_family_relationship_tables(con)
    return core.square(con, constraints, SCHEMA, order=order)


def to_standalone_sql(constraints, limit=25):
    """Render an intersection as a single pasteable SQL statement."""
    return core.to_standalone_sql(constraints, SCHEMA, limit=limit)


# Star-rating helpers, re-exported so the UI never imports core directly.
star_value = core.star_value
stars_text = core.stars_text
stars_html = core.stars_html
STAR_DISCLAIMER = core.STAR_DISCLAIMER


# Optional club-captain layer. The persistent table is created by
# afl/load_captains.py; a TEMP placeholder keeps parsing and old databases safe.
from .captains import (  # noqa: E402
    CAPTAIN_BUILDERS,
    captain_available,
    captain_count,
    ensure_captain_table,
    club_captain,
    captain_of,
    captain_between,
    captain_of_between,
)

BUILDERS.update(CAPTAIN_BUILDERS)
CAPTAIN_BUILDER_NAMES = set(CAPTAIN_BUILDERS)


# Optional Draftguru award/signing constraints. The module ships with the
# separate Draftguru update, so its absence must degrade to "no award
# builders" rather than break every import of this module.
try:
    from .awards import AWARD_BUILDERS, AWARD_SLUGS, awards_available  # noqa: E402
except ImportError:
    AWARD_BUILDERS: dict = {}
    AWARD_SLUGS: dict = {}

    def awards_available(_con) -> bool:
        return False

BUILDERS.update(AWARD_BUILDERS)
AWARD_BUILDER_NAMES = set(AWARD_BUILDERS)

# Optional full Brownlow voting results from AFL Tables. This is deliberately
# separate from Draftguru's winners-only award layer: a top-five finish is a
# season result, not another kind of award.
from .brownlow import (  # noqa: E402
    BROWNLOW_BUILDERS,
    brownlow_available,
    brownlow_count,
    ensure_brownlow_table,
)

BUILDERS.update(BROWNLOW_BUILDERS)
BROWNLOW_BUILDER_NAMES = set(BROWNLOW_BUILDERS)


def match_scores_available(con) -> bool:
    """True only when the AFL Tables played-match audit has no open gaps."""
    if not con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='afltables_match_scores'").fetchone():
        return False
    total, linked = con.execute("""
        SELECT COUNT(*),
               SUM(audit_status IN ('matched','partial_player_stats','score_only')
                   AND db_match_id IS NOT NULL)
        FROM afltables_match_scores
    """).fetchone()
    return bool(total and total == linked)


def match_scores_count(con) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM afltables_match_scores").fetchone()[0]


def player_index_available(con) -> bool:
    from .player_index_audit import player_index_available as available
    return available(con)


def player_index_count(con) -> int:
    from .player_index_audit import player_index_count as count
    return count(con)


def venue_profiles_available(con) -> bool:
    try:
        total, profiles = con.execute(
            "SELECT COUNT(*), COUNT(profile_url) FROM venue_summary").fetchone()
        return bool(total and total == profiles)
    except Exception:
        return False


def venue_profiles_count(con) -> int:
    return con.execute("SELECT COUNT(*) FROM venue_summary").fetchone()[0]


def extra_status(con) -> list[tuple[str, str]]:
    """Explain the difference between matches and player-game rows."""
    try:
        total, full, partial, score_only = con.execute("""
            SELECT COUNT(*), SUM(data_status='player_stats'),
                   SUM(data_status='partial_player_stats'),
                   SUM(data_status='score_only') FROM matches
        """).fetchone()
    except Exception:
        return []
    if not total:
        return []
    return [("Matches", f"{total:,} ({full or 0:,} complete; "
             f"{partial or 0:,} partial; {score_only or 0:,} score-only)")]

# Optional FootyWire Rising Star nomination layer.  The network fetcher is
# permission-gated; afl/load_rising_star.py links local CSV rows to players.
from .rising_star import (  # noqa: E402
    RISING_STAR_BUILDERS,
    ensure_rising_star_table,
    rising_star_available,
    rising_star_count,
    rising_star_nominee,
    rising_star_nominee_in,
    rising_star_nominee_between,
    rising_star_nominee_for,
    rising_star_nominee_for_between,
)

BUILDERS.update(RISING_STAR_BUILDERS)
RISING_STAR_BUILDER_NAMES = set(RISING_STAR_BUILDERS)


# Optional broad Wikipedia family layer. This is separate from the narrower
# father-son/father-daughter draft dataset in afl/family_draft.py.
from .family_relationships import (  # noqa: E402
    FAMILY_RELATIONSHIP_BUILDERS,
    ensure_family_relationship_tables,
    family_relationships_available,
    family_member_count,
    trusted_relationship_count,
    family_member,
    sibling_also_played,
    brother_also_played,
    parent_or_child_also_played,
    father_or_son_also_played,
    extended_family_also_played,
    same_listed_family_as,
    relative_played_for,
)

BUILDERS.update(FAMILY_RELATIONSHIP_BUILDERS)
FAMILY_RELATIONSHIP_BUILDER_NAMES = set(FAMILY_RELATIONSHIP_BUILDERS)


# Match-context constraints: margin, team score and crowd. The margin and
# result builders read `games` and always work; only the crowd builders
# need the optional all-games layer.
from .match_constraints import (  # noqa: E402
    MATCH_BUILDERS,
    CROWD_BUILDER_NAMES,
    MARQUEE_BUILDER_NAMES,
    DERBIES,
    DERBY_CHOICES,
    DERBY_LABELS,
    DERBY_MIN_GAMES,
    match_history_available,
    match_history_count,
    marquee_events,
    marquee_events_available,
    won_by_min,
    lost_by_min,
    won_by_max,
    played_in_a_draw,
    team_scored_min,
    crowd_min,
    crowd_max,
    crowd_min_in_final,
    derby_winning_record,
    derby_losing_record,
    derby_games_min,
    played_in_derby,
    marquee_event_winning_record,
    marquee_event_games_min,
    played_marquee_event,
)

BUILDERS.update(MATCH_BUILDERS)
MATCH_BUILDER_NAMES = set(MATCH_BUILDERS)

#: Builders gated on a source layer, as {builder: probe}. registry.Sport
#: .layers() reads this generically, so the marquee squares disappear on a
#: database the scraper has not tagged instead of returning nothing.
LAYER_BUILDERS = {
    **{name: "marquee_events_available" for name in MARQUEE_BUILDER_NAMES},
    **{name: "brownlow_available" for name in BROWNLOW_BUILDER_NAMES},
}
