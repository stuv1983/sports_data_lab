"""afl/match_constraints.py -- AFL constraints about the match, not the player.

Everything above is about a player's own numbers. These are about the game
they were in: the size of the win, the size of the crowd, whether it was
drawn. The player still has to have played -- every builder returns
player_ids from `games` -- but the qualifying condition sits on the match.

Two different sources, deliberately:

* **Margin and result** come from `games` itself, which carries
  `points_for`, `points_against` and `result` for all 693,194 player-game
  rows with no nulls. Nothing is gained by routing these through the
  all-games tables, and doing so would make a constraint that works
  everywhere depend on a layer that might not be loaded.

* **Attendance** is the one thing only the all-games scrape has.
  `matches.attendance` is a TEXT column populated for 15,358 of 17,009
  matches; `club_match_sources.attendance` is a proper integer. So the
  crowd builders read the source table and are gated behind
  `match_history_available()`, degrading to unavailable rather than
  raising, like every other optional layer.

Attendance is recorded for 90.3% of matches; the rest are genuinely
unrecorded at source rather than a linkage failure. A crowd constraint
therefore answers "matches known to have drawn this many" and silently
excludes the unknown ones -- which is the honest reading, but means a
"smallest crowd" style square is a floor rather than a certainty.
"""

from __future__ import annotations

import sqlite3

SOURCE_TABLE = "club_match_sources"
TRUSTED_STATUSES = ("unique",)


def _tables(con: sqlite3.Connection) -> set[str]:
    return {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def match_history_available(con: sqlite3.Connection) -> bool:
    """True when the all-games rows are loaded and carry attendance."""
    if SOURCE_TABLE not in _tables(con):
        return False
    return bool(con.execute(
        f"SELECT 1 FROM {SOURCE_TABLE} WHERE attendance IS NOT NULL LIMIT 1"
    ).fetchone())


def match_history_count(con: sqlite3.Connection) -> int:
    if SOURCE_TABLE not in _tables(con):
        return 0
    return con.execute(
        f"SELECT COUNT(DISTINCT source_game_key) FROM {SOURCE_TABLE}"
    ).fetchone()[0]


# ------------------------------------------------------ margin and result
# From `games` directly: no optional layer, no join, full coverage.

def won_by_min(points):
    """Played in a win by `points` or more."""
    return ("SELECT DISTINCT player_id FROM games "
            "WHERE result = 'W' AND (points_for - points_against) >= ?",
            [points])


def lost_by_min(points):
    """Played in a loss by `points` or more."""
    return ("SELECT DISTINCT player_id FROM games "
            "WHERE result = 'L' AND (points_against - points_for) >= ?",
            [points])


def won_by_max(points):
    """Played in a win by `points` or fewer -- a close win.

    Inclusive of `points`, matching the career_score_max convention: a
    parser translating "won by less than 10" subtracts one itself, where
    the wording is visible.
    """
    return ("SELECT DISTINCT player_id FROM games "
            "WHERE result = 'W' AND (points_for - points_against) <= ?",
            [points])


def played_in_a_draw():
    return ("SELECT DISTINCT player_id FROM games WHERE result = 'D'", [])


def team_scored_min(points):
    """Played in a match where their own team scored `points` or more."""
    return ("SELECT DISTINCT player_id FROM games WHERE points_for >= ?",
            [points])


# ------------------------------------------------------------ attendance
# These need the all-games rows. `games.match_id` is fully populated and
# every match_id resolves, so the join is on match_id alone: attendance is
# a property of the match, identical on both of its rows, and DISTINCT
# collapses the two-row fan-out.

_CROWD_JOIN = f"""SELECT DISTINCT g.player_id FROM games g
                    JOIN {SOURCE_TABLE} s ON s.match_id = g.match_id
                  WHERE s.match_status IN ('unique')
                    AND s.attendance IS NOT NULL"""


def crowd_min(people):
    """Played in front of a crowd of `people` or more."""
    return (f"{_CROWD_JOIN} AND s.attendance >= ?", [people])


def crowd_max(people):
    """Played in front of a crowd of `people` or fewer.

    Matches with no recorded attendance are excluded rather than counted as
    a crowd of zero, so this is "known to have drawn this few".
    """
    return (f"{_CROWD_JOIN} AND s.attendance <= ?", [people])


def crowd_min_in_final(people):
    """A big crowd, at a final."""
    return (f"{_CROWD_JOIN} AND s.is_final = 1 AND s.attendance >= ?",
            [people])


# ------------------------------------------------------ derbies and events
#
# Two different ideas that answer the same shape of question -- "has this
# player won more of these than they have lost" -- from two different
# sources:
#
# * A **derby** is a fixed pair of clubs, so it needs no source at all
#   beyond `games`, which already carries club_now, match_id and result for
#   every player-game. Nothing is scraped and nothing can be missing: the
#   Western Derby is every Fremantle-West Coast match there has ever been.
#
# * A **marquee event** is a fixture identified by the day it is played on
#   rather than by who plays in it, so it cannot be derived from the teams.
#   afl/scrape_marquee_games.py tags those matches with `games.match_event`
#   from Wikipedia, and the builders reading that column are gated on
#   marquee_events_available() because a database built before the scraper
#   runs has the column but no values in it.

#: derby_key -> the two clubs, under the club_now names build_db.py writes.
#: Only fixtures the sport actually calls a derby, each one a same-city or
#: same-state pair: the Victorian rivalries are rivalries, not derbies, and
#: naming them here would be this file inventing a category.
DERBIES = {
    "western_derby": ("Western Derby", "Fremantle", "West Coast"),
    "showdown": ("Showdown", "Adelaide", "Port Adelaide"),
    "q_clash": ("QClash", "Brisbane Lions", "Gold Coast"),
    "sydney_derby": ("Sydney Derby", "Sydney", "GWS"),
}

#: derby_key -> label, for the axis picker.
DERBY_LABELS = {key: label for key, (label, _a, _b) in DERBIES.items()}
DERBY_CHOICES = list(DERBY_LABELS.items())

#: Appearances below this are a cameo rather than a derby record. Matches
#: constraints_mlb.RIVALRY_MIN_GAMES, which answers the same question.
DERBY_MIN_GAMES = 5

#: Matches in which both clubs played, found through match_id rather than
#: through `opponent`.
#:
#: `opponent` and `club_now` are two different vocabularies: `opponent`
#: carries the club's *historical* name, as club_hist does, while
#: `club_now` carries the current franchise. They coincide for most clubs
#: and diverge for exactly the ones a derby cares about -- GWS is
#: 'Greater Western Sydney' in `opponent`, so `opponent = 'GWS'` matches no
#: row at all and the Sydney Derby silently returned only GWS's half of it.
#: Intersecting match_ids keeps both sides in one vocabulary, so a club
#: rename can never drop a side. match_id is non-NULL for all 693,194 rows
#: and indexed, so this stays a sub-30ms lookup.
_DERBY_MATCHES = """match_id IN (
        SELECT match_id FROM games WHERE club_now = ?
        INTERSECT
        SELECT match_id FROM games WHERE club_now = ?
    )"""


def _derby_teams(derby):
    """The two clubs of a derby, as the bound parameters _DERBY_MATCHES
    wants."""
    try:
        _label, team_a, team_b = DERBIES[derby]
    except KeyError:
        raise ValueError(f"unknown derby: {derby!r}") from None
    return [team_a, team_b]


def derby_winning_record(derby):
    """Won more matches in this derby than they lost.

    Draws count as neither, so this is strictly wins > losses rather than
    a winning percentage -- "won more than they've lost" is the question
    as the sport asks it.
    """
    return (f"""
        SELECT player_id FROM games WHERE {_DERBY_MATCHES}
        GROUP BY player_id
        HAVING SUM(CASE WHEN result = 'W' THEN 1 ELSE 0 END)
             > SUM(CASE WHEN result = 'L' THEN 1 ELSE 0 END)
           AND COUNT(*) >= ?
    """, _derby_teams(derby) + [DERBY_MIN_GAMES])


def derby_losing_record(derby):
    """Lost more matches in this derby than they won."""
    return (f"""
        SELECT player_id FROM games WHERE {_DERBY_MATCHES}
        GROUP BY player_id
        HAVING SUM(CASE WHEN result = 'L' THEN 1 ELSE 0 END)
             > SUM(CASE WHEN result = 'W' THEN 1 ELSE 0 END)
           AND COUNT(*) >= ?
    """, _derby_teams(derby) + [DERBY_MIN_GAMES])


def derby_games_min(derby, games):
    """Played X+ matches in this derby, win or lose."""
    return (f"""
        SELECT player_id FROM games WHERE {_DERBY_MATCHES}
        GROUP BY player_id HAVING COUNT(*) >= ?
    """, _derby_teams(derby) + [games])


def played_in_derby(derby):
    """Played in this derby at all."""
    return (f"SELECT DISTINCT player_id FROM games WHERE {_DERBY_MATCHES}",
            _derby_teams(derby))


def derby_won(derby):
    """Won this derby at least once -- "SHOWDOWN WINNER" asks for a win,
    not the stronger more-wins-than-losses record."""
    return (f"""SELECT DISTINCT player_id FROM games
                WHERE {_DERBY_MATCHES} AND result = 'W'""",
            _derby_teams(derby))


def derby_stat_in_game(derby, stat, n):
    """Reached `n` of a statistic in a single game of this derby --
    "SHOWDOWN KICKED A GOAL", "SYDNEY DERBY 5+ TACKLES"."""
    import sports
    if stat not in sports.AFL_STATS:
        raise ValueError(f"unknown stat: {stat}")
    return (f"""SELECT DISTINCT player_id FROM games
                WHERE {_DERBY_MATCHES} AND {stat} >= ?""",
            _derby_teams(derby) + [n])


def marquee_events_available(con: sqlite3.Connection) -> bool:
    """True once afl/scrape_marquee_games.py has tagged any match.

    The column alone is not enough: build_db.py's to_sql drops and
    recreates `games`, so between a rebuild and the retag the column is
    absent entirely, and a database that has never been tagged has it
    present but empty.
    """
    if "games" not in _tables(con):
        return False
    columns = {row[1] for row in con.execute("PRAGMA table_info(games)")}
    if "match_event" not in columns:
        return False
    return bool(con.execute(
        "SELECT 1 FROM games WHERE match_event IS NOT NULL LIMIT 1").fetchone())


def marquee_events(con: sqlite3.Connection) -> list[str]:
    """Every event name actually tagged, for the axis picker.

    Read from the database rather than hardcoded, so a fourth event added
    to the scraper's config appears here with no change to this file.
    """
    if not marquee_events_available(con):
        return []
    return [row[0] for row in con.execute(
        "SELECT DISTINCT match_event FROM games "
        "WHERE match_event IS NOT NULL ORDER BY match_event")]


def played_marquee_event(event):
    """Played in this marquee fixture -- an Anzac Day match, a Dreamtime."""
    return ("SELECT DISTINCT player_id FROM games WHERE match_event = ?",
            [event])


def marquee_event_winning_record(event):
    """Won more of this marquee fixture than they lost."""
    return ("""
        SELECT player_id FROM games WHERE match_event = ?
        GROUP BY player_id
        HAVING SUM(CASE WHEN result = 'W' THEN 1 ELSE 0 END)
             > SUM(CASE WHEN result = 'L' THEN 1 ELSE 0 END)
           AND COUNT(*) >= ?
    """, [event, DERBY_MIN_GAMES])


def marquee_event_games_min(event, games):
    """Played X+ of this marquee fixture."""
    return ("""
        SELECT player_id FROM games WHERE match_event = ?
        GROUP BY player_id HAVING COUNT(*) >= ?
    """, [event, games])


def marquee_event_won(event):
    """Played for the winning side of this marquee fixture at least once."""
    return ("""SELECT DISTINCT player_id FROM games
               WHERE match_event = ? AND result = 'W'""", [event])


def marquee_event_played_since(event, season):
    """Played this fixture from a given season on.

    The Big Freeze is the King's Birthday match, but only since 2015 --
    the fixture existed for decades before the name did, and a square
    asking for the Big Freeze means the frozen era.
    """
    return ("""SELECT DISTINCT player_id FROM games
               WHERE match_event = ? AND season >= ?""", [event, season])


MATCH_BUILDERS = {
    "Played in a win by X+ points":   (won_by_min, ["points"]),
    "Played in a loss by X+ points":  (lost_by_min, ["points"]),
    "Played in a win by X or fewer":  (won_by_max, ["points"]),
    "Team scored X+ points":          (team_scored_min, ["points"]),
    "Played in a drawn match":        (played_in_a_draw, []),
    "Played before a crowd of X+":    (crowd_min, ["people"]),
    "Played before a crowd of X or fewer": (crowd_max, ["people"]),
    "Played before a crowd of X+ in a final": (crowd_min_in_final, ["people"]),
    "Winning record in a derby":      (derby_winning_record, ["derby"]),
    "Losing record in a derby":       (derby_losing_record, ["derby"]),
    "X+ games in a derby":            (derby_games_min, ["derby", "games"]),
    "Played in a derby":              (played_in_derby, ["derby"]),
    "Won a derby":                    (derby_won, ["derby"]),
    "X+ of a stat in a derby game":   (derby_stat_in_game,
                                       ["derby", "stat", "x"]),
    "Winning record in a marquee match":
        (marquee_event_winning_record, ["event"]),
    "X+ marquee matches":             (marquee_event_games_min,
                                       ["event", "games"]),
    "Played in a marquee match":      (played_marquee_event, ["event"]),
    "Won a marquee match":            (marquee_event_won, ["event"]),
}

#: Builders needing the scraped `match_event` tags. The derby builders are
#: deliberately absent: they read `games` alone and always work.
MARQUEE_BUILDER_NAMES = {
    "Winning record in a marquee match",
    "X+ marquee matches",
    "Played in a marquee match",
    "Won a marquee match",
}

#: Builders that need the optional all-games layer, so the UI can say why
#: they are unavailable rather than letting them fail on execution.
CROWD_BUILDER_NAMES = {
    "Played before a crowd of X+",
    "Played before a crowd of X or fewer",
    "Played before a crowd of X+ in a final",
}
