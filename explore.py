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

import pandas as pd
import streamlit as st

import core

SCOPES = {
    "Single game": "game",
    "Season total": "season",
    "Career total": "career",
}


def _db_revision(db):
    """Return a cheap cache key that changes when the database changes."""
    stat = os.stat(db)
    return stat.st_mtime_ns, stat.st_size


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
        by_year.setdefault(year, []).append(stat.replace("_", " "))
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

    st.markdown(
        "<div class='hero'>"
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

    with st.expander("Coverage notes"):
        note = _era_note(sport)
        st.write(f"{V.score.capitalize()} and {V.club} history cover the "
                 f"database back to {s['season_min']}.")
        if note:
            st.write(note)
        # This sport's model, not core's: core's names goals, finals and
        # Brownlow votes, and the NBA model uses none of them.
        st.write(sport.star_disclaimer)


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
    st.caption("Search the full player database, then inspect the selected "
               "player's career and best performances — or put two careers "
               "side by side.")
    one, two = st.tabs(["One player", "Compare two"])
    with one:
        _player_profile(sport, con, player_picker)
    with two:
        _compare_players(sport, con, player_picker)


def _player_profile(sport, con, player_picker):
    selected = player_picker(sport.k("explore_player"),
                             label="Search by name")
    if selected is None:
        return
    pid, _ = selected
    render_player_profile(sport, con, pid, key_prefix="explore")


def _title_label(title: str) -> str:
    """'premiership' -> 'Premierships', 'World Series' -> 'World Series'.

    Vocab.title is singular, and the tile counts them. Anything already
    ending in 's' is left alone, which is what keeps 'World Series' from
    becoming 'World Seriess'.
    """
    label = title.strip()
    if not label:
        return "Titles"
    label = label[0].upper() + label[1:]
    return label if label.endswith("s") else label + "s"


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
    if not round_value:
        return None
    sc = sport.schema
    try:
        row = _fetchone(
            f"SELECT COUNT(DISTINCT {sc.season}) FROM {sc.games} "
            f"WHERE {sc.player_id} = ? AND UPPER(TRIM({sc.round})) = ? "
            f"AND {sc.result} = 'W'",
            (pid, round_value.upper()), revision, con)
    except sqlite3.Error:
        return None
    return row[0] if row else 0


def render_player_profile(sport, con, pid, key_prefix="explore",
                          heading_level="##"):
    """One player's career, rendered wherever it is asked for.

    Split out of the Player Search page so the Grid Solver can show the
    same profile in a dialog without navigating away from the board, and
    without a second copy of these queries drifting from this one.

    `key_prefix` namespaces the widgets inside: two callers on one page
    each need their own "ranked by" selectbox. `heading_level` lets a
    dialog, which already has a title bar, drop to a smaller heading.
    """
    V, sc = sport.vocab, sport.schema
    revision = _db_revision(sport.db)
    profile_sql = (
        f"SELECT {sc.player}, {sc.debut_season}, {sc.final_season}, "
        f"{sc.career_games}, {sc.career_score}, {sc.career_postseason}, "
        f"{sc.clubs_hist}, {sc.birth_year}, {sc.obscurity} "
        f"FROM {sc.players} WHERE {sc.player_id} = ?"
    )
    p = _fetchone(profile_sql, (pid,), revision, con)
    if not p:
        return

    st.markdown(f"{heading_level} {p[0]}")
    st.caption(f"{p[1]}–{p[2]} · {p[6].replace('|', ', ')}")

    # Show trusted captain appointments on the player profile. Gated on the
    # constraints module declaring the layer rather than on the sport's name,
    # so a sport that later gains captaincy data gets this for free.
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
                st.caption("Club captain: " + " · ".join(appointments))

    tiles = [
        (V.games.capitalize(), f"{p[3]:,}"),
        (V.score.capitalize(), f"{int(p[4] or 0):,}"),
        (V.postseason.capitalize(), p[5]),
    ]
    # Premierships / World Series / Super Bowls, for the sports whose data
    # can actually answer it. Inserted before Born so the career numbers
    # stay together.
    titles = _titles_won(sport, con, pid, revision)
    if titles is not None:
        tiles.append((_title_label(V.title), titles))
    tiles.append(("Born", int(p[7]) if p[7] else "—"))

    cols = st.columns(len(tiles) + 1)
    for col, (lab, val) in zip(cols, tiles):
        col.markdown(f"<div class='count'>{val}</div>"
                     f"<div class='count-label'>{lab}</div>",
                     unsafe_allow_html=True)
    cols[-1].markdown(f"<div>{core.stars_html(p[8])}</div>"
                      f"<div class='count-label'>Obscurity</div>",
                      unsafe_allow_html=True)

    st.markdown("### Season by season")
    seasons_sql = f"""
        SELECT {sc.season} AS Season, {sc.club_hist} AS "{V.club.capitalize()}",
               COUNT(*) AS "{V.games.capitalize()}",
               SUM({sc.game_score}) AS "{V.score.capitalize()}",
               ROUND(AVG({sc.game_score}),2) AS "{V.score}/{V.game}",
               SUM(CASE WHEN {sc.result}='W' THEN 1 ELSE 0 END) AS W,
               SUM(CASE WHEN {sc.result}='L' THEN 1 ELSE 0 END) AS L,
               SUM({sc.is_final}) AS "{V.postseason.capitalize()}"
        FROM {sc.games} WHERE {sc.player_id} = ?
        GROUP BY {sc.season}, {sc.club_hist} ORDER BY {sc.season}"""
    seasons = _read_frame(seasons_sql, (pid,), revision, con)
    st.dataframe(seasons, hide_index=True, width="stretch")

    st.markdown(f"### Biggest {V.games}")
    metric = st.selectbox("Ranked by", list(sc.stats),
                          key=sport.k(key_prefix, "best", pid))
    warning = sport.stat_era_warning(metric)
    if warning:
        st.caption(f"⚠ {warning}")
    best_sql = f"""
        SELECT {sc.season} AS Season, {sc.round} AS Rnd,
               {sc.club_hist} AS For, {sc.opponent} AS Opponent,
               {sc.venue} AS "{V.venue.capitalize()}", {sc.result} AS Res,
               {metric} AS "{metric}"
        FROM {sc.games}
        WHERE {sc.player_id} = ? AND {metric} IS NOT NULL
        ORDER BY {metric} DESC, {sc.season} LIMIT 20"""
    best = _read_frame(best_sql, (pid,), revision, con)
    st.dataframe(best, hide_index=True, width="stretch")


# ---------------------------------------------------- player comparison

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
        col.caption(f"{p.span} · {p.clubs}")
        m1, m2, m3 = col.columns(3)
        m1.metric(V.games.capitalize(), f"{p.career_games:,}")
        m2.metric(V.score.capitalize(), f"{p.career_score:,}")
        m3.metric(V.postseason.capitalize(), f"{p.finals:,}")

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
                "Statistic": stat.replace("_", " "),
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


# ------------------------------------------------------ stats explorer

def leaderboard_page(sport, con):
    V, sc = sport.vocab, sport.schema
    st.markdown("# Stats Explorer")
    st.caption(f"Search single-{V.game} performances, season totals and "
               "career records using the database's stat and context "
               "filters.")

    c1, c2, c3 = st.columns([1.2, 1.2, 1])
    stat = c1.selectbox("Statistic", list(sc.stats), key=sport.k("lb_stat"))
    scope = c2.selectbox("Scope", list(SCOPES), key=sport.k("lb_scope"))
    limit = c3.number_input("Rows", 5, 200, 30, step=5,
                            key=sport.k("lb_limit"))

    warning = sport.stat_era_warning(stat)
    if warning:
        st.caption(f"⚠ {warning}")

    where, params = _filters(sport, con, sport.k("lb"))
    mode = SCOPES[scope]

    if mode == "game":
        q = f"""SELECT g.{sc.player} AS Player, g.{sc.season} AS Season,
                       g.{sc.round} AS Rnd, g.{sc.club_hist} AS For,
                       g.{sc.opponent} AS Opponent,
                       g.{sc.venue} AS "{V.venue.capitalize()}",
                       g.{stat} AS "{stat}",
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
                       SUM(g.{stat}) AS "{stat}",
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
                       SUM(g.{stat}) AS "{stat}",
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
                    f"{saved_stat.replace('_', ' ')}")
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

    V, sc = sport.vocab, sport.schema
    st.markdown("# Game Lab")
    st.caption("A workspace for turning the database into playable "
               "challenges. This first prototype is a clue-based player "
               "game.")

    st.markdown(f"### Prototype: Guess the Player ({sport.label})")
    st.caption("The full clue ladder and the Gridley criterion bank are "
               "AFL-only for now — this is the sport-agnostic version.")
    target_key = sport.k("game_target")
    if target_key not in st.session_state:
        st.session_state[target_key] = _new_game_target(sport, con)
        st.session_state.game_result = None

    if st.button("New mystery player", key=sport.k("new_game_player")):
        st.session_state[target_key] = _new_game_target(sport, con)
        st.session_state.game_result = None
        for key in ("game_guess_query", "game_guess_choice"):
            st.session_state.pop(sport.k(key), None)

    pid = st.session_state[target_key]
    target = con.execute(f"""
        SELECT {sc.player_id}, {sc.player}, {sc.debut_season},
               {sc.final_season}, {sc.career_games}, {sc.career_score},
               {sc.career_postseason}, {sc.clubs_hist}, {sc.n_clubs}
        FROM {sc.players} WHERE {sc.player_id} = ?""", (pid,)).fetchone()
    if not target:
        st.error("No eligible game target was found.")
        return

    clues = st.slider("Clues revealed", 1, 3, 1, key=sport.k("game_clues"))
    plural = "s" if target[8] != 1 else ""
    st.write(f"**Clue 1:** Career span {target[2]}–{target[3]}; "
             f"played for {target[8]} {V.club}{plural}.")
    if clues >= 2:
        st.write(f"**Clue 2:** {target[4]:,} {V.games}, "
                 f"{int(target[5] or 0):,} {V.score} and {target[6]} "
                 f"{V.postseason}.")
    if clues >= 3:
        st.write(f"**Clue 3:** {V.clubs.capitalize()} — "
                 f"{target[7].replace('|', ', ')}.")

    selected = player_picker(sport.k("game_guess"), label="Your guess")
    if selected is not None and st.button("Submit guess",
                                          key=sport.k("submit_guess")):
        guess_pid, guess_name = selected
        if guess_pid == target[0]:
            st.session_state.game_result = (
                "correct", f"Correct — it was {target[1]}.")
        else:
            st.session_state.game_result = (
                "wrong", f"{guess_name} is not the mystery player.")

    result = st.session_state.get("game_result")
    if result:
        if result[0] == "correct":
            st.success(result[1])
        else:
            st.warning(result[1])

    if st.button("Reveal answer", key=sport.k("reveal_game")):
        st.info(f"The mystery player is **{target[1]}**.")

    with st.expander("Possible next game modes"):
        st.write(
            "Higher or Lower · Guess the career path · Name the teammate · "
            "Stat threshold challenge · Daily mystery player · Draft and "
            "award trivia")