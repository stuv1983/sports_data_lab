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
import nba_reference
import obscurity


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
    #: The obscurity.Model this sport's scores were produced by. Left None
    #: rather than defaulting to the AFL model: silently rescoring an NBA
    #: database against goals and Brownlow votes would produce a column of
    #: plausible-looking numbers that mean nothing.
    obscurity_model: object = None
    #: Optional imported club catalogue shown after the standard layers.
    club_data_table: str = ""
    #: Optional broad-family availability probe on the constraints module.
    family_probe: str = ""

    # -- capabilities --------------------------------------------------
    # Everything below exists so no page has to ask `sport.key == "afl"`.
    # A page that branches on the key is a page that will answer an NBA
    # question with an AFL assumption the first time somebody adds a third
    # sport; a page that branches on a capability just gets the right
    # answer. Defaults are all the "not available" value, so a new sport
    # starts with nothing switched on and turns things on deliberately.

    #: Dotted module name of a criterion-text parser, if one exists.
    criterion_parser: str = ""
    #: True when a library of captured historical grids exists.
    grid_library: bool = False
    #: Dotted module name of the full Game Lab, if one exists.
    game_lab_module: str = ""
    #: Pages needing sport-specific tables rather than the schema alone.
    has_club_explorer: bool = False
    has_awards_page: bool = False
    has_past_games: bool = False
    #: Probe name -> the shell command that loads that layer. Shown in the
    #: status panel when the layer is missing, so the panel says how to fix
    #: itself instead of only reporting that something is absent.
    loader_hints: dict = field(default_factory=dict)
    #: Tables the club catalogue needs, and what to run when they are gone.
    club_data_tables: frozenset = frozenset()
    club_data_hint: str = ""
    family_hint: str = ""
    #: Example queries for Advanced Search, in this sport's own data.
    search_examples: tuple = ()
    #: Opening axes for the grid builder, as (builder name, {arg: value}).
    #: Without these an NBA board opens on an AFL club name, which
    #: axis_widget silently coerces to clubs[0].
    grid_defaults: tuple = ()
    #: Display aliases for venues: stored name -> the name people use.
    venue_display: dict = field(default_factory=dict)

    @property
    def star_disclaimer(self):
        """
        The provenance sentence shown wherever a star rating appears.

        Generated from this sport's obscurity model rather than written
        out, because the hand-written AFL version went stale: it claimed
        the formula included club spread, which it never has. Telling NBA
        users their rating comes from goals and Brownlow votes would be the
        same failure with a bigger audience.
        """
        if self.obscurity_model is None:
            return core.STAR_DISCLAIMER
        return obscurity.disclaimer(self.obscurity_model, self.vocab)

    def missing_layer_hints(self, con):
        """(label, hint) for every declared layer that is not loaded.

        Only layers with a hint are yielded: a layer nobody can install yet
        should report "not loaded" in the status rows and say nothing more,
        rather than pointing at a script that does not exist.
        """
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
             "bounces", "goal_assists", "brownlow",
             # Loaded by build_db.py's HEADER_MAP since the beginning but
             # never listed here, so core._check() rejected them and three
             # layers above could not reach data already in `games`.
             # Confirmed populated by the step 0.2 era audit:
             # frees_for/frees_against from 1965, clangers from 1998,
             # uncontested from 1999.
             "frees_for", "frees_against", "clangers", "uncontested"]

#: Current club name -> every identity that counts as that club.
#:
#: Only the Brisbane entry changes any answer. The build already rewrites
#: South Melbourne to Sydney, Footscray to Western Bulldogs and Kangaroos
#: to North Melbourne in `club_now`, so those three lines are no-ops that
#: exist to state the rule in one place instead of leaving it as something
#: the loader happens to do.
#:
#: Brisbane is different: `club_now` keeps 'Brisbane Bears' and 'Fitzroy'
#: as distinct values, so a Lions square matched 249 players and missed
#: 143 Bears and 1,157 Fitzroy. The puzzle's own criterion reads "Played at
#: least 1 game for the Brisbane Lions ... Includes Brisbane Bears and
#: Fitzroy Lions players", so the shortfall was a wrong answer, not a
#: difference of opinion.
#:
#: University folded in 1914 with no successor, so it has no entry and is
#: only ever found by its own name.
AFL_CLUB_LINEAGE = {
    "Brisbane Lions": ["Brisbane Lions", "Brisbane Bears", "Fitzroy"],
    "Sydney": ["Sydney", "South Melbourne"],
    "Western Bulldogs": ["Western Bulldogs", "Footscray"],
    "North Melbourne": ["North Melbourne", "Kangaroos"],
}

#: Clubs offered in a picker.
#:
#: 'Brisbane Bears' is deliberately absent: it is the same franchise under
#: its earlier name, so offering both invited picking one and getting a
#: partial answer. It still resolves when named directly in criterion text,
#: and every Bears player is reachable through Brisbane Lions.
#:
#: Fitzroy stays. It is a distinct club with 1,157 players and a century of
#: its own history, and a Fitzroy square is a real question -- being folded
#: into the Lions for a *Lions* square does not make it stop existing.
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
    # Measured, not assumed: every value below is the first season the
    # column carries a non-null, non-zero value in the built database, as
    # reported by health.stat_era_starts. The previous hand-written values
    # were wrong for eight of the fifteen stats -- kicks was listed as 1897
    # (only goals go back that far), hitouts as 1987, goal_assists as 1998
    # and brownlow as 1902 -- so the era caption was asserting coverage the
    # data does not have. Re-measure with health.stat_era_starts after any
    # rebuild rather than editing these by hand.
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
    criterion_parser="parse_criteria",
    grid_library=True,
    game_lab_module="game_lab",
    has_club_explorer=True,
    has_awards_page=True,
    has_past_games=True,
    loader_hints={
        "draft_available": "Run `load_draftguru.py`, then `link_draft.py`.",
        "awards_available": "Run `load_draftguru.py`, then `link_people.py`.",
        "captain_available": "Run `load_captains.py` for club-captain data.",
        "rising_star_available": ("Run `fetch_footywire_rising_star.py`, "
                                  "then `load_rising_star.py`."),
    },
    club_data_tables=frozenset({
        "clubs", "club_source_snapshots", "club_wikipedia_fields",
        "club_player_totals", "club_player_register", "club_player_records",
    }),
    club_data_hint=("Run `utils/fetch_club_sources.py`, then "
                    "`utils/load_club_sources.py` for Club Explorer."),
    family_hint=("Run `scrape_wikipedia_families.py`, then "
                 "`load_family_relationships.py` for family links."),
    # Every one of these is compiled by tests/test_sport_capabilities.py.
    # An example that does not run teaches a query that does not work.
    search_examples=(
        'club:Hawthorn games>=200 sort:obscurity',
        'game.disposals>=40 postseason:true',
        'season.goals>=50 debut:1990..1999',
        'career.marks>=1000 avg.disposals>=20',
        'award:brownlow-medal',
        'club:Fitzroy club_any:Carlton',
    ),
    # (columns, rows). Unchanged from the values app.py used to hold.
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
# The schema deliberately reuses every AFL column name that means the same
# thing, so explore.py's pages work unchanged. Only the three names that
# genuinely differ are overridden.

#: `points` stays first: app.axis_widget offers stats[0] as the default, and
#: a board that opens on "40+ fouls in a game" answers a question nobody
#: asked.
NBA_STATS = ["points", "rebounds", "assists", "steals", "blocks",
             "turnovers", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta",
             "oreb", "dreb", "minutes", "plus_minus", "fouls"]

NBA_SCHEMA = core.Schema(
    career_score="career_points",
    # Not overriding this was a real bug: the schema kept the AFL default
    # `finals_played` while build_nba_db.py writes `playoffs_played`, so
    # core.require_schema would have rejected every NBA database ever built.
    career_postseason="playoffs_played",
    game_score="points",
    is_final="is_playoff",
    stats=NBA_STATS,
    # Measured from the teams table once built; the fallback list is what
    # lets a clean clone import. Previously [], which made axis_widget's
    # clubs[0] an IndexError the moment the NBA became selectable.
    clubs=nba_reference.teams(),
    club_lineage=nba_reference.club_lineage(),
    venue_aliases=nba_reference.venue_aliases(),
    required_player_cols=("name_key", "career_minutes"),
    rebuild_cmd="python build_nba_db.py",
    # app.py, fetch_grid.py and the tests index this tuple positionally, so
    # the order is part of the contract and the eight-column shape is not
    # optional -- core.Schema's six-column default would have shifted every
    # positional read. Obscurity stays last: core.square() reads the final
    # column as the rating.
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
    module="constraints_nba",
    schema=NBA_SCHEMA,
    vocab=Vocab(club="team", clubs="teams", game="game", games="games",
                season="season", venue="arena", venues="arenas",
                score="points", postseason="playoffs",
                postseason_one="playoff game", title="championship",
                grid_source="Immaculate Grid"),
    theme="nba",
    build_cmd="python build_nba_db.py",
    missing_db_hint=(f"No NBA database found at {sport_db('nba')}. "
                     "Run `python build_nba_db.py` first."),
    empty_hint=("Nothing satisfies both. Steals, blocks, turnovers and the "
                "offensive/defensive rebound split are not recorded before "
                "1973-74, and three-pointers not before 1979-80."),
    # Measured from the built database's stat_coverage table, not written by
    # hand. The AFL's era note learned this the hard way -- eight of its
    # fifteen hand-written values were wrong. See nba_reference.py.
    stat_eras=nba_reference.stat_eras(),
    optional_layers={"Draft data": "draft_available",
                     "Award data": "awards_available"},
    obscurity_model=obscurity.NBA_MODEL,
    # Every capability stays at its default. There is no NBA criterion
    # parser, grid library, Game Lab, Club Explorer, Awards page or Past
    # Games page, and saying so here is what keeps those pages from
    # pretending. No loader_hints either: the draft and award layers have
    # no NBA loader to point at yet, so the status panel says "not loaded"
    # and stops, rather than naming a script that does not exist.
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
    # No `enabled=False`. selectable() already requires exists(), which
    # requires a readable players table at the path above, so the NBA stays
    # out of the picker until it is genuinely built. A second hand-flipped
    # switch only creates a state where the database is ready and the app
    # still refuses to show it.
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
