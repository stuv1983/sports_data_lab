"""
core.py -- The sport-agnostic half of the constraint engine.

Everything here is true of any sport whose database exposes a `players`
table (one row per person, with a career summary and an obscurity score)
and a `games` table (one row per player per game). Nothing here knows what
a goal, a final or a wooden spoon is.

Three things live in this module:

  1. Schema        -- names the columns a sport's tables actually use, so
                      the generic SQL below can be written once.
  2. Generic       -- the constraint builders that differ between sports
                      only by column name or vocabulary list.
  3. The engine    -- solve / count / square / to_standalone_sql, plus the
                      star-rating helpers the UI displays.

Sport-specific constraints (finals semantics, drafts, awards) stay in the
sport's own module: afl/constraints.py for the AFL, nba/constraints_nba.py for the
NBA. See sports.py for how the two are bound together.
"""

from dataclasses import dataclass, field
from typing import Sequence

# A constraint is always (sql, params) where sql selects player_id.
Constraint = tuple


# --------------------------------------------------------------- schema

@dataclass(frozen=True)
class Schema:
    """
    Column and table names for one sport's database.

    The defaults are the AFL build produced by afl/build_db.py. The NBA build
    should reuse as many of them as it honestly can -- every name that
    matches is a page of explore.py that needs no changes at all.
    """
    players: str = "players"
    games: str = "games"

    # players
    player_id: str = "player_id"
    player: str = "player"
    debut_season: str = "debut_season"
    final_season: str = "final_season"
    career_games: str = "career_games"
    career_score: str = "career_goals"      # NBA: career_points
    career_postseason: str = "finals_played"  # NBA: playoffs_played
    birth_year: str = "birth_year"
    birth_country: str = "birth_country"
    n_clubs: str = "n_clubs"
    clubs_hist: str = "clubs_hist"
    obscurity: str = "obscurity"

    #: Optional card-back bio columns on `players`. Empty string means the
    #: sport's data does not carry it -- the AFL and MLB builds have no
    #: height, weight, position or draft data, while the NFL and NBA
    #: (position/height/weight only) builds do. A page that renders these
    #: shows only the fields whose name is set here.
    position: str = ""
    height: str = ""
    weight: str = ""
    college: str = ""
    draft_year: str = ""
    #: Units the height/weight columns are actually stored in, so a page
    #: can format them without guessing. NFL stores inches and pounds; NBA
    #: stores centimetres and kilograms.
    height_unit: str = "in"
    weight_unit: str = "lb"

    # games
    season: str = "season"
    date: str = "date"
    club_now: str = "club_now"
    club_hist: str = "club_hist"
    venue: str = "venue"
    round: str = "round"
    opponent: str = "opponent"
    career_game_no: str = "career_game_no"
    game_score: str = "goals"               # NBA: points
    is_final: str = "is_final"              # NBA: is_playoff
    result: str = "result"

    # Vocabulary lists the generic builders validate against.
    stats: Sequence[str] = ()
    clubs: Sequence[str] = ()
    venue_aliases: dict = field(default_factory=dict)

    #: Current club name -> every identity that counts as that club.
    #:
    #: A club square asks "played for this club", and for a club formed by
    #: a merger or a relocation that includes the predecessors. Where the
    #: build already normalises the predecessor into the current name via
    #: club_now the entry is a no-op and exists only so the rule is stated
    #: in one place rather than being an emergent property of the loader.
    #:
    #: Expansion is one-directional: asking for the current club includes
    #: its predecessors, but asking for a predecessor by name returns only
    #: that club. A Fitzroy square is about Fitzroy.
    club_lineage: dict = field(default_factory=dict)

    def club_identities(self, club) -> list:
        """Every identity a club-name constraint should match."""
        return list(self.club_lineage.get(club, [club]))

    #: Optional exact override for the columns solve() returns, as a
    #: sequence of (sql_expression, header). Set this when callers already
    #: depend on a specific column order -- the AFL build does.
    solve_cols: Sequence[tuple] = ()

    # Columns solve() returns, in order, with their display headers.
    def solve_columns(self):
        if self.solve_cols:
            return list(self.solve_cols)
        return [
            (f"p.{self.player}", "Player"),
            (f"p.{self.debut_season}", "From"),
            (f"p.{self.final_season}", "To"),
            (f"p.{self.career_games}", "Games"),
            (f"p.{self.career_score}", "Score"),
            (f"p.{self.obscurity}", "Obscurity"),
        ]

    def solve_index(self, column):
        """Where `column` sits in a solve_columns() row, or None.

        Positional, because that is how the rows come back: solve() and
        square() return raw tuples in solve_columns() order, and every
        sport orders and names its own columns.
        """
        expression = f"p.{column}"
        for i, (expr, _) in enumerate(self.solve_columns()):
            if expr == expression:
                return i
        return None

    def career_span(self, row):
        """`'1990–2004'` for one solve_columns() row, or "" if it cannot say.

        The grid tile shows a single name, and a name alone does not say
        whether the answer is a contemporary or someone from the 1920s --
        which is most of what a solver wants to know before typing it in.
        """
        if not row:
            return ""
        first = self.solve_index(self.debut_season)
        last = self.solve_index(self.final_season)
        if first is None or last is None:
            return ""
        debut = row[first] if first < len(row) else None
        final = row[last] if last < len(row) else None
        if debut is None and final is None:
            return ""
        if debut is None or final is None:
            return str(debut if debut is not None else final)
        return str(debut) if debut == final else f"{debut}–{final}"

    def _header_for(self, column, fallback):
        """The display header solve_columns() gave one column."""
        expression = f"p.{column}"
        for expr, header in self.solve_columns():
            if expr == expression:
                return header
        return fallback

    def clubs_hist_header(self):
        """What the club-history column is called in a results table.

        The AFL says "Clubs" and the NBA says "Teams", and app.py used to
        test for the literal "Clubs" -- which silently stopped finding the
        column the moment a second sport named it something else.
        """
        return self._header_for(self.clubs_hist, "Clubs")

    def obscurity_header(self):
        return self._header_for(self.obscurity, "Obscurity")

    def order_map(self):
        return {
            "obscurity": f"p.{self.obscurity} DESC, p.{self.career_games} ASC",
            "fewest games": f"p.{self.career_games} ASC",
            "oldest": f"p.{self.final_season} ASC",
            "newest": f"p.{self.final_season} DESC",
        }

    # Columns whose absence means the database predates a migration.
    # Additional sport-specific mandatory columns. Core fields named above
    # are checked dynamically, so an NBA schema can rename playoffs/points
    # without inheriting AFL literal column names.
    required_games_cols: Sequence[str] = ()
    required_player_cols: Sequence[str] = ("name_key",)

    #: How a career column on `players` reconciles against `games`, as
    #: {players column: (aggregate expression, WHERE predicate or "")}.
    #:
    #: health.career_totals_reconcile defaults to "one games row is one
    #: game, and a career total sums every row", which is true of the AFL
    #: and NBA builds and false of the MLB one -- Lahman has no box scores,
    #: so a row there is a player's season with one team and career_games
    #: is SUM(games) over the regular-season rows. A sport whose grain
    #: differs states it here rather than health.py learning three sports'
    #: worth of special cases. An entry mapping to None skips the check.
    career_totals_sql: dict = field(default_factory=dict)
    rebuild_cmd: str = "python -m afl.build_db"

    def canonical_venue(self, name):
        return self.venue_aliases.get(str(name).strip().lower(), name)


# ------------------------------------------------------ generic builders

class Generic:
    """
    Constraint builders that are identical across sports once the schema
    names them. A sport module instantiates this once and re-exports the
    bound methods under its own module-level names, so callers keep the
    flat `C.played_for(...)` contract they already have.
    """

    def __init__(self, schema: Schema):
        self.s = schema

    # -- membership ------------------------------------------------
    def played_for(self, club):
        """Played at least one game for this club, predecessors included.

        Brisbane Lions were formed from Fitzroy and the Brisbane Bears, and
        a Lions square counts all three -- which is what the puzzle's own
        wording says. Matching `club_now` alone returned 249 players and
        silently dropped 1,300.
        """
        s = self.s
        names = self.s.club_identities(club)
        marks = ",".join("?" for _ in names)
        # UNION lets SQLite use the separate current/historical club indexes.
        # The equivalent OR form scanned the full AFL games table whenever a
        # lineage such as Brisbane contained several identities.
        sql = (f"SELECT {s.player_id} FROM {s.games} "
               f"WHERE {s.club_now} IN ({marks}) UNION "
               f"SELECT {s.player_id} FROM {s.games} "
               f"WHERE {s.club_hist} IN ({marks})")
        return sql, [*names, *names]

    def debut_club(self, club):
        """First career game was for this club."""
        s = self.s
        names = self.s.club_identities(club)
        marks = ",".join("?" for _ in names)
        return (f"""SELECT DISTINCT {s.player_id} FROM {s.games}
                    WHERE {s.career_game_no} = 1
                      AND ({s.club_now} IN ({marks})
                           OR {s.club_hist} IN ({marks}))""",
                [*names, *names])

    def one_club_player(self):
        s = self.s
        return (f"SELECT {s.player_id} FROM {s.players} "
                f"WHERE {s.n_clubs} = 1", [])

    def played_for_n_clubs(self, n):
        s = self.s
        return (f"SELECT {s.player_id} FROM {s.players} "
                f"WHERE {s.n_clubs} >= ?", [n])

    def multi_club_player(self):
        return self.played_for_n_clubs(2)

    # -- career totals ---------------------------------------------
    def career_games_min(self, n):
        s = self.s
        return (f"SELECT {s.player_id} FROM {s.players} "
                f"WHERE {s.career_games} >= ?", [n])

    def career_games_max(self, n):
        s = self.s
        return (f"SELECT {s.player_id} FROM {s.players} "
                f"WHERE {s.career_games} <= ?", [n])

    def career_score_min(self, n):
        s = self.s
        return (f"SELECT {s.player_id} FROM {s.players} "
                f"WHERE {s.career_score} >= ?", [n])

    def career_score_max(self, n):
        """
        At most n career goals/points -- INCLUSIVE of n.

        Callers translating a strict phrase ("less than 20 goals") must
        subtract one themselves. Keeping the boundary arithmetic in the
        parser, where the wording is visible, avoids two functions that
        differ by one and are chosen by guesswork.
        """
        s = self.s
        return (f"SELECT {s.player_id} FROM {s.players} "
                f"WHERE {s.career_score} <= ?", [n])

    def career_score_between(self, lo, hi):
        s = self.s
        return (f"SELECT {s.player_id} FROM {s.players} "
                f"WHERE {s.career_score} BETWEEN ? AND ?", [lo, hi])

    # -- single-game stats -----------------------------------------
    def _check(self, *stats):
        for stat in stats:
            if stat not in self.s.stats:
                raise ValueError(f"unknown stat: {stat}")

    def stat_in_a_game(self, stat, n):
        self._check(stat)
        s = self.s
        return (f"SELECT DISTINCT {s.player_id} FROM {s.games} "
                f"WHERE {stat} >= ?", [n])

    def two_stats_same_game(self, stat_a, n_a, stat_b, n_b):
        self._check(stat_a, stat_b)
        s = self.s
        return (f"SELECT DISTINCT {s.player_id} FROM {s.games} "
                f"WHERE {stat_a} >= ? AND {stat_b} >= ?", [n_a, n_b])

    # -- per-club aggregates ---------------------------------------
    def score_at_multiple_clubs(self, score=30, clubs=2):
        """e.g. 30+ goals for each of two different clubs."""
        s = self.s
        return (f"""SELECT {s.player_id} FROM (
                      SELECT {s.player_id}, {s.club_now},
                             SUM({s.game_score}) AS agg
                      FROM {s.games} GROUP BY {s.player_id}, {s.club_now}
                      HAVING agg >= ?
                    ) GROUP BY {s.player_id} HAVING COUNT(*) >= ?""",
                [score, clubs])

    def games_at_multiple_clubs(self, games=50, clubs=2):
        s = self.s
        return (f"""SELECT {s.player_id} FROM (
                      SELECT {s.player_id}, {s.club_now}, COUNT(*) AS n
                      FROM {s.games} GROUP BY {s.player_id}, {s.club_now}
                      HAVING n >= ?
                    ) GROUP BY {s.player_id} HAVING COUNT(*) >= ?""",
                [games, clubs])

    # -- seasons ----------------------------------------------------
    #: Minimum appearances before a season average is treated as real.
    #: A player who managed one game and took six marks in it has not
    #: "averaged 5+ marks in a season" in any sense a puzzle means, and
    #: without a floor those one-game seasons dominate an obscurity-ranked
    #: result list. Five is a judgement call, not a Gridley-documented
    #: rule -- see SEASON_AVG_MIN_GAMES in the sport module.
    SEASON_AVG_MIN_GAMES = 5

    def season_stat_average_min(self, stat, avg, min_games=None):
        """
        Averaged `avg` or more of `stat` per game across a whole season.

        Grouped by (player, season), so one qualifying season is enough.
        Games where the stat is NULL are excluded rather than counted as
        zero: before a stat was recorded the column is empty, and treating
        those as zeroes would silently penalise every pre-1965 season.
        """
        self._check(stat)
        s = self.s
        floor = (self.SEASON_AVG_MIN_GAMES if min_games is None
                 else int(min_games))
        return (f"""SELECT DISTINCT {s.player_id} FROM (
                      SELECT {s.player_id} FROM {s.games}
                      WHERE {stat} IS NOT NULL
                      GROUP BY {s.player_id}, {s.season}
                      HAVING COUNT(*) >= ? AND AVG({stat}) >= ?
                    )""", [floor, avg])

    def season_stat_total_min(self, stat, n):
        """
        Accumulated `n` or more of `stat` within a single season.

        Grouped by (player, season), so one qualifying season is enough.
        No minimum-games floor, unlike season_stat_average_min: a total is
        not distorted by a short season. A player who reached the threshold
        reached it, however many games it took.

        Games where the stat is NULL are excluded rather than counted as
        zero. Before a stat was recorded the column is empty, and summing
        those as zeroes yields a real-looking season total of 0 that
        positively asserts something the source never said.
        """
        self._check(stat)
        s = self.s
        return (f"""SELECT DISTINCT {s.player_id} FROM (
                      SELECT {s.player_id} FROM {s.games}
                      WHERE {stat} IS NOT NULL
                      GROUP BY {s.player_id}, {s.season}
                      HAVING SUM({stat}) >= ?
                    )""", [n])

    # -- career totals and averages for any stat -------------------
    #: Minimum career games before a career average is treated as real.
    #: Higher than the season floor: a career average over three games is
    #: not a career average in any sense the question means.
    CAREER_AVG_MIN_GAMES = 20

    def career_stat_total_min(self, stat, n):
        """
        Accumulated `n` or more of `stat` across a whole career.

        Only `career_goals` and `career_games` were ever pre-aggregated on
        the players table, so every other statistic had no career question
        at all -- '500+ career marks' could not be asked. This sums the
        game rows instead, which works for any stat without a rebuild.

        NULL games are excluded rather than counted as zero, so a career
        spanning the start of a stat's recording era counts only the part
        that was actually recorded.
        """
        self._check(stat)
        s = self.s
        return (f"""SELECT {s.player_id} FROM {s.games}
                    WHERE {stat} IS NOT NULL
                    GROUP BY {s.player_id} HAVING SUM({stat}) >= ?""", [n])

    def career_stat_average_min(self, stat, avg, min_games=None):
        """
        Averaged `avg` or more of `stat` per game across a career.

        The games floor is over games where the stat was recorded, not over
        the whole career: a player who straddles 1965 is judged on the part
        of their career the source can actually speak to.
        """
        self._check(stat)
        s = self.s
        floor = (self.CAREER_AVG_MIN_GAMES if min_games is None
                 else int(min_games))
        return (f"""SELECT {s.player_id} FROM {s.games}
                    WHERE {stat} IS NOT NULL
                    GROUP BY {s.player_id}
                    HAVING COUNT(*) >= ? AND AVG({stat}) >= ?""",
                [floor, avg])

    def games_with_stat_min(self, stat, n, times=1):
        """
        Reached `n` of `stat` in at least `times` separate games.

        '10+ games with 30+ disposals' is a different question from either
        a single game or a career total, and is the one a lot of squares
        actually ask: not a one-off, and not an aggregate either.
        """
        self._check(stat)
        s = self.s
        return (f"""SELECT {s.player_id} FROM {s.games}
                    WHERE {stat} >= ?
                    GROUP BY {s.player_id} HAVING COUNT(*) >= ?""",
                [n, times])

    def stat_in_a_postseason_game(self, stat, n):
        """Reached `n` of `stat` in a single finals game."""
        self._check(stat)
        s = self.s
        return (f"""SELECT DISTINCT {s.player_id} FROM {s.games}
                    WHERE {s.is_final} = 1 AND {stat} >= ?""", [n])

    def postseason_stat_average_min(self, stat, avg, min_games=None):
        """Averaged `avg` or more of `stat` across finals."""
        self._check(stat)
        s = self.s
        floor = 3 if min_games is None else int(min_games)
        return (f"""SELECT {s.player_id} FROM {s.games}
                    WHERE {s.is_final} = 1 AND {stat} IS NOT NULL
                    GROUP BY {s.player_id}
                    HAVING COUNT(*) >= ? AND AVG({stat}) >= ?""",
                [floor, avg])

    def played_in_season_range(self, lo, hi):
        s = self.s
        return (f"SELECT DISTINCT {s.player_id} FROM {s.games} "
                f"WHERE {s.season} BETWEEN ? AND ?", [lo, hi])

    def played_in_round_min(self, round_code, appearances=1):
        """Appeared in a named title round in at least N seasons."""
        s = self.s
        return (f"""SELECT {s.player_id} FROM {s.games}
                    WHERE UPPER(TRIM({s.round})) = UPPER(?)
                    GROUP BY {s.player_id}
                    HAVING COUNT(DISTINCT {s.season}) >= ?""",
                [round_code, appearances])

    def round_outcome_min(self, round_code, result, appearances=1):
        """Recorded an outcome in a named title round in at least N seasons."""
        s = self.s
        return (f"""SELECT {s.player_id} FROM {s.games}
                    WHERE UPPER(TRIM({s.round})) = UPPER(?)
                      AND UPPER(TRIM({s.result})) = UPPER(?)
                    GROUP BY {s.player_id}
                    HAVING COUNT(DISTINCT {s.season}) >= ?""",
                [round_code, result, appearances])

    def debuted_between(self, lo, hi):
        s = self.s
        return (f"SELECT {s.player_id} FROM {s.players} "
                f"WHERE {s.debut_season} BETWEEN ? AND ?", [lo, hi])

    # -- teammates --------------------------------------------------
    def played_with_id(self, player_id):
        """Players who represented the same club in the same season."""
        s = self.s
        return (f"""SELECT DISTINCT g.{s.player_id} FROM {s.games} g
                    JOIN (SELECT DISTINCT {s.club_now}, {s.season}
                          FROM {s.games} WHERE {s.player_id} = ?) w
                      ON g.{s.club_now} = w.{s.club_now}
                     AND g.{s.season} = w.{s.season}
                    WHERE g.{s.player_id} <> ?""", [player_id, player_id])

    def teammate_of_id(self, player_id):
        """Players who appeared in the same match for the same club."""
        s = self.s
        return (f"""SELECT DISTINCT g.{s.player_id} FROM {s.games} g
                    JOIN (SELECT DISTINCT {s.club_hist}, {s.season},
                                          {s.date}, {s.opponent}
                          FROM {s.games} WHERE {s.player_id} = ?) w
                      ON g.{s.club_hist} = w.{s.club_hist}
                     AND g.{s.season} = w.{s.season}
                     AND g.{s.date} = w.{s.date}
                     AND g.{s.opponent} = w.{s.opponent}
                    WHERE g.{s.player_id} <> ?""", [player_id, player_id])

    def teammate_of(self, name):
        """
        Name-based teammate lookup, kept for grid labels that only give a
        surname. Matches the full name first and falls back to a surname
        match, which unions the actual matches of every namesake -- a
        superset, never a missed answer. Prefer teammate_of_id().
        """
        s = self.s
        return (f"""SELECT DISTINCT g.{s.player_id} FROM {s.games} g
                    JOIN (SELECT DISTINCT {s.club_hist}, {s.season},
                                          {s.date}, {s.opponent}, {s.player}
                          FROM {s.games}
                          WHERE LOWER({s.player}) = LOWER(?)
                             OR (LOWER({s.player}) LIKE ? AND NOT EXISTS (
                                   SELECT 1 FROM {s.players}
                                   WHERE LOWER({s.player}) = LOWER(?)))) w
                      ON g.{s.club_hist} = w.{s.club_hist}
                     AND g.{s.season} = w.{s.season}
                     AND g.{s.date} = w.{s.date}
                     AND g.{s.opponent} = w.{s.opponent}
                    WHERE LOWER(g.{s.player}) <> LOWER(w.{s.player})""",
                [name, f"% {name.lower()}", name])

    # -- venues ------------------------------------------------------
    def played_at_venue(self, venue):
        s = self.s
        return (f"SELECT DISTINCT {s.player_id} FROM {s.games} "
                f"WHERE {s.venue} LIKE ?",
                [f"%{s.canonical_venue(venue)}%"])

    def played_at_venues(self, venues):
        """Played at any venue in a pre-resolved geographic group."""
        s = self.s
        names = tuple(dict.fromkeys(str(v) for v in venues if v))
        if not names:
            return (f"SELECT {s.player_id} FROM {s.games} WHERE 0", [])
        placeholders = ",".join("?" for _ in names)
        sql = (f"SELECT DISTINCT {s.player_id} FROM {s.games} "
               f"WHERE {s.venue} IN ({placeholders})")
        return sql, list(names)

    # -- post-season (generic shape; sports name it finals or playoffs) --
    def postseason_games_min(self, n):
        s = self.s
        return (f"""SELECT {s.player_id} FROM {s.games} WHERE {s.is_final} = 1
                    GROUP BY {s.player_id} HAVING COUNT(*) >= ?""", [n])

    def played_postseason(self):
        return self.postseason_games_min(1)

    def never_played_postseason(self):
        s = self.s
        return (f"""SELECT {s.player_id} FROM {s.players}
                    WHERE {s.player_id} NOT IN
                    (SELECT {s.player_id} FROM {s.games}
                     WHERE {s.is_final} = 1)""", [])

    def no_postseason_wins(self):
        """Played at least one post-season game, never won one."""
        s = self.s
        return (f"""SELECT {s.player_id} FROM {s.games} WHERE {s.is_final} = 1
                    GROUP BY {s.player_id}
                    HAVING SUM(CASE WHEN {s.result} = 'W' THEN 1 ELSE 0 END)
                           = 0""", [])

    def won_postseason(self):
        """Won at least one post-season game (any round, any venue)."""
        s = self.s
        return (f"""SELECT DISTINCT {s.player_id} FROM {s.games}
                    WHERE {s.is_final} = 1 AND {s.result} = 'W'""", [])

    def never_won_postseason(self):
        """Includes players who never played a post-season game at all."""
        s = self.s
        return (f"""SELECT {s.player_id} FROM {s.players}
                    WHERE {s.player_id} NOT IN
                    (SELECT {s.player_id} FROM {s.games}
                     WHERE {s.is_final} = 1 AND {s.result} = 'W')""", [])

    def won_postseason_at(self, venue):
        s = self.s
        return (f"""SELECT DISTINCT {s.player_id} FROM {s.games}
                    WHERE {s.is_final} = 1 AND {s.result} = 'W'
                      AND {s.venue} LIKE ?""",
                [f"%{s.canonical_venue(venue)}%"])

    def score_average_in_postseason(self, avg=1.0):
        s = self.s
        return (f"""SELECT {s.player_id} FROM {s.games} WHERE {s.is_final} = 1
                    GROUP BY {s.player_id}
                    HAVING COUNT(*) > 0
                       AND SUM(COALESCE({s.game_score},0)) * 1.0
                           / COUNT(*) >= ?""", [avg])


# ------------------------------------------------------------- schema check

def have_tables(con, *names):
    """True if every named table exists."""
    present = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    return set(names) <= present


def require_schema(con, schema: Schema):
    """Fail loudly at startup if the database predates a migration."""
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({schema.games})")}
    required_games = set(schema.required_games_cols) | {
        schema.player_id, schema.player, schema.season, schema.date,
        schema.club_now, schema.club_hist, schema.venue, schema.round,
        schema.opponent, schema.career_game_no, schema.game_score,
        schema.is_final, schema.result,
    }
    missing = required_games - cols
    if missing:
        raise RuntimeError(
            f"{schema.games} table is missing {sorted(missing)}. "
            f"Rebuild with: {schema.rebuild_cmd}")
    pcols = {r[1] for r in con.execute(f"PRAGMA table_info({schema.players})")}
    required_players = set(schema.required_player_cols) | {
        schema.player_id, schema.player, schema.debut_season,
        schema.final_season, schema.career_games, schema.career_score,
        schema.career_postseason, schema.birth_year, schema.n_clubs,
        schema.clubs_hist, schema.obscurity,
    }
    pmissing = required_players - pcols
    if pmissing:
        raise RuntimeError(
            f"{schema.players} table is missing {sorted(pmissing)}. "
            f"Rebuild with: {schema.rebuild_cmd}")


# ------------------------------------------------------------- the engine

#: Marker for a constraint that is a predicate over ONE row of `games`
#: rather than a subquery returning player_ids. A fragment reading
#: "@row:team@club_now = ?" says "this row is for that club".
#:
#: It exists to express the Immaculate Grid pairing rule: when a team axis
#: meets a season-stat axis, the stat must have been achieved *with that
#: team, in one season*. Intersecting two independent player_id sets gives
#: "played for Cleveland at some point" AND "had a 100-RBI season for
#: anyone", which accepted 113 players for a square with 42 real answers --
#: Bobby Bonds drove in 102 for the Giants in 1971 and played Cleveland in
#: 1979, and the square took him.
#:
#: Only the MLB uses this: its `games` row already *is* a
#: (player, season, team), so one row carries both halves of the question.
#: A sport whose row is a single game cannot answer it this way and its
#: builders keep returning ordinary subqueries.
ROW_MARKER = "@row:"


def _row_parts(sql):
    """('team'|'stat', predicate) for a row-scoped fragment, else None."""
    if not sql.startswith(ROW_MARKER):
        return None
    kind, _, predicate = sql[len(ROW_MARKER):].partition("@")
    return kind, predicate


def _group_constraints(constraints):
    """Split into (combined row predicates, standalone fragments).

    The team is what ties a stat to a season, so predicates are merged only
    when exactly one team is involved:

      team x season-stat  -> merged; same row, so same team and season.
      team x team         -> NOT merged. "Played for both" is two different
                             rows, and one row cannot be two clubs at once,
                             so merging would make every such square empty.
      stat x stat         -> NOT merged, per the rule that two non-team
                             categories need not fall in the same season.
    """
    teams, stats, plain = [], [], []
    for sql, params in constraints:
        parts = _row_parts(sql)
        if parts is None:
            plain.append((sql, params))
            continue
        kind, predicate = parts
        (teams if kind == "team" else stats).append((predicate, params))

    if len(teams) == 1 and stats:
        return [teams[0]] + stats, plain
    # Nothing to tie together: every row predicate stands on its own, which
    # is exactly the old "played for X at any time" behaviour.
    return [], plain + [(f"@solo@{p}", pr) for p, pr in teams + stats]


def _row_exists(predicates, schema: Schema):
    """One row-scoped player set carrying every predicate at once.

    An uncorrelated ``IN`` lets SQLite start with selective game indexes
    (notably ``club_now``) and materialize a small player-id set. The former
    correlated ``EXISTS`` scanned game rows once for every player and made a
    two-franchise MLB square take over a minute on the production database.
    """
    where = " AND ".join(p for p, _ in predicates)
    params = [v for _, values in predicates for v in values]
    return (f"p.{schema.player_id} IN ("
            f"SELECT g.{schema.player_id} FROM {schema.games} g "
            f"WHERE {where})"), params


def _standalone(sql, params, schema: Schema):
    """A single fragment as its own WHERE clause."""
    if sql.startswith("@solo@"):
        return _row_exists([(sql[len("@solo@"):], params)], schema)
    return f"p.{schema.player_id} IN ({sql})", list(params)


def _where(constraints, schema: Schema):
    merged, plain = _group_constraints(constraints)
    frags, params = [], []
    if merged:
        sql, values = _row_exists(merged, schema)
        frags.append(sql)
        params.extend(values)
    for sql, p in plain:
        clause, values = _standalone(sql, p, schema)
        frags.append(clause)
        params.extend(values)
    return (" AND ".join(frags) if frags else "1=1"), params


def solve(con, constraints, schema: Schema, limit=25, order="obscurity",
          columns=None):
    """Intersect constraints and return ranked players."""
    where, params = _where(constraints, schema)
    order_sql = schema.order_map()[order]
    cols = columns or schema.solve_columns()
    select = ", ".join(expr for expr, _ in cols)
    q = (f"SELECT {select} FROM {schema.players} p WHERE {where} "
         f"ORDER BY {order_sql} LIMIT ?")
    return con.execute(q, params + [limit]).fetchall()


def count(con, constraints, schema: Schema):
    """How many players satisfy every constraint."""
    where, params = _where(constraints, schema)
    return con.execute(
        f"SELECT COUNT(*) FROM {schema.players} p WHERE {where}",
        params).fetchone()[0]


def matches_player(con, player_id, constraints, schema: Schema):
    """Return whether one server-selected player satisfies every criterion.

    Play mode uses the same compiled predicates as the solver.  The player id
    remains a bound parameter, so submitting an answer cannot alter the query.
    """
    where, params = _where(constraints, schema)
    return con.execute(
        f"SELECT 1 FROM {schema.players} p "
        f"WHERE p.{schema.player_id}=? AND {where} LIMIT 1",
        [player_id, *params],
    ).fetchone() is not None


@dataclass
class Square:
    """Everything a grid square shows before it is opened."""
    eligible: int
    best: tuple | None          # the top-ranked row, per `columns`
    obscurity: float | None     # 0-100 database proxy for the best answer
    defined: bool = True
    #: The obscurity spread across every answer to this square, which is
    #: what the stars are scaled against. None on an undefined or empty
    #: square, where there is nothing to compare.
    obscurity_min: float | None = None
    obscurity_max: float | None = None

    @property
    def stars(self):
        """Stars for the shown answer, scaled within this square."""
        return star_value(self.obscurity, lo=self.obscurity_min,
                          hi=self.obscurity_max)

    @property
    def absolute_stars(self):
        """Stars on the whole-database scale, for comparing across squares."""
        return star_value(self.obscurity)

    @property
    def best_name(self):
        return self.best[0] if self.best else None


def square(con, constraints, schema: Schema, order="obscurity",
           columns=None):
    """Return the eligible count and best answer in one SQLite query."""
    if not constraints:
        return Square(0, None, None, defined=False)

    where, params = _where(constraints, schema)
    cols = columns or schema.solve_columns()
    select = ", ".join(expr for expr, _ in cols)
    # The hidden fields keep Square stable even when a caller requests a
    # custom result column list that does not include obscurity.
    # The window functions have no PARTITION, so they are computed over the
    # whole matching set before LIMIT takes the top row. That gives the
    # square's obscurity spread for star scaling in the same single query
    # the square already cost.
    query = (
        f"SELECT {select}, p.{schema.obscurity} AS __obscurity, "
        f"COUNT(*) OVER() AS __eligible, "
        f"MIN(p.{schema.obscurity}) OVER() AS __obs_min, "
        f"MAX(p.{schema.obscurity}) OVER() AS __obs_max "
        f"FROM {schema.players} p WHERE {where} "
        f"ORDER BY {schema.order_map()[order]} LIMIT 1"
    )
    row = con.execute(query, params).fetchone()
    if row is None:
        return Square(0, None, None)
    width = len(cols)
    best = tuple(row[:width])
    return Square(int(row[width + 1]), best, row[width],
                  obscurity_min=row[width + 2], obscurity_max=row[width + 3])


def _sql_literal(value):
    """Render a Python parameter as a safe standalone SQLite literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "X'" + bytes(value).hex() + "'"
    return str(value)


def _inline(sql, params):
    for value in params:
        sql = sql.replace("?", _sql_literal(value), 1)
    return sql


def to_standalone_sql(constraints, schema: Schema, limit=25):
    """Render an intersection as a single pasteable SQL statement.

    Groups exactly as _where does. The pasteable SQL is the page's evidence
    for its own answer, so a merged team-and-season square has to show the
    single EXISTS that produced it rather than two innocent-looking IN
    clauses that would return a different set.
    """
    merged, plain = _group_constraints(constraints)
    frags = []
    if merged:
        sql, values = _row_exists(merged, schema)
        frags.append("  " + _inline(sql, values))
    for sql, p in plain:
        clause, values = _standalone(sql, p, schema)
        if clause.startswith("EXISTS"):
            frags.append("  " + _inline(clause, values))
        else:
            frags.append(f"  p.{schema.player_id} IN (\n    "
                         + "\n    ".join(_inline(sql, p).split("\n")) + "\n  )")
    where = "\n  AND ".join(frags) if frags else "1=1"
    select = ",\n       ".join(expr for expr, _ in schema.solve_columns())
    return (f"SELECT {select}\n"
            f"FROM {schema.players} p\nWHERE\n{where}\n"
            f"ORDER BY {schema.order_map()['obscurity']}\nLIMIT {int(limit)};")


# --------------------------------------------------------- star ratings

STAR_SCALE = 100.0          # players.obscurity is stored 0-100
STAR_MAX = 5

#: Shown wherever a star rating appears. The stars are a property of this
#: database, not of the puzzle: they rank how rarely-picked an answer is
#: likely to be. Gridley's own rarity percentage comes from what real
#: players actually guessed and is not knowable offline.
#:
#: The listed terms must stay in step with OBSCURITY_WEIGHTS in build_db.
#: This used to claim "club spread", which the formula has never included --
#: and the direction is not even obvious, since a player at several clubs
#: qualifies for more squares and so may be *easier* to recall.
STAR_DISCLAIMER = ("Stars are this database's obscurity proxy, derived from "
                   "games played, career span, era, goals, finals and "
                   "Brownlow votes — not the live crowd rarity percentage "
                   "the puzzle itself reports.")

#: The tooltip form. Shown only on hover, so it repeats the point without
#: taking up screen space. Visible prose should use STAR_DISCLAIMER once
#: per page and not again.
STAR_TOOLTIP = "Database obscurity proxy, not crowd rarity"


def star_value(obscurity, scale=STAR_SCALE, lo=None, hi=None):
    """Map an obscurity score onto 0-5 stars in half-star steps.

    With `lo` and `hi`, the score is rescaled against that range instead of
    the absolute 0-100 one, so five stars means "the rarest answer to this
    square" rather than "the most obscure player in the database".

    The absolute scale made the rating nearly useless on a board: obscurity
    is a whole-database percentile, so reaching 5/5 took the single most
    obscure player of 13,353 and real squares clustered between 1.5 and 4
    however rare or common their answers actually were. Within one square
    the spread is what a solver cares about.

    A square whose answers all share one obscurity, including a square with
    a single answer, has no spread to rescale against. Those score 5: the
    only answer to a square is trivially its rarest.
    """
    if obscurity is None:
        return None
    if lo is None or hi is None:
        v = max(0.0, min(float(obscurity), scale)) / scale * STAR_MAX
        return round(v * 2) / 2
    lo, hi = float(lo), float(hi)
    if hi <= lo:
        return float(STAR_MAX)
    v = (max(lo, min(float(obscurity), hi)) - lo) / (hi - lo) * STAR_MAX
    return round(v * 2) / 2


def stars_text(obscurity, scale=STAR_SCALE, lo=None, hi=None):
    """'★★★★☆ 4.5/5' -- safe for dataframes, CLI output and buttons."""
    v = star_value(obscurity, scale, lo, hi)
    if v is None:
        return "—"
    full = int(v)
    half = (v - full) >= 0.5
    glyphs = "★" * full + ("⯨" if half else "") + "☆" * (STAR_MAX - full -
                                                         (1 if half else 0))
    return f"{glyphs} {v:g}/5"


def stars_html(obscurity, scale=STAR_SCALE, cls="stars", lo=None, hi=None):
    """
    Half-star-accurate rendering: a filled row clipped to the score sits
    over a hollow row. Avoids relying on a half-star glyph being present
    in the user's font.
    """
    v = star_value(obscurity, scale, lo, hi)
    if v is None:
        return "<span class='stars stars-none'>—</span>"
    pct = v / STAR_MAX * 100
    return (f"<span class='{cls}' title='{STAR_TOOLTIP}'>"
            f"<span class='stars-back'>{'★' * STAR_MAX}</span>"
            f"<span class='stars-fore' style='width:{pct:.1f}%'>"
            f"{'★' * STAR_MAX}</span></span>"
            f"<span class='stars-num'>{v:g}/5</span>")
