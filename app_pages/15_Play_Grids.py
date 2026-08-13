import html
import json
import sqlite3

import streamlit as st

import accounts
import core
import ui_widgets

# Access control lives in app.py (_PROTECTED_PAGES + accounts.can_access);
# this page renders for whoever app.py let through and must not re-check.

SPORT = st.session_state.SPORT
con = st.session_state.con
DB_REVISION = st.session_state.DB_REVISION
SCHEMA = SPORT.schema
V = SPORT.vocab
C = SPORT.C

player_picker = ui_widgets.player_picker

st.markdown("# Play grids")
st.caption(
    "Choose a grid, then fill all nine squares. Each player can be used once; "
    "you have nine guesses and one undo."
)

def render_grid_selection():
    sources = ["Saved grids"]
    # A capability, not a sport key: any sport that declares a captured
    # grid library gets the source, under its own puzzle's name.
    past_label = f"Past grids ({V.grid_source})"
    if SPORT.grid_library:
        sources.append(past_label)

    default_source = past_label if past_label in sources else "Saved grids"
    source = st.pills(
        "Grid source", sources, key=SPORT.k("play_source"),
        default=default_source,
    )
    if not source:
        source = default_source
    
    if source == "Saved grids":
        viewer_id = st.session_state.get("auth_user_id")
        saved = accounts.list_playable_grids(SPORT.key, viewer_id)
        if not saved:
            st.info("No saved grids found. Go to Grid Solver to create and save some grids.")
            return
        by_id = {item["id"]: item for item in saved}
        chosen_id = st.selectbox(
            "Saved grid", list(by_id), key=SPORT.k("play_saved_grid_id"),
            format_func=lambda grid_id: by_id[grid_id]["name"])
        
        if st.button("Load grid", key=SPORT.k("play_load_grid"), type="primary", width="stretch"):
            try:
                grid_data = accounts.load_playable_grid(chosen_id, viewer_id)
                st.session_state[SPORT.k("play_loaded_grid")] = grid_data
                st.session_state[SPORT.k("play_state")] = {
                    "guesses": 0, "undo_used": False, "answers": {}, "history": [], "cell": None
                }
                st.rerun()
            except accounts.AccountError as exc:
                st.error(str(exc))
    else:
        try:
            # Every source, not just Gridley: nine of the twelve captured
            # boards came in as manual_capture and a source filter here hid
            # all of them from the picker.
            cur = con.execute("SELECT grid_num, date, source, rows_json, cols_json FROM historic_grids ORDER BY date DESC, grid_num DESC")
            past_grids = [
                {"grid_num": r[0], "date": r[1], "source": r[2], "rows_json": r[3], "cols_json": r[4]} 
                for r in cur.fetchall()
            ]
        except Exception:
            past_grids = []
            
        if not past_grids:
            st.info(f"No captured grid library exists for {SPORT.label} yet. Run the fetch_grids script.")
            return
            
        st.write("### Past grids collection")
        
        # Use a dictionary to map a friendly label to the grid dictionary
        grid_options = {f"🔴 {g['source']} #{g['grid_num']} • {g['date']}": g for g in past_grids}
        
        chosen_label = st.selectbox(
            "Select a grid",
            options=list(grid_options.keys()),
            key=SPORT.k("play_past_grid_id")
        )
        
        if st.button("Load past grid", key=SPORT.k("play_load_past_grid"), type="primary", width="stretch"):
            picked = grid_options[chosen_label]
            try:
                cols_labels = tuple(json.loads(picked["cols_json"]))
                rows_labels = tuple(json.loads(picked["rows_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                st.error("This grid contains invalid saved criteria.")
                st.stop()

            from afl import historic_grids as HG
            grid = HG.HistoricGrid(
                number=picked["grid_num"], date=picked["date"],
                source=picked["source"], rows=rows_labels, cols=cols_labels,
            )
            with st.spinner("Checking every square against the database…"):
                report = HG.analyse(grid, con, SPORT)
            if not report.authentic_playable:
                st.error(f"This grid cannot be played: {report.status}.")
                for criterion in report.unsupported:
                    st.caption(f"{criterion.text}: {criterion.reason or 'criterion is unsupported'}")
                for err in report.square_errors:
                    st.caption(err)
            else:
                rows = [(item.display, item.constraint) for item in report.rows]
                cols = [(item.display, item.constraint) for item in report.cols]

                st.session_state[SPORT.k("play_loaded_grid")] = {
                    "rows": rows, "cols": cols,
                    "grid_num": picked["grid_num"], "source": picked["source"],
                }
                st.session_state[SPORT.k("play_state")] = {
                    "guesses": 0, "undo_used": False, "answers": {}, "history": [], "cell": None
                }
                st.rerun()

@st.cache_data(ttl=3600, show_spinner="Loading the newest captured grid…")
def _get_newest_grid_cached(sport_key, db_revision):
    """The most recent playable captured board, whatever captured it.

    Every source counts. Filtering to source='Gridley' here hid the nine
    manual_capture boards that are the bulk of the library, so the page
    auto-loaded nothing whenever the newest board was a manual capture.
    """
    try:
        import db_pool
        import sports
        from afl import historic_grids as HG
        local_sport = sports.get(sport_key)
        # Only a sport that declares a captured library has the table.
        if not local_sport.grid_library:
            return None
        # The sport's own resolved path, never a bare "afl.db": the database
        # lives at data/afl/afl.db, so the literal name opened an empty file
        # in the working directory and auto-loading silently found nothing.
        local_con = db_pool.get_con(local_sport.db, db_revision)
        # Analysing one board costs seconds, so only the newest few are
        # tried before handing the choice back to the player.
        cur = local_con.execute(
            "SELECT grid_num, date, source, rows_json, cols_json "
            "FROM historic_grids ORDER BY date DESC, grid_num DESC LIMIT 3")
        for latest in cur.fetchall():
            picked = {"grid_num": latest[0], "date": latest[1],
                      "source": latest[2], "rows_json": latest[3],
                      "cols_json": latest[4]}
            try:
                cols_labels = tuple(json.loads(picked["cols_json"]))
                rows_labels = tuple(json.loads(picked["rows_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            grid = HG.HistoricGrid(
                number=picked["grid_num"], date=picked["date"],
                source=picked["source"], rows=rows_labels, cols=cols_labels,
            )
            report = HG.analyse(grid, local_con, local_sport)
            if report.authentic_playable:
                rows = [(item.display, item.constraint) for item in report.rows]
                cols = [(item.display, item.constraint) for item in report.cols]
                return {"rows": rows, "cols": cols,
                        "grid_num": picked["grid_num"],
                        "source": picked["source"]}
    except (ImportError, OSError, sqlite3.Error):
        # A sport with no captured-board table is an ordinary state, and the
        # picker still offers Saved Grids, so these stay quiet. Anything else
        # is a bug and now reaches the page instead of becoming a blank one.
        pass
    return None

def auto_load_newest_grid():
    grid_data = _get_newest_grid_cached(SPORT.key, DB_REVISION)
    if grid_data:
        st.session_state[SPORT.k("play_loaded_grid")] = grid_data
        st.session_state[SPORT.k("play_state")] = {
            "guesses": 0, "undo_used": False, "answers": {}, "history": [], "cell": None
        }
        return True
    return False

@st.dialog("Choose a grid")
def grid_selector_dialog():
    render_grid_selection()

loaded = st.session_state.get(SPORT.k("play_loaded_grid"))
if not loaded:
    if auto_load_newest_grid():
        loaded = st.session_state.get(SPORT.k("play_loaded_grid"))

if not loaded:
    st.markdown("### No grid loaded")
    if st.button("Choose a grid", key=SPORT.k("play_empty_select_btn"),
                 type="primary", icon=":material/grid_view:"):
        grid_selector_dialog()
    st.stop()

state = st.session_state[SPORT.k("play_state")]
rows_def = loaded["rows"]
cols_def = loaded["cols"]

# Keep selection beside the board instead of hiding it in the sidebar.
with st.container(border=True):
    grid_title = "Saved grid"
    if loaded.get("grid_num") is not None:
        grid_title = f"{loaded.get('source') or 'Grid'} #{loaded['grid_num']}"
    st.markdown(f"### {grid_title}")
    st.caption("This is the active board. You can switch grids at any time.")
    if st.button(
        "Choose another grid", key=SPORT.k("play_select_btn"),
        type="primary", icon=":material/grid_view:",
    ):
        grid_selector_dialog()

# Draw the board.
#
# A real container, not a pair of raw <div> markdown calls: Streamlit renders
# each st.markdown into its own element, so an opening div written that way
# is a *sibling* of the columns that follow rather than their parent and the
# mobile no-wrap rule in theme.py never matched anything. A keyed container
# emits .st-key-<key> around the real block, which the rule can target.
with st.container(key="grid_board"):
    header = st.columns([1.1, 1, 1, 1])
    for i, col in enumerate(cols_def):
        label, (sql, params) = col
        with header[i + 1].popover(label.replace(chr(10), ' ')):
            st.markdown(f"**{label}**")
            st.code(sql, language="sql")

    for r in range(3):
        row_cols = st.columns([1.1, 1, 1, 1])
        r_label, (r_sql, r_params) = rows_def[r]
        with row_cols[0].popover(r_label.replace(chr(10), ' ')):
            st.markdown(f"**{r_label}**")
            st.code(r_sql, language="sql")

        for c in range(3):
            cell = row_cols[c + 1]
            open_here = state["cell"] == (r, c)
            answered = state["answers"].get(f"{r},{c}")

            if answered:
                face = (
                    f"<div class='square{' is-open' if open_here else ''}'>"
                    f"<div class='square-name'>{html.escape(str(answered['name']))}</div>"
                    "<div class='square-meta'>correct</div></div>"
                )
                action = "selected"
            else:
                face = (
                    f"<div class='square{' is-open' if open_here else ''}'>"
                    "<div class='square-name'>Choose player</div>"
                    "<div class='square-meta'>unanswered</div></div>"
                )
                action = "answer" if not open_here else "selected"

            cell.markdown(face, unsafe_allow_html=True)
            if not answered and cell.button(action, key=SPORT.k("play_cell", r, c)):
                # One answer box serves all nine squares, so the square being
                # left has to hand it back empty. A box per square would send
                # the browser its own copy of every player name -- nine
                # downloads of the same list across a grid.
                ui_widgets.clear_player_picker(SPORT.k("game_pick"))
                state["cell"] = (r, c)
                st.rerun()

st.markdown("---")

# Game Status
st.sidebar.markdown("### Game Status")
st.sidebar.write(f"**Guesses used:** {state['guesses']} / 9")

if not state["undo_used"] and len(state["history"]) > 0:
    if st.sidebar.button("Step Back (Undo Last Move)", key=SPORT.k("game_undo")):
        last_move = state["history"].pop()
        state["guesses"] -= 1
        state["undo_used"] = True
        if last_move["type"] == "correct":
            r, c = last_move["r"], last_move["c"]
            del state["answers"][f"{r},{c}"]
        st.rerun()
else:
    st.sidebar.write(f"**Undos used:** {1 if state['undo_used'] else 0} / 1")

completed = len(state["answers"])
st.progress(completed / 9, text=f"{completed} of 9 squares complete")

game_over = False
if completed == 9:
    st.success("Grid complete — all nine answers are correct!")
    st.balloons()
    game_over = True
elif state['guesses'] >= 9:
    st.error("Game Over! You've used all 9 guesses.")
    game_over = True

if game_over and not state.get("game_logged"):
    user_id = st.session_state.get("auth_user_id")
    user = accounts.get_user(user_id) if user_id else None
    if user:
        accounts.log_game_stat(user.id, "gridley", completed)
    state["game_logged"] = True

if not game_over and state["cell"]:
    r, c = state["cell"]
    rlab, clab = rows_def[r][0], cols_def[c][0]
    st.markdown(f"### {rlab.replace(chr(10), ' ')} × {clab.replace(chr(10), ' ')}")
    selected = player_picker(SPORT.k("game_pick"), SPORT, DB_REVISION)
    
    if st.button("Submit answer", type="primary", key=SPORT.k("game_submit", r, c), disabled=selected is None):
        player_id, player_name = selected
        
        # Check uniqueness
        used_ids = [ans["id"] for ans in state["answers"].values()]
        if player_id in used_ids:
            st.error(f"{player_name} has already been used in this grid!")
        else:
            # Check correctness
            cs = []
            for axis in (rows_def[r], cols_def[c]):
                _, (sql, params) = axis
                if sql:
                    cs.append((sql, tuple(params)))
                    
            if core.matches_player(con, player_id, cs, SCHEMA):
                state["answers"][f"{r},{c}"] = {"id": player_id, "name": player_name}
                state["history"].append({"type": "correct", "r": r, "c": c, "id": player_id, "name": player_name})
                state["guesses"] += 1
                state["cell"] = None
                st.rerun()
            else:
                state["guesses"] += 1
                state["history"].append({"type": "incorrect", "r": r, "c": c, "id": player_id, "name": player_name})
                st.error(f"Incorrect! {player_name} does not satisfy both criteria. (Guess used)")
elif not game_over:
    st.info("Choose a square to answer it.")
