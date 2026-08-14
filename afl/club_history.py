"""Club-level history derived from the all-games source rows.

Step 4 of the build plan. This is a query layer over `club_match_sources`,
which holds one row per club per match: 34,018 observations across
1897-2026, every one of them carrying result, margin, scores by quarter,
venue, attendance and the club's running season record as at that match.

Nothing above `club_match_sources` currently reads it. `match_details` is
one row per match and drops anything that failed to link, so a club-history
layer built on it would silently under-report by however much was dropped.
This module reads the source rows directly.

Scope note. Everything here is about clubs, not players, which is why it is
not in afl/constraints.py: none of it returns a player_id predicate. A player-
level bridge onto these matches belongs in step 5's match_queries.py.


Three source facts that shape the API
-------------------------------------

**Opponents are resolved by self-join, never by name.** Every one of the
17,009 game keys carries exactly two rows, so the opponent's club_id is
simply the other row's `source_club_id`. `opponent_raw` is free text and is
never matched against.

**Finals have no home side.** `team_position` is 'H', 'A' or 'F', and all
1,436 finals rows are 'F'. Any home/away split must therefore say whether
finals are in or out rather than folding 'F' into either side; see `scope`
below.

**The source's own running totals are not reliable, so records are
aggregated.** `season_wins_after` / `_draws_after` / `_losses_after` look
like a free accumulation, but they disagree with the match results in the
1924 finals for both Essendon and Richmond -- that season's finals were a
round-robin, and the published running totals do not survive it (Richmond
reads 10, then 12, then 10, then 11). The per-match `result` values are
correct in both cases. So this module derives records by aggregating
`result`, and treats the running totals as something to reconcile against,
not something to trust. `reconcile()` reports both classes of disagreement.


Trust boundary
--------------

Default is `match_status='unique'`. As at the step 0.3 audit every one of
the 34,018 rows is 'unique' and nothing is excluded, but that is a property
of today's data rather than a guarantee: a rescrape can introduce
unmatched, ambiguous or score_mismatch rows at any time. Every function
therefore filters, and `excluded(con)` reports what the filter removed so a
club's all-time record never silently differs from the published one
without saying why. Pass `include_unlinked=True` to see everything.


Return values are lists of frozen dataclasses -- plain stdlib, so this
module stays importable and testable without pandas or streamlit. A caller
that wants a table can do `pd.DataFrame(r.__dict__ for r in rows)`.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from dataclasses import dataclass
from typing import Literal

SOURCE_TABLE = "club_match_sources"

#: Link outcomes the layer answers from by default.
TRUSTED_STATUSES = ("unique",)

#: What counts as a match for a given question. Finals are a separate
#: category rather than a flag, because 'F' rows have no home side and
#: folding them into a home/away split invents one.
Scope = Literal["all", "home_and_away", "finals"]
SCOPES: dict[str, str] = {
    "all": "",
    "home_and_away": "is_final = 0",
    "finals": "is_final = 1",
}


# --------------------------------------------------------------- results

@dataclass(frozen=True)
class SeasonRecord:
    club_id: str
    season: int
    played: int
    wins: int
    draws: int
    losses: int
    points_for: int
    points_against: int

    @property
    def percentage(self) -> float | None:
        """Points for as a percentage of points against, the AFL measure."""
        if not self.points_against:
            return None
        return 100.0 * self.points_for / self.points_against

    @property
    def premiership_points(self) -> int:
        return 4 * self.wins + 2 * self.draws


@dataclass(frozen=True)
class Streak:
    club_id: str
    kind: str                   # winning | losing | unbeaten
    length: int
    from_season: int
    to_season: int
    from_date: str
    to_date: str
    spans_seasons: bool


@dataclass(frozen=True)
class HeadToHead:
    club_id: str
    opponent_id: str
    played: int
    wins: int
    draws: int
    losses: int
    points_for: int
    points_against: int
    first_season: int
    last_season: int


@dataclass(frozen=True)
class SplitRecord:
    club_id: str
    split: str                  # home | away | finals
    played: int
    wins: int
    draws: int
    losses: int
    points_for: int
    points_against: int


@dataclass(frozen=True)
class VenueRecord:
    club_id: str
    venue: str
    venue_raw: str
    played: int
    wins: int
    draws: int
    losses: int
    points_for: int
    points_against: int


@dataclass(frozen=True)
class MarginRecord:
    club_id: str
    opponent_id: str
    season: int
    round: str
    match_date: str
    venue: str
    is_final: bool
    points_for: int
    points_against: int
    margin: int


@dataclass(frozen=True)
class CrowdRecord:
    club_id: str
    opponent_id: str
    season: int
    round: str
    match_date: str
    venue: str
    is_final: bool
    attendance: int


@dataclass(frozen=True)
class CrowdAverage:
    key: str                    # club_id, venue, season -- whatever grouped
    matches: int
    with_attendance: int
    average: float | None
    total: int | None


# ----------------------------------------------------------- foundations

def _tables(con: sqlite3.Connection) -> set[str]:
    return {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def club_history_available(con: sqlite3.Connection) -> bool:
    """Zero-cost probe, matching the other optional layers' contract."""
    if SOURCE_TABLE not in _tables(con):
        return False
    return bool(con.execute(f"SELECT 1 FROM {SOURCE_TABLE} LIMIT 1").fetchone())


def _trust(include_unlinked: bool) -> tuple[str, list]:
    if include_unlinked:
        return "", []
    marks = ",".join("?" for _ in TRUSTED_STATUSES)
    return f"match_status IN ({marks})", list(TRUSTED_STATUSES)


def _scope(scope: Scope) -> str:
    if scope not in SCOPES:
        raise ValueError(
            f"unknown scope {scope!r}; expected one of {sorted(SCOPES)}")
    return SCOPES[scope]


def _where(*clauses: tuple[str, list]) -> tuple[str, list]:
    """Assemble non-empty clauses into a WHERE, keeping params aligned."""
    parts, params = [], []
    for sql, values in clauses:
        if sql:
            parts.append(sql)
            params.extend(values)
    return (" WHERE " + " AND ".join(parts) if parts else ""), params


def excluded(con: sqlite3.Connection) -> dict[str, int]:
    """Rows the default trust boundary removes, by link outcome.

    Report this alongside any total. An empty dict means the trusted view
    and the complete view are the same thing.
    """
    if SOURCE_TABLE not in _tables(con):
        return {}
    marks = ",".join("?" for _ in TRUSTED_STATUSES)
    rows = con.execute(
        f"SELECT match_status, COUNT(*) FROM {SOURCE_TABLE} "
        f"WHERE match_status NOT IN ({marks}) GROUP BY match_status",
        list(TRUSTED_STATUSES)).fetchall()
    return {status: n for status, n in rows}


def coverage(con: sqlite3.Connection) -> dict:
    """Season range, row counts and attendance coverage, for a status line."""
    total, lo, hi, matches, crowds = con.execute(
        f"SELECT COUNT(*), MIN(season), MAX(season), "
        f"       COUNT(DISTINCT source_game_key), "
        f"       SUM(attendance IS NOT NULL) FROM {SOURCE_TABLE}"
    ).fetchone()
    return {
        "observations": total,
        "matches": matches,
        "season_from": lo,
        "season_to": hi,
        "with_attendance": crowds or 0,
        "excluded": excluded(con),
    }


#: games.club_now values with no `clubs` row, mapped to the club_id the
#: all-games scrape uses. `clubs` is populated from the current-club source
#: and so covers eighteen; the source rows also carry Fitzroy, Brisbane
#: Bears and University, which between them account for 44,002 player-game
#: rows. Joining through `clubs` alone silently drops all of them.
#:
#: This is a stopgap for exactly the gap step 2 exists to close. When
#: `club_aliases` lands, read it here and delete this dict -- keeping the
#: literals for one release, per the plan's migration note.
_HISTORICAL_CLUB_IDS = {
    "Fitzroy": "fitzroy",
    "Brisbane Bears": "brisbane_bears",
    "University": "university",
}


def club_id_by_db_name(con: sqlite3.Connection) -> dict[str, str]:
    """Map a `games.club_now` value to the club_id used by the source rows.

    Built from `clubs` where possible so a renaming there flows through,
    with the historical identities filled in from the literals above. Only
    names the source table actually knows are returned, so a mapping can
    never point at a club_id with no matches behind it.
    """
    known = {r[0] for r in con.execute(
        f"SELECT DISTINCT source_club_id FROM {SOURCE_TABLE}")}
    mapping = {}
    if "clubs" in _tables(con):
        for db_name, club_id in con.execute(
                "SELECT db_club_now, club_id FROM clubs "
                "WHERE db_club_now IS NOT NULL"):
            if club_id in known:
                mapping[db_name] = club_id
    for db_name, club_id in _HISTORICAL_CLUB_IDS.items():
        if club_id in known:
            mapping.setdefault(db_name, club_id)
    return mapping


def clubs_with_history(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute(
        f"SELECT DISTINCT source_club_id FROM {SOURCE_TABLE} "
        f"ORDER BY source_club_id")]


def source_club_id(con: sqlite3.Connection, club_id: str,
                   club_name: str | None = None) -> str | None:
    """The `source_club_id` a `clubs` row's matches are filed under.

    Two conventions are in use and both are legitimate, so a caller holding
    a `clubs.club_id` cannot simply pass it through. The AFL scrape files
    matches under a slug ('adelaide'), which is also that club's
    `clubs.club_id`. The MLB and NFL loaders file them under the club's
    name ('Arizona Diamondbacks', 'Oakland Raiders'), because their sources
    identify teams by opaque codes and the name is what keeps a franchise's
    earlier identity distinct from the one it relocated into.

    Passing the slug to a name-keyed table matches nothing, and every
    aggregate then reads as a real 0-0-0 rather than as an absence -- which
    is what the Club Explorer's Past games tab was showing for every MLB
    club. Returning None lets a caller say so instead.
    """
    known = {r[0] for r in con.execute(
        f"SELECT DISTINCT source_club_id FROM {SOURCE_TABLE}")}
    for candidate in (club_id, club_name):
        if candidate and candidate in known:
            return candidate
    return None


def _canonical_venue(sport, raw: str) -> str:
    """Best-effort venue name.

    Venue normalisation proper is step 6. Until then this applies the
    partial alias map on the sport schema and otherwise passes the raw
    value through unchanged, so an unmapped venue stays visibly unmapped
    rather than being bucketed into something it is not.
    """
    if sport is None or raw is None:
        return raw
    return sport.schema.canonical_venue(raw)


# ------------------------------------------------------- season records

_AGG = ("COUNT(*), SUM(result='W'), SUM(result='D'), SUM(result='L'), "
        "SUM(points_for), SUM(points_against)")


def season_records(con: sqlite3.Connection, club_id: str | None = None,
                   season: int | None = None, scope: Scope = "home_and_away",
                   include_unlinked: bool = False) -> list[SeasonRecord]:
    """W/D/L, points for/against and percentage per club-season.

    Defaults to `home_and_away`, which is what a ladder and a published
    season record mean. Pass scope='all' to include finals.
    """
    where, params = _where(
        (_scope(scope), []),
        ("source_club_id = ?" if club_id else "", [club_id] if club_id else []),
        ("season = ?" if season is not None else "",
         [season] if season is not None else []),
        _trust(include_unlinked),
    )
    rows = con.execute(
        f"SELECT source_club_id, season, {_AGG} FROM {SOURCE_TABLE}{where} "
        f"GROUP BY source_club_id, season ORDER BY season, source_club_id",
        params)
    return [SeasonRecord(*row) for row in rows]


# --------------------------------------------------------------- streaks

_STREAK_KINDS = {
    "winning": lambda r: r == "W",
    "losing": lambda r: r == "L",
    "unbeaten": lambda r: r in ("W", "D"),
    "winless": lambda r: r in ("L", "D"),
}


def streaks(con: sqlite3.Connection, club_id: str | None = None,
            kind: str = "winning", within_season: bool = False,
            scope: Scope = "all", limit: int = 10,
            include_unlinked: bool = False) -> list[Streak]:
    """Longest runs of a result type, ordered longest first.

    `within_season=True` breaks a run at a season boundary; the default
    lets a run carry across one, which is how the record books count them.

    Computed in Python over date-ordered rows rather than in SQL. Fewer
    than 35,000 rows are involved, and a gaps-and-islands window query for
    something this order-dependent trades clarity for nothing measurable.
    """
    if kind not in _STREAK_KINDS:
        raise ValueError(
            f"unknown streak kind {kind!r}; expected one of "
            f"{sorted(_STREAK_KINDS)}")
    qualifies = _STREAK_KINDS[kind]

    where, params = _where(
        (_scope(scope), []),
        ("source_club_id = ?" if club_id else "", [club_id] if club_id else []),
        _trust(include_unlinked),
    )
    rows = con.execute(
        f"SELECT source_club_id, season, match_date, result FROM {SOURCE_TABLE}"
        f"{where} ORDER BY source_club_id, match_date", params).fetchall()

    found: list[Streak] = []
    run: list[tuple] = []
    current_club = None

    def flush() -> None:
        if not run:
            return
        found.append(Streak(
            club_id=current_club, kind=kind, length=len(run),
            from_season=run[0][1], to_season=run[-1][1],
            from_date=run[0][2], to_date=run[-1][2],
            spans_seasons=run[0][1] != run[-1][1],
        ))

    previous_season = None
    for row in rows:
        club, season, _date, result = row
        broken = (club != current_club
                  or not qualifies(result)
                  or (within_season and season != previous_season))
        if broken:
            flush()
            run = []
        if qualifies(result):
            run.append(row)
        current_club, previous_season = club, season
    flush()

    found.sort(key=lambda s: (-s.length, s.club_id, s.from_date))
    return found[:limit] if limit else found


# ---------------------------------------------------------- head to head

def head_to_head(con: sqlite3.Connection, club_id: str,
                 opponent_id: str | None = None, scope: Scope = "all",
                 season_from: int | None = None, season_to: int | None = None,
                 venue: str | None = None,
                 include_unlinked: bool = False) -> list[HeadToHead]:
    """Record against one opponent, or against every opponent.

    The opponent comes from the other row of the same game key, so this
    never depends on `opponent_raw` matching a club name.
    """
    trust_sql, trust_params = _trust(include_unlinked)
    where, params = _where(
        ("a.source_club_id = ?", [club_id]),
        ("b.source_club_id = ?" if opponent_id else "",
         [opponent_id] if opponent_id else []),
        (f"a.{_scope(scope)}" if _scope(scope) else "", []),
        ("a.season >= ?" if season_from is not None else "",
         [season_from] if season_from is not None else []),
        ("a.season <= ?" if season_to is not None else "",
         [season_to] if season_to is not None else []),
        ("a.venue_raw = ?" if venue else "", [venue] if venue else []),
        # Both sides are filtered. The two rows of a match normally share a
        # link outcome, but a half-trusted match would otherwise contribute
        # to one club's record and not the other's.
        (f"a.{trust_sql}" if trust_sql else "", trust_params),
        (f"b.{trust_sql}" if trust_sql else "", list(trust_params)),
    )
    rows = con.execute(
        f"""SELECT a.source_club_id, b.source_club_id,
                   COUNT(*), SUM(a.result='W'), SUM(a.result='D'),
                   SUM(a.result='L'), SUM(a.points_for), SUM(a.points_against),
                   MIN(a.season), MAX(a.season)
            FROM {SOURCE_TABLE} a
            JOIN {SOURCE_TABLE} b
              ON b.source_game_key = a.source_game_key
             AND b.source_club_id <> a.source_club_id
            {where}
            GROUP BY a.source_club_id, b.source_club_id
            ORDER BY COUNT(*) DESC, b.source_club_id""", params)
    return [HeadToHead(*row) for row in rows]


# ----------------------------------------------------------- home / away

def home_away_splits(con: sqlite3.Connection, club_id: str,
                     include_unlinked: bool = False) -> list[SplitRecord]:
    """Home, away and finals as three separate records.

    Finals are their own split, not folded into either side. Every finals
    row is `team_position='F'` because the source records no home side for
    them, so assigning one would be an invention.
    """
    where, params = _where(
        ("source_club_id = ?", [club_id]),
        _trust(include_unlinked),
    )
    rows = con.execute(
        f"SELECT team_position, {_AGG} FROM {SOURCE_TABLE}{where} "
        f"GROUP BY team_position", params).fetchall()
    label = {"H": "home", "A": "away", "F": "finals"}
    out = [SplitRecord(club_id, label.get(pos, pos), *rest)
           for pos, *rest in rows]
    out.sort(key=lambda r: ("home", "away", "finals").index(r.split)
             if r.split in ("home", "away", "finals") else 9)
    return out


# -------------------------------------------------------- venue records

def venue_records(con: sqlite3.Connection, club_id: str | None = None,
                  scope: Scope = "all", sport=None, min_played: int = 1,
                  include_unlinked: bool = False) -> list[VenueRecord]:
    """Record per club per venue.

    `venue_raw` is free text with 52 distinct values in the current data,
    already close to canonical. Passing a `sport` applies its partial alias
    map so 'Marvel Stadium' and 'Docklands' aggregate together; without
    one, raw values are grouped as-is. Both the canonical and the raw name
    are returned so an unmapped value is visible rather than silent. Step 6
    replaces this with a real venue table.
    """
    where, params = _where(
        (_scope(scope), []),
        ("source_club_id = ?" if club_id else "", [club_id] if club_id else []),
        _trust(include_unlinked),
    )
    rows = con.execute(
        f"SELECT source_club_id, venue_raw, {_AGG} FROM {SOURCE_TABLE}{where} "
        f"GROUP BY source_club_id, venue_raw", params).fetchall()

    merged: dict[tuple[str, str], list] = {}
    raws: dict[tuple[str, str], set] = {}
    for club, raw, played, w, d, l, pf, pa in rows:
        canon = _canonical_venue(sport, raw)
        key = (club, canon)
        acc = merged.setdefault(key, [0, 0, 0, 0, 0, 0])
        for i, value in enumerate((played, w, d, l, pf, pa)):
            acc[i] += value or 0
        raws.setdefault(key, set()).add(raw)

    out = [VenueRecord(club, canon, " / ".join(sorted(raws[(club, canon)])),
                       *acc)
           for (club, canon), acc in merged.items() if acc[0] >= min_played]
    out.sort(key=lambda r: (r.club_id, -r.played))
    return out


def unmapped_venues(con: sqlite3.Connection, sport) -> list[tuple[str, int]]:
    """Venue values the alias map does not recognise, with row counts.

    An unmapped venue must be visibly unmapped. This is the list step 6
    hand-resolves.
    """
    aliases = sport.schema.venue_aliases
    rows = con.execute(
        f"SELECT venue_raw, COUNT(*) FROM {SOURCE_TABLE} "
        f"GROUP BY venue_raw ORDER BY COUNT(*) DESC").fetchall()
    known = set(aliases.values())
    return [(raw, n) for raw, n in rows
            if str(raw).strip().lower() not in aliases and raw not in known]


# -------------------------------------------------------------- margins

#: One row per match rather than one per club, for match-level listings
#: with no club filter. Every match contributes two rows to the source
#: table, so a bare "largest crowds" list would otherwise show the 1970
#: Grand Final twice, once from each side. The home side represents an
#: ordinary match; finals have no home side, so the alphabetically-first
#: club_id represents those -- an arbitrary but deterministic choice, and
#: only a choice of which row to display, not of which match qualifies.
_ONE_PER_MATCH = ("(a.team_position = 'H' OR (a.team_position = 'F' "
                  "AND a.source_club_id < b.source_club_id))")


def _match_rows(con, select: str, where: str, params, order: str, limit: int):
    return con.execute(
        f"""SELECT a.source_club_id, b.source_club_id, a.season, a.round,
                   a.match_date, a.venue_raw, a.is_final, {select}
            FROM {SOURCE_TABLE} a
            JOIN {SOURCE_TABLE} b
              ON b.source_game_key = a.source_game_key
             AND b.source_club_id <> a.source_club_id
            {where} {order} LIMIT ?""", [*params, limit]).fetchall()


def margins(con: sqlite3.Connection, club_id: str | None = None,
            kind: str = "win", scope: Scope = "all",
            season: int | None = None, limit: int = 10,
            include_unlinked: bool = False) -> list[MarginRecord]:
    """Biggest wins or biggest losses.

    `margin` is signed from the source club's point of view, so a win is
    positive and a loss negative; ordering differs accordingly rather than
    taking an absolute value, which would mix the two.
    """
    if kind not in ("win", "loss"):
        raise ValueError(f"kind must be 'win' or 'loss', got {kind!r}")
    result = "W" if kind == "win" else "L"
    order = "ORDER BY a.margin DESC" if kind == "win" else "ORDER BY a.margin ASC"

    trust_sql, trust_params = _trust(include_unlinked)
    where, params = _where(
        ("a.result = ?", [result]),
        (f"a.{_scope(scope)}" if _scope(scope) else "", []),
        ("a.source_club_id = ?" if club_id else "", [club_id] if club_id else []),
        ("a.season = ?" if season is not None else "",
         [season] if season is not None else []),
        (f"a.{trust_sql}" if trust_sql else "", trust_params),
    )
    rows = _match_rows(con, "a.points_for, a.points_against, a.margin",
                       where, params, order, limit)
    return [MarginRecord(c, o, s, rd, dt, v, bool(f), pf, pa, m)
            for c, o, s, rd, dt, v, f, pf, pa, m in rows]


# --------------------------------------------------------------- crowds

def crowds(con: sqlite3.Connection, club_id: str | None = None,
           kind: str = "largest", scope: Scope = "all",
           season: int | None = None, limit: int = 10,
           include_unlinked: bool = False) -> list[CrowdRecord]:
    """Biggest or smallest recorded crowds.

    Rows with no attendance are excluded rather than treated as a crowd of
    zero, which would otherwise take every 'smallest crowd' place. 90.3% of
    observations carry a figure; the remainder are genuinely unrecorded at
    source, not a linkage failure.

    Without a `club_id` this lists matches, so it returns one row per match
    rather than one per club. With a `club_id` it lists that club's
    matches, and the club side is already the only one selected.
    """
    if kind not in ("largest", "smallest"):
        raise ValueError(
            f"kind must be 'largest' or 'smallest', got {kind!r}")
    order = ("ORDER BY a.attendance DESC" if kind == "largest"
             else "ORDER BY a.attendance ASC")

    trust_sql, trust_params = _trust(include_unlinked)
    where, params = _where(
        ("a.attendance IS NOT NULL", []),
        ("" if club_id else _ONE_PER_MATCH, []),
        (f"a.{_scope(scope)}" if _scope(scope) else "", []),
        ("a.source_club_id = ?" if club_id else "", [club_id] if club_id else []),
        ("a.season = ?" if season is not None else "",
         [season] if season is not None else []),
        (f"a.{trust_sql}" if trust_sql else "", trust_params),
    )
    rows = _match_rows(con, "a.attendance", where, params, order, limit)
    return [CrowdRecord(c, o, s, rd, dt, v, bool(f), att)
            for c, o, s, rd, dt, v, f, att in rows]


_CROWD_KEYS = {"club": "source_club_id", "venue": "venue_raw",
               "season": "season", "opponent": None}


def crowd_averages(con: sqlite3.Connection, by: str = "club",
                   club_id: str | None = None, scope: Scope = "all",
                   include_unlinked: bool = False) -> list[CrowdAverage]:
    """Average attendance grouped by club, venue, season or opponent.

    `matches` counts every match in the group and `with_attendance` counts
    those carrying a figure, so a thin average is visible as thin rather
    than passing as a full one.
    """
    if by not in _CROWD_KEYS:
        raise ValueError(
            f"unknown grouping {by!r}; expected one of {sorted(_CROWD_KEYS)}")

    trust_sql, trust_params = _trust(include_unlinked)
    if by == "opponent":
        where, params = _where(
            ("a.source_club_id = ?" if club_id else "",
             [club_id] if club_id else []),
            (f"a.{_scope(scope)}" if _scope(scope) else "", []),
            (f"a.{trust_sql}" if trust_sql else "", trust_params),
        )
        rows = con.execute(
            f"""SELECT b.source_club_id, COUNT(*),
                       SUM(a.attendance IS NOT NULL),
                       AVG(a.attendance), SUM(a.attendance)
                FROM {SOURCE_TABLE} a
                JOIN {SOURCE_TABLE} b
                  ON b.source_game_key = a.source_game_key
                 AND b.source_club_id <> a.source_club_id
                {where} GROUP BY b.source_club_id
                ORDER BY AVG(a.attendance) DESC""", params).fetchall()
    else:
        column = _CROWD_KEYS[by]
        where, params = _where(
            (_scope(scope), []),
            ("source_club_id = ?" if club_id else "",
             [club_id] if club_id else []),
            _trust(include_unlinked),
        )
        rows = con.execute(
            f"""SELECT {column}, COUNT(*), SUM(attendance IS NOT NULL),
                       AVG(attendance), SUM(attendance)
                FROM {SOURCE_TABLE}{where} GROUP BY {column}
                ORDER BY AVG(attendance) DESC""", params).fetchall()

    return [CrowdAverage(str(key), n, known or 0, avg, total)
            for key, n, known, avg, total in rows]


# -------------------------------------------------------- match search

@dataclass(frozen=True)
class Match:
    """One match from the searching club's point of view."""
    club_id: str
    opponent_id: str
    season: int
    round: str
    is_final: bool
    match_date: str
    venue: str
    team_position: str          # H | A | F
    result: str                 # W | D | L
    points_for: int
    points_against: int
    margin: int
    attendance: int | None
    match_id: int | None
    source_game_key: str

    @property
    def score(self) -> str:
        return f"{self.points_for}-{self.points_against}"

    @property
    def where(self) -> str:
        return {"H": "home", "A": "away", "F": "finals"}.get(
            self.team_position, self.team_position)


#: The two sides' scores, as the smaller and the larger. Every row already
#: carries both -- `points_against` on one row is `points_for` on the
#: other -- so a condition about "both teams" needs no second join, and
#: means the same thing whichever side's row is being read.
_LOW_SCORE = "MIN(a.points_for, a.points_against)"
_HIGH_SCORE = "MAX(a.points_for, a.points_against)"
_TOTAL = "(a.points_for + a.points_against)"

#: Scoreline filter name -> the expression it bounds. `margin` is absolute
#: here, not signed: "decided by under a goal" is a fact about the match,
#: and a club filter must not turn it into "and that club lost by under a
#: goal".
_SCORELINE_BOUNDS = {
    "margin": "ABS(a.margin)",
    "low_score": _LOW_SCORE,
    "high_score": _HIGH_SCORE,
    "total": _TOTAL,
}


def _scoreline(bounds: dict) -> list[tuple[str, list]]:
    """`{'max_margin': 5, 'min_low_score': 100}` -> WHERE clauses.

    Zero is a real bound, not an absent one -- `max_low_score=0` is how a
    shutout is asked for, and `min_margin=0` excludes nothing but is what a
    page passes when its floor is at rest. Only None means "no bound".
    """
    clauses = []
    for name, value in bounds.items():
        if value is None:
            continue
        edge, _, subject = name.partition("_")
        expression = _SCORELINE_BOUNDS.get(subject)
        if expression is None or edge not in ("min", "max"):
            raise ValueError(f"unknown scoreline filter {name!r}; expected "
                             f"min_/max_ on one of "
                             f"{sorted(_SCORELINE_BOUNDS)}")
        clauses.append((f"{expression} {'>=' if edge == 'min' else '<='} ?",
                        [value]))
    return clauses


def search_matches(con: sqlite3.Connection, club_id: str | None = None,
                   opponent_id: str | None = None, season: int | None = None,
                   season_from: int | None = None, season_to: int | None = None,
                   venue: str | None = None, result: str | None = None,
                   team_position: str | None = None, scope: Scope = "all",
                   round_text: str | None = None,
                   min_margin: int | None = None,
                   max_margin: int | None = None,
                   min_low_score: int | None = None,
                   max_low_score: int | None = None,
                   min_high_score: int | None = None,
                   max_high_score: int | None = None,
                   min_total: int | None = None,
                   max_total: int | None = None,
                   min_attendance: int | None = None,
                   order: str = "date_desc", limit: int = 500,
                   include_unlinked: bool = False) -> list[Match]:
    """Past results, filtered.

    Every filter is optional and they compose. Without a `club_id` this
    lists matches rather than club-perspectives, so it returns one row per
    match instead of two; with one, every row is that club's view and
    `result`, `margin` and `points_for` are all from its side.

    The eight scoreline bounds ask about the result itself rather than
    about who was in it: `max_margin` is an absolute margin, `low_score`
    and `high_score` are the smaller and the larger of the two sides'
    scores, and `total` is the two added together. So a game decided by
    under a goal in which both sides passed a hundred is
    `max_margin=5, min_low_score=100`, with or without a club.
    """
    orders = {
        "date_desc": "a.match_date DESC",
        "date_asc": "a.match_date ASC",
        "margin_desc": "a.margin DESC",
        "margin_asc": "a.margin ASC",
        # Absolute margin, for a listing with no club in it: there, a
        # signed sort ranks by how heavily the *first-named* side won,
        # which puts the biggest away wins at the bottom of "biggest
        # margin" and at the top of "smallest".
        "margin_abs_desc": "ABS(a.margin) DESC",
        "margin_abs_asc": "ABS(a.margin) ASC",
        "total_desc": f"{_TOTAL} DESC",
        "total_asc": f"{_TOTAL} ASC",
        "crowd_desc": "a.attendance DESC",
        "crowd_asc": "a.attendance ASC",
    }
    if order not in orders:
        raise ValueError(
            f"unknown order {order!r}; expected one of {sorted(orders)}")
    if result and result not in ("W", "D", "L"):
        raise ValueError(f"result must be W, D or L, got {result!r}")

    trust_sql, trust_params = _trust(include_unlinked)
    where, params = _where(
        ("" if club_id else _ONE_PER_MATCH, []),
        ("a.source_club_id = ?" if club_id else "", [club_id] if club_id else []),
        ("b.source_club_id = ?" if opponent_id else "",
         [opponent_id] if opponent_id else []),
        (f"a.{_scope(scope)}" if _scope(scope) else "", []),
        ("a.season = ?" if season is not None else "",
         [season] if season is not None else []),
        ("a.season >= ?" if season_from is not None else "",
         [season_from] if season_from is not None else []),
        ("a.season <= ?" if season_to is not None else "",
         [season_to] if season_to is not None else []),
        ("a.venue_raw = ?" if venue else "", [venue] if venue else []),
        ("a.result = ?" if result else "", [result] if result else []),
        ("a.team_position = ?" if team_position else "",
         [team_position] if team_position else []),
        ("UPPER(a.round) = ?" if round_text else "",
         [round_text.upper()] if round_text else []),
        *_scoreline({
            "min_margin": min_margin, "max_margin": max_margin,
            "min_low_score": min_low_score, "max_low_score": max_low_score,
            "min_high_score": min_high_score, "max_high_score": max_high_score,
            "min_total": min_total, "max_total": max_total,
        }),
        ("a.attendance >= ?" if min_attendance is not None else "",
         [min_attendance] if min_attendance is not None else []),
        (f"a.{trust_sql}" if trust_sql else "", trust_params),
        (f"b.{trust_sql}" if trust_sql else "", list(trust_params)),
    )
    rows = con.execute(
        f"""SELECT a.source_club_id, b.source_club_id, a.season, a.round,
                   a.is_final, a.match_date, a.venue_raw, a.team_position,
                   a.result, a.points_for, a.points_against, a.margin,
                   a.attendance, a.match_id, a.source_game_key
            FROM {SOURCE_TABLE} a
            JOIN {SOURCE_TABLE} b
              ON b.source_game_key = a.source_game_key
             AND b.source_club_id <> a.source_club_id
            {where} ORDER BY {orders[order]} LIMIT ?""",
        [*params, limit]).fetchall()
    return [Match(c, o, s, rd, bool(f), dt, v, pos, res, pf, pa, m, att,
                  mid, key)
            for c, o, s, rd, f, dt, v, pos, res, pf, pa, m, att, mid, key
            in rows]


def seasons_available(con: sqlite3.Connection) -> list[int]:
    return [r[0] for r in con.execute(
        f"SELECT DISTINCT season FROM {SOURCE_TABLE} ORDER BY season DESC")]


def venues_available(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute(
        f"SELECT venue_raw, COUNT(*) n FROM {SOURCE_TABLE} "
        f"WHERE venue_raw IS NOT NULL GROUP BY 1 ORDER BY n DESC")]


#: A round label that is a number, optionally behind a letter or two:
#: the AFL's 'R14', the NFL's 'W3'. Finals codes never match.
_NUMBERED_ROUND = re.compile(r"^[A-Za-z]*(\d+)$")


def rounds_available(con: sqlite3.Connection, season: int | None = None,
                     season_from: int | None = None,
                     season_to: int | None = None) -> list[str]:
    """The round labels actually played, in the order they were played.

    Sorting the labels themselves gets two things wrong: 'R2' does not sort
    before 'R10' alphabetically, and the AFL's finals codes are not
    alphabetical in the order they are played -- 'EF, GF, PF, QF, SF' is
    nobody's September. So numbered rounds are ordered by their number, and
    everything else by where in its own season it fell.

    "Where in its own season" is the round's average position among that
    season's rounds, as a fraction, not its date. A fraction is comparable
    across eras where a bare date is not: the 1897 season ran 15 rounds from
    May to September and 2026 runs 27 from March, so September means
    'the finals' in one and 'round 21' in the other. It also survives the
    NFL's season crossing the new year, where the Super Bowl is played in
    February and would otherwise sort in front of week 1.

    Numbers are used ahead of position because position alone is skewed by
    which seasons a round exists in at all: round 17 only ever appears in
    seasons long enough to have one, so it averages later in the season than
    round 19, which needs a longer season still.

    Rounds with no label at all -- the MLB and NBA regular seasons, which
    record one only for the postseason -- are absent rather than listed as a
    blank, so a caller offering these as a filter offers only the filters
    that mean something.
    """
    where, params = _where(
        ("round IS NOT NULL", []),
        ("season = ?" if season is not None else "",
         [season] if season is not None else []),
        ("season >= ?" if season_from is not None else "",
         [season_from] if season_from is not None else []),
        ("season <= ?" if season_to is not None else "",
         [season_to] if season_to is not None else []),
        _trust(False),
    )
    rows = con.execute(
        f"""SELECT season, round, MIN(match_date) FROM {SOURCE_TABLE}{where}
            GROUP BY season, round""", params).fetchall()

    positions: dict[str, list[float]] = {}
    by_season: dict[int, list[tuple[str, str]]] = {}
    for season_of, label, first_date in rows:
        by_season.setdefault(season_of, []).append((first_date or "", label))
    for played in by_season.values():
        played.sort()
        for i, (_, label) in enumerate(played, start=1):
            positions.setdefault(label, []).append(i / len(played))

    def key(label: str) -> tuple[int, float, str]:
        numbered = _NUMBERED_ROUND.match(label)
        if numbered:
            return (0, int(numbered.group(1)), label)
        seen = positions[label]
        return (1, sum(seen) / len(seen), label)

    return sorted(positions, key=key)


def matches_without_a_round(con: sqlite3.Connection,
                            season: int | None = None) -> int:
    """How many matches `rounds_available` cannot describe.

    The MLB and NBA sources record a round for the postseason and nothing
    for the regular season, so a round filter over those reaches a few
    hundred matches out of a couple of hundred thousand. A caller offering
    the filter should say so rather than let a reader conclude the regular
    season is missing.
    """
    where, params = _where(
        ("round IS NULL", []),
        ("season = ?" if season is not None else "",
         [season] if season is not None else []),
        _trust(False),
    )
    # Counted by game key, not by row: the source holds one row per club per
    # match, so counting rows would double every match.
    return con.execute(
        f"SELECT COUNT(DISTINCT source_game_key) "
        f"FROM {SOURCE_TABLE}{where}", params).fetchone()[0]


# --------------------------------------------------------- reconciliation

def reconcile(con: sqlite3.Connection,
              include_unlinked: bool = False) -> list[str]:
    """Free integrity checks on every season record this module derives.

    Two independent cross-checks, both of which have already found real
    problems and so are worth running as a test over the loaded data rather
    than only at parse time:

    1. The source's own running totals (`season_wins_after` and friends)
       against the aggregated results. These disagree for Essendon and
       Richmond in 1924, whose finals were a round-robin the published
       running totals do not survive.

    2. `team_seasons`, built independently from the fitzRoy player-stats
       source, against the home-and-away record aggregated here. Two
       different sources agreeing on a completed club season is a real
       check; this module deriving its own numbers twice would not be. The
       active season is omitted because the feeds refresh independently
       and can legitimately be one round apart.
    """
    problems: list[str] = []
    trust_sql, trust_params = _trust(include_unlinked)
    where, params = _where((trust_sql, trust_params))

    running = con.execute(
        f"""SELECT agg.source_club_id, agg.season, agg.w, agg.d, agg.l,
                   last.w2, last.d2, last.l2
            FROM (SELECT source_club_id, season, SUM(result='W') w,
                         SUM(result='D') d, SUM(result='L') l
                  FROM {SOURCE_TABLE}{where}
                  GROUP BY source_club_id, season) agg
            JOIN (SELECT source_club_id, season,
                         season_wins_after w2, season_draws_after d2,
                         season_losses_after l2,
                         ROW_NUMBER() OVER (
                             PARTITION BY source_club_id, season
                             ORDER BY match_date DESC) rn
                  FROM {SOURCE_TABLE}{where}) last
              ON last.source_club_id = agg.source_club_id
             AND last.season = agg.season AND last.rn = 1
            WHERE agg.w <> last.w2 OR agg.d <> last.d2 OR agg.l <> last.l2
            ORDER BY agg.season, agg.source_club_id""",
        params + params).fetchall()
    for club, season, w, d, l, w2, d2, l2 in running:
        problems.append(
            f"{club} {season}: results aggregate to {w}/{d}/{l} (W/D/L) but "
            f"the source's running total ends at {w2}/{d2}/{l2}")

    if "team_seasons" not in _tables(con):
        problems.append("team_seasons missing -- cross-source check skipped")
        return problems

    ladder = con.execute(
        f"""SELECT c.club_id, t.season, t.played, t.wins, t.draws, t.losses,
                   t.points_for, t.points_against
            FROM team_seasons t JOIN clubs c ON c.db_club_now = t.club_now"""
    ).fetchall() if "clubs" in _tables(con) else []

    derived = {(r.club_id, r.season): r for r in
               season_records(con, scope="home_and_away",
                              include_unlinked=include_unlinked)}
    this_year = date.today().year
    season_finished = con.execute(
        f"""SELECT 1 FROM {SOURCE_TABLE}
             WHERE season = ? AND UPPER(TRIM(round)) IN ('GF', 'GRAND FINAL')
             LIMIT 1""", (this_year,)).fetchone()
    active_season = None if season_finished else this_year
    for club, season, played, w, d, l, pf, pa in ladder:
        if int(season) == active_season:
            continue
        row = derived.get((club, season))
        if row is None:
            continue
        for label, theirs, ours in (
                ("played", played, row.played), ("wins", w, row.wins),
                ("draws", d, row.draws), ("losses", l, row.losses),
                ("points_for", pf, row.points_for),
                ("points_against", pa, row.points_against)):
            if theirs is not None and int(theirs) != int(ours):
                problems.append(
                    f"{club} {season}: team_seasons {label}={theirs}, "
                    f"all-games rows give {ours}")
    return problems
