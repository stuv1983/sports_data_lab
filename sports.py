"""
sports.py -- The registry that lets one app serve two sports.

Each Sport binds together a database file, a core.Schema naming its
columns, the module holding its sport-specific constraints, and the
vocabulary the UI puts on screen ("club" vs "team", "final" vs "playoff").

Adding a sport is one entry here plus one constraints module. Nothing in
app.py or explore.py should ever branch on `sport.key`; if it needs to,
the difference belongs in this file as a vocabulary or schema field.

Two rules that are easy to get wrong and expensive to debug:

  * Every @st.cache_data function must take `sport.key` as a HASHED
    argument. Connections are conventionally passed as `_con`, which
    Streamlit deliberately does not hash, so swapping databases will not
    invalidate a cache keyed only on the connection. You will get AFL
    players in the NBA picker.

  * Every widget key must go through `sport.k()`. Streamlit's session
    state is flat, so an axis left set to "Played for club / St Kilda"
    survives a switch to the NBA and throws on the club lookup.
"""

from data_paths import sport_db
import importlib
import sqlite3
from dataclasses import dataclass, field
from typing import Sequence

import core


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
    build_cmd: str = "python build_db.py"
    missing_db_hint: str = ""
    #: Shown when a square returns nothing, to explain era gaps honestly.
    empty_hint: str = ""
    #: Stat -> first season it was recorded. Constraints on a stat outside
    #: its era must be declined, not silently answered from partial data.
    stat_eras: dict = field(default_factory=dict)
    enabled: bool = True
    #: Optional imported club catalogue shown after the standard layers.
    club_data_table: str = ""
    #: Optional broad-family availability probe on the constraints module.
    family_probe: str = ""

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
        """
        A human sentence if a stat predates its recording, else None.
        Call this before answering a stat square rather than after.
        """
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
        """
        Live counts for the Database Status panel. Returns a list of
        (label, value) so the panel renders identically for any sport.

        Optional layers are probed by calling the sport's own module. An
        earlier version of this file listed the tables itself and guessed
        one of them wrong, so a fully loaded award layer reported as "not
        loaded". The module that defines a layer is the only thing that
        should know how to detect it.
        """
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
            (f"Player-{self.vocab.games}", f"{appearances:,}"),
        ]
        for label, probe in self.optional_layers.items():
            if not self.layer_ready(probe, con):
                rows.append((label, "not loaded"))
                continue
            # A layer may optionally expose a matching ``*_count`` function
            # reporting how many trusted rows it contributes. This keeps
            # counts inside the same aligned panel instead of a separate
            # caption in a different font.
            rows.append((label, self.layer_value(probe, con)))

        # These richer AFL layers need descriptive counts rather than the
        # generic single-number optional-layer format. Keeping them here puts
        # every ready state in the same aligned status block.
        if self.club_data_table:
            rows.append(("Club data", self.club_data_value(con)))
        if self.family_probe:
            rows.append(("Family links", self.family_links_value(con)))
        return rows

    def layer_value(self, probe, con):
        """"ready", or "ready (1,375)" when the layer reports a count."""
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
        """Call the named availability function on the sport's module."""
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
        """Status text for the optional current-club catalogue."""
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
        """Status text for linked family members and explicit relationships."""
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
        # Source relationships are already explicit. The constraints module's
        # trusted counter remains authoritative when available.
        return con.execute(
            "SELECT COUNT(*) FROM family_relationships"
        ).fetchone()[0]

    #: Optional import layers: display label -> the name of the zero-cost
    #: availability function on the sport's constraints module. Never list
    #: table names here; the module already knows them.
    optional_layers: dict = field(default_factory=dict)


# ------------------------------------------------------------------ AFL

AFL_STATS = ["disposals", "kicks", "handballs", "marks", "goals", "behinds",
             "tackles", "hitouts", "inside50s", "clearances", "rebounds",
             "contested", "contested_marks", "marks_i50", "one_percenters",
             "bounces", "goal_assists", "brownlow"]

AFL_CLUBS = ["Adelaide", "Brisbane Bears", "Brisbane Lions", "Carlton",
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
    venue_aliases=AFL_VENUE_ALIASES,
    rebuild_cmd="python build_db.py",
    # app.py, fetch_grid.py and the tests index this tuple positionally,
    # so the order is part of the contract. Obscurity stays last: core's
    # square() reads the final column as the rating.
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
    module="constraints",
    schema=AFL_SCHEMA,
    vocab=Vocab(),
    theme="afl",
    missing_db_hint=("No AFL database found at "
                     f"{sport_db('afl', 'gridley.db')}. "
                     "Run `python build_db.py` first."),
    empty_hint=("Nothing satisfies both. Note that disposals, marks and "
                "tackles are not recorded before 1965 — no earlier player "
                "can have them."),
    stat_eras={"disposals": 1965, "kicks": 1897, "handballs": 1965,
               "marks": 1965, "tackles": 1987, "hitouts": 1987,
               "inside50s": 1998, "clearances": 1998, "rebounds": 1998,
               "contested": 1998, "contested_marks": 1998,
               "marks_i50": 1998, "one_percenters": 1998,
               "goal_assists": 1998, "brownlow": 1902},
    optional_layers={"Draft data": "draft_available",
                     "Award data": "awards_available",
                     "Captain data": "captain_available",
                     "Rising Star": "rising_star_available"},
    club_data_table="clubs",
    family_probe="family_relationships_available",
)


# ------------------------------------------------------------------ NBA
# The schema deliberately reuses every AFL column name that means the same
# thing, so explore.py's pages work unchanged. Only the three names that
# genuinely differ are overridden.

NBA_STATS = ["points", "rebounds", "assists", "steals", "blocks",
             "turnovers", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta",
             "oreb", "dreb", "minutes", "plus_minus", "fouls"]

NBA_SCHEMA = core.Schema(
    career_score="career_points",
    game_score="points",
    is_final="is_playoff",
    stats=NBA_STATS,
    clubs=[],                       # filled from the teams table at build
    rebuild_cmd="python build_nba_db.py",
)

NBA = Sport(
    key="nba",
    label="NBA Data Lab",
    icon="🏀",
    db=sport_db("nba", "nba.db"),
    module="constraints_nba",
    schema=NBA_SCHEMA,
    vocab=Vocab(club="team", clubs="teams", game="game", games="games",
                season="season", venue="arena", venues="arenas",
                score="points", postseason="playoffs",
                postseason_one="playoff game", title="championship",
                grid_source="Immaculate Grid"),
    theme="nba",
    build_cmd="python build_nba_db.py",
    missing_db_hint="No nba.db found. Run `python build_nba_db.py` first.",
    empty_hint=("Nothing satisfies both. Steals, blocks, turnovers and the "
                "offensive/defensive rebound split are not recorded before "
                "1973-74, and three-pointers not before 1979-80."),
    stat_eras={"steals": 1974, "blocks": 1974, "turnovers": 1974,
               "oreb": 1974, "dreb": 1974, "fg3m": 1980, "fg3a": 1980,
               "minutes": 1952, "plus_minus": 1997},
    optional_layers={"Draft data": "draft_available",
                     "Award data": "awards_available"},
    enabled=False,      # flip once build_nba_db.py exists
)


# -------------------------------------------------------------- registry

SPORTS = {s.key: s for s in (AFL, NBA)}
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
