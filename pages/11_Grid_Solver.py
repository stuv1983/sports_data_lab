import streamlit as st
import pandas as pd
import accounts
import core
import ui_widgets
import components
import sports
import random
import datetime
import db_pool

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


def _auto_grid():
    """Create a supported 3x3 board whose nine cells all have an answer."""
    import random

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
SOURCES = ["Build my own", "Saved grid", "Auto-made grid", "Paste criteria",
           "Today's grid", "Past grid", "Random supported grid"]
source = st.sidebar.radio("Source", SOURCES, key=SPORT.k("gridsource"),
                          label_visibility="collapsed")

if source in ("Paste criteria", "Today's grid", "Past grid",
              "Random supported grid"):
    mode = st.sidebar.radio(
        "Mode", ["Authentic", "Practice"], horizontal=True,
        key=SPORT.k("gridmode"),
        help="Authentic plays only grids whose six criteria are all "
             "supported, with no substitutions. Practice replaces an "
             "unsupported criterion with a comparable one and labels the "
             "swap on the axis.")
else:
    mode = "Authentic"

if source == "Saved grid":
    saved = accounts.list_saved_grids(AUTH_USER.id, SPORT.key)
    if not saved:
        st.sidebar.info("You have no saved grids for this sport yet.")
    else:
        by_id = {item["id"]: item for item in saved}
        chosen_id = st.sidebar.selectbox(
            "Saved grid", list(by_id), key=SPORT.k("saved_grid_id"),
            format_func=lambda grid_id: by_id[grid_id]["name"])
        open_col, delete_col = st.sidebar.columns(2)
        if open_col.button("Open", key=SPORT.k("saved_grid_open"),
                           type="primary"):
            try:
                st.session_state.loaded = accounts.load_grid(
                    AUTH_USER.id, chosen_id)
            except accounts.AccountError as exc:
                st.sidebar.error(str(exc))
            else:
                st.session_state.cell = None
                st.rerun()
        if delete_col.button("Delete", key=SPORT.k("saved_grid_delete")):
            accounts.delete_grid(AUTH_USER.id, chosen_id)
            st.session_state.pop("loaded", None)
            st.rerun()

elif source == "Auto-made grid":
    st.sidebar.caption(
        "Builds a fresh board from clubs, eras, and career milestones, and checks that all nine squares have an answer.")
    if (st.sidebar.button("Make another", key=SPORT.k("auto_grid_button"),
                          type="primary")
            or SPORT.k("auto_grid") not in st.session_state):
        with st.spinner("Making a playable grid…"):
            generated = _auto_grid()
        if generated is None:
            st.sidebar.warning("No complete automatic grid was found. Try again.")
        else:
            st.session_state[SPORT.k("auto_grid")] = generated
            st.session_state.loaded = generated
            st.session_state.cell = None
    elif SPORT.k("auto_grid") in st.session_state:
        st.session_state.loaded = st.session_state[SPORT.k("auto_grid")]

elif source == "Today's grid":
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

with st.sidebar.expander("Save this grid", expanded=False):
    save_name = st.text_input("Grid name", key=SPORT.k("save_grid_name"),
                              placeholder="Friday challenge")
    if st.button("Save grid", key=SPORT.k("save_grid_button"),
                 type="primary", use_container_width=True):
        try:
            accounts.save_grid(
                AUTH_USER.id, SPORT.key, save_name, rows_def, cols_def)
        except (accounts.AccountError, PermissionError) as exc:
            st.error(str(exc))
        else:
            st.success("Grid saved. Open it from Grid source → Saved grid.")

st.sidebar.markdown("---")
order = st.sidebar.radio("Rank by",
                         ["obscurity", f"fewest {V.games}", "oldest", "newest"],
                         key=SPORT.k("order"))
order = "fewest games" if order.startswith("fewest") else order
limit = st.sidebar.slider("Results per square", 5, 100, 25,
                          key=SPORT.k("limit"))


# -------------------------------------------------------------- the board
st.markdown("# Grid Solver")
board_mode = st.radio(
    "Board mode", ["Solve", "Play grid"], horizontal=True,
    key=SPORT.k("board_mode"),
    help="Solve shows the database-ranked answers. Play grid hides them and checks your picks.")
if board_mode == "Solve":
    st.caption(f"Build or load a {V.grid_source} board. Every square is solved as soon as its axes are set.")
else:
    st.caption("Choose a square, submit a player, and fill all nine without revealing the solver's answers.")


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


_grid_signature = repr((SPORT.key, rows_def, cols_def))
_all_play_answers = st.session_state.setdefault(SPORT.k("play_answers"), {})
play_answers = _all_play_answers.setdefault(_grid_signature, {})


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
            face = ("<div class='square is-empty'>"
                    "<div class='square-name'>—</div>"
                    "<div class='square-meta'>define both axes</div></div>")
            action = "unavailable"
        elif sq.eligible == 0:
            face = ("<div class='square is-empty'>"
                    "<div class='square-name'>No answer</div>"
                    "<div class='square-meta'>0 eligible</div></div>")
            action = "no answers"
        elif board_mode == "Play grid":
            answered = play_answers.get((r, c))
            if answered:
                face = (
                    f"<div class='square{' is-open' if open_here else ''}'>"
                    f"<div class='square-name'>{answered['name']}</div>"
                    "<div class='square-meta'>correct</div></div>")
                action = "change" if not open_here else "selected"
            else:
                face = (
                    f"<div class='square{' is-open' if open_here else ''}'>"
                    "<div class='square-name'>Choose player</div>"
                    "<div class='square-meta'>unanswered</div></div>")
                action = "answer" if not open_here else "selected"
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
                f"<div class='square-name'>{sq.best_name}</div>"
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

if board_mode == "Play grid":
    completed = len(play_answers)
    st.progress(completed / 9, text=f"{completed} of 9 squares complete")
    if completed == 9:
        st.success("Grid complete — all nine answers are correct.")
        if not st.session_state.get(SPORT.k("grid_celebrated", _grid_signature)):
            st.balloons()
            st.session_state[SPORT.k("grid_celebrated", _grid_signature)] = True
    elif st.session_state.cell:
        r, c = st.session_state.cell
        rlab, clab = rows_def[r][0], cols_def[c][0]
        st.markdown(
            f"### {rlab.replace(chr(10), ' ')} × {clab.replace(chr(10), ' ')}")
        selected = player_picker(SPORT.k("play_pick", r, c))
        submit_col, clear_col = st.columns([1, 1])
        if submit_col.button(
                "Submit answer", type="primary", key=SPORT.k("play_submit", r, c),
                disabled=selected is None):
            player_id, player_name = selected
            if core.matches_player(con, player_id, constraints_for(r, c), SCHEMA):
                play_answers[(r, c)] = {"id": player_id, "name": player_name}
                st.rerun()
            else:
                st.error(f"{player_name} does not satisfy both criteria for this square.")
        if clear_col.button(
                "Clear square", key=SPORT.k("play_clear", r, c),
                disabled=(r, c) not in play_answers):
            play_answers.pop((r, c), None)
            st.rerun()
    else:
        st.info("Choose a square to answer it.")
    st.stop()


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
        with card1:
            components.player_button(
                f"Open {best_name}", SPORT, con, pids[0],
                key=SPORT.k("best_answer", r, c), key_prefix="gridbest")
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
        components.clickable_player_table(
            df, pids, SPORT, con, key=SPORT.k("answers", r, c),
            key_prefix="griddlg")

        with st.expander("SQL for this square"):
            st.code(C.to_standalone_sql(cs, limit), language="sql")
else:
    st.caption("Define both axes on at least one square to see answers.")
