"""
explore.py -- Research and play modes.

Pages:
  Home              database overview and navigation
  Player Search     player career, seasons and biggest games
  Stats Explorer    game, season and career leaderboards
  Random Discovery  database-driven random facts and performances
  Game Lab          an early playable "Guess the Player" prototype

Imported by app.py; not run directly.

Every page takes the active Sport and reads its column names from
sport.schema and its wording from sport.vocab, so nothing here is written
twice for two sports. Obscurity is shown as a star rating throughout; the
raw 0-100 score appears only where the exact number is the point.
"""

import os
import random
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

import core
import labels
import ui_widgets

SCOPES = {
    "Single game": "game",
    "Season total": "season",
    "Career total": "career",
}


def _db_revision(db):
    """Return a cheap cache key that changes when the database changes.

    Same shape as app.py's db_revision: the path is part of the value so
    two sports' caches can never collide on a (mtime, size) coincidence.
    """
    stat = os.stat(db)
    return str(db), stat.st_mtime_ns, stat.st_size


@st.cache_data(show_spinner=False, max_entries=512)
def _read_frame(sql, params, revision, _con):
    """Cache repeat dataframe queries for the current database revision."""
    return pd.read_sql_query(sql, _con, params=params)


@st.cache_data(show_spinner=False, max_entries=512)
def _fetchone(sql, params, revision, _con):
    return _con.execute(sql, params).fetchone()


@st.cache_data(show_spinner=False, max_entries=256)
def _fetchall(sql, params, revision, _con):
    return _con.execute(sql, params).fetchall()


def _era_note(sport, stats=None):
    """
    A sentence naming the stats that do not exist for the whole database,
    built from sport.stat_eras rather than hardcoded. Returns None when a
    sport records everything for its full history.
    """
    eras = sport.stat_eras
    if not eras:
        return None
    span_start = min(eras.values())
    late = {s: y for s, y in eras.items() if y > span_start}
    if stats:
        late = {s: y for s, y in late.items() if s in stats}
    if not late:
        return None
    by_year = {}
    for stat, year in sorted(late.items(), key=lambda kv: (kv[1], kv[0])):
        by_year.setdefault(year, []).append(labels.words(stat))
    parts = [f"{', '.join(names)} from {year}"
             for year, names in sorted(by_year.items())]
    return "Not recorded for the full database: " + "; ".join(parts) + "."


@st.cache_data(show_spinner=False)
def _summary(sport_key, revision, _con):
    """Headline counts. sport_key is hashed so a switch re-queries."""
    import sports
    s = sports.get(sport_key).schema
    players = _con.execute(
        f"SELECT COUNT(*) FROM {s.players}").fetchone()[0]
    appearances = _con.execute(
        f"SELECT COUNT(*) FROM {s.games}").fetchone()[0]
    lo, hi = _con.execute(
        f"SELECT MIN({s.season}), MAX({s.season}) FROM {s.games}").fetchone()
    clubs = _con.execute(
        f"SELECT COUNT(DISTINCT {s.club_hist}) FROM {s.games}").fetchone()[0]
    venues = _con.execute(
        f"SELECT COUNT(DISTINCT {s.venue}) FROM {s.games}").fetchone()[0]
    return {"players": players, "games": appearances, "season_min": lo,
            "season_max": hi, "clubs": clubs, "venues": venues}


@st.cache_data(show_spinner=False)
def _season_span(sport_key, revision, _con):
    import sports
    s = sports.get(sport_key).schema
    return _con.execute(
        f"SELECT MIN({s.season}), MAX({s.season}) FROM {s.games}").fetchone()


@st.cache_data(show_spinner=False)
def _venues(sport_key, revision, _con):
    import sports
    s = sports.get(sport_key).schema
    return [(r[0], r[0]) for r in _con.execute(
        f"SELECT {s.venue}, COUNT(*) c FROM {s.games} "
        f"GROUP BY {s.venue} ORDER BY c DESC")]


# ------------------------------------------------------------------ home

def home_page(sport, con, draft_ok, awards_ok):
    V = sport.vocab
    revision = _db_revision(sport.db)
    s = _summary(sport.key, revision, con)

    sport_logos = {
        "afl": "https://upload.wikimedia.org/wikipedia/en/e/e4/Australian_Football_League.svg",
        "nba": "https://upload.wikimedia.org/wikipedia/en/0/03/National_Basketball_Association_logo.svg",
        "mlb": "https://upload.wikimedia.org/wikipedia/commons/a/a6/Major_League_Baseball_logo.svg",
        "nfl": "https://upload.wikimedia.org/wikipedia/en/a/a2/National_Football_League_logo.svg"
    }
    logo_url = sport_logos.get(sport.key, "")
    logo_html = f"<img src='{logo_url}' style='height: 80px; float: left; margin-right: 25px;' />" if logo_url else ""

    st.markdown(
        "<div class='hero' style='overflow: auto;'>"
        f"{logo_html}"
        f"<div class='hero-title'>{sport.label}</div>"
        f"<div class='hero-copy'>A local {sport.key.upper()} research "
        f"database for finding players, exploring records, discovering "
        f"unusual stats, solving {V.grid_source} boards and building "
        f"database-driven games.</div></div>",
        unsafe_allow_html=True)

    metrics = st.columns(5)
    values = [
        ("Players", f"{s['players']:,}"),
        (f"Player-{V.games}", f"{s['games']:,}"),
        ("Seasons", f"{s['season_min']}–{s['season_max']}"),
        (f"{V.club.capitalize()} names", f"{s['clubs']}"),
        (V.venues.capitalize(), f"{s['venues']}"),
    ]
    for col, (label, value) in zip(metrics, values):
        col.metric(label, value)

    st.markdown("### Explore the database")
    row1 = st.columns(3)
    cards = [
        ("Player Search",
         "Type a name and inspect a player's full career, season totals, "
         f"{V.clubs}, {V.postseason} record and biggest individual "
         f"{V.games}."),
        ("Stats Explorer",
         f"Build {V.game}, season and career leaderboards with {V.club}, "
         f"{V.venue}, season and {V.postseason} filters."),
        ("Random Discovery",
         f"Pull a random player, notable performance or multi-{V.club} "
         "career from the database."),
    ]
    for col, (title, copy) in zip(row1, cards):
        col.markdown(
            f"<div class='feature-card'><h4>{title}</h4><p>{copy}</p></div>",
            unsafe_allow_html=True)

    row2 = st.columns(3)
    cards = [
        ("Grid Solver",
         f"Keep the original {V.grid_source} workflow as one tool inside "
         "the larger database application."),
        ("Game Lab",
         "Prototype database-driven games. The first playable concept is a "
         "clue-based Guess the Player challenge."),
        ("Data Coverage",
         f"Core match data is ready. Draft data is "
         f"{'ready' if draft_ok else 'not loaded'} and award data is "
         f"{'ready' if awards_ok else 'not loaded'}."),
    ]
    for col, (title, copy) in zip(row2, cards):
        col.markdown(
            f"<div class='feature-card'><h4>{title}</h4><p>{copy}</p></div>",
            unsafe_allow_html=True)

    if sport.has_ground_explorer:
        row3 = st.columns(3)
        extra = [
            ("Ground Explorer",
             "Search every ground for overall and head-to-head records, "
             "match history, leading players and best performances."),
            (f"Past {V.games.capitalize()}",
             f"Search the complete match archive by season, round, "
             f"{V.club}, opponent and {V.venue}."),
            ("Player Connections",
             "Choose any two players to find every match they played as "
             "teammates or opponents."),
        ]
        for col, (title, copy) in zip(row3, extra):
            col.markdown(
                f"<div class='feature-card'><h4>{title}</h4><p>{copy}</p></div>",
                unsafe_allow_html=True)

    with st.expander("Coverage notes"):
        note = _era_note(sport)
        st.write(f"{V.score.capitalize()} and {V.club} history cover the "
                 f"database back to {s['season_min']}.")
        if note:
            st.write(note)
        # This sport's model, not core's: core's names goals, finals and
        # Brownlow votes, and the NBA model uses none of them.
        st.write(sport.star_disclaimer)

    _data_notes(sport)


def _data_notes(sport) -> None:
    """The full list of the sport's caveats, grouped by what they are about.

    The season and round cards each show the handful that apply to what is
    on screen; this is the place a reader comes to when they want the lot,
    which is why it names the round-numbering rule up front rather than
    leaving it eleventh in a list.
    """
    notes = sport.notes()
    if notes is None:
        return
    with st.expander("Data notes — rounds, ladders and disputed results"):
        st.warning(notes.ROUND_NUMBERING.text, icon=":material/info:")
        for topic, items in notes.by_topic().items():
            listed = [note for note in items
                      if note is not notes.ROUND_NUMBERING]
            if not listed:
                continue
            st.markdown(f"**{topic}**")
            for item in listed:
                seasons = item.seasons
                st.markdown(f"- {f'**{seasons}** — ' if seasons else ''}"
                            f"{item.text}")


# ------------------------------------------------------------- filters

def _filters(sport, con, key):
    """Shared leaderboard filters. Returns (where_sql, params)."""
    V, sc = sport.vocab, sport.schema
    clubs = ["Any"] + list(sc.clubs)
    revision = _db_revision(sport.db)
    venues = ["Any"] + [v for v, _ in _venues(sport.key, revision, con)]
    season_min, season_max = _season_span(sport.key, revision, con)

    c1, c2, c3, c4 = st.columns([1.2, 1.4, 1, 1])
    club = c1.selectbox(V.club.capitalize(), clubs, key=f"{key}_club")
    venue = c2.selectbox(V.venue.capitalize(), venues, key=f"{key}_venue")
    lo = c3.number_input("From season", season_min, season_max, season_min,
                         key=f"{key}_lo")
    hi = c4.number_input("To season", season_min, season_max, season_max,
                         key=f"{key}_hi")
    postseason = st.checkbox(f"{V.postseason.capitalize()} only",
                             key=f"{key}_fin")

    if lo > hi:
        lo, hi = hi, lo
        st.caption("The season range was reversed automatically.")

    where = [f"g.{sc.season} BETWEEN ? AND ?"]
    params = [lo, hi]
    if club != "Any":
        where.append(f"(g.{sc.club_now} = ? OR g.{sc.club_hist} = ?)")
        params += [club, club]
    if venue != "Any":
        where.append(f"g.{sc.venue} = ?")
        params.append(venue)
    if postseason:
        where.append(f"g.{sc.is_final} = 1")
    return " AND ".join(where), params


# ------------------------------------------------------- player search

def player_page(sport, con, player_picker):
    st.markdown("# Player Search")
    st.caption("Search the full player database by name or by what a career "
               "looks like, then inspect the selected player's career and "
               "best performances — or put two careers side by side.")
    find, one, two, together = st.tabs(
        ["Find players", "One player", "Compare two",
         "Played with / against"])
    with find:
        _find_players(sport, con)
    with one:
        _player_profile(sport, con, player_picker)
    with two:
        _compare_players(sport, con, player_picker)
    with together:
        _player_connections(sport, con, player_picker)


def _find_players(sport, con):
    """Find a player by what their career looks like, not by their name.

    Every other tab here starts from a name, which is no use to somebody
    who is trying to remember one. These are controls over the same query
    compiler the Advanced Search page runs: the widgets build a query, the
    compiler turns it into one parameterised statement, and the query it
    built is shown underneath so a reader can take it to that page and
    keep going.

    Building the query rather than the SQL is the point. Only the
    compiler knows which columns exist for a sport and which layers are
    loaded, and a second path into the database would be a second set of
    rules to keep in step with the first.
    """
    import components
    import query_filters_family as Q
    import ui_widgets

    V, sc = sport.vocab, sport.schema
    revision = _db_revision(sport.db)
    season_min, season_max = _season_span(sport.key, revision, con)
    tokens: list[str] = []

    c1, c2, c3 = st.columns(3)
    seasons = c1.select_slider(
        f"Played between {V.season}s", options=range(season_min, season_max + 1),
        value=(season_min, season_max), key=sport.k("find_seasons"))
    if seasons != (season_min, season_max):
        tokens.append(f"played:{seasons[0]}..{seasons[1]}")

    clubs = c2.multiselect(
        V.clubs.capitalize(), list(sc.clubs), key=sport.k("find_clubs"),
        help=f"Two or more {V.clubs} means a career that took in every one "
             f"of them.")
    tokens += [f"club:{Q.quote_token(club)}" for club in clubs]

    games = c3.number_input(
        f"Minimum career {V.games}", min_value=0, value=0, step=25,
        key=sport.k("find_games"))
    if games:
        tokens.append(f"games>={int(games)}")

    # -- the draft, where the sport has one loaded ------------------------
    if getattr(sport.C, "draft_available", None) and sport.C.draft_available(con):
        d1, d2, d3 = st.columns(3)
        sources = ui_widgets.recruit_source_options(
            sport.key, sport.db, revision)
        if sources:
            names = ["Anywhere"] + [name for name, _ in sources]
            counts = dict(sources)
            source = d1.selectbox(
                "Recruited from", names, key=sport.k("find_source"),
                format_func=lambda n: (
                    n if n == "Anywhere" else f"{n}  ·  {counts[n]}"),
                help="Any step of the path to the draft — the junior club, "
                     "the school, the talent-league or state-league club.")
            if source != "Anywhere":
                tokens.append(f"recruited_from:{Q.quote_token(source)}")

        picks = d2.select_slider(
            "National draft pick", options=range(1, 101),
            value=(1, 100), key=sport.k("find_pick"),
            help="Pick numbers restart for the rookie and pre-season "
                 "drafts, so this asks about the national draft only.")
        if picks != (1, 100):
            tokens.append(f"pick:{picks[0]}..{picks[1]}")

        drafted = d3.select_slider(
            "Drafted between", options=range(season_min, season_max + 1),
            value=(season_min, season_max), key=sport.k("find_drafted"))
        if drafted != (season_min, season_max):
            tokens.append(f"draft_year:{drafted[0]}..{drafted[1]}")

    e1, e2 = st.columns([1, 2])
    order = e1.selectbox(
        "Sort by", ["obscurity", "games", "fewest_games", "score", "name",
                    "newest", "oldest"],
        format_func=lambda s: {
            "obscurity": "Most obscure", "games": f"Most {V.games}",
            "fewest_games": f"Fewest {V.games}",
            "score": f"Most {V.score}", "name": "Name",
            "newest": "Most recent", "oldest": "Earliest"}[s],
        key=sport.k("find_sort"))
    tokens.append(f"sort:{order}")
    limit = e2.slider("How many to show", 25, 500, 100, step=25,
                      key=sport.k("find_limit"))
    tokens.append(f"limit:{limit}")

    query = " ".join(tokens)
    # Nothing but the sort and the limit means every player in the
    # database, ranked -- a real answer, but not one anybody asked for.
    if len(tokens) <= 2:
        st.info(f"Choose a filter above to search. Every {V.game}, "
                f"{V.club} and draft field can be combined.")
        return

    try:
        sql, params, spec = Q.compile_query(
            sc, query, con=con, extensions=sport.search_extensions())
        frame = pd.read_sql_query(sql, con, params=params)
    except (Q.QuerySyntaxError, ValueError) as exc:
        st.error(str(exc))
        return
    except (sqlite3.Error, pd.errors.DatabaseError) as exc:
        st.error(f"Database error while searching: {exc}")
        return

    if frame.empty:
        st.info("No players match every filter.")
        return

    st.caption(f"{len(frame):,} player{'s' if len(frame) != 1 else ''} shown.")
    shown = components.player_results_table(
        frame, sport, con, key=sport.k("find_results"))
    st.download_button(
        "Download these players as CSV",
        data=shown.to_csv(index=False).encode("utf-8"),
        file_name=f"{sport.key}_player_filter.csv", mime="text/csv",
        key=sport.k("find_download"))
    with st.expander("The query these filters built"):
        st.code(query, language=None)
        st.caption("Advanced Search takes the same text, and understands "
                   "more than these controls offer.")


def _player_profile(sport, con, player_picker):
    selected = player_picker(sport.k("explore_player"),
                             label="Search by name")
    if selected is None:
        return
    pid, _ = selected
    render_player_profile(sport, con, pid, key_prefix="explore")


def _titles_won(sport, con, pid, revision):
    """How many titles this player won, or None if the sport cannot say.

    Counts *distinct seasons* rather than rows. For the AFL and the NFL a
    title is decided in one match so the two are the same, but counting
    seasons is what keeps the number a title count rather than a
    matches-won count if a sport's rows ever become per-game within the
    deciding series.

    Sports that do not declare a title_round return None and show no tile
    at all -- see registry.Sport.title_round for why the NBA is one of
    them. A sport whose database has no `round` or `result` column also
    returns None rather than raising, so this can never take a profile
    page down.
    """
    round_value = getattr(sport, "title_round", "")
    sc = sport.schema
    if not round_value:
        # The NBA title is a best-of-seven series, so there is no single
        # title-winning game row. Its derived team-season table carries the
        # series outcome instead.
        team_columns = {
            row[1] for row in con.execute("PRAGMA table_info(team_seasons)")
        }
        needed = {"season", "club_now", "champion"}
        if not needed <= team_columns:
            return None
        phase = " AND t.phase = 'regular'" if "phase" in team_columns else ""
        try:
            row = _fetchone(
                f"""SELECT COUNT(DISTINCT g.{sc.season})
                      FROM {sc.games} g JOIN team_seasons t
                        ON t.season = g.{sc.season}
                       AND t.club_now = g.{sc.club_now}
                     WHERE g.{sc.player_id} = ? AND t.champion = 1{phase}""",
                (pid,), revision, con)
        except sqlite3.Error:
            return None
        return row[0] if row else 0
    try:
        row = _fetchone(
            f"SELECT COUNT(DISTINCT {sc.season}) FROM {sc.games} "
            f"WHERE {sc.player_id} = ? AND UPPER(TRIM({sc.round})) = ? "
            f"AND {sc.result} = 'W'",
            (pid, round_value.upper()), revision, con)
    except sqlite3.Error:
        return None
    return row[0] if row else 0


_CARD_METRICS = {
    "afl": (("career_brownlow", "Brownlow votes", "int"),),
    "mlb": (("career_hits", "Hits", "int"),
            ("career_war", "Career bWAR", "decimal")),
    "nba": (("career_rebounds", "Rebounds", "int"),
            ("career_assists", "Assists", "int"),
            ("career_steals", "Steals", "int"),
            ("career_blocks", "Blocks", "int")),
    "nfl": (("career_passing_yards", "Passing yards", "int"),
            ("career_rushing_yards", "Rushing yards", "int"),
            ("career_receiving_yards", "Receiving yards", "int"),
            ("career_tackles", "Tackles", "int"),
            ("career_sacks", "Sacks", "decimal"),
            ("career_interceptions", "Interceptions", "int")),
}


def _clean_award_name(value):
    text = str(value or "").strip()
    for suffix in (" (AFL)", " (AFLCA)", " (AFLPA)"):
        if text.endswith(suffix):
            return text[:-len(suffix)]
    return text


def _honour_order(sport_key, label):
    priorities = {
        # One "rising star" token covers the award, the nomination and the
        # ineligible variant: all three contain it, so all three take this
        # rank and sort next to each other, which is the point. Their order
        # within the group is the usual count-then-name tie-break.
        "afl": ("norm smith", "all-australian", "brownlow", "rising star",
                "gary ayres", "leigh matthews", "aflca", "best and fairest",
                "medal"),
        "mlb": ("most valuable player", "cy young", "world series mvp",
                "gold glove", "silver slugger", "rookie of the year",
                "all-star", "triple crown"),
    }.get(sport_key, ())
    lowered = label.casefold()
    return next((i for i, token in enumerate(priorities) if token in lowered),
                len(priorities))


@st.cache_data(show_spinner=False, max_entries=512)
def _player_card_enrichment(sport_key, pid, revision, _con):
    """Extra career totals, draft detail and honours for a player card."""
    import sports

    sport = sports.get(sport_key)
    sc = sport.schema
    player_columns = {
        row[1] for row in _con.execute(f"PRAGMA table_info({sc.players})")
    }
    requested = [item for item in _CARD_METRICS.get(sport_key, ())
                 if item[0] in player_columns]
    metrics = []
    if requested:
        row = _con.execute(
            f"SELECT {', '.join(col for col, _, _ in requested)} "
            f"FROM {sc.players} WHERE {sc.player_id} = ?", (pid,)
        ).fetchone()
        for (_, label, kind), value in zip(requested, row or ()):
            if value is None:
                continue
            if isinstance(value, (int, float)) and value <= 0:
                continue
            display = (f"{float(value):,.1f}" if kind == "decimal"
                       else f"{int(round(value)):,}")
            metrics.append((label, display))

    bio = []
    tables = {row[0] for row in _con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    game_columns = {
        row[1] for row in _con.execute(f"PRAGMA table_info({sc.games})")
    }
    # A layer capability, not a sport key: any sport whose build loads a
    # per-row WAR column and declares the probe gets the metric.
    if "war" in game_columns and sport.layer_ready("war_available", _con):
        best_war = _con.execute(
            f"""SELECT ROUND(MAX(season_war), 1) FROM (
                  SELECT SUM(war) AS season_war FROM {sc.games}
                   WHERE {sc.player_id}=? AND war IS NOT NULL
                   GROUP BY {sc.season})""", (pid,)).fetchone()
        if best_war and best_war[0] is not None:
            metrics.append(("Best season bWAR", f"{best_war[0]:,.1f}"))
    honours: dict[str, set] = {}

    if sport.has_draftguru_player_cards:
        if {"draft", "draft_links"} <= tables:
            draft = _con.execute(
                """SELECT d.draft_year, d.draft_type, d.pick, d.club,
                          d.original_club
                     FROM draft d JOIN draft_links l ON l.draft_rowid=d.rowid
                    WHERE l.player_id=? AND l.match_status IN
                          ('from_draft','unique','resolved')
                    ORDER BY CASE WHEN LOWER(d.draft_type) LIKE '%national%'
                                  THEN 0 ELSE 1 END, d.draft_year LIMIT 1""",
                (pid,)).fetchone()
            if draft:
                year, draft_type, pick, club, recruited = draft
                parts = [f"Pick {int(pick)}" if pick is not None else None,
                         str(int(year)) if year is not None else None,
                         str(draft_type) if draft_type else None]
                value = " · ".join(part for part in parts if part)
                if club:
                    value += f" · {club}"
                bio.append(("Draft", value))
                # The path to the draft, junior club first, as the source
                # writes it: "Greythorn / Xavier College / Oakleigh U18".
                if recruited and str(recruited).strip():
                    bio.append(("Recruited from", str(recruited).strip()))

        if {"awards", "person_links"} <= tables:
            for name, season in _con.execute(
                """SELECT a.award_name, a.season
                     FROM awards a JOIN person_links l
                       ON l.dg_person_id=a.dg_person_id
                    WHERE l.player_id=? AND l.match_status IN
                          ('from_draft','unique','resolved')""", (pid,)):
                honours.setdefault(_clean_award_name(name), set()).add(season)
        if {"all_australian", "person_links"} <= tables:
            seasons = {row[0] for row in _con.execute(
                """SELECT aa.season FROM all_australian aa JOIN person_links l
                       ON l.dg_person_id=aa.dg_person_id
                    WHERE l.player_id=? AND l.match_status IN
                          ('from_draft','unique','resolved')""", (pid,))}
            if seasons:
                honours["All-Australian"] = seasons

    elif sport.native_awards_table and sport.native_awards_table in tables:
        # An awards table keyed directly by player_id, the Lahman shape --
        # declared by the sport rather than inferred from its key.
        for name, season in _con.execute(
                f"SELECT award, season FROM {sport.native_awards_table} "
                f"WHERE player_id=?", (pid,)):
            honours.setdefault(_clean_award_name(name), set()).add(season)

    else:
        # Draft facts carried as plain columns on `players` (the nflverse
        # shape). `wanted` is empty for any build without them, so this
        # falls through quietly rather than being keyed to one sport.
        wanted = [col for col in ("draft_year", "draft_round", "draft_pick",
                                  "draft_team") if col in player_columns]
        if wanted:
            row = _con.execute(
                f"SELECT {', '.join(wanted)} FROM {sc.players} "
                f"WHERE {sc.player_id}=?", (pid,)).fetchone()
            values = dict(zip(wanted, row or ()))
            parts = []
            if values.get("draft_pick") is not None:
                parts.append(f"Pick {int(values['draft_pick'])}")
            if values.get("draft_round") is not None:
                parts.append(f"round {int(values['draft_round'])}")
            if values.get("draft_year") is not None:
                parts.append(str(int(values["draft_year"])))
            if values.get("draft_team"):
                parts.append(str(values["draft_team"]))
            if parts:
                bio.append(("Draft", " · ".join(parts)))

    if "wiki_awards" in tables:
        for name, season in _con.execute(
                """SELECT award_name, season FROM wiki_awards
                    WHERE player_id=? AND match_status IN ('unique','resolved')""",
                (pid,)):
            honours.setdefault(_clean_award_name(name), set()).add(season)

    # A nomination and the award itself are separate honours, and a player
    # can hold both in one season -- every winner was nominated first. They
    # are listed apart so "nominated three times, won once" is legible,
    # rather than one line that cannot say which season was the win.
    #
    # The win is filed under the label the Draftguru awards layer already
    # uses, because it already records every winner from 1993 on. A label
    # of its own listed the same win twice under two names; sharing this
    # one means the seasons merge into a single honour, and the win still
    # shows on a database that has the nominations but not that layer.
    if "rising_star_nominees" in tables:
        won_label = _clean_award_name("Rising Star Award (AFL)")
        for season, won, ineligible in _con.execute(
                """SELECT season, is_season_winner, ineligible
                     FROM rising_star_nominees
                    WHERE player_id=? AND match_status IN
                          ('unique','resolved')""", (pid,)):
            honours.setdefault("AFL Rising Star nominee", set()).add(season)
            if won:
                honours.setdefault(won_label, set()).add(season)
            elif ineligible:
                # Worth its own line: the nomination stood, but suspension
                # put the award out of reach. Folding it into the nominee
                # row would lose the only part that is unusual.
                honours.setdefault(
                    "Rising Star nominee, ineligible (suspension)",
                    set()).add(season)

    honour_rows = [
        {"Honour": label, "Times": len(seasons),
         "Seasons": ", ".join(str(s) for s in sorted(
             season for season in seasons if season is not None))}
        for label, seasons in honours.items() if label
    ]
    honour_rows.sort(key=lambda row: (
        _honour_order(sport_key, row["Honour"]), -row["Times"], row["Honour"]))
    return {"metrics": metrics, "bio": bio, "honours": honour_rows}


def _render_card_tiles(tiles):
    """Render data-rich footer metrics without squeezing past four columns."""
    for start in range(0, len(tiles), 4):
        group = tiles[start:start + 4]
        columns = st.columns(len(group))
        for col, (label, value) in zip(columns, group):
            col.markdown(
                f"<div class='count'>{value}</div>"
                f"<div class='count-label'>{label}</div>",
                unsafe_allow_html=True)


def _format_height(value, unit):
    """`70, "in"` -> `5′10″`; `198.1, "cm"` -> `198 cm`."""
    if value is None:
        return None
    if unit == "cm":
        return f"{value:.0f} cm"
    inches = int(round(value))
    return f"{inches // 12}′{inches % 12}″"


def _format_weight(value, unit):
    if value is None:
        return None
    return f"{value:.0f} {unit}"


def _career_blurb(V, debut, final, games, n_clubs, draft_year=None,
                  career_score=0, titles=None, honours=(), name=None):
    """One flavour-text sentence, in the voice of a trading card back.

    `name` is the subject when the caller has it. The card already knows who
    it is -- the name is in the banner directly above -- so the older
    "Across 195 games ..., this player recorded 232 goals" read as though
    the sentence had been written for somebody anonymous and then pasted
    onto a card that names them. Falls back to "This player" so a caller
    without a name still gets a grammatical sentence.

    `position` is deliberately not woven into the sentence: the data holds
    codes ("RB", "G"), not nouns, and "this rb played..." reads as a typo
    rather than prose. The position still shows in the bio rows above,
    where it is a label rather than a sentence subject.

    Returns "" for a player the database knows of but has no games for --
    thousands of the NFL's -- because "played 0 games for 0 teams between
    None and None" is worse than saying nothing.
    """
    if not (debut and final and games):
        return ""
    club_clause = f"one {V.club}" if n_clubs == 1 else f"{n_clubs} {V.clubs}"
    prefix = f"Drafted in {int(draft_year)}. " if draft_year else ""
    subject = str(name).strip() if name and str(name).strip() else "This player"
    sentence = (f"{subject} played {games:,} {V.games} for {club_clause} "
                f"from {debut} to {final}")
    achievements = []
    if career_score:
        achievements.append(f"recorded {int(career_score):,} {V.score}")
    if titles:
        title_label = V.title if int(titles) == 1 else V.title_plural.lower()
        achievements.append(f"won {int(titles)} {title_label}")
    for honour in list(honours)[:2]:
        achievements.append(
            f"earned {int(honour['Times'])}x {honour['Honour']}"
        )
    if achievements:
        # The clauses hang off the opening one, so each is introduced by a
        # comma rather than the bare space the anonymous phrasing needed.
        if len(achievements) == 1:
            sentence += ", " + achievements[0]
        else:
            sentence += ", " + ", ".join(achievements[:-1])
            sentence += ", and " + achievements[-1]
    return prefix + sentence + "."


def _career_charts(sport, seasons, V) -> None:
    """The shape of a career, above the season table that details it.

    Two charts rather than one with two scales: games and goals are
    different sizes, and drawing them against a shared axis would invent a
    relationship the numbers do not have. The score chart is dropped
    entirely for a player who never scored, where a row of nothing is not
    a finding about them but a fact about their position.
    """
    import charts

    games_column = V.games.capitalize()
    score_column = V.score.capitalize()
    blue, orange = charts.series_colours()
    drawn = [
        (f"{games_column} per {V.season}",
         charts.career_chart(seasons, "Season", games_column,
                             games_column, blue)),
        (f"{score_column} per {V.season}",
         charts.career_chart(seasons, "Season", score_column,
                             score_column, orange)),
    ]
    drawn = [(title, chart) for title, chart in drawn if chart is not None]
    if not drawn:
        return
    for column, (title, chart) in zip(st.columns(len(drawn)), drawn):
        with column:
            st.caption(title)
            st.altair_chart(chart, width="stretch")


def _player_card_logos(sport, con, clubs_hist):
    """League badge and resolved team logos for a player's card header."""
    import overlays

    league = (Path(__file__).resolve().parent / "resources" / "teams" /
              sport.key / f"{sport.key}.png")
    logos = [(sport.label, str(league))] if league.is_file() else []
    seen = {str(league)} if league.is_file() else set()
    for club in (part.strip() for part in clubs_hist.split("|") if part.strip()):
        path = overlays.logo_for(sport, con, club)
        if path and path not in seen:
            logos.append((club, path))
            seen.add(path)
    return logos


def render_player_profile(sport, con, pid, key_prefix="explore",
                          heading_level="##", nested=False):
    """One player's career, rendered as a trading-card back wherever it is
    asked for.

    Split out of the Player Search page so the Grid Solver can show the
    same profile in a dialog without navigating away from the board, and
    without a second copy of these queries drifting from this one.

    `key_prefix` namespaces the widgets inside: two callers on one page
    each need their own "ranked by" selectbox. `heading_level` is accepted
    for backward compatibility but no longer used -- the card banner
    carries its own heading treatment regardless of caller.

    `nested` says this profile is itself inside a dialog. Streamlit opens
    at most one dialog per script run, so the season and game tables below
    are drawn plain in that case rather than raising when a click inside
    the card tries to open a second overlay.
    """
    V, sc = sport.vocab, sport.schema
    revision = _db_revision(sport.db)

    bio_fields = [(name, col) for name, col in (
        ("position", sc.position), ("height", sc.height),
        ("weight", sc.weight), ("college", sc.college),
        ("draft_year", sc.draft_year),
    ) if col]
    bio_select = "".join(f", {col}" for _, col in bio_fields)
    profile_sql = (
        f"SELECT {sc.player}, {sc.debut_season}, {sc.final_season}, "
        f"{sc.career_games}, {sc.career_score}, {sc.career_postseason}, "
        f"{sc.clubs_hist}, {sc.birth_year}, {sc.obscurity}{bio_select} "
        f"FROM {sc.players} WHERE {sc.player_id} = ?"
    )
    p = _fetchone(profile_sql, (pid,), revision, con)
    if not p:
        return
    bio = dict(zip((name for name, _ in bio_fields), p[9:]))
    # Every one of these is optional in at least one build. 13,670 of the
    # NFL's players are known to the rosters without ever appearing in a
    # game row, so they have no club history, no debut or final season and
    # no career games -- and this card is reachable for them from the
    # player picker and from any table that lists them.
    clubs_hist = p[6] or ""
    debut, final = p[1], p[2]
    career_games = p[3] or 0
    # One entry per club, named as the club was at the time: "Kangaroos|
    # North Melbourne" is one club that renamed itself mid-career, and
    # counting or listing it twice reads as a two-club journeyman.
    clubs_shown = sport.collapse_club_path(clubs_hist)
    n_clubs = len(clubs_shown.split("|")) if clubs_shown else 0

    # Trusted captain appointments, shown in the bio column when a sport
    # has them. Gated on the constraints module declaring the layer rather
    # than on the sport's name, so a sport that later gains captaincy data
    # gets this for free.
    captain_line = None
    if getattr(sport.C, "captain_available", None):
        has_captains = con.execute(
            "SELECT 1 FROM main.sqlite_master "
            "WHERE type='table' AND name='captaincies'"
        ).fetchone()
        if has_captains:
            captain_rows = _fetchall(
                "SELECT club, MIN(season), MAX(season), COUNT(DISTINCT season) "
                "FROM captaincies WHERE player_id = ? "
                "AND match_status IN ('unique','resolved') "
                "GROUP BY club ORDER BY MIN(season), club",
                (pid,), revision, con,
            )
            if captain_rows:
                appointments = []
                for club, lo, hi, seasons in captain_rows:
                    years = str(lo) if lo == hi else f"{lo}–{hi}"
                    appointments.append(
                        f"{club} ({years}; {seasons} season"
                        f"{'s' if seasons != 1 else ''})"
                    )
                captain_line = "Club captain: " + " · ".join(appointments)

    # Premierships / World Series / Super Bowls, for the sports whose data
    # can actually answer it.
    titles = _titles_won(sport, con, pid, revision)
    enrichment = _player_card_enrichment(sport.key, pid, revision, con)
    tiles = [
        (V.games.capitalize(), f"{career_games:,}"),
        (V.score.capitalize(), f"{int(p[4] or 0):,}"),
        (V.postseason.capitalize(), p[5] if p[5] is not None else 0),
    ]
    if titles is not None:
        tiles.append((V.title_plural, titles))
    tiles.extend(enrichment["metrics"])

    import overlays
    card_logos = _player_card_logos(sport, con, clubs_hist)
    logos_html = "".join(
        overlays.logo_html(path, height=42, alt=label)
        for label, path in card_logos[:8]
    )

    span = (f"{debut}–{final}" if debut and final
            else str(debut or final or ""))
    subtitle = " · ".join(
        part for part in (span, clubs_shown.replace("|", ", ")) if part)

    with st.container(border=True):
        st.markdown(
            f"<div class='card-banner'>"
            f"<div><div class='card-banner-name'>{p[0]}</div>"
            f"<div class='card-banner-logos'>{logos_html}</div></div>"
            f"<div class='card-banner-stars'>{core.stars_html(p[8])}</div>"
            f"</div>"
            f"<div class='card-banner-sub'>{subtitle}</div>",
            unsafe_allow_html=True)
        _render_card_tiles(tiles)
        if captain_line:
            st.caption(captain_line)

        bio_col, stat_col = st.columns([1, 2])
        with bio_col:
            st.markdown("<div class='card-section-label'>Bio</div>",
                       unsafe_allow_html=True)
            bio_rows = []
            if p[7]:
                bio_rows.append(("Born", int(p[7])))
            if bio.get("position"):
                bio_rows.append(("Position", bio["position"]))
            height = _format_height(bio.get("height"), sc.height_unit)
            if height:
                bio_rows.append(("Height", height))
            weight = _format_weight(bio.get("weight"), sc.weight_unit)
            if weight:
                bio_rows.append(("Weight", weight))
            if bio.get("college"):
                bio_rows.append(("College", bio["college"]))
            if (bio.get("draft_year")
                    and not any(label == "Draft"
                                for label, _ in enrichment["bio"])):
                bio_rows.append(("Drafted", int(bio["draft_year"])))
            bio_rows.extend(enrichment["bio"])
            for label, val in bio_rows:
                st.markdown(
                    f"<div class='bio-row'><span class='bio-label'>{label}"
                    f"</span><span class='bio-value'>{val}</span></div>",
                    unsafe_allow_html=True)
            blurb = _career_blurb(
                V, debut, final, career_games, n_clubs,
                bio.get("draft_year"), p[4] or 0, titles,
                enrichment["honours"], name=p[0],
            )
            if blurb:
                st.caption(blurb)

        with stat_col:
            st.markdown(
                "<div class='card-section-label'>Season by season</div>",
                unsafe_allow_html=True)
            game_columns = {
                row[1] for row in con.execute(
                    f"PRAGMA table_info({sc.games})")
            }
            has_player_war = (
                "war" in game_columns
                and sport.layer_ready("war_available", con)
                and con.execute(
                    f"SELECT 1 FROM {sc.games} WHERE {sc.player_id}=? "
                    "AND war IS NOT NULL LIMIT 1", (pid,)).fetchone()
            )
            war_select = (", ROUND(SUM(war), 1) AS bWAR"
                          if has_player_war else "")
            if sc.games_per_row and sc.games_per_row in game_columns:
                # Season-grain rows: each stands for `games_per_row` games,
                # so appearances are summed rather than rows counted.
                season_games_sql = (
                    f"SUM(CASE WHEN {sc.is_final}=0 "
                    f"THEN {sc.games_per_row} ELSE 0 END)")
                season_score_sql = (
                    f"SUM(CASE WHEN {sc.is_final}=0 "
                    f"THEN {sc.game_score} ELSE 0 END)")
                season_average_sql = (
                    f"ROUND(CAST({season_score_sql} AS REAL) / "
                    f"NULLIF({season_games_sql}, 0), 2)")
                postseason_sql = (
                    f"SUM(CASE WHEN {sc.is_final}=1 "
                    f"THEN {sc.games_per_row} ELSE 0 END)")
            else:
                season_games_sql = "COUNT(*)"
                season_score_sql = f"SUM({sc.game_score})"
                season_average_sql = f"ROUND(AVG({sc.game_score}), 2)"
                postseason_sql = f"SUM({sc.is_final})"
            seasons_sql = f"""
                SELECT {sc.season} AS Season, {sc.club_hist} AS "{V.club.capitalize()}",
                       {season_games_sql} AS "{V.games.capitalize()}",
                       {season_score_sql} AS "{V.score.capitalize()}",
                       {season_average_sql} AS "{V.score}/{V.game}",
                       SUM(CASE WHEN {sc.result}='W' THEN 1 ELSE 0 END) AS W,
                       SUM(CASE WHEN {sc.result}='L' THEN 1 ELSE 0 END) AS L,
                       {postseason_sql} AS "{V.postseason.capitalize()}"
                       {war_select}
                FROM {sc.games} WHERE {sc.player_id} = ?
                GROUP BY {sc.season}, {sc.club_hist} ORDER BY {sc.season}"""
            seasons = _read_frame(seasons_sql, (pid,), revision, con)
            if seasons.empty:
                st.dataframe(seasons, hide_index=True, width="stretch",
                             height=300)
            else:
                _career_charts(sport, seasons, V)
                # A row here is one club's season, so it opens that season
                # for that club: who won it, how the club went, who led it.
                import components
                st.caption(f"Select a {V.season} for its overview.")
                components.clickable_season_table(
                    seasons, seasons["Season"].tolist(), sport, con,
                    key=sport.k(key_prefix, "seasons", pid),
                    clubs=seasons[V.club.capitalize()].tolist(),
                    nested=nested,
                    height=300)

        honours = enrichment["honours"]
        if honours:
            st.markdown("<div class='card-section-label'>Career honours</div>",
                        unsafe_allow_html=True)
            selected_award = st.pills(
                "Select an award for details:", 
                [f"{r['Times']}× {r['Honour']}" for r in honours],
                label_visibility="collapsed",
                key=sport.k(key_prefix, "awards", pid)
            )
            if selected_award:
                for r in honours:
                    if f"{r['Times']}× {r['Honour']}" == selected_award:
                        st.info(f"**{r['Honour']}** (Won {r['Times']} times)\n\nSeasons: {r['Seasons']}")
                        break

    # Brownlow voting is an optional AFL-only enrichment. Keep it as its own
    # compact season record rather than repeating one season total on every
    # club row when a player changed clubs mid-year.
    has_brownlow = con.execute(
        "SELECT 1 FROM main.sqlite_master "
        "WHERE type='table' AND name='brownlow_results'"
    ).fetchone()
    if has_brownlow:
        brownlow = _read_frame(
            """SELECT season AS Season, votes AS Votes,
                      eligible_rank AS Finish, vote_rank AS "Vote rank",
                      clubs AS Club,
                      CASE WHEN winner=1 THEN 'Winner'
                           WHEN ineligible=1 THEN 'Ineligible' ELSE '' END AS Status
                 FROM brownlow_results
                WHERE player_id=? AND match_status IN ('unique','resolved')
                ORDER BY season""",
            (pid,), revision, con)
        if not brownlow.empty:
            st.markdown("### Brownlow record")
            st.caption("Seasons in which this player polled at least one vote.")
            import components
            components.clickable_season_table(
                brownlow, brownlow["Season"].tolist(), sport, con,
                key=sport.k(key_prefix, "brownlow", pid), nested=nested)

    _form_section(sport, con, pid, key_prefix, revision)

    st.markdown(f"### Biggest {V.games}")
    metric = st.selectbox("Ranked by", list(sc.stats),
                          format_func=labels.title,
                          key=sport.k(key_prefix, "best", pid))
    warning = sport.stat_era_warning(metric)
    if warning:
        st.caption(f"⚠ {warning}")
    metric_header = labels.title(metric)
    best_sql = f"""
        SELECT {sc.player_id} AS PlayerID,
               {sc.player} AS Player, {sc.season} AS Season,
               {sc.round} AS Rnd, {sc.club_hist} AS For,
               {sc.opponent} AS Opponent,
               {sc.venue} AS "{V.venue.capitalize()}", {sc.result} AS Res,
               {metric} AS "{metric_header}"
        FROM {sc.games}
        WHERE {sc.player_id} = ? AND {metric} IS NOT NULL
        ORDER BY {metric} DESC, {sc.season} LIMIT 20"""
    best = _read_frame(best_sql, (pid,), revision, con)
    if best.empty:
        st.dataframe(best.drop(columns=["Player"], errors="ignore"),
                     hide_index=True, width="stretch")
    else:
        import components
        st.caption(f"Select a {V.game} for its full record.")
        components.clickable_game_table(
            best, sport, con, key=sport.k(key_prefix, "best_games", pid),
            stat=metric_header, nested=nested,
            column_order=[c for c in best.columns
                          if c not in ("Player", "PlayerID")])


def _form_section(sport, con, pid, key_prefix, revision) -> None:
    """A stat across the whole career with its rolling average on top.

    Game-grain sports chart every recorded game by career game number; a
    season-grain sport (MLB — `schema.games_per_row` is set) charts the
    per-game rate of each season instead, because it has no game rows to
    roll over. Either way the average is taken over *recorded* entries
    only: a career that straddles a stat's first recorded season starts
    its line where the record starts, not at an invented zero.
    """
    import charts

    V, sc = sport.vocab, sport.schema
    st.markdown("### Form")

    stats = list(sc.stats)
    default = stats.index(sc.game_score) if sc.game_score in stats else 0
    left, right = st.columns([2, 1])
    stat = left.selectbox("Statistic", stats, index=default,
                          format_func=labels.title,
                          key=sport.k(key_prefix, "form_stat", pid))
    warning = sport.stat_era_warning(stat)

    if sc.games_per_row:
        window = 3
        right.caption(f"{window}-{V.season} average")
        sql = (f"SELECT {sc.season} AS Season, "
               f"ROUND(CAST(SUM({stat}) AS REAL) "
               f"/ NULLIF(SUM({sc.games_per_row}), 0), 2) AS Value "
               f"FROM {sc.games} WHERE {sc.player_id} = ? "
               f"AND {sc.is_final} = 0 AND {stat} IS NOT NULL "
               f"GROUP BY {sc.season} ORDER BY {sc.season}")
        frame = _read_frame(sql, (pid,), revision, con)
        chart = charts.rolling_form_chart(
            frame, "Season", "Value",
            f"{labels.title(stat)} per {V.game}", window,
            x_title=V.season.capitalize(), ordinal_x=True)
        caption = (f"Each bar is one {V.season}'s per-{V.game} rate; "
                   f"the line is the {window}-{V.season} average.")
    else:
        window = right.segmented_control(
            "Window", [5, 10, 20], default=10,
            key=sport.k(key_prefix, "form_window", pid),
            label_visibility="collapsed") or 10
        sql = (f"SELECT {sc.career_game_no} AS Game, {stat} AS Value "
               f"FROM {sc.games} WHERE {sc.player_id} = ? "
               f"ORDER BY {sc.career_game_no}")
        frame = _read_frame(sql, (pid,), revision, con)
        chart = charts.rolling_form_chart(
            frame, "Game", "Value", labels.title(stat), int(window),
            x_title=f"Career {V.game}")
        caption = (f"Each bar is one {V.game}; the line is the mean of "
                   f"the last {window} {V.games} the stat was recorded "
                   f"in, and starts once {window} exist.")

    if chart is None:
        st.caption(f"{labels.title(stat)} was not recorded for any of "
                   f"this player's {V.games}."
                   + (f" {warning}" if warning else ""))
        return
    if warning:
        st.caption(f"⚠ {warning}")
    st.altair_chart(chart, width="stretch")
    st.caption(caption)


# ---------------------------------------------------- player comparison

@st.cache_data(show_spinner=False, max_entries=8)
def _career_rates(sport_key, revision, _con) -> pd.DataFrame:
    """Career per-game rates for every qualifying player, one stat a column.

    The percentile a profile bar shows is a rank within this frame, so
    who qualifies changes every number: the floor is the same career
    games minimum the constraint engine's averages use. Season-grain
    sports rate SUM(stat)/SUM(games) over regular-season rows; game-grain
    sports average the game rows, NULLs excluded either way.
    """
    import sports as _sports

    sport = _sports.get(sport_key)
    sc = sport.schema
    stats = list(sc.stats)[:8]
    if sc.games_per_row:
        selects = ", ".join(
            f"CAST(SUM({stat}) AS REAL) / NULLIF(SUM({sc.games_per_row}), 0)"
            f" AS {stat}" for stat in stats)
        volume = f"SUM({sc.games_per_row})"
        where = f"WHERE {sc.is_final} = 0"
    else:
        selects = ", ".join(f"AVG({stat}) AS {stat}" for stat in stats)
        volume = "COUNT(*)"
        where = ""
    frame = pd.read_sql_query(
        f"SELECT {sc.player_id} AS pid, {volume} AS n, {selects} "
        f"FROM {sc.games} {where} GROUP BY {sc.player_id} "
        f"HAVING n >= {int(core.Generic.CAREER_AVG_MIN_GAMES)}",
        _con)
    for stat in stats:
        frame[f"{stat}__pct"] = frame[stat].rank(pct=True) * 100
    return frame


def _percentile_profile(sport, con, revision, a, b):
    """A tidy (Player, Attribute, Value, Percentile) frame for two careers.

    Returns (frame, skipped): attributes neither player has a recorded
    rate for are skipped and named, so the chart never draws a zero for
    "the source measured nothing".
    """
    rates = _career_rates(sport.key, revision, con)
    stats = list(sport.schema.stats)[:8]
    rows, skipped = [], []
    by_pid = rates.set_index("pid")
    for stat in stats:
        drawn = False
        for pid, name in (a, b):
            if pid in by_pid.index:
                value = by_pid.at[pid, stat]
                pct = by_pid.at[pid, f"{stat}__pct"]
                if pd.notna(value) and pd.notna(pct):
                    rows.append({"Player": name,
                                 "Attribute": labels.title(stat),
                                 "Value": round(float(value), 2),
                                 "Percentile": float(pct)})
                    drawn = True
        if not drawn:
            skipped.append(labels.title(stat))
    return pd.DataFrame(rows), skipped


def _compare_players(sport, con, player_picker):
    """Two careers side by side, honest about what is comparable."""
    import player_compare as PC

    V, sc = sport.vocab, sport.schema
    left, right = st.columns(2)
    with left:
        a_sel = player_picker(sport.k("cmp_a"), label="First player")
    with right:
        b_sel = player_picker(sport.k("cmp_b"), label="Second player")

    if a_sel is None or b_sel is None:
        st.caption("Pick a player on each side to compare them.")
        return
    if a_sel[0] == b_sel[0]:
        st.info("Pick two different players.")
        return

    a = PC.profile(con, a_sel[0], sc)
    b = PC.profile(con, b_sel[0], sc)
    if a is None or b is None:
        return

    # -- headline -------------------------------------------------------
    hl, hr = st.columns(2)
    for col, p in ((hl, a), (hr, b)):
        col.markdown(f"### {p.player}")
        col.caption(f"{p.span} · {sport.collapse_club_path(p.clubs)}")
        m1, m2, m3 = col.columns(3)
        m1.metric(V.games.capitalize(), f"{p.career_games:,}")
        m2.metric(V.score.capitalize(), f"{p.career_score:,}")
        m3.metric(V.postseason.capitalize(), f"{p.finals:,}")

    # -- skill profile --------------------------------------------------
    import charts

    name_a, name_b = a.player, b.player
    if name_a == name_b:            # 460 names belong to more than one player
        name_a = f"{a.player} ({a.span})"
        name_b = f"{b.player} ({b.span})"
    profile, skipped = _percentile_profile(
        sport, con, _db_revision(sport.db),
        (a_sel[0], name_a), (b_sel[0], name_b))
    profile_chart = charts.percentile_profile_chart(profile,
                                                    (name_a, name_b))
    if profile_chart is not None:
        st.markdown("#### Skill profile")
        st.caption(
            f"League percentile of each per-{V.game} rate, ranked among "
            f"players with {core.Generic.CAREER_AVG_MIN_GAMES}+ career "
            f"{V.games}. A missing bar is a rate the era never recorded "
            f"for that career, not a zero.")
        st.altair_chart(profile_chart, width="stretch")
        if skipped:
            st.caption("Not recorded for either career: "
                       + ", ".join(skipped) + ".")

    # -- career shape ---------------------------------------------------
    st.markdown("#### Career")
    shape = [
        (V.games.capitalize(), a.career_games, b.career_games),
        (V.score.capitalize(), a.career_score, b.career_score),
        (V.postseason.capitalize(), a.finals, b.finals),
        ("Seasons spanned", a.seasons, b.seasons),
        (f"{V.score.capitalize()} per {V.game}",
         round(a.career_score / a.career_games, 2) if a.career_games else 0,
         round(b.career_score / b.career_games, 2) if b.career_games else 0),
    ]
    st.dataframe(
        pd.DataFrame([{"Measure": label, a.player: x, b.player: y,
                       "Leader": a.player if x > y else
                                 (b.player if y > x else "—")}
                      for label, x, y in shape]),
        hide_index=True, width="stretch")

    # -- statistics -----------------------------------------------------
    shared = PC.comparable_stats(a, b)
    if shared:
        st.markdown("#### Statistics")
        basis = st.radio("Compare on", ["Career total", f"Per {V.game}",
                                        f"Best single {V.game}"],
                         horizontal=True, key=sport.k("cmp_basis"))
        # Streamlit's NumberColumn takes a printf-style format string, not
        # a str.format one: '{:,.0f}' is rendered literally in every cell.
        if basis == "Career total":
            pick = lambda p, s: p.totals[s]          # noqa: E731
            fmt = "%.0f"
        elif basis == f"Per {V.game}":
            pick = lambda p, s: p.per_game[s]        # noqa: E731
            fmt = "%.2f"
        else:
            pick = lambda p, s: p.best[s]            # noqa: E731
            fmt = "%.0f"

        rows = []
        for stat in shared:
            x, y = pick(a, stat), pick(b, stat)
            lo_a, hi_a = a.covered[stat]
            lo_b, hi_b = b.covered[stat]
            rows.append({
                "Statistic": labels.title(stat),
                a.player: float(x), b.player: float(y),
                "Leader": a.player if x > y else (b.player if y > x else "—"),
                "Recorded": f"{lo_a}–{hi_a} vs {lo_b}–{hi_b}",
            })
        frame = pd.DataFrame(rows)
        st.dataframe(
            frame, hide_index=True, width="stretch",
            column_config={
                a.player: st.column_config.NumberColumn(format=fmt),
                b.player: st.column_config.NumberColumn(format=fmt),
                "Recorded": st.column_config.TextColumn(
                    help="Seasons each player actually has this statistic "
                         "for. A shorter window is a recording-era limit, "
                         "not a weaker career."),
            })

    # A statistic only one of them could ever record is not a comparison.
    gaps = PC.era_gap(a, b, list(sc.stats))
    if gaps:
        with st.expander(f"{len(gaps)} statistics not comparable across "
                         "these eras"):
            for note in gaps:
                st.write(f"• {note}")

    # -- honours --------------------------------------------------------
    if a.honours or b.honours:
        st.markdown("#### Honours")
        oh1, oh2 = st.columns(2)
        for col, p in ((oh1, a), (oh2, b)):
            col.markdown(f"**{p.player}**")
            if p.honours:
                col.dataframe(
                    pd.DataFrame([{"Honour": k, "Detail": v}
                                  for k, v in p.honours]),
                    hide_index=True, width="stretch")
            else:
                col.caption("No linked award or selection rows.")

    # -- shared matches -------------------------------------------------
    both = PC.overlap(con, a, b, sc)
    if both:
        together, against = both.get("together", 0), both.get("against", 0)
        if together or against:
            st.markdown("#### Shared matches")
            s1, s2 = st.columns(2)
            s1.metric("As teammates", f"{together:,}",
                      help=(f"{both.get('together_from')}–"
                            f"{both.get('together_to')}") if together else None)
            s2.metric("As opponents", f"{against:,}",
                      help=(f"{both.get('against_from')}–"
                            f"{both.get('against_to')}") if against else None)
        else:
            st.caption("These two never played in the same match.")


def _player_connections(sport, con, player_picker):
    """Search the exact matches shared by any two selected players."""
    sc = sport.schema
    game_columns = {row[1] for row in con.execute(
        f"PRAGMA table_info({sc.games})")}
    if "match_id" not in game_columns:
        st.info("Shared-match search is not available for this sport's data.")
        return

    left, right = st.columns(2)
    with left:
        a_sel = player_picker(sport.k("connections_a"), label="First player")
    with right:
        b_sel = player_picker(sport.k("connections_b"), label="Second player")
    if a_sel is None or b_sel is None:
        st.caption("Pick two players to find every match they played together or against each other.")
        return
    if a_sel[0] == b_sel[0]:
        st.info("Pick two different players.")
        return

    stat_columns = [name for name in
                    ("goals", "marks", "disposals", "kicks", "tackles", "brownlow")
                    if name in game_columns]
    stat_sql = "".join(
        f', ga.{name} AS "A {labels.title(name)}", '
        f'gb.{name} AS "B {labels.title(name)}"'
        for name in stat_columns)
    score_sql = ""
    if {"points_for", "points_against"}.issubset(game_columns):
        score_sql = (", CAST(ga.points_for AS INTEGER) || '–' || "
                     "CAST(ga.points_against AS INTEGER) AS Score")
    rows = pd.read_sql_query(
        f"""SELECT ga.season AS Season, ga.round AS Round, ga.date AS Date,
                   ga.venue AS Ground,
                   CASE WHEN ga.club_hist = gb.club_hist
                        THEN 'Teammates' ELSE 'Opponents' END AS Relationship,
                   ga.club_hist AS "A Club", gb.club_hist AS "B Club",
                   ga.opponent AS "A Opponent", ga.result AS "A Result"
                   {score_sql}{stat_sql}
              FROM {sc.games} ga
              JOIN {sc.games} gb ON gb.match_id = ga.match_id
             WHERE ga.{sc.player_id} = ? AND gb.{sc.player_id} = ?
             ORDER BY ga.date DESC, ga.season DESC""",
        con, params=(a_sel[0], b_sel[0]))

    if rows.empty:
        st.info(f"{a_sel[1]} and {b_sel[1]} never played in the same match.")
        return
    together = int((rows["Relationship"] == "Teammates").sum())
    against = len(rows) - together
    with st.container(horizontal=True):
        st.metric("Shared matches", f"{len(rows):,}", border=True)
        st.metric("As teammates", f"{together:,}", border=True)
        st.metric("As opponents", f"{against:,}", border=True)
        st.metric("Seasons", f"{rows['Season'].nunique():,}", border=True)

    relationship = st.segmented_control(
        "Relationship", ["All", "Teammates", "Opponents"], default="All",
        key=sport.k("connections_relationship"))
    shown = rows if relationship == "All" else rows.loc[
        rows["Relationship"] == relationship]
    shown = shown.rename(columns={
        "A Club": f"{a_sel[1]} club", "B Club": f"{b_sel[1]} club",
        "A Opponent": f"{a_sel[1]} opponent",
        "A Result": f"{a_sel[1]} result",
        **{f"A {labels.title(stat)}": f"{a_sel[1]} {labels.title(stat)}"
           for stat in stat_columns},
        **{f"B {labels.title(stat)}": f"{b_sel[1]} {labels.title(stat)}"
           for stat in stat_columns},
    })
    st.caption("Select a round, ground or club in the table to open its full record.")
    import components
    components.clickable_entity_table(
        shown, sport, con,
        key=sport.k("connections", a_sel[0], b_sel[0], relationship))


# ------------------------------------------------------ stats explorer

def leaderboard_page(sport, con):
    V, sc = sport.vocab, sport.schema
    st.markdown("# Stats Explorer")
    st.caption(f"Search single-{V.game} performances, season totals and "
               "career records using the database's stat and context "
               "filters.")

    c1, c2, c3 = st.columns([1.2, 1.2, 1])
    stat = c1.selectbox("Statistic", list(sc.stats),
                        format_func=labels.title, key=sport.k("lb_stat"))
    scope = c2.selectbox("Scope", list(SCOPES), key=sport.k("lb_scope"))
    limit = c3.number_input("Rows", 5, 200, 30, step=5,
                            key=sport.k("lb_limit"))

    warning = sport.stat_era_warning(stat)
    if warning:
        st.caption(f"⚠ {warning}")

    where, params = _filters(sport, con, sport.k("lb"))
    mode = SCOPES[scope]
    stat_header = labels.title(stat)

    if mode == "game":
        q = f"""SELECT g.{sc.player} AS Player, g.{sc.season} AS Season,
                       g.{sc.round} AS Rnd, g.{sc.club_hist} AS For,
                       g.{sc.opponent} AS Opponent,
                       g.{sc.venue} AS "{V.venue.capitalize()}",
                       g.{stat} AS "{stat_header}",
                       p.{sc.career_games} AS "Career {V.games}",
                       p.{sc.obscurity} AS Obsc,
                       p.{sc.player_id} AS PlayerID
                FROM {sc.games} g JOIN {sc.players} p
                  ON p.{sc.player_id} = g.{sc.player_id}
                WHERE {where} AND g.{stat} IS NOT NULL
                ORDER BY g.{stat} DESC LIMIT ?"""
    elif mode == "season":
        q = f"""SELECT g.{sc.player} AS Player, g.{sc.season} AS Season,
                       g.{sc.club_hist} AS For,
                       COUNT(*) AS "{V.games.capitalize()}",
                       SUM(g.{stat}) AS "{stat_header}",
                       ROUND(AVG(g.{stat}),1) AS "per {V.game}",
                       p.{sc.obscurity} AS Obsc,
                       p.{sc.player_id} AS PlayerID
                FROM {sc.games} g JOIN {sc.players} p
                  ON p.{sc.player_id} = g.{sc.player_id}
                WHERE {where} AND g.{stat} IS NOT NULL
                GROUP BY g.{sc.player_id}, g.{sc.season}, g.{sc.club_hist}
                ORDER BY SUM(g.{stat}) DESC LIMIT ?"""
    else:
        q = f"""SELECT g.{sc.player} AS Player,
                       MIN(g.{sc.season}) || '-' || MAX(g.{sc.season})
                         AS Career,
                       COUNT(*) AS "{V.games.capitalize()}",
                       SUM(g.{stat}) AS "{stat_header}",
                       ROUND(AVG(g.{stat}),1) AS "per {V.game}",
                       p.{sc.obscurity} AS Obsc,
                       p.{sc.player_id} AS PlayerID
                FROM {sc.games} g JOIN {sc.players} p
                  ON p.{sc.player_id} = g.{sc.player_id}
                WHERE {where} AND g.{stat} IS NOT NULL
                GROUP BY g.{sc.player_id}
                ORDER BY SUM(g.{stat}) DESC LIMIT ?"""

    revision = _db_revision(sport.db)
    df = _read_frame(q, tuple(params + [limit]), revision, con)
    if df.empty:
        note = _era_note(sport, {stat})
        st.info("Nothing matches those filters." + (f" {note}" if note else ""))
        return

    # Raw obscurity is replaced by the star rating everywhere it is shown.
    df["Rating"] = df["Obsc"].map(core.stars_text)
    df = df.drop(columns=["Obsc"])
    player_ids = df["PlayerID"].tolist()
    df = df.drop(columns=["PlayerID"])
    st.caption("Select a row to see that player's full career.")
    import components   # deferred: components imports explore for the dialog body
    components.clickable_player_table(
        df, player_ids, sport, con, key=sport.k("lb_results"))
    with st.expander("SQL"):
        st.code(q, language="sql")

    _quadrant_section(sport, con, stat, where, params, revision)


def _quadrant_section(sport, con, stat, where, params, revision) -> None:
    """Career volume against per-game efficiency, quartered by medians.

    Behind a toggle because it aggregates every qualifying career: the
    leaderboard above stays instant and this renders only when asked.
    The active context filters carry over, so quartering "the field"
    means the field currently being looked at. Clicking a point opens
    that player's card.
    """
    import charts
    import components

    V, sc = sport.vocab, sport.schema
    if not st.toggle(f"Volume vs efficiency (career {labels.words(stat)})",
                     key=sport.k("lb_quadrant_on")):
        return

    games_word = V.games.capitalize()
    rate_column = f"{labels.title(stat)} per {V.game}"
    floor = int(core.Generic.CAREER_AVG_MIN_GAMES)
    if sc.games_per_row:
        volume = f"SUM(g.{sc.games_per_row})"
        rate = (f"ROUND(CAST(SUM(g.{stat}) AS REAL) "
                f"/ NULLIF(SUM(g.{sc.games_per_row}), 0), 2)")
    else:
        volume = "COUNT(*)"
        rate = f"ROUND(AVG(g.{stat}), 2)"
    quadrant_sql = f"""
        SELECT p.{sc.player_id} AS PlayerID, g.{sc.player} AS Player,
               {volume} AS "{games_word}", {rate} AS "{rate_column}"
        FROM {sc.games} g JOIN {sc.players} p
          ON p.{sc.player_id} = g.{sc.player_id}
        WHERE {where} AND g.{stat} IS NOT NULL
        GROUP BY g.{sc.player_id}
        HAVING {volume} >= {floor}
        ORDER BY SUM(g.{stat}) DESC LIMIT 400"""
    frame = _read_frame(quadrant_sql, tuple(params), revision, con)
    chart = charts.quadrant_chart(
        frame, games_word, rate_column, "Player",
        x_title=f"Career {V.games} with {labels.words(stat)} recorded",
        y_title=rate_column, id_column="PlayerID")
    if chart is None:
        st.caption("Nothing qualifies under the current filters.")
        return
    st.caption(
        f"The top 400 careers by total {labels.words(stat)} under the "
        f"current filters, {floor}+ recorded {V.games} each. The dashed "
        "lines are the medians of the shown field; the named points are "
        "the furthest from them. Click a point to open that player.")
    event = st.altair_chart(chart, on_select="rerun",
                            key=sport.k("lb_quadrant", stat))
    picked = [row.get("PlayerID")
              for row in event.selection.get("quadrant", [])
              if row.get("PlayerID") is not None]
    if picked:
        chosen = frame[frame["PlayerID"].isin(picked)]
        components.clickable_player_table(
            chosen.drop(columns=["PlayerID"]),
            chosen["PlayerID"].tolist(), sport, con,
            key=sport.k("lb_quadrant_pick", stat))


# --------------------------------------------------- random discovery

def _random_player(sport, con):
    sc = sport.schema
    return con.execute(f"""
        SELECT {sc.player_id}, {sc.player}, {sc.debut_season},
               {sc.final_season}, {sc.career_games}, {sc.career_score},
               {sc.career_postseason}, {sc.clubs_hist}, {sc.obscurity}
        FROM {sc.players} WHERE {sc.career_games} >= 10
        ORDER BY RANDOM() LIMIT 1""").fetchone()


def _random_performance(sport, con, stat):
    # Randomise within the top 200 rather than scanning the full games
    # table with ORDER BY RANDOM().
    sc = sport.schema
    rows = con.execute(f"""
        SELECT {sc.player}, {sc.season}, {sc.round}, {sc.club_hist},
               {sc.opponent}, {sc.venue}, {sc.result}, {stat}
        FROM {sc.games} WHERE {stat} IS NOT NULL
        ORDER BY {stat} DESC LIMIT 200""").fetchall()
    return random.choice(rows) if rows else None


def _random_journey(sport, con):
    """Return a multi-team player with clubs in actual career order."""
    sc = sport.schema
    player = con.execute(f"""
        SELECT {sc.player_id}, {sc.player}, {sc.debut_season},
               {sc.final_season}, {sc.career_games}, {sc.career_score},
               {sc.n_clubs}, {sc.obscurity}
        FROM {sc.players} WHERE {sc.n_clubs} >= 2
        ORDER BY RANDOM() LIMIT 1""").fetchone()
    if not player:
        return None

    clubs = con.execute(f"""
        SELECT {sc.club_hist}
        FROM {sc.games}
        WHERE {sc.player_id} = ?
        ORDER BY {sc.season}, {sc.date}, {sc.career_game_no}
    """, (player[0],)).fetchall()
    # Preserve transitions, including a return to an earlier club, while
    # collapsing repeated game rows for the same club.
    path = []
    for (club,) in clubs:
        if club and (not path or path[-1] != club):
            path.append(club)
    return (*player[:6], "|".join(path), player[6], player[7])


def _rating_tile(col, obscurity):
    col.markdown(f"<div>{core.stars_html(obscurity)}</div>"
                 f"<div class='count-label'>Obscurity</div>",
                 unsafe_allow_html=True)


def random_page(sport, con):
    V, sc = sport.vocab, sport.schema
    st.markdown("# Random Discovery")
    st.caption("Use the database as a discovery engine rather than starting "
               "with a specific player or record in mind.")

    kind = st.radio(
        "Discovery type",
        ["Player snapshot", "Notable performance",
         f"Multi-{V.club} journey"],
        horizontal=True, key=sport.k("random_kind"))

    stat = None
    if kind == "Notable performance":
        stat = st.selectbox("Performance statistic", list(sc.stats),
                            format_func=labels.title,
                            key=sport.k("random_stat"))

    result_key = sport.k("random_result")
    if st.button("Generate discovery", key=sport.k("random_generate")):
        if kind == "Player snapshot":
            st.session_state[result_key] = (kind, _random_player(sport, con))
        elif kind == "Notable performance":
            st.session_state[result_key] = (
                kind, stat, _random_performance(sport, con, stat))
        else:
            st.session_state[result_key] = (kind, _random_journey(sport, con))

    result = st.session_state.get(result_key)
    if not result:
        st.info("Choose a discovery type and generate a result.")
        return

    if result[0] == "Player snapshot":
        r = result[1]
        if not r:
            return
        st.markdown(f"## {r[1]}")
        st.write(f"Played **{r[2]}–{r[3]}** for "
                 f"**{r[7].replace('|', ', ')}**.")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric(V.games.capitalize(), f"{r[4]:,}")
        c2.metric(V.score.capitalize(), f"{int(r[5] or 0):,}")
        c3.metric(V.postseason.capitalize(), r[6])
        _rating_tile(c4, r[8])
    elif result[0] == "Notable performance":
        _, saved_stat, r = result
        if not r:
            return
        st.markdown(f"## {r[0]} — {int(r[7]) if r[7] is not None else '—'} "
                    f"{labels.words(saved_stat)}")
        st.write(f"**{r[1]} {r[2]}** · {r[3]} v {r[4]} · {r[5]} · {r[6]}")
        st.caption("Selected randomly from the top 200 performances for the "
                   "chosen statistic.")
    else:
        r = result[1]
        if not r:
            return
        st.markdown(f"## {r[1]}")
        st.write(f"A **{r[7]}-{V.club}** career from **{r[2]}–{r[3]}**: "
                 f"{r[6].replace('|', ' → ')}")
        c1, c2, c3 = st.columns(3)
        c1.metric(V.games.capitalize(), f"{r[4]:,}")
        c2.metric(V.score.capitalize(), f"{int(r[5] or 0):,}")
        _rating_tile(c3, r[8])


# ------------------------------------------------------------ game lab

def _new_game_target(sport, con):
    sc = sport.schema
    row = con.execute(f"""
        SELECT {sc.player_id} FROM {sc.players}
        WHERE {sc.career_games} >= 100
        ORDER BY RANDOM() LIMIT 1""").fetchone()
    return row[0] if row else None


def _new_game_pair(sport, con):
    """Two *different* players for a head-to-head round.

    Drawing twice from _new_game_target can return the same player twice,
    which made "who played more games?" unanswerable -- and, because the
    comparison is `>=`, scored both buttons as correct. One query with
    LIMIT 2 cannot repeat a row.
    """
    sc = sport.schema
    rows = con.execute(f"""
        SELECT {sc.player_id} FROM {sc.players}
        WHERE {sc.career_games} >= 100
        ORDER BY RANDOM() LIMIT 2""").fetchall()
    if len(rows) < 2:
        return None, None
    return rows[0][0], rows[1][0]


def _threshold_target(sport, con, keep=50, step=50):
    """A round career-games mark with roughly `keep` players above it.

    The mark was hard-coded at 300 games, which is a reasonable AFL career
    and a nonsensical target in three of the four sports -- an NFL career
    that long is nearly unheard of, so the challenge had no answers at all.
    Measuring the sport's own distribution keeps every league playable.
    """
    sc = sport.schema
    row = con.execute(
        f"SELECT {sc.career_games} FROM {sc.players} "
        f"WHERE {sc.career_games} IS NOT NULL "
        f"ORDER BY {sc.career_games} DESC LIMIT 1 OFFSET ?",
        (max(keep - 1, 0),)).fetchone()
    if not row or not row[0]:
        return None
    return max(int(row[0]) // step * step, step)


def game_lab_page(sport, con, player_picker):
    """
    Game Lab entry point, called by app.py.

    The real game lives in afl/game_lab.py: a clue ladder built from SQL
    predicates, plus a question bank drawn from criteria that have
    actually appeared on Gridley boards. That module reads AFL-only
    columns (career_brownlow and the AFL finals count) and AFL-only criterion
    wording, so any other sport falls through to the generic three-clue
    prototype below rather than being served AFL questions.
    """
    if sport.game_lab_module:
        import importlib

        module = importlib.import_module(sport.game_lab_module)
        module.game_lab_page(sport, con, player_picker)
        return

    st.markdown("# Game Lab")
    st.caption("A workspace for turning the database into playable "
               "challenges. This first prototype is a clue-based player "
               "game.")

    st.markdown("### Prototype Modes")
    st.caption("These modes are sport-agnostic and work across AFL, NBA, MLB, and NFL.")
    
    t1, t2, t3, t4 = st.tabs(["Guess the Player", "Guess the Career Path", "Higher or Lower", "Stat Threshold Challenge"])
    
    with t1:
        _game_guess_player(sport, con, player_picker)
    with t2:
        _game_career_path(sport, con, player_picker)
    with t3:
        _game_higher_lower(sport, con)
    with t4:
        _game_stat_threshold(sport, con)

def _game_guess_player(sport, con, player_picker):
    V, sc = sport.vocab, sport.schema
    target_key = sport.k("game_target")
    state_key = sport.k("game_state")
    
    if target_key not in st.session_state:
        st.session_state[target_key] = _new_game_target(sport, con)
        st.session_state[state_key] = {"guesses": 0, "history": [], "game_over": False, "won": False}

    if st.button("New mystery player", key=sport.k("new_game_player")):
        st.session_state[target_key] = _new_game_target(sport, con)
        st.session_state[state_key] = {"guesses": 0, "history": [], "game_over": False, "won": False}
        ui_widgets.clear_player_picker(sport.k("game_guess"))

    pid = st.session_state[target_key]
    target = con.execute(f"""
        SELECT {sc.player_id}, {sc.player}, {sc.debut_season},
               {sc.final_season}, {sc.career_games}, {sc.career_score},
               {sc.career_postseason}, {sc.clubs_hist}, {sc.n_clubs}
        FROM {sc.players} WHERE {sc.player_id} = ?""", (pid,)).fetchone()
    if not target:
        st.error("No eligible game target was found.")
        return

    state = st.session_state[state_key]
    max_guesses = 5
    
    # Render Game Status
    st.markdown(f"**Guesses used:** {state['guesses']} / {max_guesses}")
    if state["history"]:
        with st.expander("Guess History", expanded=True):
            for g in state["history"]:
                st.write(f"❌ {g}")

    clues_revealed = 1 + state["guesses"]
    plural = "s" if target[8] != 1 else ""
    st.info(f"**Clue 1:** Career span {target[2]}–{target[3]}; "
             f"played for {target[8]} {V.club}{plural}.")
    if clues_revealed >= 2:
        st.info(f"**Clue 2:** {target[4]:,} {V.games}, "
                 f"{int(target[5] or 0):,} {V.score} and {target[6]} "
                 f"{V.postseason}.")
    if clues_revealed >= 3:
        st.info(f"**Clue 3:** {V.clubs.capitalize()} — "
                 f"{target[7].replace('|', ', ')}.")

    if state["game_over"]:
        if state["won"]:
            st.success(f"You won! The mystery player was **{target[1]}**.")
            st.balloons()
        else:
            st.error(f"Game over. The mystery player was **{target[1]}**.")
        
        if st.button("Play Again", key=sport.k("play_again_btn"), type="primary"):
            st.session_state[target_key] = _new_game_target(sport, con)
            st.session_state[state_key] = {"guesses": 0, "history": [], "game_over": False, "won": False}
            st.rerun()
    else:
        selected = player_picker(sport.k("game_guess"), label="Your guess")
        if selected is not None and st.button("Submit guess", type="primary", key=sport.k("submit_guess")):
            guess_pid, guess_name = selected
            if guess_pid == target[0]:
                state["game_over"] = True
                state["won"] = True
            else:
                state["guesses"] += 1
                state["history"].append(guess_name)
                if state["guesses"] >= max_guesses:
                    state["game_over"] = True
            st.rerun()

    if not state["game_over"] and st.button("Reveal answer (Give up)", key=sport.k("reveal_game")):
        state["game_over"] = True
        state["won"] = False
        st.rerun()

    with st.expander("Possible next game modes"):
        st.write(
            "Name the teammate · Daily mystery player · Draft and award trivia")

def _game_career_path(sport, con, player_picker):
    sc = sport.schema
    target_key = sport.k("career_target")
    if target_key not in st.session_state:
        st.session_state[target_key] = _new_game_target(sport, con)

    if st.button("New mystery career", key=sport.k("new_career_target")):
        st.session_state[target_key] = _new_game_target(sport, con)
        # Without this the next player arrived already "solved", showing
        # somebody else's answer before a guess had been made.
        st.session_state.pop(sport.k("career_solved"), None)
        st.session_state[sport.k("career_wrong")] = []
        ui_widgets.clear_player_picker(sport.k("career_guess"))


    pid = st.session_state[target_key]
    rows = con.execute(f"""
        SELECT {sc.season}, {sc.club_hist}
        FROM {sc.games}
        WHERE {sc.player_id} = ?
        GROUP BY {sc.season}, {sc.club_hist}
        ORDER BY {sc.season}
    """, (pid,)).fetchall()

    st.write("Can you guess the player from their career path?")
    import pandas as pd
    st.dataframe(pd.DataFrame(rows, columns=["Season", "Club"]),
                 hide_index=True)

    name = con.execute(
        f"SELECT {sc.player} FROM {sc.players} WHERE {sc.player_id} = ?",
        (pid,)).fetchone()
    name = name[0] if name else "unknown"

    # Held in state rather than shown inline: a Streamlit button is only
    # True on the run it was clicked, so the old verdict disappeared as
    # soon as anything else on the page caused a rerun.
    wrong = st.session_state.setdefault(sport.k("career_wrong"), [])
    solved = st.session_state.get(sport.k("career_solved"))

    if solved is True:
        st.success(f"Correct — it was **{name}**.")
    elif solved is False:
        st.info(f"It was **{name}**.")
    else:
        guess = player_picker(sport.k("career_guess"), label="Who is this?")
        if guess is not None and st.button(
                "Submit guess", key=sport.k("submit_career_guess"),
                type="primary"):
            if guess[0] == pid:
                st.session_state[sport.k("career_solved")] = True
            else:
                wrong.append(guess[1])
            st.rerun()
        if wrong:
            st.caption("Already tried: " + ", ".join(wrong))
        if st.button("Reveal answer", key=sport.k("career_reveal")):
            st.session_state[sport.k("career_solved")] = False
            st.rerun()
            
def _game_higher_lower(sport, con):
    V, sc = sport.vocab, sport.schema
    pair_key, score_key, verdict_key = (
        sport.k("hl_pair"), sport.k("hl_score"), sport.k("hl_verdict"))

    if pair_key not in st.session_state:
        st.session_state[pair_key] = _new_game_pair(sport, con)
        st.session_state[score_key] = [0, 0]
        st.session_state[verdict_key] = None

    st.write(f"Which player has more career {V.games}?")
    right, played = st.session_state[score_key]
    if played:
        st.caption(f"Score: {right} of {played}.")

    p1_id, p2_id = st.session_state[pair_key]
    if p1_id is None:
        st.info("Not enough players to build a matchup.")
        return

    def info(pid):
        return con.execute(
            f"SELECT {sc.player}, {sc.career_games} FROM {sc.players} "
            f"WHERE {sc.player_id} = ?", (pid,)).fetchone()

    p1, p2 = info(p1_id), info(p2_id)
    if not p1 or not p2:
        return

    verdict = st.session_state[verdict_key]
    c1, c2 = st.columns(2)
    c1.markdown(f"### {p1[0]}")
    c2.markdown(f"### {p2[0]}")

    def answer(picked, other):
        # Recorded before the rerun that displays it, so the verdict
        # survives -- previously it vanished the moment anything else on
        # the page was touched.
        correct = (picked[1] or 0) >= (other[1] or 0)
        st.session_state[score_key] = [right + int(correct), played + 1]
        st.session_state[verdict_key] = (
            correct,
            f"{p1[0]} played {p1[1] or 0:,} {V.games}; "
            f"{p2[0]} played {p2[1] or 0:,} {V.games}.")
        st.rerun()

    if verdict is None:
        if c1.button("Higher", key=sport.k("hl_b1"), width="stretch"):
            answer(p1, p2)
        if c2.button("Higher", key=sport.k("hl_b2"), width="stretch"):
            answer(p2, p1)
    else:
        correct, detail = verdict
        (st.success if correct else st.error)(
            ("Correct. " if correct else "Not this time. ") + detail)
        if st.button("Next matchup", key=sport.k("new_hl"), type="primary"):
            st.session_state[pair_key] = _new_game_pair(sport, con)
            st.session_state[verdict_key] = None
            st.rerun()
            
def _game_stat_threshold(sport, con):
    V, sc = sport.vocab, sport.schema
    target = _threshold_target(sport, con)
    if not target:
        st.info(f"This sport has no recorded career {V.games} to set a "
                "target from.")
        return

    found_key = sport.k("st_correct")
    if found_key not in st.session_state:
        st.session_state[found_key] = []
    found = st.session_state[found_key]

    total = con.execute(
        f"SELECT COUNT(*) FROM {sc.players} WHERE {sc.career_games} >= ?",
        (target,)).fetchone()[0]
    st.write(f"Name players with at least **{target:,} {V.games}**.")
    st.caption(f"Found {len(found)} of {total:,}.")

    query = st.text_input("Enter player name:", key=sport.k("st_input"))
    if query:
        matches = ui_widgets.player_matches(
            query, sport, _db_revision(sport.db), limit=5)
        if not matches:
            st.caption("No player of that name.")
        # (pid, name, label) -- unpacking a fourth value raised ValueError
        # here, so this mode crashed on the first character typed.
        for pid, name, label in matches:
            if not st.button(label, key=sport.k(f"st_btn_{pid}")):
                continue
            row = con.execute(
                f"SELECT {sc.career_games} FROM {sc.players} "
                f"WHERE {sc.player_id} = ?", (pid,)).fetchone()
            played = int(row[0] or 0) if row else 0
            if played < target:
                st.error(f"Not quite. {name} played {played:,} {V.games}.")
            elif name in found:
                st.warning(f"{name} is already on your list.")
            else:
                found.append(name)
                st.success(f"Correct — {name} played {played:,} {V.games}.")

    if found:
        st.write("### Found so far")
        for name in found:
            st.write(f"✅ {name}")
        if st.button("Start again", key=sport.k("st_reset")):
            st.session_state[found_key] = []
            st.rerun()
