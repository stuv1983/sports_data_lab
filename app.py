"""
app.py -- Grid solver and data lab.

    pip install streamlit pandas
    streamlit run app.py

Pick a sport, define the six axes of today's grid in the sidebar, and the
board draws itself already solved: every square shows its best
database-ranked answer, a 0-5 star obscurity rating and the number of
eligible players. Click a square to see the full ranked list.

Nothing in this file branches on which sport is loaded. Everything that
differs -- database path, column names, vocabulary, era caveats -- comes
from the Sport object in sports.py, and the query engine underneath is
core.py.
"""

import os
import re
import sqlite3

import pandas as pd
import streamlit as st

import core
import db_pool
import sports
import theme

# The page title needs the sport before any other Streamlit call, so read
# the previous choice straight out of session state.
_key = st.session_state.get("sport", sports.DEFAULT)
_pre = sports.get(_key)
st.set_page_config(page_title=f"Sports Data Lab — {_pre.label.replace(' Data Lab', '')}", page_icon=_pre.icon, layout="wide",
                   initial_sidebar_state="expanded")

# ------------------------------------------------------- sport and theme
SPORT = sports.picker(st)
C = SPORT.C
SCHEMA = SPORT.schema
V = SPORT.vocab

def db_revision(db):
    """Cheap cache key that changes whenever the SQLite file is replaced."""
    stat = os.stat(db)
    return stat.st_mtime_ns, stat.st_size


# Deliberately NOT @st.cache_resource -- a single sqlite3.Connection shared
# across Streamlit's script-runner threads corrupts its own statement cache,
# which showed up as InterfaceError at startup and, worse, as squares
# silently reporting no answers. db_pool.py has the full account.
get_con = db_pool.get_con


try:
    DB_REVISION = db_revision(SPORT.db)
    con = get_con(SPORT.db, DB_REVISION)
    con.execute(f"SELECT 1 FROM {SCHEMA.players} LIMIT 1")
except (OSError, sqlite3.OperationalError):
    st.error(SPORT.missing_db_hint)
    st.stop()

try:
    C.require_schema(con)
except RuntimeError as e:
    st.error(str(e))
    st.stop()


# -------------------------------------------------------------- lookups
# Every cached function takes sport_key as a hashed argument. `con` is
# passed with a leading underscore elsewhere in the codebase, which
# Streamlit does not hash, so without this a sport switch would serve the
# previous sport's players out of cache.

@st.cache_data(show_spinner=False)
def venue_options(sport_key, db, revision):
    """Real ground names, most-used first, with the alias the grid uses."""
    s = sports.get(sport_key).schema
    rows = get_con(db, revision).execute(
        f"SELECT {s.venue}, COUNT(*) c, MIN({s.season}), MAX({s.season}) "
        f"FROM {s.games} GROUP BY {s.venue} ORDER BY c DESC").fetchall()
    # Stored name -> the name people actually use. Declared per sport in
    # sports.py rather than here, so this function does not have to know
    # that Docklands is Marvel Stadium and Staples Center is now Crypto.com.
    alias = sports.get(sport_key).venue_display
    out = []
    for v, c, lo, hi in rows:
        extra = f"  ({alias[v]})" if v in alias else ""
        out.append((v, f"{v}{extra}  ·  {lo}-{hi}  ·  {c:,} {V.games}"))
    return out


PLAYER_MATCH_LIMIT = 30


def _player_search_key(value):
    """Search-friendly name key: punctuation and spacing are interchangeable."""
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


@st.cache_data(show_spinner=False)
def player_options(sport_key, db, revision):
    """Every player, with an unambiguous label and a search-only name key."""
    s = sports.get(sport_key).schema
    rows = get_con(db, revision).execute(
        f"SELECT {s.player_id}, {s.player}, {s.debut_season}, "
        f"{s.final_season}, {s.career_games}, {s.clubs_hist} "
        f"FROM {s.players} ORDER BY {s.player}").fetchall()
    return [(pid, nm,
             f"{nm} ({d}-{f}, {g}g, {cl.replace('|', '/')})",
             _player_search_key(nm))
            for pid, nm, d, f, g, cl in rows]


def player_matches(query, limit=PLAYER_MATCH_LIMIT):
    """Return a small, relevance-ranked list instead of every player."""
    q = _player_search_key(query)
    if not q:
        return []

    q_words = q.split()
    ranked = []
    for pid, name, label, search_key in player_options(
            SPORT.key, SPORT.db, DB_REVISION):
        words = search_key.split()
        if search_key == q:
            rank = 0
        elif search_key.startswith(q):
            rank = 1
        elif any(word.startswith(q) for word in words):
            rank = 2
        elif all(any(token in word for word in words) for token in q_words):
            rank = 3
        elif q in search_key:
            rank = 4
        else:
            continue
        ranked.append((rank, len(name), name.casefold(), pid, name, label))

    ranked.sort()
    return [(pid, name, label)
            for _, _, _, pid, name, label in ranked[:limit]]


def player_picker(key, label="Player name", default_name=""):
    """Text-first player picker with at most 30 matching choices."""
    query_key = f"{key}_query"
    choice_key = f"{key}_choice"
    if query_key not in st.session_state:
        st.session_state[query_key] = default_name

    query = st.text_input(
        label, key=query_key, placeholder="Start typing a player name…")
    if not query.strip():
        st.caption("Type part of a first name or surname.")
        return None

    matches = player_matches(query)
    if not matches:
        st.warning(f"No player matches ‘{query.strip()}’.")
        return None

    options = [pid for pid, _, _ in matches]
    details = {pid: (name, player_label)
               for pid, name, player_label in matches}

    # A changed search can invalidate the previous selection. Clear only the
    # result widget, never the text the user is typing.
    if (choice_key in st.session_state
            and st.session_state[choice_key] not in options):
        del st.session_state[choice_key]

    pid = st.selectbox(
        "Matching players", options, index=0, key=choice_key,
        format_func=lambda player_id: details[player_id][1],
        label_visibility="collapsed")

    if len(matches) == PLAYER_MATCH_LIMIT:
        st.caption(f"Showing the first {PLAYER_MATCH_LIMIT} matches — "
                   "type more letters to narrow the list.")
    return pid, details[pid][0]


DRAFT_OK = C.draft_available(con)
AWARDS_OK = C.awards_available(con)
CAPTAIN_OK = getattr(C, "captain_available", lambda _con: False)(con)
CAPTAIN_BUILDERS = getattr(C, "CAPTAIN_BUILDER_NAMES", set())
RISING_STAR_OK = getattr(C, "rising_star_available", lambda _con: False)(con)
RISING_STAR_BUILDERS = getattr(C, "RISING_STAR_BUILDER_NAMES", set())
# The table list is a property of the sport's Club Explorer, not of this
# page. An empty set is a subset of anything, so a sport without one is
# trivially "OK" -- which is fine, because the flag is only ever read
# inside a SPORT.has_club_explorer guard.
CLUB_DATA_OK = SPORT.club_data_tables <= {
    row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
}
FAMILY_RELATIONSHIPS_OK = getattr(
    C, "family_relationships_available", lambda _con: False
)(con)
FAMILY_RELATIONSHIP_BUILDERS = getattr(
    C, "FAMILY_RELATIONSHIP_BUILDER_NAMES", set()
)
AVAILABLE = [k for k in C.BUILDERS
             if (DRAFT_OK or k not in C.DRAFT_BUILDERS)
             and (AWARDS_OK or k not in C.AWARD_BUILDER_NAMES)
             and (CAPTAIN_OK or k not in CAPTAIN_BUILDERS)
             and (RISING_STAR_OK or k not in RISING_STAR_BUILDERS)
             and (FAMILY_RELATIONSHIPS_OK
                  or k not in FAMILY_RELATIONSHIP_BUILDERS)]

st.sidebar.markdown(
    f"<div class='brand'>{SPORT.label}</div>"
    "<div class='brand-sub'>SEARCH · EXPLORE · PLAY</div>",
    unsafe_allow_html=True)

# ------------------------------------------------------- database status
# Rendered from the live database rather than hardcoded, so the panel is
# correct for whichever sport is loaded and after any mid-season refresh.
with st.sidebar.expander("Database status", expanded=False):
    lines = "<br>".join(f"{label}: <b>{value}</b>"
                        for label, value in SPORT.status(con))
    st.markdown(f"<div class='status-row'>{lines}</div>",
                unsafe_allow_html=True)
    # One loop over whatever this sport declares, instead of five branches
    # naming AFL scripts. A layer with no loader yet simply has no hint and
    # reports "not loaded" in the rows above, which is the honest answer.
    for _label, hint in SPORT.missing_layer_hints(con):
        st.caption(hint)
    if SPORT.has_club_explorer and not CLUB_DATA_OK and SPORT.club_data_hint:
        st.caption(SPORT.club_data_hint)
    if (SPORT.has_club_explorer and not FAMILY_RELATIONSHIPS_OK
            and SPORT.family_hint):
        st.caption(SPORT.family_hint)


# ---------------------------------------------------------- appearance
PALETTE = theme.controls(st, SPORT.key)
st.markdown(theme.css(PALETTE), unsafe_allow_html=True)


# ------------------------------------------------------- axis definition
def _stat_label(stat):
    """'frees_against' -> 'frees against', for a board header."""
    return str(stat).replace("_", " ")


#: Builder name -> how its square reads on the board. The generic builders
#: are named for what they do ("X+ of a stat in one season"), which is right
#: in a picker and useless as an axis label, so each one restates itself
#: using the arguments actually chosen.
_STAT_SCOPE_LABELS = {
    "X+ of a stat in one game":
        lambda a, V: f"{a[1]}+ {_stat_label(a[0])}\nin a {V.game}",
    "X+ of a stat in one season":
        lambda a, V: f"{a[1]}+ {_stat_label(a[0])}\nin a {V.season}",
    "X+ of a stat in a career":
        lambda a, V: f"{a[1]}+ {_stat_label(a[0])}\ncareer",
    "Season average of a stat":
        lambda a, V: f"{a[1]}+ {_stat_label(a[0])} avg\nin a {V.season}",
    "Career average of a stat":
        lambda a, V: f"{a[1]}+ {_stat_label(a[0])} avg\nper {V.game}",
    "X+ of a stat in a final":
        lambda a, V: f"{a[1]}+ {_stat_label(a[0])}\nin a {V.postseason_one}",
    "Finals average of a stat":
        lambda a, V: f"{a[1]}+ {_stat_label(a[0])} avg\nin {V.postseason}",
}


def axis_widget(key, default_type, defaults=None):
    """One axis of the grid. Returns (label, constraint) or (label, None)."""
    defaults = defaults or {}
    kinds = AVAILABLE
    default_index = kinds.index(default_type) if default_type in kinds else 0
    kind = st.selectbox("Type", kinds, index=default_index,
                        key=f"{key}_kind", label_visibility="collapsed")
    fn, argnames = C.BUILDERS[kind]
    args = []

    for a in argnames:
        wk = f"{key}_{a}"
        if a == "club":
            clubs = list(SCHEMA.clubs)
            want = defaults.get("club", clubs[0])
            idx = clubs.index(want) if want in clubs else 0
            args.append(st.selectbox(V.club.capitalize(), clubs,
                                     key=wk, index=idx))
        elif a == "venue":
            vs = venue_options(SPORT.key, SPORT.db, DB_REVISION)
            names = [v for v, _ in vs]
            want = defaults.get("venue", names[0] if names else "")
            idx = names.index(want) if want in names else 0
            pick = st.selectbox(V.venue.capitalize(), range(len(vs)),
                                index=idx, format_func=lambda i: vs[i][1],
                                key=wk)
            args.append(vs[pick][0])
        elif a == "player_id":
            selected = player_picker(
                wk, default_name=str(defaults.get("player", "")))
            if selected is None:
                args.append(None)
                st.session_state[f"{wk}_label"] = "—"
            else:
                pid, player_name = selected
                args.append(pid)
                st.session_state[f"{wk}_label"] = player_name
        elif a == "kind":
            # The categories belong to the sport's draft layer, not to this
            # widget. A sport with no draft layer defines no builder taking
            # `kind`, so the empty default is never reached in practice.
            draft_kinds = list(getattr(C, "DRAFT_TYPES", ()))
            args.append(st.selectbox("Draft type", draft_kinds, key=wk))
        elif a == "source":
            args.append(st.text_input("Recruited from",
                                      defaults.get("source", ""), key=wk))
        elif a == "award":
            names = list(C.AWARD_SLUGS)
            want = defaults.get("award")
            idx = names.index(want) if want in names else 0
            pick = st.selectbox("Award", names, index=idx, key=wk)
            args.append(C.AWARD_SLUGS[pick])
        elif a == "times":
            label = ("How many such games" if kind == "X+ games with Y+ of a stat"
                     else "Times")
            args.append(st.number_input(label, min_value=1,
                                        value=int(defaults.get("times", 1)),
                                        step=1, key=wk))
        elif a == "avg":
            # The same argument name means several different things
            # depending on the builder: a per-game average across finals,
            # across a season, or across a whole career. Naming it after
            # the finals builder mislabelled the season one as "goals per
            # final".
            caption = {
                "Season average of a stat":
                    f"per-{V.game} average across a {V.season}",
                "Career average of a stat":
                    f"per-{V.game} average across a career",
                "Finals average of a stat":
                    f"per-{V.game} average across {V.postseason}",
            }.get(kind, f"{V.score} per {V.postseason_one}")
            args.append(st.number_input(caption, value=1.0, step=0.5,
                                        key=wk))
        elif a == "min_games":
            args.append(st.number_input(
                f"Minimum {V.games}",
                min_value=1, step=1,
                value=int(defaults.get("min_games", C.CAREER_AVG_MIN_GAMES)),
                key=wk,
                help=f"An average over a handful of {V.games} is not a "
                     f"career average in any sense the question means."))
        elif a == "player":
            args.append(st.text_input("Player", defaults.get("player", ""),
                                      key=wk))
        elif a in ("stat", "stat_a", "stat_b"):
            stats = list(SCHEMA.stats)
            want = defaults.get(a, stats[0])
            idx = stats.index(want) if want in stats else 0
            chosen = st.selectbox(a.replace("_", " "), stats, key=wk,
                                  index=idx)
            # Era honesty: say so before the square answers, not after.
            warning = SPORT.stat_era_warning(chosen)
            if warning:
                st.caption(f"⚠ {warning}")
            args.append(chosen)
        else:
            year_kinds = {
                "Played between seasons", "Debuted between seasons",
                "Drafted between years", "All-Australian between years",
                "Rising Star nominee in season",
                "Rising Star nominee between seasons",
                "Rising Star nominee for club between seasons",
            }
            if kind in year_kinds and a in ("from", "to", "season"):
                lo, hi = season_span(SPORT.key, SPORT.db, DB_REVISION)
                fallback = max(lo, hi - 46) if a == "from" else hi
                args.append(st.number_input(
                    a.replace("_", " "), min_value=lo, max_value=hi,
                    value=int(defaults.get(a, fallback)), step=1, key=wk))
            else:
                fallback = {
                    "from": 1, "to": 10, "clubs": 2,
                    "x": 5, "y": 5, "x_a": 30, "x_b": 3,
                }.get(a, 30)
                args.append(st.number_input(
                    a.replace("_", " ").upper(),
                    value=int(defaults.get(a, fallback)),
                    step=1, key=wk,
                    help="This is the X in the builder name above. It keeps "
                         "its last value when you change builder, so read it "
                         "against the criterion you are answering."))

    label = kind
    if kind == "Played for club":
        label = args[0]
    elif kind == "Teammate of…":
        label = f"{st.session_state.get(f'{key}_player_id_label', '—')} teammate"
    elif kind in ("Played at venue", "Won a final at venue"):
        verb = ("played at" if kind.startswith("Played")
                else f"won a {V.postseason_one} at")
        label = f"{verb}\n{args[0]}"
    elif kind == "X+ finals games":
        label = f"{args[0]}+ {V.postseason} {V.games}"
    elif kind == "Goal average in finals":
        label = f"{args[0]}+ {V.score} avg\nin {V.postseason}"
    elif kind == "First career game for club":
        label = f"{args[0]}\nfirst career {V.game}"
    elif kind == "150+ / X+ career games":
        label = f"{args[0]}+ {V.games} played"
    elif kind == "X+ goals at 2+ clubs":
        label = f"{args[0]}+ {V.score}\ntwo diff {V.clubs}"
    elif kind == "Fewer than X career games":
        label = f"under {args[0]} {V.games}"
    elif kind == "Two stats in the same game":
        label = f"{args[1]}+ {args[0]} &\n{args[3]}+ {args[2]}"
    elif kind == "X+ games with Y+ of a stat":
        label = f"{args[2]}+ {V.games} with\n{args[1]}+ {_stat_label(args[0])}"
    elif kind in _STAT_SCOPE_LABELS:
        # Without these the board header read "N+ OF A STAT IN ONE SEASON"
        # instead of naming the square, which is the one thing the header
        # exists to do.
        label = _STAT_SCOPE_LABELS[kind](args, V)

    if kind == "Teammate of…" and (not args or args[0] is None):
        return label, None

    try:
        return label, fn(*args)
    except Exception as e:
        st.warning(str(e))
        return label, None


@st.cache_data(show_spinner=False)
def season_span(sport_key, db, revision):
    s = sports.get(sport_key).schema
    return get_con(db, revision).execute(
        f"SELECT MIN({s.season}), MAX({s.season}) FROM {s.games}").fetchone()


NAV_ITEMS = [
    "Home",
    "Player Search",
    "Club Explorer",
    "Past Games",
    "Awards",
    "Advanced Search",
    "Stats Explorer",
    "Random Discovery",
    "Grid Solver",
    "Game Lab",
    "Database Health",
]
requested_page = st.query_params.get("page")
if "page" not in st.session_state and requested_page == "search":
    st.session_state["page"] = "Advanced Search"
PAGE = st.sidebar.radio("Navigate", NAV_ITEMS, key="page")

if PAGE != "Grid Solver":
    import explore
    if PAGE == "Home":
        explore.home_page(SPORT, con, DRAFT_OK, AWARDS_OK)
    elif PAGE == "Player Search":
        explore.player_page(SPORT, con, player_picker)
    elif PAGE == "Club Explorer":
        from afl import club_explorer
        club_explorer.club_explorer_page(SPORT, con)
    elif PAGE == "Past Games":
        from afl import past_games
        past_games.past_games_page(SPORT, con)
    elif PAGE == "Awards":
        from afl import awards_page
        awards_page.awards_page(SPORT, con)
    elif PAGE == "Advanced Search":
        import advanced_search
        advanced_search.search_page(SPORT, con)
    elif PAGE == "Stats Explorer":
        explore.leaderboard_page(SPORT, con)
    elif PAGE == "Random Discovery":
        explore.random_page(SPORT, con)
    elif PAGE == "Game Lab":
        explore.game_lab_page(SPORT, con, player_picker)
    elif PAGE == "Database Health":
        import health
        health.health_page(SPORT, con)
    st.stop()

st.sidebar.markdown("---")
st.sidebar.markdown("### Grid setup")

# Axis keys are namespaced by sport. Streamlit's session state is flat, so
# without this an axis left on "Played for club / St Kilda" survives a
# switch to the NBA and throws on the club lookup.
st.sidebar.markdown("### Columns")
cols_def = []
# Declared per sport: an NBA board opening on "St Kilda" would be silently
# coerced to clubs[0] by axis_widget and answer a question nobody asked.
col_defaults, row_defaults = SPORT.grid_defaults
for i, (dt, dv) in enumerate(col_defaults):
    with st.sidebar.expander(f"Column {i+1}", expanded=False):
        cols_def.append(axis_widget(SPORT.k("c", i), dt, dv))

st.sidebar.markdown("### Rows")
rows_def = []
for i, (dt, dv) in enumerate(row_defaults):
    with st.sidebar.expander(f"Row {i+1}", expanded=False):
        rows_def.append(axis_widget(SPORT.k("r", i), dt, dv))

st.sidebar.markdown("---")

# ---------------------------------------------------------- grid sources
# Three ways in besides building the axes by hand. All of them run every
# criterion through the parser BEFORE the board is drawn, so a grid that
# cannot be answered says why instead of quietly answering something else.

@st.cache_data(show_spinner=False)
def grid_library(sport_key, db, revision):
    """Every captured grid, analysed once at startup."""
    from afl import historic_grids as HG

    sport = sports.get(sport_key)
    return HG.analyse_all(get_con(db, revision), sport)


def _axes_from(reports):
    """GridReport criteria -> the (label, constraint) pairs the board wants."""
    return [(r.display, r.constraint) for r in reports]


# historic_grids imports the AFL constraints module at module scope, so the
# import stays inside grid_library() -- selecting the NBA should not pull in
# the AFL constraint module and analyse a library that is not about it.
LIBRARY = grid_library(
    SPORT.key, SPORT.db, DB_REVISION) if SPORT.grid_library else []
LIB_BY_NUMBER = {r.grid.number: r for r in LIBRARY}


def show_report(picked, mode):
    from afl import historic_grids as HG

    """Sidebar verdict for an analysed grid, and load it onto the board.

    Shared by every source that produces a GridReport -- the captured
    library, the random picker and the pasted criteria -- so all of them
    apply the same Authentic/Practice rules and print the same criterion
    report.
    """
    g = picked.grid
    st.sidebar.markdown(f"**{g.key}** · source `{g.source}`")
    st.sidebar.caption(picked.line())

    if not g.complete:
        st.sidebar.warning(
            f"Partial capture — {len(g.partial_criteria)} of six "
            "criteria were recorded, so this board cannot be drawn. "
            "The criteria themselves still parse; see the report below.")
        st.session_state.pop("loaded", None)
    elif picked.unsupported and mode == "Authentic":
        names = ", ".join(f"“{c.text}”" for c in picked.unsupported)
        st.sidebar.error(
            f"Not playable in Authentic mode: {names} "
            f"({'; '.join(c.reason for c in picked.unsupported)}). "
            "Switch to Practice to play a substituted board.")
        st.session_state.pop("loaded", None)
    else:
        if picked.unsupported:      # Practice mode
            cache = st.session_state.setdefault(SPORT.k("practice"), {})
            prows, pcols, swaps = HG.practice_board(con, picked, cache)
            for original, replacement in swaps:
                st.sidebar.warning(
                    f"Original: {original} — replaced for Practice Mode "
                    f"by “{replacement}”.")
        else:
            prows, pcols = picked.rows, picked.cols
        st.session_state.loaded = {"rows": _axes_from(prows),
                                   "cols": _axes_from(pcols)}

    with st.sidebar.expander("Criterion report", expanded=False):
        for c in picked.all_criteria:
            if c.supported:
                n = f"{c.eligible:,} eligible" if c.eligible is not None \
                    else "count unavailable"
                st.markdown(f"✅ **{c.axis}** — {c.text} → {c.label} "
                            f"({n})")
            else:
                st.markdown(f"⛔ **{c.axis}** — {c.text}: {c.reason}")
            for w in c.warnings:
                st.caption(f"⚠ {w}")
        if picked.square_errors:
            for e in picked.square_errors:
                st.error(e)
        elif picked.squares_ok:
            st.caption("All nine intersections execute.")


st.sidebar.markdown("### Grid source")
SOURCES = ["Build my own", "Paste criteria", "Today's grid", "Past grid",
           "Random supported grid"]
source = st.sidebar.radio("Source", SOURCES, key=SPORT.k("gridsource"),
                          label_visibility="collapsed")

if source != "Build my own":
    mode = st.sidebar.radio(
        "Mode", ["Authentic", "Practice"], horizontal=True,
        key=SPORT.k("gridmode"),
        help="Authentic plays only grids whose six criteria are all "
             "supported, with no substitutions. Practice replaces an "
             "unsupported criterion with a comparable one and labels the "
             "swap on the axis.")
else:
    mode = "Authentic"

if source == "Today's grid":
    with st.sidebar.expander("Fetch today's grid", expanded=True):
        st.caption(f"Pulls the day's criteria from {V.grid_source}. If the "
                   "site's data shape has changed this reports the miss "
                   "rather than guessing — fall back to Build my own.")
        d = st.text_input("Date (YYYY-MM-DD, or blank for today)",
                          key=SPORT.k("griddate"))
        cA, cB = st.columns(2)
        if cA.button("Load", key=SPORT.k("gridload")):
            import datetime
            date = d.strip() or datetime.date.today().isoformat()
            try:
                from afl import fetch_grid as FG
                from afl import parse_criteria as PC
                grid, attempts = FG.fetch(date)
                if not grid:
                    st.error("Grid data not found. Run "
                             f"`python -m afl.fetch_grid {date} --discover`.")
                    st.json({w: h for w, h in attempts})
                else:
                    r = [FG.to_label(x) for x in grid["rows"]]
                    c = [FG.to_label(x) for x in grid["cols"]]
                    pr, pc, probs = PC.parse_grid(r, c)
                    st.session_state.loaded = {"rows": pr, "cols": pc}
                    for p in probs:
                        st.warning(p)
                    st.success(f"Loaded {date}")
            except Exception as e:
                st.error(f"{type(e).__name__}: {e}")
        if cB.button("Clear", key=SPORT.k("gridclear")):
            st.session_state.pop("loaded", None)

elif source in ("Past grid", "Random supported grid") and LIBRARY:
    from afl import historic_grids as HG

    ready = HG.supported_grids(LIBRARY)

    if source == "Random supported grid":
        pool = ready if mode == "Authentic" else [r for r in LIBRARY
                                                  if r.grid.complete]
        if st.sidebar.button("Shuffle", key=SPORT.k("gridshuffle")) \
                or SPORT.k("randgrid") not in st.session_state:
            import random
            st.session_state[SPORT.k("randgrid")] = (
                random.choice(pool).grid.number if pool else None)
        picked = LIB_BY_NUMBER.get(st.session_state.get(SPORT.k("randgrid")))
        if picked is None:
            st.sidebar.warning("No grid in the library is fully supported.")
    else:
        by = st.sidebar.radio("Select by", ["Gridley number", "Date"],
                              horizontal=True, key=SPORT.k("gridby"))
        # Every grid stays selectable, including the ones that cannot be
        # played. Hiding them would hide the reason they cannot be played,
        # which is the more useful half of the information.
        if by == "Gridley number":
            numbers = [r.grid.number for r in LIBRARY]
            n = st.sidebar.selectbox(
                "Grid", numbers, key=SPORT.k("gridnum"),
                format_func=lambda x: LIB_BY_NUMBER[x].line())
        else:
            dates = {r.grid.date: r.grid.number for r in LIBRARY}
            dsel = st.sidebar.selectbox(
                "Date", list(dates), key=SPORT.k("griddatesel"),
                format_func=lambda x: f"{x} — {LIB_BY_NUMBER[dates[x]].line()}")
            n = dates[dsel]
        picked = LIB_BY_NUMBER[n]

    if picked is not None:
        show_report(picked, mode)

elif source in ("Past grid", "Random supported grid"):
    st.sidebar.info(f"No captured grid library exists for {SPORT.label} yet.")

elif source == "Paste criteria" and not SPORT.criterion_parser:
    # afl/parse_criteria.py compiles against the AFL constraint set, so offering
    # it for another sport would answer AFL questions from an NBA database.
    st.sidebar.info(f"Criterion parsing is not available for {SPORT.label}.")

elif source == "Paste criteria":
    from afl import historic_grids as HG

    # The failure this exists to prevent: hand-picking an axis builder that
    # is one word away from the question actually asked. "50+ GAMES TWO
    # DIFF CLUBS" and "50+ GOALS TWO DIFF CLUBS" are different squares with
    # different answers, and choosing between them from a dropdown is a
    # guess made once and never re-checked. Typing Gridley's own wording
    # hands that decision to afl/parse_criteria.py, which reads the words.
    with st.sidebar.expander("Type the six criteria", expanded=True):
        st.caption(
            "Copy each axis exactly as the board words it — "
            "“50+ GAMES TWO DIFF CLUBS”, “PLAYED IN 2010s”, "
            "“CARLTON FIRST CAREER GAME”. Club-logo axes take the club "
            "name. Anything the parser cannot read is named below rather "
            "than answered as something else.")
        pasted_cols = [
            st.text_input(f"Column {i + 1}", key=SPORT.k("pastec", i))
            for i in range(3)]
        pasted_rows = [
            st.text_input(f"Row {i + 1}", key=SPORT.k("paster", i))
            for i in range(3)]

    typed_cols = [t.strip() for t in pasted_cols]
    typed_rows = [t.strip() for t in pasted_rows]
    if all(typed_cols) and all(typed_rows):
        pasted_grid = HG.HistoricGrid(
            number=0, date="pasted grid", source="pasted",
            cols=tuple(typed_cols), rows=tuple(typed_rows))
        show_report(HG.analyse(pasted_grid, con, SPORT), mode)
    else:
        st.sidebar.info("Enter all six criteria to draw the board.")
        st.session_state.pop("loaded", None)

if "loaded" in st.session_state and source != "Build my own":
    rows_def = st.session_state.loaded["rows"]
    cols_def = st.session_state.loaded["cols"]
    st.sidebar.info("Using a loaded grid. Pick “Build my own” to go back "
                    "to the axes set above.")
elif source == "Build my own":
    st.session_state.pop("loaded", None)

st.sidebar.markdown("---")
order = st.sidebar.radio("Rank by",
                         ["obscurity", f"fewest {V.games}", "oldest", "newest"],
                         key=SPORT.k("order"))
order = "fewest games" if order.startswith("fewest") else order
limit = st.sidebar.slider("Results per square", 5, 100, 25,
                          key=SPORT.k("limit"))


# -------------------------------------------------------------- the board
st.markdown("# Grid Solver")
st.caption(f"Build or load a {V.grid_source} board. Every square is solved "
           "as soon as its axes are set.")


def _rebuild(frags, params):
    """
    Reattach a flattened parameter tuple to its fragments.

    Constraints are split into (fragments, params) so both halves hash
    cleanly for the cache; each fragment reclaims as many parameters as it
    has placeholders, in order.
    """
    out, i = [], 0
    for sql in frags:
        n = sql.count("?")
        out.append((sql, list(params[i:i + n])))
        i += n
    return out


@st.cache_data(show_spinner=False, max_entries=512)
def _square(sport_key, db, revision, frags, params, order):
    """Eligible count plus the single best answer, for one square."""
    schema = sports.get(sport_key).schema
    return core.square(get_con(db, revision), _rebuild(frags, params), schema,
                       order=order)


@st.cache_data(show_spinner=False, max_entries=256)
def _solve(sport_key, db, revision, frags, params, order, limit):
    """Cache the expanded result list for an opened square.

    player_id is selected ahead of the display columns and stripped before
    the table is drawn. Clicking a row has to open that exact player, and
    460 names in this database belong to more than one person -- resolving
    the click by name would show the wrong career for any of them.
    """
    schema = sports.get(sport_key).schema
    cols = [(f"p.{schema.player_id}", "__pid")] + list(schema.solve_columns())
    rows = core.solve(
        get_con(db, revision), _rebuild(frags, params), schema,
        limit=limit, order=order, columns=cols,
    )
    return tuple(rows)


@st.dialog("Player", width="large")
def show_player_dialog(pid):
    """The selected answer's full career, over the board rather than away
    from it.

    A grid solver's question is "who is this, and does the career actually
    fit the square?", asked about one row of a list the board just
    produced. Sending that to the Player Search page loses the board, the
    open square and the scroll position, and makes the solver re-find their
    place afterwards. The profile body is the same one Player Search
    renders -- explore.render_player_profile -- so the two cannot drift.
    """
    import explore
    explore.render_player_profile(SPORT, con, pid, key_prefix="griddlg",
                                  heading_level="###")


def constraints_for(r, c):
    return [x for x in (rows_def[r][1], cols_def[c][1]) if x]


def square_for(r, c):
    cs = constraints_for(r, c)
    if len(cs) < 2:
        return None
    return _square(SPORT.key, SPORT.db, DB_REVISION,
                   tuple(s for s, _ in cs),
                   tuple(v for _, p in cs for v in p),
                   order)


# The first square with both axes defined opens automatically, so the page
# never lands on an empty results panel.
if "cell" not in st.session_state or st.session_state.cell is None:
    st.session_state.cell = next(
        ((r, c) for r in range(3) for c in range(3)
         if len(constraints_for(r, c)) == 2), None)

header = st.columns([1.1, 1, 1, 1])
for i, (label, _) in enumerate(cols_def):
    header[i + 1].markdown(
        f"<div class='axis'>{label.replace(chr(10), '<br>')}</div>",
        unsafe_allow_html=True)

for r in range(3):
    row = st.columns([1.1, 1, 1, 1])
    row[0].markdown(
        f"<div class='axis'>{rows_def[r][0].replace(chr(10), '<br>')}</div>",
        unsafe_allow_html=True)
    for c in range(3):
        sq = square_for(r, c)
        open_here = st.session_state.cell == (r, c)
        cell = row[c + 1]

        if sq is None:
            face = (f"<div class='square is-empty'>"
                    f"<div class='square-name'>—</div>"
                    f"<div class='square-meta'>define both axes</div></div>")
            action = "unavailable"
        elif sq.eligible == 0:
            face = (f"<div class='square is-empty'>"
                    f"<div class='square-name'>No answer</div>"
                    f"<div class='square-meta'>0 eligible</div></div>")
            action = "no answers"
        else:
            # Absolute stars here, within-square stars in the results table
            # below. The tile shows one answer -- the square's most obscure
            # one -- so rescaling against that square's own spread rated it
            # 5/5 by construction, every square, always: the best answer's
            # obscurity IS the square's maximum, so (v-lo)/(hi-lo) is 1
            # whatever the answers look like. Nine identical ratings told a
            # solver nothing about which square to attack first. On the
            # absolute scale the tiles differ, and comparing squares is the
            # only question a board-level rating can answer.
            face = (
                f"<div class='square{' is-open' if open_here else ''}'>"
                f"<div class='square-name'>{sq.best_name}</div>"
                f"<div>{core.stars_html(sq.obscurity)}</div>"
                f"<div class='square-meta'>{sq.eligible:,} eligible</div>"
                f"</div>")
            action = "open" if not open_here else "showing"

        cell.markdown(face, unsafe_allow_html=True)
        if cell.button(action, key=SPORT.k("cell", r, c),
                       disabled=(sq is None or sq.eligible == 0)):
            st.session_state.cell = (r, c)
            st.rerun()

# The only visible statement of what the stars mean. The rating card and
# every star row carry it as a hover tooltip instead of repeating it.
# Generated from this sport's own obscurity model. core's names goals,
# finals and Brownlow votes, none of which the NBA model uses.
st.caption(SPORT.star_disclaimer)
st.markdown("---")


# ------------------------------------------------------------- the answer
if st.session_state.cell:
    r, c = st.session_state.cell
    rlab, clab = rows_def[r][0], cols_def[c][0]
    cs = constraints_for(r, c)

    st.markdown(f"### {rlab.replace(chr(10), ' ')} × {clab.replace(chr(10), ' ')}")
    rows = _solve(
        SPORT.key, SPORT.db, DB_REVISION,
        tuple(sql for sql, _ in cs),
        tuple(value for _, values in cs for value in values),
        order, limit,
    )

    if not rows:
        st.info(SPORT.empty_hint)
    else:
        headers = ["__pid"] + [h for _, h in SCHEMA.solve_columns()]
        df = pd.DataFrame(rows, columns=headers)
        # The header the schema gave this column, not a literal: the AFL
        # calls it "Clubs" and the NBA "Teams".
        clubs_header = SCHEMA.clubs_hist_header()
        if clubs_header in df:
            df[clubs_header] = df[clubs_header].str.replace(
                "|", ", ", regex=False)
        pids = df["__pid"].tolist()
        df = df.drop(columns=["__pid"])

        best = rows[0][1:]          # drop the id the table does not show
        best_name, best_obsc = best[0], best[-1]
        selected_square = square_for(r, c)
        total = selected_square.eligible if selected_square else len(rows)

        # Star ratings replace the raw 0-100 obscurity score everywhere,
        # scaled against this square's own spread rather than the whole
        # database: five stars is the rarest answer to THIS square. On the
        # absolute scale every square landed between 1.5 and 4 regardless
        # of how rare its answers were, because obscurity is a percentile
        # across 13,353 players and 5/5 needed the most obscure of them.
        obs_lo = selected_square.obscurity_min if selected_square else None
        obs_hi = selected_square.obscurity_max if selected_square else None
        obscurity_header = SCHEMA.obscurity_header()
        df["Rating"] = df[obscurity_header].map(
            lambda o: core.stars_text(o, lo=obs_lo, hi=obs_hi))
        df = df.drop(columns=[obscurity_header])

        card1, card2, card3 = st.columns(3)
        card1.markdown(
            f"<div class='card'><div class='card-label'>Best answer</div>"
            f"<div class='card-value'>{best_name}</div>"
            f"<div class='card-sub'>{best[1]}–{best[2]} · "
            f"{best[3]:,} {V.games}</div></div>", unsafe_allow_html=True)
        card2.markdown(
            f"<div class='card'><div class='card-label'>Rarity for this "
            f"square</div><div class='card-value'>"
            f"{core.stars_html(best_obsc, lo=obs_lo, hi=obs_hi)}</div>"
            f"<div class='card-sub'>obscurity {best_obsc:.1f} / 100 "
            f"database-wide</div>"
            f"</div>", unsafe_allow_html=True)
        card3.markdown(
            f"<div class='card'><div class='card-label'>Eligible players"
            f"</div><div class='card-value'>{total:,}</div>"
            f"<div class='card-sub'>showing the top {len(rows)}</div></div>",
            unsafe_allow_html=True)

        st.markdown("")
        st.caption("Select a row to see that player's full career without "
                   "leaving the board.")
        table = st.dataframe(
            df, hide_index=True, width="stretch",
            on_select="rerun", selection_mode="single-row",
            key=SPORT.k("answers", r, c))

        picked_rows = table.selection.rows if table and table.selection else []
        if picked_rows:
            show_player_dialog(pids[picked_rows[0]])

        with st.expander("SQL for this square"):
            st.code(C.to_standalone_sql(cs, limit), language="sql")
else:
    st.caption("Define both axes on at least one square to see answers.")