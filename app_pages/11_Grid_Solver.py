import datetime
import hashlib
import html
import random
import sqlite3

import streamlit as st
import pandas as pd
import accounts
import core
import ui_widgets
import components
import sports

SPORT = st.session_state.SPORT
con = st.session_state.con
DB_REVISION = st.session_state.DB_REVISION
SCHEMA = SPORT.schema
V = SPORT.vocab
C = SPORT.C
AVAILABLE = st.session_state.AVAILABLE

def get_con(db, rev):
    import db_pool
    return db_pool.get_con(db, rev)

def season_span(*args):
    return ui_widgets.season_span(*args)

player_picker = ui_widgets.player_picker

AUTH_USER = accounts.get_user(st.session_state.get("auth_user_id"))

def axis_widget(key, default_type, defaults):
    return ui_widgets.axis_widget(key, default_type, defaults, SPORT, DB_REVISION, AVAILABLE)


st.markdown("# Grid solver")
st.caption(
    f"Build or load a {V.grid_source} board. Every square is solved as soon "
    "as its row and column are set."
)

SETUP = st.container(border=True)
SETUP.markdown("## Grid setup")

# Only offered where the sport declares a feed AND a parser: a fetched board
# arrives as six lines of the site's own wording, and reading those is what
# afl/parse_criteria.py does -- for the AFL only.
DAILY = f"Latest {V.grid_source}"
CAN_FETCH_DAILY = bool(SPORT.daily_grid_feed and SPORT.criterion_parser)
SOURCES = ["Build my own", "Saved grid", "Auto-made grid", "Paste criteria",
           "Past grid", "Random supported grid"]
if CAN_FETCH_DAILY:
    SOURCES.insert(0, DAILY)
source_key = SPORT.k("gridsource")
if st.session_state.get(source_key) not in SOURCES:
    st.session_state[source_key] = DAILY if CAN_FETCH_DAILY else "Build my own"
source = SETUP.selectbox(
    "Start with", SOURCES, key=source_key,
    help="Choose the latest published board, create your own, or open a saved grid.",
)

# Axis keys are namespaced by sport. Streamlit's session state is flat, so a
# club from one sport cannot leak into another. The six controls live beside
# the board now, where a first-time solver can actually find them.
cols_def, rows_def = [], []
if source == "Build my own":
    col_defaults, row_defaults = SPORT.grid_defaults
    SETUP.markdown("### Columns")
    column_cards = SETUP.columns(3)
    for i, (dt, dv) in enumerate(col_defaults):
        with column_cards[i].container(border=True):
            st.markdown(f"**Column {i + 1}**")
            cols_def.append(axis_widget(SPORT.k("c", i), dt, dv))

    SETUP.markdown("### Rows")
    row_cards = SETUP.columns(3)
    for i, (dt, dv) in enumerate(row_defaults):
        with row_cards[i].container(border=True):
            st.markdown(f"**Row {i + 1}**")
            rows_def.append(axis_widget(SPORT.k("r", i), dt, dv))

# ---------------------------------------------------------- grid sources
# Three ways in besides building the axes by hand. All of them run every
# criterion through the parser BEFORE the board is drawn, so a grid that
# cannot be answered says why instead of quietly answering something else.

@st.cache_data(show_spinner=False)
def grid_library(sport_key, db, revision):
    """Every captured grid, parsed and checked for support -- not counted.

    This runs on every load of the page, whichever source is selected, so
    it may only do work that is free. Counting each criterion and executing
    all nine intersections for all fifteen captured boards is over two
    minutes against the AFL database, and it was being spent before the
    board was drawn even when the chosen source never touched the library.

    What survives here is what the picker actually reads: whether every
    criterion parses, and whether the data it needs is loaded. The counts
    and the square check belong to the one grid a solver opens, and
    grid_report() does them there.
    """
    from afl import historic_grids as HG

    sport = sports.get(sport_key)
    return HG.analyse_all(get_con(db, revision), sport,
                          check_squares=False, count_eligible=False)


@st.cache_data(show_spinner=False, max_entries=64)
def grid_report(sport_key, db, revision, number):
    """One captured board, counted and with all nine squares executed."""
    from afl import historic_grids as HG

    sport = sports.get(sport_key)
    catalogue = grid_library(sport_key, db, revision)
    grid = next((r.grid for r in catalogue if r.grid.number == number), None)
    if grid is None:
        return None
    return HG.analyse(grid, get_con(db, revision), sport)


@st.cache_data(show_spinner=False, max_entries=64)
def feed_report(sport_key, db, revision, date, source, board):
    """The same, for a board read from the feed rather than the library."""
    from afl import historic_grids as HG

    grid = HG.from_feed(board, date, source)
    if grid is None:
        return None
    return HG.analyse(grid, get_con(db, revision), sports.get(sport_key))


def _axes_from(reports):
    """GridReport criteria -> the (label, constraint) pairs the board wants."""
    return [(r.display, r.constraint) for r in reports]


# historic_grids imports the AFL constraints module at module scope, so the
# import stays inside grid_library() -- selecting the NBA should not pull in
# the AFL constraint module and analyse a library that is not about it.
LIBRARY = grid_library(
    SPORT.key, SPORT.db, DB_REVISION) if SPORT.grid_library else []
LIB_BY_NUMBER = {r.grid.number: r for r in LIBRARY}
# Newest capture wins a duplicated date: LIBRARY arrives grid_num DESC, so
# the first report seen for a date is the one to keep.
LIB_BY_DATE = {}
for _report in LIBRARY:
    LIB_BY_DATE.setdefault(_report.grid.date, _report)


def lib_label(report):
    """'#1120 · 2026-08-09 · Gridley — 6/6 criteria supported · Ready'.

    GridReport.line() names the number and the verdict. The day the board
    was set and who captured it are the other two things a solver picking
    from a mixed library wants, and the number alone says neither.
    """
    grid = report.grid
    name = f"#{grid.number}" if grid.number else grid.key
    return (f"{name} · {grid.date} · {grid.source.replace('_', ' ')} — "
            f"{report.supported_count}/{report.total} criteria supported · "
            f"{report.status}")


@st.cache_data(ttl=900, show_spinner=False)
def _fetch_daily_board(sport_key, date):
    """One dated board straight from the sport's own feed, or None.

    Cached — including the misses — so a rerun does not re-request the site
    on every widget change. A board that has not been published yet is a
    None worth remembering for a few minutes; "Check again" clears it.
    """
    fetcher = sports.get(sport_key).daily_grid_fetcher()
    if fetcher is None:
        return None
    try:
        return fetcher(date)
    except Exception:
        # fetch_gridley already swallows network errors and returns None;
        # this covers a feed that does not.
        return None


def _auto_grid():
    """Create a supported 3x3 board whose nine cells all have an answer."""
    played_for = C.BUILDERS["Played for club"][0]
    career_min = C.BUILDERS["150+ / X+ career games"][0]
    played_between = C.BUILDERS["Played between seasons"][0]
    multi_club = C.BUILDERS["Multi-club player"][0]

    base = next((int(values.get("games")) for kind, values in
                 SPORT.grid_defaults[1]
                 if kind == "150+ / X+ career games" and values.get("games")),
                100)
    lo, hi = season_span(SPORT.key, SPORT.db, DB_REVISION)
    width = max(5, (hi - lo + 1) // 3)
    row_pool = [
        (f"{max(10, base // 2)}+ {V.games} played",
         career_min(max(10, base // 2))),
        (f"{base}+ {V.games} played", career_min(base)),
        (f"multi-{V.club} player", multi_club()),
    ]
    for start in range(lo, hi + 1, width):
        end = min(hi, start + width - 1)
        row_pool.append((f"played {start}–{end}", played_between(start, end)))

    clubs = list(SCHEMA.clubs)
    rng = random.SystemRandom()
    for _ in range(60):
        picked_cols = rng.sample(clubs, 3)
        picked_rows = rng.sample(row_pool, 3)
        generated_cols = [(club, played_for(club)) for club in picked_cols]
        if all(core.count(con, [row[1], col[1]], SCHEMA) > 0
               for row in picked_rows for col in generated_cols):
            return {"rows": picked_rows, "cols": generated_cols}
    return None


def show_report(picked, mode):
    from afl import historic_grids as HG

    """Sidebar verdict for an analysed grid, and load it onto the board.

    Shared by every source that produces a GridReport -- the captured
    library, the random picker and the pasted criteria -- so all of them
    apply the same Authentic/Practice rules and print the same criterion
    report.
    """
    g = picked.grid
    SETUP.markdown(f"**{g.key}** · source `{g.source}`")
    SETUP.caption(picked.line())

    if not g.complete:
        SETUP.warning(
            f"Partial capture — {len(g.partial_criteria)} of six "
            "criteria were recorded, so this board cannot be drawn. "
            "The criteria themselves still parse; see the report below.")
        st.session_state.pop("loaded", None)
    elif picked.unsupported and mode == "Authentic":
        names = ", ".join(f"“{c.text}”" for c in picked.unsupported)
        SETUP.error(
            f"Not playable in Authentic mode: {names} "
            f"({'; '.join(c.reason for c in picked.unsupported)}). "
            "Switch to Practice to play a substituted board.")
        st.session_state.pop("loaded", None)
    else:
        if picked.unsupported:      # Practice mode
            cache = st.session_state.setdefault(SPORT.k("practice"), {})
            prows, pcols, swaps = HG.practice_board(con, picked, cache)
            for original, replacement in swaps:
                SETUP.warning(
                    f"Original: {original} — replaced for Practice Mode "
                    f"by “{replacement}”.")
        else:
            prows, pcols = picked.rows, picked.cols
        st.session_state.loaded = {"rows": _axes_from(prows),
                                   "cols": _axes_from(pcols)}

    with SETUP.expander("Criterion report", expanded=False):
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


if source in ("Paste criteria", "Past grid",
              "Random supported grid", DAILY):
    mode = SETUP.segmented_control(
        "Mode", ["Authentic", "Practice"],
        default="Authentic", selection_mode="single",
        key=SPORT.k("gridmode"),
        help="Authentic plays only grids whose six criteria are all "
             "supported, with no substitutions. Practice replaces an "
             "unsupported criterion with a comparable one and labels the "
             "swap on the axis.") or "Authentic"
else:
    mode = "Authentic"

if source == DAILY and CAN_FETCH_DAILY:
    # The board the site is running today, opened in the solver rather than
    # retyped criterion by criterion into "Paste criteria". The saved
    # library is tried first -- the scheduled scan writes each day's board
    # into `historic_grids`, and a captured board has already been analysed
    # above. Only a day the scan has not reached yet costs an HTTP request,
    # which keeps the common case free while still making today's board
    # playable before the scan next runs.
    SETUP.caption(
        f"Opens the {V.grid_source} board for a given day. Saved boards "
        "come from the database; anything newer is read straight from "
        f"[{SPORT.daily_grid_site.split('//')[-1]}]"
        f"({SPORT.daily_grid_site}).")
    newest_saved_day = max(LIB_BY_DATE) if LIB_BY_DATE else None
    try:
        default_day = datetime.date.fromisoformat(newest_saved_day)
    except (TypeError, ValueError):
        default_day = datetime.date.today()
    day = SETUP.date_input(
        "Board date", value=default_day, key=SPORT.k("dailydate"),
        format="YYYY-MM-DD",
        help="Defaults to the newest board saved in the database.",
    )
    day_iso = day.isoformat()

    picked = None
    saved = LIB_BY_DATE.get(day_iso)
    if saved is not None:
        SETUP.caption(
            f"From the saved library — captured by "
            f"{saved.grid.source.replace('_', ' ')}.")
        with st.spinner("Checking every square against the database…"):
            picked = grid_report(SPORT.key, SPORT.db, DB_REVISION,
                                 saved.grid.number)
    else:
        with st.spinner(f"Asking {V.grid_source} for {day_iso}…"):
            board = _fetch_daily_board(SPORT.key, day_iso)
        if board:
            # Save a trusted live board immediately, then reopen it through
            # the normal captured-library path on the fresh DB revision.
            from utils.fetch_grids import save_grid as save_captured_grid
            try:
                action = save_captured_grid(
                    SPORT.db, day_iso, V.grid_source,
                    board["rows"], board["cols"], board.get("grid_num"),
                )
            except (KeyError, OSError, RuntimeError, ValueError,
                    sqlite3.Error) as exc:
                SETUP.warning(
                    "The live board opened, but it could not be added to the "
                    f"saved library: {exc}"
                )
            else:
                if action != "unchanged":
                    st.toast("Latest board saved", icon=":material/check_circle:")
                    st.rerun()
            with st.spinner("Checking every square against the database…"):
                picked = feed_report(SPORT.key, SPORT.db, DB_REVISION,
                                     day_iso, V.grid_source, board)
        if picked is None:
            SETUP.warning(
                f"No {V.grid_source} board came back for {day_iso}. Each "
                "board is published on its own day, an older one may have "
                "dropped off the feed, and the site may simply be "
                "unreachable — the feed answers all three the same way.")
            st.session_state.pop("loaded", None)
        else:
            SETUP.caption("Fetched live from the published board feed.")
        if SETUP.button("Check again", key=SPORT.k("daily_refetch"),
                        icon=":material/refresh:"):
            _fetch_daily_board.clear()
            st.rerun()

    if picked is not None:
        show_report(picked, mode)

elif source == "Saved grid":
    # The page can be opened anonymously when an administrator loosens the
    # grid_solver audience, so AUTH_USER is not guaranteed here.
    saved = (accounts.list_saved_grids(AUTH_USER.id, SPORT.key)
             if AUTH_USER else [])
    if AUTH_USER is None:
        SETUP.info("Log in to open your saved grids.")
    elif not saved:
        SETUP.info("You have no saved grids for this sport yet.")
    else:
        by_id = {item["id"]: item for item in saved}
        chosen_id = SETUP.selectbox(
            "Saved grid", list(by_id), key=SPORT.k("saved_grid_id"),
            format_func=lambda grid_id: by_id[grid_id]["name"])
        open_col, delete_col = SETUP.columns(2)
        if open_col.button("Open", key=SPORT.k("saved_grid_open"),
                           type="primary"):
            try:
                st.session_state.loaded = accounts.load_grid(
                    AUTH_USER.id, chosen_id)
            except accounts.AccountError as exc:
                SETUP.error(str(exc))
            else:
                st.session_state.cell = None
                st.rerun()
        if delete_col.button("Delete", key=SPORT.k("saved_grid_delete")):
            accounts.delete_grid(AUTH_USER.id, chosen_id)
            st.session_state.pop("loaded", None)
            st.rerun()

elif source == "Auto-made grid":
    SETUP.caption(
        "Builds a fresh board from clubs, eras, and career milestones, and checks that all nine squares have an answer.")
    if (SETUP.button("Make another", key=SPORT.k("auto_grid_button"),
                          type="primary")
            or SPORT.k("auto_grid") not in st.session_state):
        with st.spinner("Making a playable grid…"):
            generated = _auto_grid()
        if generated is None:
            SETUP.warning("No complete automatic grid was found. Try again.")
        else:
            st.session_state[SPORT.k("auto_grid")] = generated
            st.session_state.loaded = generated
            st.session_state.cell = None
    elif SPORT.k("auto_grid") in st.session_state:
        st.session_state.loaded = st.session_state[SPORT.k("auto_grid")]


elif source in ("Past grid", "Random supported grid") and LIBRARY:
    from afl import historic_grids as HG

    ready = HG.supported_grids(LIBRARY)

    if source == "Random supported grid":
        pool = ready if mode == "Authentic" else [r for r in LIBRARY
                                                  if r.grid.complete]
        if SETUP.button("Shuffle", key=SPORT.k("gridshuffle")) \
                or SPORT.k("randgrid") not in st.session_state:
            st.session_state[SPORT.k("randgrid")] = (
                random.choice(pool).grid.number if pool else None)
        chosen = st.session_state.get(SPORT.k("randgrid"))
        picked = None
        if chosen is None or chosen not in LIB_BY_NUMBER:
            SETUP.warning("No grid in the library is fully supported.")
        else:
            with st.spinner("Checking every square against the database…"):
                picked = grid_report(SPORT.key, SPORT.db, DB_REVISION, chosen)
    else:
        by = SETUP.segmented_control("Select by",
                              [f"{V.grid_source} number", "Date"],
                              default=f"{V.grid_source} number",
                              key=SPORT.k("gridby"))
        # Every grid stays selectable, including the ones that cannot be
        # played. Hiding them would hide the reason they cannot be played,
        # which is the more useful half of the information.
        if by == f"{V.grid_source} number":
            numbers = [r.grid.number for r in LIBRARY]
            n = SETUP.selectbox(
                "Grid", numbers, key=SPORT.k("gridnum"),
                format_func=lambda x: lib_label(LIB_BY_NUMBER[x]))
        else:
            dates = {r.grid.date: r.grid.number for r in LIBRARY}
            dsel = SETUP.selectbox(
                "Date", list(dates), key=SPORT.k("griddatesel"),
                format_func=lambda x: f"{x} — {LIB_BY_NUMBER[dates[x]].line()}")
            n = dates[dsel]
        # The listing above is parse-only. The board a solver actually
        # picks is the one worth counting, so the eligible numbers and the
        # nine-intersection check are done here, for that grid alone.
        with st.spinner("Checking every square against the database…"):
            picked = grid_report(SPORT.key, SPORT.db, DB_REVISION, n)

    if picked is not None:
        show_report(picked, mode)

elif source in ("Past grid", "Random supported grid"):
    SETUP.info(f"No captured grid library exists for {SPORT.label} yet.")

elif source == "Paste criteria" and not SPORT.criterion_parser:
    # afl/parse_criteria.py compiles against the AFL constraint set, so offering
    # it for another sport would answer AFL questions from an NBA database.
    SETUP.info(f"Criterion parsing is not available for {SPORT.label}.")

elif source == "Paste criteria":
    from afl import historic_grids as HG

    # The failure this exists to prevent: hand-picking an axis builder that
    # is one word away from the question actually asked. "50+ GAMES TWO
    # DIFF CLUBS" and "50+ GOALS TWO DIFF CLUBS" are different squares with
    # different answers, and choosing between them from a dropdown is a
    # guess made once and never re-checked. Typing Gridley's own wording
    # hands that decision to afl/parse_criteria.py, which reads the words.
    with SETUP.expander("Type the six criteria", expanded=True):
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
        SETUP.info("Enter all six criteria to draw the board.")
        st.session_state.pop("loaded", None)

if "loaded" in st.session_state and source != "Build my own":
    rows_def = st.session_state.loaded["rows"]
    cols_def = st.session_state.loaded["cols"]
    SETUP.info("Using a loaded grid. Pick “Build my own” to go back "
                    "to the axes set above.")
elif source == "Build my own":
    st.session_state.pop("loaded", None)

with SETUP.expander("Save this grid", expanded=False,
                    icon=":material/save:"):
    if AUTH_USER is None:
        st.caption("Log in to save grids.")
    else:
        save_name = st.text_input("Grid name", key=SPORT.k("save_grid_name"),
                                  placeholder="Friday challenge")
        if st.button("Save grid", key=SPORT.k("save_grid_button"),
                     type="primary", width="stretch"):
            try:
                accounts.save_grid(
                    AUTH_USER.id, SPORT.key, save_name, rows_def, cols_def)
            except (accounts.AccountError, PermissionError) as exc:
                st.error(str(exc))
            else:
                st.success("Grid saved. Open it from Start with → Saved grid.")

with st.popover("Result options", icon=":material/tune:"):
    order = st.selectbox(
        "Rank answers by",
        ["obscurity", f"fewest {V.games}", "oldest", "newest"],
        key=SPORT.k("order"),
    )
    order = "fewest games" if order.startswith("fewest") else order
    default_limit = st.slider(
        "Default rows per square", 5, 100, 25,
        key=SPORT.k("limit"),
        help="Sets the initial result count. Each opened square can be "
             "changed independently above its results table.",
    )


# -------------------------------------------------------------- the board


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


def _rows_key(r, c, constraints):
    """Stable per-square key for its independently chosen result count."""
    signature = hashlib.blake2s(
        repr(constraints).encode("utf-8"), digest_size=6).hexdigest()
    return SPORT.k("result_rows", r, c, signature)


def _rows_to_show(r, c, constraints, total):
    """Render the selected square's custom row-count form."""
    key = _rows_key(r, c, constraints)
    initial = min(int(default_limit), int(total))
    try:
        current = int(st.session_state.get(key, initial))
    except (TypeError, ValueError):
        current = initial
    st.session_state[key] = max(1, min(current, int(total)))

    with st.form(SPORT.k("result_rows_form", r, c), border=False):
        field, action = st.columns([2, 1], vertical_alignment="bottom")
        with field:
            chosen = st.number_input(
                "Rows to show",
                min_value=1,
                max_value=int(total),
                step=25,
                key=key,
                help="Type any number up to the square's eligible-player "
                     "count.",
            )
        with action:
            st.form_submit_button("Show rows", width="stretch")
    return int(chosen)


# The first square with both axes defined opens automatically, so the page
# never lands on an empty results panel.
if "cell" not in st.session_state or st.session_state.cell is None:
    st.session_state.cell = next(
        ((r, c) for r in range(3) for c in range(3)
         if len(constraints_for(r, c)) == 2), None)

def _axis_html(label):
    """Escape an axis label before it enters unsafe_allow_html markup.

    Labels carry data-sourced text -- player names from scraped imports,
    free-typed teammate names -- so only the newline-to-<br> is ours.
    """
    return html.escape(str(label)).replace(chr(10), "<br>")


header = st.columns([1.1, 1, 1, 1])
for i, (label, _) in enumerate(cols_def):
    header[i + 1].markdown(
        f"<div class='axis'>{_axis_html(label)}</div>",
        unsafe_allow_html=True)

for r in range(3):
    row = st.columns([1.1, 1, 1, 1])
    row[0].markdown(
        f"<div class='axis'>{_axis_html(rows_def[r][0])}</div>",
        unsafe_allow_html=True)
    for c in range(3):
        sq = square_for(r, c)
        open_here = st.session_state.cell == (r, c)
        cell = row[c + 1]

        if sq is None:
            face = ("<div class='square is-empty'>"
                    "<div class='square-name'>—</div>"
                    "<div class='square-meta'>define both axes</div></div>")
            action = "unavailable"
        elif sq.eligible == 0:
            face = ("<div class='square is-empty'>"
                    "<div class='square-name'>No answer</div>"
                    "<div class='square-meta'>0 eligible</div></div>")
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
            #
            # The years the shown answer played. A name on its own does not
            # say whether the best pick is a current player or someone from
            # the 1920s, which is the first thing a solver wants to know
            # about a name they do not recognise. Read positionally from the
            # schema's own solve columns, so it is right for whichever sport
            # is loaded rather than assuming the AFL's column order.
            span = SCHEMA.career_span(sq.best)
            face = (
                f"<div class='square{' is-open' if open_here else ''}'>"
                f"<div class='square-name'>{html.escape(str(sq.best_name))}</div>"
                + (f"<div class='square-meta'>{span}</div>" if span else "")
                + f"<div>{core.stars_html(sq.obscurity)}</div>"
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
    selected_square = square_for(r, c)
    total = selected_square.eligible if selected_square else 0

    st.markdown(f"### {rlab.replace(chr(10), ' ')} × {clab.replace(chr(10), ' ')}")
    if selected_square is None or total <= 0:
        rows = ()
    else:
        limit = _rows_to_show(r, c, cs, total)
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

        # NULL, never 0: a career with no recorded games count shows a
        # dash rather than formatting None and crashing the card.
        games_text = (f"{best[3]:,}" if isinstance(best[3], (int, float))
                      else "—")
        card1, card2, card3 = st.columns(3)
        card1.markdown(
            f"<div class='card'><div class='card-label'>Best answer</div>"
            f"<div class='card-value'>{html.escape(str(best_name))}</div>"
            f"<div class='card-sub'>{best[1]}–{best[2]} · "
            f"{games_text} {V.games}</div></div>", unsafe_allow_html=True)
        with card1:
            components.player_button(
                f"Open {best_name}", SPORT, con, pids[0],
                key=SPORT.k("best_answer", r, c), key_prefix="gridbest")
        obsc_sub = (f"obscurity {best_obsc:.1f} / 100 database-wide"
                    if isinstance(best_obsc, (int, float))
                    else "obscurity unrecorded")
        card2.markdown(
            f"<div class='card'><div class='card-label'>Rarity for this "
            f"square</div><div class='card-value'>"
            f"{core.stars_html(best_obsc, lo=obs_lo, hi=obs_hi)}</div>"
            f"<div class='card-sub'>{obsc_sub}</div>"
            f"</div>", unsafe_allow_html=True)
        card3.markdown(
            f"<div class='card'><div class='card-label'>Eligible players"
            f"</div><div class='card-value'>{total:,}</div>"
            f"<div class='card-sub'>showing the top {len(rows)}</div></div>",
            unsafe_allow_html=True)

        st.markdown("")
        st.caption("Select a row to see that player's full career without "
                   "leaving the board.")
        components.clickable_player_table(
            df, pids, SPORT, con, key=SPORT.k("answers", r, c),
            key_prefix="griddlg")

        with st.expander("SQL for this square"):
            st.code(C.to_standalone_sql(cs, limit), language="sql")
else:
    st.caption("Define both axes on at least one square to see answers.")
