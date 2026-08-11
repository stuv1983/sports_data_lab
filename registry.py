"""
registry.py -- The types a sport is described with.

`Vocab` is the words the UI puts on screen ("club" vs "team" vs
"franchise", "final" vs "playoff" vs "postseason"). `Sport` binds a database
file, a core.Schema naming its columns, the module holding its constraints,
that vocabulary, and the capabilities the sport has.

The four descriptions themselves live with their sport -- afl/sport.py,
nba/sport.py, mlb/sport.py, nfl/sport.py -- and sports.py collects them into
the registry. This module is only the shape they share, and it imports no
sport: that is what lets a sport module import it.
"""

import importlib
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Sequence

import core
import labels
import obscurity

#: Sport key -> {era name -> current name}, computed once per process.
_CLUB_RENAMES: dict = {}


def collapse_clubs(parts, renames) -> list:
    """Collapse era names of one club to a single entry, order preserved.

    `renames` maps an era name to the club's current name. Groups the
    parts by that identity: a group holding one era name keeps it (the
    team of the time), a group holding several is named by the identity,
    because no single era name covers the span. Order-independent within
    a group, so a ground's GROUP_CONCAT and a career's chronological path
    both land on the same answer.
    """
    order: list = []
    groups: dict = {}
    for part in parts:
        identity = renames.get(part, part)
        if identity not in groups:
            groups[identity] = []
            order.append(identity)
        if part not in groups[identity]:
            groups[identity].append(part)
    return [groups[identity][0] if len(groups[identity]) == 1 else identity
            for identity in order]


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

    @property
    def title_plural(self) -> str:
        """The countable form of `title`, for a tile that counts them.

        'premiership' -> 'Premierships', 'Super Bowl' -> 'Super Bowls'.
        Anything already ending in 's' is left alone, which is what keeps
        the MLB's 'World Series' from becoming 'World Seriess'.
        """
        label = self.title.strip()
        if not label:
            return "Titles"
        label = label[0].upper() + label[1:]
        return label if label.endswith("s") else label + "s"


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
    #: Show in the sport picker before its database exists, labelled
    #: "coming soon" and explained by missing_db_hint when chosen. Without
    #: this a sport that is announced but not yet built is simply invisible,
    #: which reads as "this app does not do that sport".
    preview: bool = False
    #: The obscurity.Model this sport's scores were produced by.
    obscurity_model: object = None
    #: SQL predicate naming the players an obscurity score is computed over,
    #: empty for "all of them". Obscurity is a percentile rank, so who is in
    #: the population changes every score: the NFL's `players` table holds
    #: 13,669 identities with no game in the 1999-onward data, and scoring
    #: them put 55% of the table at a tied 100 and pushed every player who
    #: did play down the scale.
    obscurity_population: str = ""
    #: Optional imported club catalogue shown after the standard layers.
    club_data_table: str = ""
    #: Optional broad-family availability probe on the constraints module.
    family_probe: str = ""
    #: What one row of the `games` table actually is, when it is not a
    #: player-game. Empty means it is, and the status panel says
    #: "Player-<games>". MLB sets it because Lahman's finest grain is a
    #: player's season with one team, not a game.
    games_row_label: str = ""

    # -- capabilities --------------------------------------------------
    criterion_parser: str = ""
    grid_library: bool = False
    #: "module:function" that returns one dated board straight from the
    #: sport's own daily puzzle, or None when that day has not been
    #: published. Declared per sport rather than inferred from the key:
    #: utils.fetch_grids.fetch_gridley reads gridleygame.com's AFL feed and
    #: would answer an NBA request with an AFL board.
    daily_grid_feed: str = ""
    #: Where that feed comes from, for the credit line the picker shows.
    daily_grid_site: str = ""
    game_lab_module: str = ""
    #: Which module renders the Awards page. The AFL's is built for the
    #: Draftguru shape and the MLB's for Lahman's, and the two have no
    #: columns in common, so the sport names its own rather than one page
    #: branching on which tables it happens to find.
    awards_page_module: str = ""
    has_club_explorer: bool = False
    has_awards_page: bool = False
    has_past_games: bool = False
    #: Module holding this sport's data caveats -- how it numbers rounds,
    #: which results it records differently from the competition's own
    #: record. Declared per sport because they are inherited from whichever
    #: source the sport was built from, and only the AFL has any.
    data_notes_module: str = ""
    #: Optional sport-owned page for venue/ground history and records.
    ground_explorer_module: str = ""
    has_ground_explorer: bool = False
    #: Optional sport-owned page for the draft. Declared per sport because
    #: what a draft record even holds differs: the AFL's carries a signing
    #: rule (father-son, academy, zone) that no other sport here has.
    draft_page_module: str = ""
    has_draft_page: bool = False
    #: Module supplying starting lineups for a sport whose `games` rows are
    #: not box scores. The MLB build is season-grain -- Lahman has no box
    #: scores -- so its match card has no per-player rows to draw and shows
    #: who took the field instead.
    lineup_module: str = ""
    #: The `games.round` value whose win means the sport's title -- 'GF' for
    #: the AFL, 'WS' for the MLB, 'SB' for the NFL. The player profile counts
    #: distinct seasons won to show a premiership/World Series/Super Bowl
    #: tally.
    #:
    #: Left unset where a title-round win is not a title. The NBA Finals is a
    #: best-of-seven and its rows are per-game with no series result, so a
    #: side that lost 4-3 would still be credited -- which is why
    #: constraints_nba.won_the_finals() is careful to call itself "won a
    #: Finals game". A sport declares this only when the data can answer it.
    title_round: str = ""
    loader_hints: dict = field(default_factory=dict)
    club_data_tables: frozenset = frozenset()
    club_data_hint: str = ""
    #: How to fill `club_match_sources` for this sport, shown when Past
    #: Games finds the table empty. Three sports now feed that one table
    #: from three unrelated loaders -- the AFL scrapes it, the MLB
    #: downloads Retrosheet game logs, the NFL projects its own schedule --
    #: so the page cannot name a command without being told which.
    past_games_hint: str = ""
    family_hint: str = ""
    search_examples: tuple = ()
    grid_defaults: tuple = ()
    venue_display: dict = field(default_factory=dict)
    #: Player-card data supplied by Draftguru's linked people tables.  This
    #: is a data-shape capability, not a synonym for a particular sport: a
    #: shared page must never infer table semantics from ``sport.key``.
    has_draftguru_player_cards: bool = False

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

    def notes(self):
        """The sport's data-notes module, or None when it declares none.

        Imported on use like `C`, so a page can ask any sport for its
        caveats and get nothing back rather than having to know which
        sports have them.
        """
        if not self.data_notes_module:
            return None
        try:
            return importlib.import_module(self.data_notes_module)
        except ImportError:
            return None

    # -- club identity -------------------------------------------------
    def club_renames(self) -> dict:
        """games.club_hist -> club_now, wherever the two differ.

        The renames the data itself records: Kangaroos -> North Melbourne,
        South Melbourne -> Sydney, Footscray -> Western Bulldogs. This is
        deliberately narrower than `schema.club_lineage`, which also folds
        genuinely separate clubs into a successor for constraint answers --
        the Bears into the Lions -- and those must never collapse on
        screen: the Bears played as the Bears.

        Cached for the life of the process rather than per revision: a
        rename is history, and a database refresh does not change history.
        """
        cached = _CLUB_RENAMES.get(self.key)
        if cached is not None:
            return cached
        renames: dict[str, str] = {}
        s = self.schema
        hist = getattr(s, "club_hist", "")
        now = getattr(s, "club_now", "")
        games = getattr(s, "games", "")
        if hist and now and games and self.exists():
            try:
                con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
                try:
                    renames = {h: n for h, n in con.execute(
                        f"SELECT DISTINCT {hist}, {now} FROM {games} "
                        f"WHERE {hist} <> {now}") if h and n}
                finally:
                    con.close()
            except sqlite3.Error:
                renames = {}
        _CLUB_RENAMES[self.key] = renames
        return renames

    def collapse_clubs(self, parts) -> list:
        """One entry per club, named as the club was at the time.

        A career list reading "Kangaroos, North Melbourne" is one club
        twice, not two clubs. Within one identity: a single era name is
        kept as written -- a Footscray career that ended before the rename
        stays Footscray -- and several era names collapse to the name the
        club goes by now, because no single era covers them.
        """
        return collapse_clubs(parts, self.club_renames())

    def collapse_club_path(self, value) -> str:
        """`collapse_clubs` over a '|' or comma separated club string."""
        text = str(value or "")
        parts = [part.strip() for part in re.split(r"[|,]", text)
                 if part.strip()]
        if not parts:
            return text
        sep = "|" if "|" in text else ", "
        return sep.join(self.collapse_clubs(parts))

    def lineups(self):
        """The sport's lineup module, or None when it declares none."""
        if not self.lineup_module:
            return None
        try:
            return importlib.import_module(self.lineup_module)
        except ImportError:
            return None

    def daily_grid_fetcher(self):
        """The callable behind `daily_grid_feed`, or None when unset.

        Imported on use, not at module scope: a sport without a live feed
        should not drag its scraper's dependencies into every page.
        """
        if not self.daily_grid_feed:
            return None
        module_name, _, func = self.daily_grid_feed.partition(":")
        return getattr(importlib.import_module(module_name), func)

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
            return (f"{labels.words(stat)} was not recorded before "
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
            ("Players", self.player_count_value(con, players)),
            (self.games_row_label or f"Player-{self.vocab.games}",
             f"{appearances:,}"),
        ]
        extra = getattr(self.C, "extra_status", None)
        if extra is not None:
            rows.extend(extra(con))
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

    def player_count_value(self, con, total):
        """How many players the database can actually answer for.

        Where `obscurity_population` names a subset -- the NFL's player list
        is every known identity, over half of them with no game in the data
        -- reporting the table's row count alone states a number the app
        cannot back up.
        """
        if not self.obscurity_population:
            return f"{total:,}"
        try:
            played = con.execute(
                f"SELECT COUNT(*) FROM {self.schema.players} "
                f"WHERE {self.obscurity_population}").fetchone()[0]
        except sqlite3.Error:
            return f"{total:,}"
        if played >= total:
            return f"{total:,}"
        return f"{played:,} with {self.vocab.games} ({total:,} listed)"

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

    # -- capability probing --------------------------------------------
    def layers(self, con):
        """Which optional layers this database has, and what to offer.

        Every probe is optional. A sport declares only the layers it has,
        and a missing probe reads as "not loaded" rather than raising --
        app.py used to call `awards_available` directly and took the whole
        application down at import for a sport that has no awards.
        """
        def probe(name):
            fn = getattr(self.C, name, None)
            if fn is None:
                return False
            try:
                return bool(fn(con))
            except Exception:
                return False

        def names(attribute):
            return set(getattr(self.C, attribute, set()) or set())

        gates = [
            (probe("draft_available"), names("DRAFT_BUILDERS")),
            (probe("awards_available"), names("AWARD_BUILDER_NAMES")),
            (probe("captain_available"), names("CAPTAIN_BUILDER_NAMES")),
            (probe("rising_star_available"),
             names("RISING_STAR_BUILDER_NAMES")),
            (probe("family_relationships_available"),
             names("FAMILY_RELATIONSHIP_BUILDER_NAMES")),
        ]
        # Builders gated on a source layer, as {builder: probe}. Unlike the
        # five above this needs no entry here: a sport that imports a new
        # layer declares the pairing in its own module and nothing in the
        # framework changes.
        by_layer = {
            builder: probe(name) for builder, name
            in (getattr(self.C, "LAYER_BUILDERS", {}) or {}).items()
        }
        builders = [k for k in self.C.BUILDERS
                    if all(ok or k not in gated for ok, gated in gates)
                    and by_layer.get(k, True)]

        return Layers(
            draft=gates[0][0], awards=gates[1][0],
            captain=gates[2][0], rising_star=gates[3][0],
            family_relationships=gates[4][0],
            # The table list belongs to the sport's Club Explorer, not to a
            # page. An empty set is a subset of anything, so a sport without
            # one is trivially available -- which is fine, because the flag
            # is only read inside a has_club_explorer guard.
            club_data=self.club_data_tables <= {
                row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")},
            builders=builders,
        )


@dataclass(frozen=True)
class Layers:
    """What one database has, as `Sport.layers` measured it."""
    draft: bool
    awards: bool
    captain: bool
    rising_star: bool
    family_relationships: bool
    club_data: bool
    #: Builder names to offer, with every unavailable one already removed.
    builders: Sequence[str]
