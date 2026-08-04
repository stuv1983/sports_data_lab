"""
sports.py -- The registry that lets one app serve multiple sports.

Each Sport binds together a database file, a core.Schema naming its
columns, the module holding its sport-specific constraints, and the
vocabulary the UI puts on screen ("club" vs "team" vs "franchise", 
"final" vs "playoff" vs "postseason").

Adding a sport is one entry here plus one constraints module. Nothing in
app.py or explore.py should ever branch on `sport.key`; if it needs to,
the difference belongs in this file as a vocabulary or schema field.
"""

from data_paths import sport_db
import importlib
import sqlite3
from dataclasses import dataclass, field
from typing import Sequence

import core
import obscurity
from mlb import mlb_reference
from nba import nba_reference


# ------------------------------------------------------------ vocabulary

@dataclass(frozen=True)
class Vocab:
    """Words the UI substitutes so pages can be written once."""
    club: str = "club"
    clubs: str = "clubs"
    game: str = "game"
    games: str = "games"
    season: str = "season"
    venue: str = "venue"
    venues: str = "venues"
    score: str = "goals"            # the headline career counting stat
    postseason: str = "finals"
    postseason_one: str = "final"
    title: str = "premiership"
    grid_source: str = "Gridley"

    def title_case(self, word):
        return getattr(self, word).capitalize()


# ---------------------------------------------------------------- sport

@dataclass(frozen=True)
class Sport:
    key: str
    label: str
    icon: str
    db: str
    module: str                     # dotted name of the constraints module
    schema: core.Schema
    vocab: Vocab
    theme: str = "afl"              # default palette name in theme.py
    build_cmd: str = "python -m afl.build_db"
    missing_db_hint: str = ""
    #: Shown when a square returns nothing, to explain era gaps honestly.
    empty_hint: str = ""
    #: Stat -> first season it was recorded. Constraints on a stat outside
    #: its era must be declined, not silently answered from partial data.
    stat_eras: dict = field(default_factory=dict)
    enabled: bool = True
    #: The obscurity.Model this sport's scores were produced by.
    obscurity_model: object = None
    #: Optional imported club catalogue shown after the standard layers.
    club_data_table: str = ""
    #: Optional broad-family availability probe on the constraints module.
    family_probe: str = ""
    #: What one row of the `games` table actually is, when it is not a
    #: player-game. Empty means it is, and the status panel says
    #: "Player-<games>". The MLB build sets it because Lahman's finest
    #: grain is a player's season with one team: labelling 147,199
    #: season lines "Player-games" would state a number that is not true.
    games_row_label: str = ""

    # -- capabilities --------------------------------------------------
    criterion_parser: str = ""
    grid_library: bool = False
    game_lab_module: str = ""
    has_club_explorer: bool = False
    has_awards_page: bool = False
    has_past_games: bool = False
    loader_hints: dict = field(default_factory=dict)
    club_data_tables: frozenset = frozenset()
    club_data_hint: str = ""
    family_hint: str = ""
    search_examples: tuple = ()
    grid_defaults: tuple = ()
    venue_display: dict = field(default_factory=dict)

    @property
    def star_disclaimer(self):
        if self.obscurity_model is None:
            return core.STAR_DISCLAIMER
        return obscurity.disclaimer(self.obscurity_model, self.vocab)

    def missing_layer_hints(self, con):
        for label, probe in self.optional_layers.items():
            hint = self.loader_hints.get(probe)
            if hint and not self.layer_ready(probe, con):
                yield label, hint

    # -- module access ------------------------------------------------
    @property
    def C(self):
        """The sport's constraints module, imported on first use."""
        return importlib.import_module(self.module)

    # -- namespacing --------------------------------------------------
    def k(self, *parts):
        """A session-state / widget key namespaced to this sport."""
        return ":".join((self.key,) + tuple(str(p) for p in parts))

    # -- database -----------------------------------------------------
    def connect(self):
        return sqlite3.connect(f"file:{self.db}?mode=ro", uri=True,
                               check_same_thread=False)

    def exists(self):
        try:
            con = self.connect()
            con.execute(f"SELECT 1 FROM {self.schema.players} LIMIT 1")
            return True
        except sqlite3.OperationalError:
            return False

    # -- era honesty ---------------------------------------------------
    def stat_available_from(self, stat):
        return self.stat_eras.get(stat)

    def stat_era_warning(self, stat, season_from=None):
        first = self.stat_eras.get(stat)
        if first is None:
            return None
        if season_from is None or season_from < first:
            return (f"{stat.replace('_', ' ')} was not recorded before "
                    f"{first} — players from earlier {self.vocab.season}s "
                    f"cannot satisfy this square.")
        return None

    # -- status line ---------------------------------------------------
    def status(self, con):
        s = self.schema
        lo, hi = con.execute(
            f"SELECT MIN({s.season}), MAX({s.season}) FROM {s.games}"
        ).fetchone()
        players = con.execute(
            f"SELECT COUNT(*) FROM {s.players}").fetchone()[0]
        appearances = con.execute(
            f"SELECT COUNT(*) FROM {s.games}").fetchone()[0]
        rows = [
            ("Seasons", f"{lo}–{hi}"),
            ("Players", f"{players:,}"),
            (self.games_row_label or f"Player-{self.vocab.games}",
             f"{appearances:,}"),
        ]
        for label, probe in self.optional_layers.items():
            if not self.layer_ready(probe, con):
                rows.append((label, "not loaded"))
                continue
            rows.append((label, self.layer_value(probe, con)))

        if self.club_data_table:
            rows.append(("Club data", self.club_data_value(con)))
        if self.family_probe:
            rows.append(("Family links", self.family_links_value(con)))
        return rows

    def layer_value(self, probe, con):
        counter = getattr(self.C, probe.replace("_available", "_count"), None)
        if counter is not None:
            try:
                total = counter(con)
                if total is not None:
                    return f"ready ({int(total):,})"
            except Exception:
                pass
        return "ready"

    def layer_ready(self, probe, con):
        fn = getattr(self.C, probe, None)
        if fn is None:
            return False
        try:
            return bool(fn(con))
        except Exception:
            return False

    @staticmethod
    def _table_exists(con, table):
        if not table:
            return False
        try:
            return bool(con.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type IN ('table','view') AND name=?", (table,)
            ).fetchone())
        except sqlite3.Error:
            return False

    @staticmethod
    def _table_columns(con, table):
        try:
            return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            return set()

    def club_data_value(self, con):
        table = self.club_data_table
        if not self._table_exists(con, table):
            return "not loaded"
        columns = self._table_columns(con, table)
        try:
            if "is_current" in columns:
                total = con.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE is_current=1"
                ).fetchone()[0]
            else:
                total = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            return "not loaded"
        if not total:
            return "not loaded"
        noun = "club" if int(total) == 1 else "clubs"
        return f"ready ({int(total):,} current {noun})"

    def family_links_value(self, con):
        ready = self.layer_ready(self.family_probe, con)
        if not ready:
            ready = (self._table_exists(con, "family_members") and
                     self._table_exists(con, "family_relationships"))
        if not ready:
            return "not loaded"

        members = self._family_counter(
            con, "family_member_count", self._fallback_family_members
        )
        relationships = self._family_counter(
            con, "trusted_relationship_count",
            self._fallback_family_relationships,
        )
        if members is not None and relationships is not None:
            return (f"ready ({members:,} linked players; "
                    f"{relationships:,} explicit relationships)")
        if members is not None:
            return f"ready ({members:,} linked players)"
        return "ready"

    def _family_counter(self, con, name, fallback):
        counter = getattr(self.C, name, None)
        if counter is not None:
            try:
                value = counter(con)
                if value is not None:
                    return int(value)
            except Exception:
                pass
        try:
            value = fallback(con)
            return None if value is None else int(value)
        except sqlite3.Error:
            return None

    def _fallback_family_members(self, con):
        if not self._table_exists(con, "family_members"):
            return None
        columns = self._table_columns(con, "family_members")
        where = ["player_id IS NOT NULL"] if "player_id" in columns else []
        if "match_status" in columns:
            where.append("match_status IN ('unique','resolved')")
        predicate = " WHERE " + " AND ".join(where) if where else ""
        distinct = "DISTINCT player_id" if "player_id" in columns else "*"
        return con.execute(
            f"SELECT COUNT({distinct}) FROM family_members{predicate}"
        ).fetchone()[0]

    def _fallback_family_relationships(self, con):
        if not self._table_exists(con, "family_relationships"):
            return None
        return con.execute(
            "SELECT COUNT(*) FROM family_relationships"
        ).fetchone()[0]

    optional_layers: dict = field(default_factory=dict)


# ------------------------------------------------------------------ AFL

AFL_STATS = ["disposals", "kicks", "handballs", "marks", "goals", "behinds",
             "tackles", "hitouts", "inside50s", "clearances", "rebounds",
             "contested", "contested_marks", "marks_i50", "one_percenters",
             "bounces", "goal_assists", "brownlow", "frees_for", 
             "frees_against", "clangers", "uncontested"]

AFL_CLUB_LINEAGE = {
    "Brisbane Lions": ["Brisbane Lions", "Brisbane Bears", "Fitzroy"],
    "Sydney": ["Sydney", "South Melbourne"],
    "Western Bulldogs": ["Western Bulldogs", "Footscray"],
    "North Melbourne": ["North Melbourne", "Kangaroos"],
}

AFL_CLUBS = ["Adelaide", "Brisbane Lions", "Carlton",
             "Collingwood", "Essendon", "Fitzroy", "Fremantle", "Geelong",
             "Gold Coast", "GWS", "Hawthorn", "Melbourne", "North Melbourne",
             "Port Adelaide", "Richmond", "St Kilda", "Sydney", "University",
             "West Coast", "Western Bulldogs"]

AFL_VENUE_ALIASES = {
    "marvel stadium": "Docklands", "marvel": "Docklands",
    "etihad stadium": "Docklands", "telstra dome": "Docklands",
    "docklands": "Docklands", "colonial stadium": "Docklands",
    "mcg": "M.C.G.", "m.c.g.": "M.C.G.", "melbourne cricket ground": "M.C.G.",
    "scg": "S.C.G.", "s.c.g.": "S.C.G.",
    "gabba": "Gabba", "kardinia park": "Kardinia Park",
    "gmhba stadium": "Kardinia Park", "skilled stadium": "Kardinia Park",
    "optus stadium": "Perth Stadium", "perth stadium": "Perth Stadium",
    "subiaco": "Subiaco", "adelaide oval": "Adelaide Oval",
    "football park": "Football Park", "aami stadium": "Football Park",
    "princes park": "Princes Park", "waverley": "Waverley Park",
    "victoria park": "Victoria Park", "windy hill": "Windy Hill",
}

AFL_SCHEMA = core.Schema(
    career_score="career_goals",
    game_score="goals",
    is_final="is_final",
    stats=AFL_STATS,
    clubs=AFL_CLUBS,
    club_lineage=AFL_CLUB_LINEAGE,
    venue_aliases=AFL_VENUE_ALIASES,
    rebuild_cmd="python -m afl.build_db",
    solve_cols=(
        ("p.player", "Player"),
        ("p.debut_season", "From"),
        ("p.final_season", "To"),
        ("p.career_games", "Games"),
        ("p.career_goals", "Goals"),
        ("p.finals_played", "Finals"),
        ("p.clubs_hist", "Clubs"),
        ("p.obscurity", "Obscurity"),
    ),
)

AFL = Sport(
    key="afl",
    label="AFL Data Lab",
    icon="🏉",
    db=sport_db("afl", "gridley.db"),
    module="afl.constraints",
    schema=AFL_SCHEMA,
    vocab=Vocab(),
    theme="afl",
    missing_db_hint=("No AFL database found at "
                     f"{sport_db('afl', 'gridley.db')}. "
                     "Run `python -m afl.build_db` first."),
    empty_hint=("Nothing satisfies both. Note that disposals, marks and "
                "tackles are not recorded before 1965 — no earlier player "
                "can have them."),
    stat_eras={"goals": 1897, "brownlow": 1931,
               "disposals": 1965, "kicks": 1965, "handballs": 1965,
               "marks": 1965, "frees_for": 1965, "frees_against": 1965,
               "behinds": 1965, "hitouts": 1966, "tackles": 1987,
               "inside50s": 1998, "clearances": 1998, "rebounds": 1998,
               "clangers": 1998,
               "contested": 1999, "contested_marks": 1999,
               "marks_i50": 1999, "one_percenters": 1999,
               "uncontested": 1999, "bounces": 1999,
               "goal_assists": 2003},
    optional_layers={"Draft data": "draft_available",
                     "Award data": "awards_available",
                     "Captain data": "captain_available",
                     "Rising Star": "rising_star_available"},
    obscurity_model=obscurity.AFL_MODEL,
    club_data_table="clubs",
    family_probe="family_relationships_available",
    criterion_parser="afl.parse_criteria",
    grid_library=True,
    game_lab_module="afl.game_lab",
    has_club_explorer=True,
    has_awards_page=True,
    has_past_games=True,
    loader_hints={
        "draft_available": "Run `afl/load_draftguru.py`, then `afl/link_draft.py`.",
        "awards_available": "Run `afl/load_draftguru.py`, then `afl/link_people.py`.",
        "captain_available": "Run `afl/load_captains.py` for club-captain data.",
        "rising_star_available": ("Run `afl/fetch_footywire_rising_star.py`, "
                                  "then `afl/load_rising_star.py`."),
    },
    club_data_tables=frozenset({
        "clubs", "club_source_snapshots", "club_wikipedia_fields",
        "club_player_totals", "club_player_register", "club_player_records",
    }),
    club_data_hint=("Run `utils/fetch_club_sources.py`, then "
                    "`utils/load_club_sources.py` for Club Explorer."),
    family_hint=("Run `afl/scrape_wikipedia_families.py`, then "
                 "`afl/load_family_relationships.py` for family links."),
    search_examples=(
        'club:Hawthorn games>=200 sort:obscurity',
        'game.disposals>=40 postseason:true',
        'season.goals>=50 debut:1990..1999',
        'career.marks>=1000 avg.disposals>=20',
        'award:brownlow-medal',
        'club:Fitzroy club_any:Carlton',
    ),
    grid_defaults=(
        (("Played for club", {"club": "St Kilda"}),
         ("Played for club", {"club": "North Melbourne"}),
         ("150+ / X+ career games", {"games": 150})),
        (("X+ goals at 2+ clubs", {"goals": 30, "clubs": 2}),
         ("Teammate of…", {"player": "Mason Wood"}),
         ("No finals wins (played finals)", {})),
    ),
    venue_display={
        "Docklands": "Marvel Stadium",
        "Kardinia Park": "GMHBA Stadium",
        "Perth Stadium": "Optus Stadium",
        "Football Park": "AAMI Stadium",
        "M.C.G.": "MCG",
        "S.C.G.": "SCG",
        "Western Oval": "Whitten Oval",
        "Princes Park": "Optus Oval",
        "Carrara": "People First Stadium",
        "Sydney Showground": "ENGIE Stadium",
        "York Park": "UTAS Stadium",
        "Manuka Oval": "Manuka",
    },
)


# ------------------------------------------------------------------ NBA

NBA_STATS = ["points", "rebounds", "assists", "steals", "blocks",
             "turnovers", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta",
             "oreb", "dreb", "minutes", "plus_minus", "fouls"]

NBA_SCHEMA = core.Schema(
    career_score="career_points",
    career_postseason="playoffs_played",
    game_score="points",
    is_final="is_playoff",
    stats=NBA_STATS,
    clubs=nba_reference.teams(),
    club_lineage=nba_reference.club_lineage(),
    venue_aliases=nba_reference.venue_aliases(),
    required_player_cols=("name_key", "career_minutes"),
    rebuild_cmd="python -m nba.build_nba_db",
    solve_cols=(
        ("p.player", "Player"),
        ("p.debut_season", "From"),
        ("p.final_season", "To"),
        ("p.career_games", "Games"),
        ("p.career_points", "Points"),
        ("p.playoffs_played", "Playoffs"),
        ("p.clubs_hist", "Teams"),
        ("p.obscurity", "Obscurity"),
    ),
)

NBA = Sport(
    key="nba",
    label="NBA Data Lab",
    icon="🏀",
    db=sport_db("nba"),
    module="nba.constraints_nba",
    schema=NBA_SCHEMA,
    vocab=Vocab(club="team", clubs="teams", game="game", games="games",
                season="season", venue="arena", venues="arenas",
                score="points", postseason="playoffs",
                postseason_one="playoff game", title="championship",
                grid_source="Immaculate Grid"),
    theme="nba",
    build_cmd="python -m nba.build_nba_db",
    missing_db_hint=(f"No NBA database found at {sport_db('nba')}. "
                     "Run `python -m nba.build_nba_db` first."),
    empty_hint=("Nothing satisfies both. Steals, blocks, turnovers and the "
                "offensive/defensive rebound split are not recorded before "
                "1973-74, and three-pointers not before 1979-80."),
    stat_eras=nba_reference.stat_eras(),
    optional_layers={"Draft data": "draft_available",
                     "Award data": "awards_available"},
    obscurity_model=obscurity.NBA_MODEL,
    search_examples=(
        'club:"Boston Celtics" games>=500 sort:obscurity',
        'game.points>=50 postseason:true',
        'season.points>=2000 debut:1980..1999',
        'club:"Seattle SuperSonics" club_any:"Oklahoma City Thunder"',
        'career.rebounds>=10000 avg.assists>=6',
        'played:1996..2005 sort:fewest_games',
    ),
    grid_defaults=(
        (("Played for club", {"club": "Boston Celtics"}),
         ("Played for club", {"club": "Los Angeles Lakers"}),
         ("150+ / X+ career games", {"games": 500})),
        (("X+ goals at 2+ clubs", {"goals": 1000, "clubs": 2}),
         ("X+ of a stat in one game", {"stat": "points", "x": 40}),
         ("No finals wins (played finals)", {})),
    ),
)


# ------------------------------------------------------------------ MLB
#
# A row of the MLB `games` table is a player's *season* with one team, not
# a game: Lahman has no box scores. build_mlb_db.py's docstring has the
# full account, and constraints_mlb.py declines the per-game squares
# rather than answering them from season totals.

MLB_STATS = ["home_runs", "hits", "rbis", "stolen_bases", "walks",
             "strikeouts", "at_bats", "runs", "doubles", "triples",
             "wins", "losses", "era", "saves"]

MLB_SCHEMA = core.Schema(
    career_score="career_home_runs",
    career_postseason="postseason_played",
    game_score="home_runs",
    is_final="is_postseason",
    stats=MLB_STATS,
    clubs=mlb_reference.teams(),
    club_lineage=mlb_reference.club_lineage(),
    venue_aliases=mlb_reference.venue_aliases(),
    rebuild_cmd="python -m mlb.build_mlb_db",
    #: A row is a season, so career_games is SUM(games), not COUNT(*), and
    #: the career batting totals are regular season -- which is what the
    #: sport means by a career home-run count.
    career_totals_sql={
        "career_games": ("SUM(g.games)", "g.is_postseason = 0"),
        "career_home_runs": ("SUM(g.home_runs)", "g.is_postseason = 0"),
        "postseason_played": ("SUM(g.games)", "g.is_postseason = 1"),
    },
    solve_cols=(
        ("p.player", "Player"),
        ("p.debut_season", "From"),
        ("p.final_season", "To"),
        ("p.career_games", "Games"),
        ("p.career_home_runs", "Home Runs"),
        ("p.postseason_played", "Postseason"),
        ("p.clubs_hist", "Franchises"),
        ("p.obscurity", "Obscurity"),
    ),
)

MLB = Sport(
    key="mlb",
    label="MLB Data Lab",
    icon="⚾",
    db=sport_db("mlb"),
    module="mlb.constraints_mlb",
    schema=MLB_SCHEMA,
    vocab=Vocab(club="franchise", clubs="franchises", game="game", games="games",
                season="season", venue="ballpark", venues="ballparks",
                score="home runs", postseason="postseason",
                postseason_one="postseason game", title="World Series",
                grid_source="Immaculate Grid"),
    theme="mlb",
    build_cmd="python -m mlb.build_mlb_db",
    missing_db_hint=(f"No MLB database found at {sport_db('mlb')}. "
                     "Run `python -m mlb.build_mlb_db` first."),
    empty_hint=("Nothing satisfies both. Note that a row here is a player's "
                "season with one team rather than a game -- Lahman has no "
                "box scores, so there are no single-game squares -- and "
                "that 19th-century seasons are missing many RBI, stolen "
                "base and strikeout lines even though the columns exist."),
    stat_eras=mlb_reference.stat_eras(),
    games_row_label="Player-seasons",
    optional_layers={"Awards data": "awards_available",
                     "Hall of Fame": "hall_of_fame_available"},
    obscurity_model=obscurity.MLB_MODEL,
    search_examples=(
        'club:"New York Yankees" games>=1000 sort:obscurity',
        'season.home_runs>=40 debut:1990..1999',
        'career.hits>=3000',
        'club:"Brooklyn Dodgers" club_any:"Los Angeles Dodgers"',
        'played:1946..1955 sort:fewest_games',
    ),
    grid_defaults=(
        (("Played for club", {"club": "New York Yankees"}),
         ("Played for club", {"club": "Boston Red Sox"}),
         ("150+ / X+ career games", {"games": 1000})),
        (("X+ goals at 2+ clubs", {"goals": 100, "clubs": 2}),
         ("X+ of a stat in one season", {"stat": "home_runs", "x": 40}),
         ("No finals wins (played finals)", {})),
    ),
)


# -------------------------------------------------------------- registry

SPORTS = {s.key: s for s in (AFL, NBA, MLB)}
DEFAULT = AFL.key


def get(key):
    return SPORTS.get(key, SPORTS[DEFAULT])


def selectable():
    """Sports worth offering: enabled, with a database actually present."""
    out = [s for s in SPORTS.values() if s.enabled and s.exists()]
    return out or [SPORTS[DEFAULT]]


def picker(st, key="sport"):
    """
    Render the sport switcher and return the chosen Sport.

    Call this before anything else touches the database. When the sport
    changes, every widget key belonging to the previous sport is dropped,
    which is what stops a stale AFL axis from leaking into an NBA board.
    """
    options = selectable()
    if len(options) == 1:
        st.session_state[key] = options[0].key
        return options[0]

    labels = {s.key: f"{s.icon}  {s.label}" for s in options}
    chosen = st.sidebar.radio(
        "Sport", [s.key for s in options],
        format_func=lambda k: labels[k], key=key, horizontal=True)

    previous = st.session_state.get("_sport_prev")
    if previous and previous != chosen:
        stale = [k for k in st.session_state
                 if isinstance(k, str) and k.startswith(f"{previous}:")]
        for k in stale:
            del st.session_state[k]
        st.session_state.pop("loaded", None)
        st.session_state.pop("cell", None)
    st.session_state["_sport_prev"] = chosen
    return get(chosen)