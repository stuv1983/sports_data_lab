import json

import streamlit as st

import accounts
import core
import ui_widgets

# We removed the hardcoded admin check here because app.py handles access control 
# via _PROTECTED_PAGES and accounts.can_access, which checks the admin policy toggle.

SPORT = st.session_state.SPORT
con = st.session_state.con
DB_REVISION = st.session_state.DB_REVISION
SCHEMA = SPORT.schema
V = SPORT.vocab
C = SPORT.C

player_picker = ui_widgets.player_picker

st.markdown("# Play Grids")
st.caption("Select a saved grid to play. You have 9 guesses, and a player can only be used once. You have 1 undo.")

def render_grid_selection():
    sources = ["Saved Grids"]
    if SPORT.key == "afl":
        sources.append("Past Grids (Gridley)")
    
    default_source = "Past Grids (Gridley)" if "Past Grids (Gridley)" in sources else "Saved Grids"
    source = st.pills("Grid source", sources, key=SPORT.k("play_source"), label_visibility="collapsed", default=default_source)
    if not source:
        source = default_source
    
    if source == "Saved Grids":
        saved = accounts.list_all_grids(SPORT.key)
        if not saved:
            st.info("No saved grids found. Go to Grid Solver to create and save some grids.")
            return
        by_id = {item["id"]: item for item in saved}
        chosen_id = st.selectbox(
            "Saved grid", list(by_id), key=SPORT.k("play_saved_grid_id"),
            format_func=lambda grid_id: by_id[grid_id]["name"])
        
        if st.button("Load Grid", key=SPORT.k("play_load_grid"), type="primary", use_container_width=True):
            try:
                grid_data = accounts.load_any_grid(chosen_id)
                st.session_state[SPORT.k("play_loaded_grid")] = grid_data
                st.session_state[SPORT.k("play_state")] = {
                    "guesses": 0, "undo_used": False, "answers": {}, "history": [], "cell": None
                }
                st.rerun()
            except accounts.AccountError as exc:
                st.error(str(exc))
    else:
        try:
            # Fetch all grids without LIMIT 7 and across all sources
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
            
        st.write("### Past Grids Collection")
        
        # Use a dictionary to map a friendly label to the grid dictionary
        grid_options = {f"🔴 {g['source']} #{g['grid_num']} • {g['date']}": g for g in past_grids}
        
        chosen_label = st.selectbox(
            "Select a Grid",
            options=list(grid_options.keys()),
            key=SPORT.k("play_past_grid_id")
        )
        
        if st.button("Load Past Grid", key=SPORT.k("play_load_past_grid"), type="primary", use_container_width=True):
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

                    st.session_state[SPORT.k("play_loaded_grid")] = {"rows": rows, "cols": cols, "grid_num": picked["grid_num"]}
                    st.session_state[SPORT.k("play_state")] = {
                        "guesses": 0, "undo_used": False, "answers": {}, "history": [], "cell": None
                    }
                    st.rerun()

@st.cache_data(ttl=3600, show_spinner=False)
def _get_newest_grid_cached(sport_key, db_revision):
    if sport_key != "afl": return None
    try:
        import db_pool
        import sports
        from afl import historic_grids as HG
        local_con = db_pool.get_con("afl.db", db_revision)
        local_sport = sports.get(sport_key)
        cur = local_con.execute("SELECT grid_num, date, source, rows_json, cols_json FROM historic_grids WHERE source='Gridley' ORDER BY date DESC, grid_num DESC LIMIT 1")
        latest = cur.fetchone()
        if not latest: return None
        picked = {"grid_num": latest[0], "date": latest[1], "source": latest[2], "rows_json": latest[3], "cols_json": latest[4]}
        cols_labels = tuple(json.loads(picked["cols_json"]))
        rows_labels = tuple(json.loads(picked["rows_json"]))
        grid = HG.HistoricGrid(
            number=picked["grid_num"], date=picked["date"],
            source=picked["source"], rows=rows_labels, cols=cols_labels,
        )
        report = HG.analyse(grid, local_con, local_sport)
        if report.authentic_playable:
            rows = [(item.display, item.constraint) for item in report.rows]
            cols = [(item.display, item.constraint) for item in report.cols]
            return {"rows": rows, "cols": cols, "grid_num": picked["grid_num"]}
    except Exception as e:
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

@st.dialog("Select a Grid")
def grid_selector_dialog():
    render_grid_selection()

loaded = st.session_state.get(SPORT.k("play_loaded_grid"))
if not loaded and not st.session_state.get(SPORT.k("play_explicitly_cleared")):
    if auto_load_newest_grid():
        loaded = st.session_state.get(SPORT.k("play_loaded_grid"))

if not loaded:
    st.markdown("### No Grid Loaded")
    if st.button("Select a Grid", key=SPORT.k("play_empty_select_btn")):
        grid_selector_dialog()
    st.stop()

state = st.session_state[SPORT.k("play_state")]
rows_def = loaded["rows"]
cols_def = loaded["cols"]

# Top action bar
col_btn, _ = st.columns([1, 4])
with col_btn:
    grid_title = "Select Grid"
    if "grid_num" in loaded:
        grid_title = f"Gridley #{loaded['grid_num']}"
    if st.button(f"📅 {grid_title}", key=SPORT.k("play_select_btn"), use_container_width=True):
        grid_selector_dialog()

st.markdown("---")

st.markdown('<div class="grid-board-container">', unsafe_allow_html=True)

# Draw the board
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
                f"<div class='square-name'>{answered['name']}</div>"
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
            state["cell"] = (r, c)
            st.rerun()

st.markdown("</div>", unsafe_allow_html=True)

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
    selected = player_picker(SPORT.k("game_pick", r, c), SPORT, DB_REVISION)
    
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
