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
sport's own module: constraints.py for the AFL, constraints_nba.py for the
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

    The defaults are the AFL build produced by build_db.py. The NBA build
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
    n_clubs: str = "n_clubs"
    clubs_hist: str = "clubs_hist"
    obscurity: str = "obscurity"

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
    rebuild_cmd: str = "python build_db.py"

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
        s = self.s
        return (f"SELECT DISTINCT {s.player_id} FROM {s.games} "
                f"WHERE {s.club_now} = ? OR {s.club_hist} = ?", [club, club])

    def debut_club(self, club):
        """First career game was for this club."""
        s = self.s
        return (f"""SELECT DISTINCT {s.player_id} FROM {s.games}
                    WHERE {s.career_game_no} = 1
                      AND ({s.club_now} = ? OR {s.club_hist} = ?)""",
                [club, club])

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

    def played_in_season_range(self, lo, hi):
        s = self.s
        return (f"SELECT DISTINCT {s.player_id} FROM {s.games} "
                f"WHERE {s.season} BETWEEN ? AND ?", [lo, hi])

    def debuted_between(self, lo, hi):
        s = self.s
        return (f"SELECT {s.player_id} FROM {s.players} "
                f"WHERE {s.debut_season} BETWEEN ? AND ?", [lo, hi])

    # -- teammates --------------------------------------------------
    def teammate_of_id(self, player_id):
        """Teammate lookup by id -- unambiguous even for namesakes."""
        s = self.s
        return (f"""SELECT DISTINCT g.{s.player_id} FROM {s.games} g
                    JOIN (SELECT DISTINCT {s.club_now}, {s.season}
                          FROM {s.games} WHERE {s.player_id} = ?) w
                      ON g.{s.club_now} = w.{s.club_now}
                     AND g.{s.season} = w.{s.season}
                    WHERE g.{s.player_id} <> ?""", [player_id, player_id])

    def teammate_of(self, name):
        """
        Name-based teammate lookup, kept for grid labels that only give a
        surname. Matches the full name first and falls back to a surname
        match, which unions the club-seasons of every namesake -- a
        superset, never a missed answer. Prefer teammate_of_id().
        """
        s = self.s
        return (f"""SELECT DISTINCT g.{s.player_id} FROM {s.games} g
                    JOIN (SELECT DISTINCT {s.club_now}, {s.season}, {s.player}
                          FROM {s.games}
                          WHERE LOWER({s.player}) = LOWER(?)
                             OR (LOWER({s.player}) LIKE ? AND NOT EXISTS (
                                   SELECT 1 FROM {s.players}
                                   WHERE LOWER({s.player}) = LOWER(?)))) w
                      ON g.{s.club_now} = w.{s.club_now}
                     AND g.{s.season} = w.{s.season}
                    WHERE LOWER(g.{s.player}) <> LOWER(w.{s.player})""",
                [name, f"% {name.lower()}", name])

    # -- venues ------------------------------------------------------
    def played_at_venue(self, venue):
        s = self.s
        return (f"SELECT DISTINCT {s.player_id} FROM {s.games} "
                f"WHERE {s.venue} LIKE ?",
                [f"%{s.canonical_venue(venue)}%"])

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

def _where(constraints, schema: Schema):
    frags, params = [], []
    for sql, p in constraints:
        frags.append(f"p.{schema.player_id} IN ({sql})")
        params.extend(p)
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


@dataclass
class Square:
    """Everything a grid square shows before it is opened."""
    eligible: int
    best: tuple | None          # the top-ranked row, per `columns`
    obscurity: float | None     # 0-100 database proxy for the best answer
    defined: bool = True

    @property
    def stars(self):
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
    query = (
        f"SELECT {select}, p.{schema.obscurity} AS __obscurity, "
        f"COUNT(*) OVER() AS __eligible "
        f"FROM {schema.players} p WHERE {where} "
        f"ORDER BY {schema.order_map()[order]} LIMIT 1"
    )
    row = con.execute(query, params).fetchone()
    if row is None:
        return Square(0, None, None)
    width = len(cols)
    best = tuple(row[:width])
    return Square(int(row[width + 1]), best, row[width])


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


def to_standalone_sql(constraints, schema: Schema, limit=25):
    """Render an intersection as a single pasteable SQL statement."""
    frags = []
    for sql, p in constraints:
        for value in p:
            sql = sql.replace("?", _sql_literal(value), 1)
        frags.append(f"  p.{schema.player_id} IN (\n    "
                     + "\n    ".join(sql.split("\n")) + "\n  )")
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
#: likely to be, derived from career length, era and club spread. Gridley's
#: own rarity percentage comes from what real players actually guessed and
#: is not knowable offline.
STAR_DISCLAIMER = ("Stars are this database's obscurity proxy, derived from "
                   "career length, era and club spread — not the live crowd "
                   "rarity percentage the puzzle itself reports.")

#: The tooltip form. Shown only on hover, so it repeats the point without
#: taking up screen space. Visible prose should use STAR_DISCLAIMER once
#: per page and not again.
STAR_TOOLTIP = "Database obscurity proxy, not crowd rarity"


def star_value(obscurity, scale=STAR_SCALE):
    """Map a 0-100 obscurity score onto 0-5 stars in half-star steps."""
    if obscurity is None:
        return None
    v = max(0.0, min(float(obscurity), scale)) / scale * STAR_MAX
    return round(v * 2) / 2


def stars_text(obscurity, scale=STAR_SCALE):
    """'★★★★☆ 4.5/5' -- safe for dataframes, CLI output and buttons."""
    v = star_value(obscurity, scale)
    if v is None:
        return "—"
    full = int(v)
    half = (v - full) >= 0.5
    glyphs = "★" * full + ("⯨" if half else "") + "☆" * (STAR_MAX - full -
                                                         (1 if half else 0))
    return f"{glyphs} {v:g}/5"


def stars_html(obscurity, scale=STAR_SCALE, cls="stars"):
    """
    Half-star-accurate rendering: a filled row clipped to the score sits
    over a hollow row. Avoids relying on a half-star glyph being present
    in the user's font.
    """
    v = star_value(obscurity, scale)
    if v is None:
        return "<span class='stars stars-none'>—</span>"
    pct = v / STAR_MAX * 100
    return (f"<span class='{cls}' title='{STAR_TOOLTIP}'>"
            f"<span class='stars-back'>{'★' * STAR_MAX}</span>"
            f"<span class='stars-fore' style='width:{pct:.1f}%'>"
            f"{'★' * STAR_MAX}</span></span>"
            f"<span class='stars-num'>{v:g}/5</span>")
