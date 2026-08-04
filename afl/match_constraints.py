"""Player constraints that depend on what happened in the match.

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


MATCH_BUILDERS = {
    "Played in a win by X+ points":   (won_by_min, ["points"]),
    "Played in a loss by X+ points":  (lost_by_min, ["points"]),
    "Played in a win by X or fewer":  (won_by_max, ["points"]),
    "Team scored X+ points":          (team_scored_min, ["points"]),
    "Played in a drawn match":        (played_in_a_draw, []),
    "Played before a crowd of X+":    (crowd_min, ["people"]),
    "Played before a crowd of X or fewer": (crowd_max, ["people"]),
    "Played before a crowd of X+ in a final": (crowd_min_in_final, ["people"]),
}

#: Builders that need the optional all-games layer, so the UI can say why
#: they are unavailable rather than letting them fail on execution.
CROWD_BUILDER_NAMES = {
    "Played before a crowd of X+",
    "Played before a crowd of X or fewer",
    "Played before a crowd of X+ in a final",
}
