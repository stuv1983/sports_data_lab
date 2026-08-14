"""visual_queries.py -- Aggregate SQL for the Visual Explorer, and the
capability model that decides which of it a database can answer.

WHY A SEPARATE QUERY LAYER
--------------------------
The four databases are 0.3 to 1.2 GB and the largest `games` table holds
1.6 million rows. A chart that pulls rows into pandas and groups them
there moves a hundred megabytes to answer a question whose answer is
eighty numbers. Every function here therefore returns a frame that is
already the chart -- a row per season, per bin, per cell -- and SQLite
does the aggregation against its own indexes.

The flow, in one line:

    validated parameters -> parameterised SQL -> SQLite aggregation
    -> small frame -> Altair -> Streamlit

THE CAPABILITY MODEL IS MEASURED, NOT DECLARED
----------------------------------------------
`Capabilities` is what a hand-written SPORT_VISUAL_CAPABILITIES table
would hold, except that it is read off the database rather than typed in.
That is not a style preference. Two of the differences that decide whether
a chart is honest are invisible from a sport's registry entry:

  * The MLB's `games` is a player's *season* with one club. Lahman has no
    box scores. Any chart implying a single-game figure for it is drawing
    something that does not exist, so `player_game_grain` is False there
    and the per-game charts decline.
  * `team_seasons` exists in three of the four builds and means a
    different thing in each: the AFL's carries wins and a ladder rank, the
    NBA's wins and a conference rank, the NFL's is a bag of stat sums with
    no wins column at all. Asking the table what columns it has is the
    only way to know which.

A declared table would also be a fifth place to update when a loader runs
for the first time, and would go stale silently. The measurement costs one
`sqlite_master` read and a `PRAGMA table_info` per table, once per
database revision, behind `st.cache_data`.

Nothing here branches on `sport.key`; tests/test_sport_capabilities.py
enforces that for every module at the repository root, and the rule is
what keeps a fifth sport from inheriting four sports' exceptions.

SQL SAFETY
----------
Every value a reader chooses is bound, never formatted. Identifiers cannot
be bound by SQLite, so every identifier that reaches an f-string comes
from one of exactly two places: the sport's own `core.Schema`, or
`_allowed_stat` / `_allowed_columns`, which intersect a requested name
against the columns the table actually has. A name that survives neither
is refused rather than quoted -- an allowlist, not an escape.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Sequence

import pandas as pd
import streamlit as st

import labels

#: One row per club per match: result, margin, scores, venue, attendance.
#: Every build has it -- the AFL scrapes it, the MLB loads Retrosheet game
#: logs into it, the NBA and NFL project their schedules into it -- which
#: makes it the one game-level team table that is the same shape in all
#: four databases. The MLB's 475,432 rows are the only game-level data
#: that build has at all.
MATCH_SOURCE = "club_match_sources"

#: The trust boundary club_history.py sets, kept here for the same reason:
#: a rescrape can introduce ambiguous or score-mismatched rows at any time,
#: and a club's record must not silently drift from the published one.
TRUSTED = "match_status = 'unique'"

#: Rate statistics are never summed and never averaged across rows -- an
#: ERA of 3.2 with one club and 4.1 with another is not a 7.3, and it is
#: not a 3.65 either without innings pitched to weight it, which these
#: tables do not carry. Charts show them per row and say so.
def _rate_stats(sport) -> frozenset:
    return frozenset(getattr(sport.schema, "rate_stats", ()) or ())


# ------------------------------------------------------- schema probing

def _tables(con: sqlite3.Connection) -> frozenset:
    try:
        return frozenset(row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"))
    except sqlite3.Error:
        return frozenset()


def _columns(con: sqlite3.Connection, table: str) -> frozenset:
    if not table:
        return frozenset()
    try:
        return frozenset(row[1] for row in
                         con.execute(f"PRAGMA table_info({table})"))
    except sqlite3.Error:
        return frozenset()


def _has_rows(con: sqlite3.Connection, table: str) -> bool:
    """Whether a table holds anything, without counting all of it.

    A layer that exists but was never loaded is not a capability, and
    `COUNT(*)` on the MLB's half-million source rows to learn that costs
    more than the chart it is gating.
    """
    try:
        return con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


# ------------------------------------------------------------ the model

@dataclass(frozen=True)
class Capabilities:
    """What one database can actually be asked to draw.

    Read off the file, once per revision. Every field is a fact about the
    data rather than about the sport: a chart asks "is there game-level
    team data" and not "is this the AFL", so a build that gains a loader
    gains the chart with no code change.
    """
    #: A row of `games` is one player in one match. False where it is
    #: coarser -- the MLB's is a player's season with one club -- and the
    #: per-game charts must then decline rather than imply a box score.
    player_game_grain: bool = False
    #: The `games` column saying how many real games one row stands for,
    #: empty when a row is a game. Every appearance count multiplexes
    #: through this, so a season-grain build is counted in games and not
    #: in rows.
    games_per_row: str = ""
    #: One row per match, with the two sides and the score.
    matches: bool = False
    #: One row per club per match: result, margin, scores.
    team_match_rows: bool = False
    #: The column naming a club in those rows, and the one to show.
    team_key: str = ""
    team_label: str = ""
    #: Curated season records with a wins column. Distinct from deriving
    #: them: the AFL's carries draws and a ladder rank that no aggregate
    #: over match rows can reproduce.
    team_season_table: bool = False
    #: The finishing-position column on it, "" where there is none.
    team_rank_column: str = ""
    team_rank_label: str = ""
    #: A draws column, "" for a competition that cannot draw. The NBA's
    #: table has none, and a query naming it there returns nothing at all.
    team_draws_column: str = ""
    #: The column splitting regular season from post-season, and the value
    #: that means the regular one. The NBA's table carries a row per phase,
    #: so a team has two rows a season and a chart that does not filter
    #: draws an 82-game season and an 11-game playoff run as one line.
    team_phase_column: str = ""
    team_phase_primary: str = ""
    #: Attendance on the match rows, and whether any of it is filled in.
    attendance: bool = False
    #: A finals/postseason flag on the match rows.
    match_postseason: bool = False
    #: Home and away are distinguishable. Finals often have no home side,
    #: which is why this is separate from `matches`.
    home_away: bool = False
    #: A venue on the match rows, and what share of them carry one. The
    #: share is the capability: the NBA build has the column and fills in
    #: 1.8% of it, and a "busiest arenas" chart drawn from that ranks the
    #: handful of rows somebody happened to record rather than the arenas.
    venues: bool = False
    venue_coverage: float = 0.0
    #: Statistics that are both declared by the sport and present on
    #: `games`, in the sport's own order.
    stats: tuple = ()
    #: Of those, the ones that are rates and must not be summed.
    rate_stats: frozenset = frozenset()
    #: Seasons the `games` table spans, and the match rows separately --
    #: the NFL's schedule reaches back further than its player statistics.
    season_range: tuple = ()
    match_season_range: tuple = ()
    #: Optional layers the specialist charts read.
    draft_table: str = ""
    #: The awards table and the columns it spells its own way. Three
    #: builds have one and no two agree: the AFL's names a slug and the
    #: winner's name, the NBA's a key and a recipient, the MLB's an award
    #: and nothing but a player id -- which is why the recipient's name is
    #: sometimes a column and sometimes a join.
    awards_table: str = ""
    award_key_column: str = ""
    award_name_column: str = ""
    award_recipient_column: str = ""
    #: The column joining an award row to `players`, "" where there is
    #: none. The AFL's `dg_person_id` is deliberately not used: it is a
    #: Draftguru person, reachable only through `person_links`, and
    #: treating it as a player id opens the wrong card.
    award_player_id_column: str = ""
    brownlow: bool = False
    hall_of_fame_voting: bool = False
    families: bool = False
    #: Loader hints for the layers that are missing, as {label: hint}.
    missing: dict = field(default_factory=dict)

    # -- what a section can offer ------------------------------------
    @property
    def league_activity(self) -> bool:
        return bool(self.season_range)

    @property
    def player_trajectory(self) -> bool:
        return bool(self.stats and self.season_range)

    @property
    def team_trends(self) -> bool:
        return self.team_season_table or self.team_match_rows

    @property
    def team_postseason_toggle(self) -> bool:
        """Whether "include finals" is a question this source can answer."""
        if self.team_season_table:
            return bool(self.team_phase_column)
        return self.team_match_rows and self.match_postseason

    @property
    def volume_efficiency(self) -> bool:
        return bool(self.stats)

    @property
    def match_distributions(self) -> bool:
        return self.team_match_rows

    @property
    def coverage(self) -> bool:
        return bool(self.stats and self.season_range)

    @property
    def venue_charts(self) -> bool:
        return self.venues and self.venue_coverage >= 50.0

    @property
    def award_charts(self) -> bool:
        return bool(self.awards_table and self.award_key_column)

    def summary(self) -> list:
        """(label, ready, note) rows for the page's capability panel."""
        grain = ("player-game" if self.player_game_grain
                 else "player-season")
        return [
            ("League activity", self.league_activity,
             f"{grain} rows"),
            ("Player trajectories", self.player_trajectory,
             f"{len(self.stats)} statistics"),
            ("Team trends", self.team_trends,
             "season table" if self.team_season_table else "match rows"),
            ("Volume vs efficiency", self.volume_efficiency,
             "per-game rates" if self.player_game_grain
             else "per-season rates"),
            ("Match distributions", self.match_distributions,
             "margins and scores" if self.match_distributions
             else "no game-level team rows"),
            ("Data coverage", self.coverage, "measured per season"),
            ("Venues", self.venue_charts,
             f"{self.venue_coverage:.0f}% of match rows name one"),
            ("Awards", self.award_charts,
             self.awards_table or "no awards table loaded"),
        ]


@st.cache_data(show_spinner=False, max_entries=16)
def capabilities(sport_key: str, revision, _con) -> Capabilities:
    """Measure one database. Cached for the life of a revision."""
    import sports

    sport = sports.get(sport_key)
    sc = sport.schema
    tables = _tables(_con)
    game_columns = _columns(_con, sc.games)

    stats = tuple(stat for stat in sc.stats if stat in game_columns)
    span = _span(_con, sc.games, sc.season)

    source_columns = _columns(_con, MATCH_SOURCE)
    has_source = (MATCH_SOURCE in tables
                  and {"source_club_id", "season", "result", "margin"}
                  <= source_columns
                  and _has_rows(_con, MATCH_SOURCE))
    team_label = ("source_club_label" if "source_club_label" in source_columns
                  else "source_club_id")

    season_columns = _columns(_con, "team_seasons")
    has_season_table = ("team_seasons" in tables and "wins" in season_columns
                        and "season" in season_columns
                        and _has_rows(_con, "team_seasons"))
    rank_column, rank_label = "", ""
    for candidate, spoken in (("ladder_rank", "Ladder position"),
                              ("conference_rank", "Conference position"),
                              ("league_rank", "League position")):
        if has_season_table and candidate in season_columns:
            rank_column, rank_label = candidate, spoken
            break
    phase_column = ("phase" if has_season_table and "phase" in season_columns
                    else "")
    phase_primary = _primary_phase(_con, phase_column) if phase_column else ""

    match_columns = _columns(_con, sc.matches) if sc.matches else frozenset()
    has_matches = bool(sc.matches and sc.matches in tables
                       and sc.match_home_score in match_columns
                       and _has_rows(_con, sc.matches))

    awards_table = next((name for name in ("awards", "wiki_awards")
                         if name in tables and _has_rows(_con, name)), "")
    award_columns = _columns(_con, awards_table) if awards_table else frozenset()
    award_key = next((name for name in ("award_slug", "award_key", "award")
                      if name in award_columns), "")
    if "season" not in award_columns:
        awards_table, award_key = "", ""

    missing = {}
    if not has_source and sport.past_games_hint:
        missing["Game-level team rows"] = sport.past_games_hint

    return Capabilities(
        player_game_grain=not sc.games_per_row,
        games_per_row=sc.games_per_row,
        matches=has_matches,
        team_match_rows=has_source,
        team_key="source_club_id" if has_source else "",
        team_label=team_label if has_source else "",
        team_season_table=has_season_table,
        team_rank_column=rank_column,
        team_rank_label=rank_label,
        team_draws_column=("draws" if has_season_table
                           and "draws" in season_columns else ""),
        team_phase_column=phase_column,
        team_phase_primary=phase_primary,
        attendance=has_source and _any_value(_con, MATCH_SOURCE, "attendance",
                                             source_columns),
        match_postseason=has_source and "is_final" in source_columns,
        home_away=has_source and _any_value(_con, MATCH_SOURCE,
                                            "team_position", source_columns),
        venues=has_source and "venue_raw" in source_columns,
        venue_coverage=(_filled_share(_con, MATCH_SOURCE, "venue_raw")
                        if has_source and "venue_raw" in source_columns
                        else 0.0),
        stats=stats,
        rate_stats=frozenset(stat for stat in _rate_stats(sport)
                             if stat in stats),
        season_range=span,
        match_season_range=(_span(_con, MATCH_SOURCE, "season")
                            if has_source else ()),
        draft_table=next((name for name in ("draft", "draft_picks")
                          if name in tables and _has_rows(_con, name)), ""),
        awards_table=awards_table,
        award_key_column=award_key,
        award_name_column=("award_name" if "award_name" in award_columns
                           else award_key),
        award_recipient_column=next(
            (name for name in ("player", "recipient")
             if name in award_columns), ""),
        award_player_id_column=("player_id" if "player_id" in award_columns
                                else ""),
        brownlow=("brownlow_results" in tables
                  and _has_rows(_con, "brownlow_results")),
        hall_of_fame_voting=("hall_of_fame" in tables
                             and {"votes", "ballots"}
                             <= _columns(_con, "hall_of_fame")),
        families=("family_relationships" in tables
                  and _has_rows(_con, "family_relationships")),
        missing=missing,
    )


def _span(con, table, column) -> tuple:
    try:
        low, high = con.execute(
            f"SELECT MIN({column}), MAX({column}) FROM {table}").fetchone()
    except (sqlite3.Error, TypeError):
        return ()
    if low is None or high is None:
        return ()
    return int(low), int(high)


def _primary_phase(con, column) -> str:
    """Which phase value is the main competition, measured not assumed.

    The phase accounting for the most games played is the regular season,
    by definition -- an 82-game schedule against an 11-game playoff run.
    Measured rather than matched against a list of spellings, because the
    next build to grow a phase column will spell it its own way.
    """
    try:
        row = con.execute(
            f"SELECT {column} FROM team_seasons WHERE {column} IS NOT NULL "
            f"GROUP BY {column} ORDER BY SUM(played) DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return ""
    return str(row[0]) if row and row[0] is not None else ""


def _filled_share(con, table, column) -> float:
    """What percentage of a table's rows carry a value in one column.

    A column that exists is not a column that is populated, and the
    difference decides whether a chart drawn from it is a summary or a
    sample of whatever happened to be recorded.
    """
    try:
        total, filled = con.execute(
            f"SELECT COUNT(*), COUNT({column}) FROM {table}").fetchone()
    except sqlite3.Error:
        return 0.0
    return round(100.0 * filled / total, 1) if total else 0.0


def _any_value(con, table, column, columns) -> bool:
    if column not in columns:
        return False
    try:
        return con.execute(
            f"SELECT 1 FROM {table} WHERE {column} IS NOT NULL LIMIT 1"
        ).fetchone() is not None
    except sqlite3.Error:
        return False


# ------------------------------------------------------- identifier gate

def _allowed_stat(caps: Capabilities, stat: str) -> str:
    """A statistic name, or a raised ValueError.

    The only door a column name gets through into an f-string. `stats` was
    itself built by intersecting the sport's declared list with the
    columns `games` really has, so a name that passes here is both
    meaningful to the sport and present in the file.
    """
    if stat not in caps.stats:
        raise ValueError(f"{stat!r} is not a statistic this database has")
    return stat


def _season_clause(column: str, seasons) -> tuple:
    """`(sql, params)` for an optional inclusive season range."""
    if not seasons:
        return "", []
    low, high = int(seasons[0]), int(seasons[1])
    return f" AND {column} BETWEEN ? AND ?", [min(low, high), max(low, high)]


def _frame(con, sql, params) -> pd.DataFrame:
    try:
        return pd.read_sql_query(sql, con, params=list(params))
    except (sqlite3.Error, pd.errors.DatabaseError):
        # A page must never show a reader a stack trace, and a layer that
        # vanished between the capability probe and the query is a real
        # possibility on a machine that rebuilds databases nightly.
        return pd.DataFrame()


# ------------------------------------------------- 1. league activity

@st.cache_data(show_spinner=False, max_entries=32)
def league_activity(sport_key: str, revision, _con,
                    seasons: tuple = ()) -> pd.DataFrame:
    """Participation per season: players, clubs, appearances, matches.

    Appearances multiplex through `games_per_row`, so a season-grain build
    reports the games its rows stand for rather than the number of rows --
    counting Lahman's player-seasons as appearances would report 1901 as
    having had 1,300 games played in it.

    Matches come from the game-level team rows, halved, because those hold
    one row per club per match. Absent where the build has none, rather
    than guessed at from player rows.
    """
    import sports

    sport = sports.get(sport_key)
    sc = sport.schema
    caps = capabilities(sport_key, revision, _con)
    where, params = _season_clause(sc.season, seasons)
    appearances = (f"SUM({sc.games_per_row})" if caps.games_per_row
                   else "COUNT(*)")
    frame = _frame(_con, f"""
        SELECT {sc.season} AS Season,
               COUNT(DISTINCT {sc.player_id}) AS Players,
               COUNT(DISTINCT {sc.club_hist}) AS Clubs,
               {appearances} AS Appearances
          FROM {sc.games}
         WHERE {sc.season} IS NOT NULL{where}
         GROUP BY {sc.season}
         ORDER BY {sc.season}""", params)

    if frame.empty or not caps.team_match_rows:
        return frame
    match_where, match_params = _season_clause("season", seasons)
    matches = _frame(_con, f"""
        SELECT season AS Season,
               COUNT(DISTINCT source_game_key) AS Matches
          FROM {MATCH_SOURCE}
         WHERE {TRUSTED}{match_where}
         GROUP BY season""", match_params)
    if matches.empty:
        return frame
    return frame.merge(matches, on="Season", how="left")


#: What one row of `league_activity` can be plotted as, and what to call
#: it. The appearance label is filled in from the sport's vocabulary,
#: because "player-games" is wrong for a build whose row is a season.
ACTIVITY_METRICS = ("Players", "Clubs", "Appearances", "Matches")


def activity_label(sport, caps: Capabilities, metric: str) -> str:
    vocab = sport.vocab
    if metric == "Clubs":
        return vocab.clubs.capitalize()
    if metric == "Matches":
        return f"{vocab.games.capitalize()} played"
    if metric == "Appearances":
        return f"Player-{vocab.games}"
    return metric


# --------------------------------------------- 2. player trajectories

@st.cache_data(show_spinner=False, max_entries=256)
def player_seasons(sport_key: str, revision, _con, player_id,
                   stat: str) -> pd.DataFrame:
    """One player's season-by-season total, average and club.

    The average is the total over the games the statistic was *recorded*
    in, not over the games played: a 1963 season has no disposals column
    filled in, and dividing by games played would report a real average of
    zero for a player who was never measured. `Recorded` is returned so a
    caller can say which it is.

    A rate statistic is neither summed nor averaged here -- see
    `_rate_stats` -- so its rows come back one per season and club, with
    the value as the source recorded it.
    """
    import sports

    sport = sports.get(sport_key)
    sc = sport.schema
    caps = capabilities(sport_key, revision, _con)
    column = _allowed_stat(caps, stat)

    if column in caps.rate_stats:
        return _frame(_con, f"""
            SELECT {sc.season} AS Season, {sc.club_hist} AS Club,
                   {column} AS Value
              FROM {sc.games}
             WHERE {sc.player_id} = ? AND {column} IS NOT NULL
             ORDER BY {sc.season}""", [player_id])

    played = (f"SUM({sc.games_per_row})" if caps.games_per_row
              else "COUNT(*)")
    recorded = (f"SUM(CASE WHEN {column} IS NOT NULL "
                f"THEN {sc.games_per_row} ELSE 0 END)"
                if caps.games_per_row else f"COUNT({column})")
    return _frame(_con, f"""
        SELECT {sc.season} AS Season,
               {played} AS Played,
               {recorded} AS Recorded,
               SUM({column}) AS Total,
               GROUP_CONCAT(DISTINCT {sc.club_hist}) AS Clubs
          FROM {sc.games}
         WHERE {sc.player_id} = ?
         GROUP BY {sc.season}
         HAVING {recorded} > 0
         ORDER BY {sc.season}""", [player_id])


def with_rate(frame: pd.DataFrame, label: str) -> pd.DataFrame:
    """Add the per-game rate to a `player_seasons` frame.

    Total over *recorded* games, computed once here rather than in each
    caller, and left missing where nothing was recorded rather than
    filled with a zero the source never said.
    """
    if frame.empty or not {"Total", "Recorded"} <= set(frame.columns):
        return frame
    out = frame.copy()
    # Both sides coerced to float before the division, and the zero
    # denominator turned into a NaN rather than a pd.NA: pandas 3 raises
    # TypeError rounding a column that holds pd.NA, so a single season
    # with nothing recorded would take the whole chart down.
    total = pd.to_numeric(out["Total"], errors="coerce").astype("float64")
    recorded = pd.to_numeric(out["Recorded"], errors="coerce").astype("float64")
    out[label] = (total / recorded.where(recorded != 0)).round(2)
    return out


# ------------------------------------------------------ 3. team trends

#: How a team season can be measured, as {label: (column, rule, zero)}.
#: `rule` is the reference line worth drawing -- a .500 record, an even
#: differential -- and `zero` whether the axis must include it.
TEAM_METRICS = {
    "Wins": ("Wins", None, True),
    "Win %": ("WinPct", 50.0, True),
    "Points for": ("PointsFor", None, True),
    "Points against": ("PointsAgainst", None, True),
    "Points differential": ("Differential", 0.0, False),
}


@st.cache_data(show_spinner=False, max_entries=64)
def team_seasons(sport_key: str, revision, _con, teams: tuple = (),
                 seasons: tuple = (), include_postseason: bool = False
                 ) -> pd.DataFrame:
    """Season records for named clubs, from whichever source is better.

    The curated `team_seasons` table wins where it has a wins column: it
    carries draws and a finishing position that no aggregate over match
    rows can reproduce, and it is the competition's own record rather than
    this app's reading of it. Where a build has no such table -- the MLB
    has none, the NFL's is a bag of stat sums -- the records are derived
    from the game-level team rows instead, which is the same arithmetic
    the Club Explorer already does.

    Deriving means aggregating each match's `result`, never trusting the
    source's running season totals: club_history.py documents why, and the
    1924 AFL finals are the counter-example that made it a rule.
    """
    caps = capabilities(sport_key, revision, _con)
    if caps.team_season_table:
        return _curated_team_seasons(_con, caps, teams, seasons,
                                     include_postseason)
    if caps.team_match_rows:
        return _derived_team_seasons(_con, caps, teams, seasons,
                                     include_postseason)
    return pd.DataFrame()


def _in_clause(column: str, values) -> tuple:
    marks = ", ".join("?" for _ in values)
    return f" AND {column} IN ({marks})", list(values)


def _curated_team_seasons(con, caps, teams, seasons,
                          include_postseason) -> pd.DataFrame:
    """The competition's own season table, one row per team per season.

    Aggregated rather than selected straight, because a build may file a
    team's season under more than one row: the NBA's carries a `phase`,
    so a 2024 Celtics season is an 82-game regular row and an 11-game
    playoff row. Summing them is right only when the reader asked for
    both; otherwise the phase that is the competition proper is the one
    drawn, and the finishing position -- which the playoff row leaves
    NULL -- survives the aggregate through MAX.
    """
    where, params = _season_clause("season", seasons)
    if teams:
        clause, team_params = _in_clause("club_now", teams)
        where += clause
        params += team_params
    if caps.team_phase_column and not include_postseason:
        where += f" AND {caps.team_phase_column} = ?"
        params.append(caps.team_phase_primary)
    draws = (f"SUM(COALESCE({caps.team_draws_column}, 0))"
             if caps.team_draws_column else "0")
    rank = (f", MAX({caps.team_rank_column}) AS Rank"
            if caps.team_rank_column else "")
    return _frame(con, f"""
        SELECT season AS Season, club_now AS Team,
               SUM(played) AS Played, SUM(wins) AS Wins,
               SUM(losses) AS Losses, {draws} AS Draws,
               SUM(points_for) AS PointsFor,
               SUM(points_against) AS PointsAgainst,
               SUM(points_for) - SUM(points_against) AS Differential,
               ROUND(100.0 * (SUM(wins) + 0.5 * {draws})
                     / NULLIF(SUM(played), 0), 1) AS WinPct{rank}
          FROM team_seasons
         WHERE played > 0 AND club_now IS NOT NULL{where}
         GROUP BY season, club_now
         ORDER BY Season, Team""", params)


def _derived_team_seasons(con, caps, teams, seasons,
                          include_postseason) -> pd.DataFrame:
    where, params = _season_clause("season", seasons)
    if teams:
        clause, team_params = _in_clause(caps.team_label, teams)
        where += clause
        params += team_params
    if not include_postseason and caps.match_postseason:
        where += " AND is_final = 0"
    return _frame(con, f"""
        SELECT season AS Season, {caps.team_label} AS Team,
               COUNT(*) AS Played,
               SUM(result = 'W') AS Wins,
               SUM(result = 'L') AS Losses,
               SUM(result = 'D') AS Draws,
               SUM(points_for) AS PointsFor,
               SUM(points_against) AS PointsAgainst,
               SUM(points_for) - SUM(points_against) AS Differential,
               ROUND(100.0 * (SUM(result = 'W') + 0.5 * SUM(result = 'D'))
                     / NULLIF(COUNT(*), 0), 1) AS WinPct
          FROM {MATCH_SOURCE}
         WHERE {TRUSTED}{where}
         GROUP BY season, {caps.team_label}
         ORDER BY Season, Team""", params)


@st.cache_data(show_spinner=False, max_entries=16)
def team_options(sport_key: str, revision, _con) -> list:
    """Every club the team charts can draw, alphabetically.

    Alphabetical rather than by record: the order decides which hue each
    club gets, and an ordering that moves when a filter changes repaints
    lines a reader has already learned.
    """
    caps = capabilities(sport_key, revision, _con)
    if caps.team_season_table:
        sql = ("SELECT DISTINCT club_now FROM team_seasons "
               "WHERE club_now IS NOT NULL ORDER BY club_now")
    elif caps.team_match_rows:
        sql = (f"SELECT DISTINCT {caps.team_label} FROM {MATCH_SOURCE} "
               f"WHERE {TRUSTED} AND {caps.team_label} IS NOT NULL "
               f"ORDER BY {caps.team_label}")
    else:
        return []
    try:
        return [row[0] for row in _con.execute(sql)]
    except sqlite3.Error:
        return []


# ----------------------------------------------- 4. volume/efficiency

@st.cache_data(show_spinner=False, max_entries=64)
def volume_efficiency(sport_key: str, revision, _con, stat: str,
                      seasons: tuple = (), min_games: int = 50,
                      limit: int = 2000) -> pd.DataFrame:
    """Career volume against per-game rate, one row per player.

    The rate divides by the games the statistic was *recorded* in. That is
    the only denominator that means anything across an era boundary: a
    career spanning 1960 to 1972 has tackles recorded for its last stretch
    only, and dividing by every game played would report a tackle rate for
    seasons nobody counted tackles in.

    `min_games` is a floor on that recorded count, not on games played,
    for the same reason -- and because a rate over three games is noise
    wearing a leaderboard's clothes.

    `limit` is capped at `charts.SCATTER_CAP`. Past a few thousand marks a
    scatterplot stops being a chart of players and becomes a smear no
    individual point can be hovered or clicked out of.
    """
    import charts
    import sports

    sport = sports.get(sport_key)
    sc = sport.schema
    caps = capabilities(sport_key, revision, _con)
    column = _allowed_stat(caps, stat)
    if column in caps.rate_stats:
        return pd.DataFrame()

    where, params = _season_clause(f"g.{sc.season}", seasons)
    recorded = (f"SUM(CASE WHEN g.{column} IS NOT NULL "
                f"THEN g.{sc.games_per_row} ELSE 0 END)"
                if caps.games_per_row else f"COUNT(g.{column})")
    volume_label = f"{sport.vocab.games.capitalize()} recorded"
    rate_label = f"{labels.title(column)} per {sport.vocab.game}"
    total_label = labels.title(column)

    params = params + [int(min_games), min(int(limit), charts.SCATTER_CAP)]
    return _frame(_con, f"""
        SELECT p.{sc.player_id} AS PlayerID,
               p.{sc.player} AS Player,
               {recorded} AS "{volume_label}",
               SUM(g.{column}) AS "{total_label}",
               ROUND(SUM(g.{column}) * 1.0 / NULLIF({recorded}, 0), 2)
                 AS "{rate_label}",
               MIN(g.{sc.season}) AS "From", MAX(g.{sc.season}) AS "To"
          FROM {sc.games} g
          JOIN {sc.players} p ON p.{sc.player_id} = g.{sc.player_id}
         WHERE g.{column} IS NOT NULL{where}
         GROUP BY g.{sc.player_id}
        HAVING {recorded} >= ?
         ORDER BY SUM(g.{column}) DESC
         LIMIT ?""", params)


def efficiency_labels(sport, stat: str) -> tuple:
    """The volume and rate column names `volume_efficiency` returns."""
    return (f"{sport.vocab.games.capitalize()} recorded",
            f"{labels.title(stat)} per {sport.vocab.game}")


# ----------------------------------------------- 5. match distributions

#: Roughly how many bars a distribution should have. Fewer and the shape
#: is lost to the bucketing; many more and every bar is one match.
_TARGET_BINS = 30


@st.cache_data(show_spinner=False, max_entries=16)
def margin_bin_width(sport_key: str, revision, _con) -> int:
    """A bin width measured from the sport's own scoring scale.

    A six-point bucket is right for Australian football and absurd for
    baseball, where it puts every game in the first two bars. Rather than
    keep a table of per-sport widths -- which is a sport-name branch with
    extra steps -- the width comes from the margins in the file.
    """
    caps = capabilities(sport_key, revision, _con)
    if not caps.team_match_rows:
        return 1
    try:
        peak = _con.execute(
            f"SELECT MAX(margin) FROM {MATCH_SOURCE} WHERE {TRUSTED}"
        ).fetchone()[0]
    except sqlite3.Error:
        return 1
    if not peak:
        return 1
    for width in (1, 2, 3, 5, 6, 10, 12, 15, 20, 25, 50):
        if peak / width <= _TARGET_BINS:
            return width
    return 50


@st.cache_data(show_spinner=False, max_entries=64)
def margin_distribution(sport_key: str, revision, _con, width: int,
                        seasons: tuple = (), split: str = "None"
                        ) -> pd.DataFrame:
    """Winning margins, binned in SQL, optionally split into two series.

    One row per match, taken as `MAX(margin)` over that match's two club
    rows -- the winner's, which is the margin anyone means. A draw is zero
    on both rows and lands in the first bin exactly once. This is done
    without a self-join: the two rows of a match share `source_game_key`,
    so grouping on it is both the cheaper plan and the one that cannot
    double-count a finals match, which has no home side to key on.

    `split` of "Era" compares the first and last thirds of the seasons in
    range. "Home/away" is a different chart -- see `home_away_margins` --
    because a winning margin has no side.
    """
    caps = capabilities(sport_key, revision, _con)
    if not caps.team_match_rows:
        return pd.DataFrame()
    width = max(1, int(width))
    where, params = _season_clause("season", seasons)

    bucket = f"CAST(MAX(margin) / {width} AS INTEGER) * {width}"
    if split == "Era":
        span = seasons or caps.match_season_range
        if not span or span[1] - span[0] < 6:
            return pd.DataFrame()
        low, high = int(span[0]), int(span[1])
        cut = (high - low) // 3
        series = (f"CASE WHEN season <= {low + cut} THEN '{low}-{low + cut}' "
                  f"WHEN season >= {high - cut} THEN '{high - cut}-{high}' "
                  f"END")
        return _frame(_con, f"""
            SELECT Era, Bin, COUNT(*) AS Matches FROM (
                SELECT {bucket} AS Bin, MAX({series}) AS Era
                  FROM {MATCH_SOURCE}
                 WHERE {TRUSTED}{where}
                 GROUP BY source_game_key)
             WHERE Era IS NOT NULL
             GROUP BY Era, Bin
             ORDER BY Era, Bin""", params)

    return _frame(_con, f"""
        SELECT Bin, COUNT(*) AS Matches FROM (
            SELECT {bucket} AS Bin FROM {MATCH_SOURCE}
             WHERE {TRUSTED}{where}
             GROUP BY source_game_key)
         GROUP BY Bin ORDER BY Bin""", params)


@st.cache_data(show_spinner=False, max_entries=64)
def home_away_margins(sport_key: str, revision, _con, width: int,
                      seasons: tuple = ()) -> pd.DataFrame:
    """Signed margins from each side's own perspective.

    Home and away are the two halves of the same matches, so the pair of
    curves is the competition's home advantage drawn directly: two
    distributions of the same shape offset from each other by however much
    the ground is worth.

    Finals are excluded and not folded into either side. `team_position`
    marks them 'F' precisely because they have no home side, and counting
    them as away would move the away curve by a fact about the fixture
    rather than about the venue.
    """
    caps = capabilities(sport_key, revision, _con)
    if not (caps.team_match_rows and caps.home_away):
        return pd.DataFrame()
    width = max(1, int(width))
    where, params = _season_clause("season", seasons)
    return _frame(_con, f"""
        SELECT CASE team_position WHEN 'H' THEN 'Home' ELSE 'Away' END AS Side,
               CAST(margin / {width} AS INTEGER) * {width} AS Bin,
               COUNT(*) AS Matches
          FROM {MATCH_SOURCE}
         WHERE {TRUSTED} AND team_position IN ('H', 'A'){where}
         GROUP BY Side, Bin
         ORDER BY Side, Bin""", params)


@st.cache_data(show_spinner=False, max_entries=32)
def scoring_by_season(sport_key: str, revision, _con,
                      seasons: tuple = ()) -> pd.DataFrame:
    """Average total score and average winning margin, per season.

    Both are per *match*, taken off one row per game key rather than off
    the club rows, where every match appears twice. The total is read as
    `points_for + points_against` on a single row -- which is already the
    whole match -- rather than as the two rows' scores added: the second
    form breaks on a game key that lost one of its two rows, and reads as
    arithmetic on a match rather than on a row.
    """
    caps = capabilities(sport_key, revision, _con)
    if not caps.team_match_rows:
        return pd.DataFrame()
    where, params = _season_clause("season", seasons)
    return _frame(_con, f"""
        SELECT Season,
               ROUND(AVG(Total), 1) AS "Average total score",
               ROUND(AVG(Margin), 1) AS "Average winning margin",
               COUNT(*) AS Matches
          FROM (SELECT season AS Season,
                       MAX(points_for + points_against) AS Total,
                       MAX(margin) AS Margin
                  FROM {MATCH_SOURCE}
                 WHERE {TRUSTED}{where}
                 GROUP BY source_game_key)
         GROUP BY Season ORDER BY Season""", params)


# --------------------------------------------------- 6. data coverage

@st.cache_data(show_spinner=False, max_entries=16)
def stat_coverage(sport_key: str, revision, _con,
                  stats: tuple = ()) -> pd.DataFrame:
    """What share of each season's rows carry each statistic.

    Measured from the data rather than read from the build's own
    `stat_coverage` table. That table records a first and last season,
    which cannot say that hit-outs are on 60% of 1966 rows and 99% of
    1990s ones -- and one of the four builds has no such table at all,
    so measuring is both the fuller answer and the only portable one.

    One pass over `games` with a COUNT per statistic. A season the
    competition did not play has no row and is absent from the result, so
    the heatmap leaves a hole there rather than drawing a black cell that
    reads as "recorded nothing".
    """
    import sports

    sport = sports.get(sport_key)
    sc = sport.schema
    caps = capabilities(sport_key, revision, _con)
    chosen = tuple(stat for stat in (stats or caps.stats)
                   if stat in caps.stats)
    if not chosen:
        return pd.DataFrame()

    counts = ", ".join(f'COUNT({stat}) AS "{stat}"' for stat in chosen)
    frame = _frame(_con, f"""
        SELECT {sc.season} AS Season, COUNT(*) AS Rows_, {counts}
          FROM {sc.games}
         WHERE {sc.season} IS NOT NULL
         GROUP BY {sc.season} ORDER BY {sc.season}""", [])
    if frame.empty:
        return frame

    long = frame.melt(id_vars=["Season", "Rows_"], value_vars=list(chosen),
                      var_name="Statistic", value_name="Recorded")
    long["Coverage"] = (long["Recorded"] / long["Rows_"] * 100).round(1)
    long["Statistic"] = long["Statistic"].map(labels.title)
    # A statistic recorded in no row of a season is a real zero -- the
    # season happened and nobody counted it -- and is kept. A season with
    # no rows at all never reaches here.
    return long[["Season", "Statistic", "Recorded", "Rows_", "Coverage"]]


def coverage_rows(caps: Capabilities, stats: Sequence = ()) -> list:
    """The statistic labels a coverage heatmap should stack, in order."""
    chosen = [stat for stat in (stats or caps.stats) if stat in caps.stats]
    return [labels.title(stat) for stat in chosen]


# ------------------------------------------------------------- venues

@st.cache_data(show_spinner=False, max_entries=32)
def busiest_venues(sport_key: str, revision, _con, seasons: tuple = (),
                   limit: int = 20) -> pd.DataFrame:
    """The grounds a competition actually played at, most-used first.

    Counted as distinct game keys, not as source rows: the table holds one
    row per club per match, so counting rows would report every ground as
    having hosted twice what it did.

    Attendance is averaged over the matches that recorded one and left
    missing where none did. A ground whose crowds were never counted has
    no average rather than an average of nothing.
    """
    caps = capabilities(sport_key, revision, _con)
    if not caps.venue_charts:
        return pd.DataFrame()
    where, params = _season_clause("season", seasons)
    crowd = (", ROUND(AVG(attendance)) AS \"Average crowd\""
             if caps.attendance else "")
    return _frame(_con, f"""
        SELECT venue_raw AS Venue,
               COUNT(DISTINCT source_game_key) AS Matches,
               MIN(season) AS "From", MAX(season) AS "To"{crowd}
          FROM {MATCH_SOURCE}
         WHERE {TRUSTED} AND venue_raw IS NOT NULL
               AND venue_raw <> ''{where}
         GROUP BY venue_raw
         ORDER BY Matches DESC
         LIMIT ?""", params + [max(1, int(limit))])


@st.cache_data(show_spinner=False, max_entries=64)
def venue_by_season(sport_key: str, revision, _con, venues: tuple,
                    seasons: tuple = ()) -> pd.DataFrame:
    """One or more grounds' use, season by season.

    Capped at `charts.MAX_SERIES` grounds, and returns nothing rather than
    a truncated set past it -- the caller is expected to have limited the
    picker, and silently dropping the ninth ground is worse than saying so.
    """
    import charts

    caps = capabilities(sport_key, revision, _con)
    if not caps.venue_charts or not venues or len(venues) > charts.MAX_SERIES:
        return pd.DataFrame()
    where, params = _season_clause("season", seasons)
    marks = ", ".join("?" for _ in venues)
    crowd = (", ROUND(AVG(attendance)) AS \"Average crowd\""
             if caps.attendance else "")
    return _frame(_con, f"""
        SELECT season AS Season, venue_raw AS Venue,
               COUNT(DISTINCT source_game_key) AS Matches{crowd}
          FROM {MATCH_SOURCE}
         WHERE {TRUSTED} AND venue_raw IN ({marks}){where}
         GROUP BY season, venue_raw
         ORDER BY Season, Venue""", list(venues) + params)


VENUE_METRICS = ("Matches", "Average crowd")


# ------------------------------------------------------------- awards

@st.cache_data(show_spinner=False, max_entries=16)
def award_options(sport_key: str, revision, _con) -> pd.DataFrame:
    """Every award the database holds, with its span and how many rows.

    The row count is not a winner count: an All-Australian squad files
    twenty-two rows a season and a Brownlow one, so the two numbers mean
    different things and the page says which it is showing.
    """
    caps = capabilities(sport_key, revision, _con)
    if not caps.award_charts:
        return pd.DataFrame()
    return _frame(_con, f"""
        SELECT {caps.award_key_column} AS Key,
               MAX({caps.award_name_column}) AS Award,
               COUNT(*) AS Records,
               MIN(season) AS "From", MAX(season) AS "To"
          FROM {caps.awards_table}
         WHERE {caps.award_key_column} IS NOT NULL AND season IS NOT NULL
         GROUP BY {caps.award_key_column}
         ORDER BY Records DESC, Award""", [])


@st.cache_data(show_spinner=False, max_entries=64)
def award_by_season(sport_key: str, revision, _con, award: str,
                    seasons: tuple = ()) -> pd.DataFrame:
    """How many of one award were handed out in each season.

    Which is a coverage chart as much as an honours one: a season with no
    row is a season the source has nothing for, and for an award that has
    run continuously that gap is a hole in the data rather than a year
    nobody won it.
    """
    caps = capabilities(sport_key, revision, _con)
    if not caps.award_charts:
        return pd.DataFrame()
    where, params = _season_clause("season", seasons)
    return _frame(_con, f"""
        SELECT season AS Season, COUNT(*) AS Recipients
          FROM {caps.awards_table}
         WHERE {caps.award_key_column} = ? AND season IS NOT NULL{where}
         GROUP BY season ORDER BY season""", [award] + params)


@st.cache_data(show_spinner=False, max_entries=64)
def award_leaders(sport_key: str, revision, _con, award: str,
                  seasons: tuple = (), limit: int = 15) -> pd.DataFrame:
    """Who has the most of one award, and over what span.

    The recipient's name is a column in two of the three builds and a
    join in the third -- the MLB's awards table carries nothing but a
    Lahman player id. Where the id is there it comes back as `PlayerID`
    so a click can open the card; where it is not, the name is text and
    the row simply does not open.
    """
    import sports

    sport = sports.get(sport_key)
    sc = sport.schema
    caps = capabilities(sport_key, revision, _con)
    if not caps.award_charts:
        return pd.DataFrame()
    where, params = _season_clause("a.season", seasons)

    if caps.award_player_id_column:
        # Named from `players` where the join lands, and from the award's
        # own column otherwise, so an unlinked row still shows a name.
        fallback = (f"a.{caps.award_recipient_column}"
                    if caps.award_recipient_column else "NULL")
        select = (f"COALESCE(p.{sc.player}, {fallback}) AS Recipient, "
                  f"a.{caps.award_player_id_column} AS PlayerID")
        join = (f" LEFT JOIN {sc.players} p "
                f"ON p.{sc.player_id} = a.{caps.award_player_id_column}")
        group = f"a.{caps.award_player_id_column}, Recipient"
    elif caps.award_recipient_column:
        select = f"a.{caps.award_recipient_column} AS Recipient"
        join, group = "", "Recipient"
    else:
        return pd.DataFrame()

    return _frame(_con, f"""
        SELECT {select}, COUNT(*) AS Awards,
               MIN(a.season) AS "First", MAX(a.season) AS "Last"
          FROM {caps.awards_table} a{join}
         WHERE a.{caps.award_key_column} = ?{where}
         GROUP BY {group}
        HAVING Recipient IS NOT NULL AND Recipient <> ''
         ORDER BY Awards DESC, "First"
         LIMIT ?""", [award] + params + [max(1, int(limit))])
