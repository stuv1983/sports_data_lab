"""
dedupe_games.py -- Resolve duplicate player-game rows before career totals.

WHY THIS EXISTS
---------------
The AFL Tables source occasionally emits two rows for the same player on the
same date. They are not always identical: for two St Kilda players both named
Jim Stewart (IDs 5230 and 5685), the 1909-07-03 match against Essendon carries
*both* players' `career_game_no`/`birth_year_est` pairs under *both* IDs. The
per-ID statistics are correct; only the identity attributes are crossed.

Every row looks individually plausible -- valid ID, date, club and match -- so
nothing downstream notices. But `career_games` is `groupby(player_id).size()`,
so each duplicate silently inflates a career total by one, and from there the
error spreads into obscurity ranking, career-games search filters and the row
counts shown in the status panel.

HOW IT RESOLVES THEM
--------------------
`career_game_no` is monotonic within a player: appearance N is always followed
by N+1. That makes the surrounding unambiguous appearances a bound, and the
bound is usually decisive. For Jim Stewart 5230 the neighbouring rows number 67
and 69, so of the two candidates (68 and 1) only 68 can be real; for 5685 the
next appearance is number 2, so only candidate 1 fits.

Exact duplicates -- every field identical -- need no sequence evidence and
collapse to one row.

Anything else is left UNRESOLVED rather than guessed. A wrong pick here is
worse than a loud failure, because it produces a database that looks clean.
"""

from __future__ import annotations

from collections import defaultdict


class Duplicate:
    """One unresolved duplicate key, for reporting and the audit table."""

    __slots__ = ("player_id", "date", "candidates", "reason")

    def __init__(self, player_id, date, candidates, reason):
        self.player_id = player_id
        self.date = date
        self.candidates = candidates      # list of career_game_no values
        self.reason = reason

    def __repr__(self):                                   # pragma: no cover
        return (f"Duplicate(player_id={self.player_id!r}, date={self.date!r}, "
                f"candidates={self.candidates!r}, reason={self.reason!r})")


def _sequence_bounds(anchors, date):
    """(previous, next) career_game_no around `date` among unambiguous rows.

    `anchors` is a date-sorted list of (date, career_game_no). Returns None
    for either side when there is no appearance on that side.
    """
    previous = next_ = None
    for anchor_date, number in anchors:
        if anchor_date < date:
            previous = number
        elif anchor_date > date:
            next_ = number
            break
    return previous, next_


def _fits(number, previous, next_):
    """True when `number` can occupy the gap between two known appearances."""
    if number is None:
        return False
    if previous is not None and number <= previous:
        return False
    if next_ is not None and number >= next_:
        return False
    # With no previous appearance this is the player's first, so it must be
    # game 1 -- the source numbers careers from one.
    if previous is None and number != 1:
        return False
    return True


def resolve_player(rows):
    """
    Pick one row per (player, date) for a single player's appearances.

    `rows` is a sequence of dicts, each needing at least ``date``,
    ``career_game_no`` and ``ref`` (an opaque handle -- a DataFrame index or a
    SQLite rowid -- returned untouched). ``fields`` may carry a hashable
    signature of the whole row; when present it is used to detect exact
    duplicates.

    Returns ``(drop, unresolved)``: refs the caller should delete, and
    `Duplicate` records for keys no evidence could settle. Rows that are
    already unique are simply absent from both.
    """
    by_date = defaultdict(list)
    for row in rows:
        by_date[row["date"]].append(row)

    anchors = sorted((date, group[0]["career_game_no"])
                     for date, group in by_date.items() if len(group) == 1)

    drop, unresolved = [], []
    for date, group in by_date.items():
        if len(group) == 1:
            continue

        signatures = {row.get("fields") for row in group}
        if len(signatures) == 1 and None not in signatures:
            drop.extend(row["ref"] for row in group[1:])
            continue

        previous, next_ = _sequence_bounds(anchors, date)
        fitting = [row for row in group
                   if _fits(row["career_game_no"], previous, next_)]

        if len(fitting) == 1:
            keep = fitting[0]["ref"]
            drop.extend(row["ref"] for row in group if row["ref"] != keep)
            continue

        unresolved.append(Duplicate(
            player_id=group[0].get("player_id"),
            date=date,
            candidates=[row["career_game_no"] for row in group],
            reason=("no candidate fits the career sequence"
                    if not fitting else
                    "more than one candidate fits the career sequence"),
        ))

    return drop, unresolved


def resolve(rows):
    """`resolve_player` across a mixed stream of players.

    Rows must additionally carry ``player_id``. Returns the same
    ``(drop, unresolved)`` pair.
    """
    by_player = defaultdict(list)
    for row in rows:
        by_player[row["player_id"]].append(row)

    drop, unresolved = [], []
    for player_id, player_rows in by_player.items():
        # Cheap skip: nothing to do unless this player has a repeated date.
        dates = [row["date"] for row in player_rows]
        if len(set(dates)) == len(dates):
            continue
        player_drop, player_unresolved = resolve_player(player_rows)
        drop.extend(player_drop)
        unresolved.extend(player_unresolved)
    return drop, unresolved
