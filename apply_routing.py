import os

# Fix 11_Grid_Solver.py
with open(os.path.join("pages", "11_Grid_Solver.py"), "r", encoding="utf-8") as f:
    grid_content = f.read()

# Add AUTH_USER and axis_widget
insertion = """
AUTH_USER = accounts.get_user(st.session_state.get("auth_user_id"))

def axis_widget(key, default_type, defaults):
    return ui_widgets.axis_widget(key, default_type, defaults, SPORT, DB_REVISION, AVAILABLE)

"""

grid_content = grid_content.replace("player_picker = ui_widgets.player_picker\n", "player_picker = ui_widgets.player_picker\n" + insertion)

with open(os.path.join("pages", "11_Grid_Solver.py"), "w", encoding="utf-8") as f:
    f.write(grid_content)

# Rewrite app.py
app_content = """import os
import sqlite3
import streamlit as st

import accounts
import accounts_ui
import db_pool
import sports
import theme

_key = st.session_state.get("sport", sports.DEFAULT)
_pre = sports.get(_key)
st.set_page_config(page_title=f"Sports Data Lab — {_pre.label.replace(' Data Lab', '')}", page_icon=_pre.icon, layout="wide", initial_sidebar_state="expanded")

SPORT = sports.picker(st)
st.session_state.SPORT = SPORT

if not SPORT.exists():
    st.title(f"{SPORT.icon}  {SPORT.label}")
    st.info(SPORT.missing_db_hint or f"{SPORT.label} is coming soon. Pick another sport in the sidebar.")
    st.stop()

def db_revision(db):
    stat = os.stat(db)
    return stat.st_mtime_ns, stat.st_size

try:
    DB_REVISION = db_revision(SPORT.db)
    con = db_pool.get_con(SPORT.db, DB_REVISION)
    con.execute(f"SELECT 1 FROM {SPORT.schema.players} LIMIT 1")
except (OSError, sqlite3.OperationalError):
    st.error(SPORT.missing_db_hint)
    st.stop()

try:
    SPORT.C.require_schema(con)
except RuntimeError as e:
    st.error(str(e))
    st.stop()

st.session_state.DB_REVISION = DB_REVISION
st.session_state.con = con

LAYERS = SPORT.layers(con)
st.session_state.DRAFT_OK = LAYERS.draft
st.session_state.AWARDS_OK = LAYERS.awards
st.session_state.CLUB_DATA_OK = LAYERS.club_data
st.session_state.FAMILY_RELATIONSHIPS_OK = LAYERS.family_relationships
st.session_state.AVAILABLE = LAYERS.builders

st.sidebar.markdown(f"<div class='brand'>{SPORT.label}</div><div class='brand-sub'>SEARCH · EXPLORE · PLAY</div>", unsafe_allow_html=True)

with st.sidebar.expander("Database status", expanded=False):
    lines = "<br>".join(f"{label}: <b>{value}</b>" for label, value in SPORT.status(con))
    st.markdown(f"<div class='status-row'>{lines}</div>", unsafe_allow_html=True)
    for _label, hint in SPORT.missing_layer_hints(con):
        st.caption(hint)
    if SPORT.has_club_explorer and not LAYERS.club_data and SPORT.club_data_hint:
        st.caption(SPORT.club_data_hint)
    if SPORT.has_club_explorer and not LAYERS.family_relationships and SPORT.family_hint:
        st.caption(SPORT.family_hint)

PALETTE = theme.controls(st, SPORT.key)
st.markdown(theme.css(PALETTE), unsafe_allow_html=True)

accounts.ensure_schema()
AUTH_USER = accounts.get_user(st.session_state.get("auth_user_id"))

with st.sidebar.expander(f"Account · {AUTH_USER.display_name}" if AUTH_USER else "Join or log in", expanded=False):
    if AUTH_USER:
        st.caption(f"{AUTH_USER.email} · {AUTH_USER.role}")
        if st.button("Log out", key="account_logout"):
            st.session_state.pop("auth_user_id", None)
            st.rerun()
    else:
        login_tab, join_tab = st.tabs(["Log in", "Join"])
        with login_tab:
            accounts_ui._login_form()
        with join_tab:
            accounts_ui._join_form()

# Build Navigation
pages = {
    "Discover": [
        st.Page("pages/1_Home.py", title="Home", icon=":material/home:"),
        st.Page("pages/10_Random_Discovery.py", title="Random Discovery", icon=":material/shuffle:"),
    ],
    "Explore": [
        st.Page("pages/3_Player_Search.py", title="Player Search", icon=":material/person_search:"),
        st.Page("pages/8_Advanced_Search.py", title="Advanced Search", icon=":material/manage_search:"),
        st.Page("pages/9_Stats_Explorer.py", title="Stats Explorer", icon=":material/bar_chart:"),
        st.Page("pages/4_Club_Explorer.py", title=f"{SPORT.vocab.club.capitalize()} Explorer", icon=":material/shield:"),
        st.Page("pages/6_Past_Games.py", title=f"Past {SPORT.vocab.games.capitalize()}", icon=":material/history:"),
        st.Page("pages/7_Awards.py", title="Awards", icon=":material/emoji_events:"),
    ],
    "Play": [
        st.Page("pages/11_Grid_Solver.py", title="Grid Solver", icon=":material/grid_on:"),
        st.Page("pages/12_Game_Lab.py", title="Game Lab", icon=":material/science:"),
    ],
    "Account & Settings": [
        st.Page("pages/2_Account.py", title="Account", icon=":material/account_circle:"),
        st.Page("pages/13_Database_Health.py", title="Database Health", icon=":material/health_and_safety:"),
    ]
}

if SPORT.has_ground_explorer:
    pages["Explore"].insert(4, st.Page("pages/5_Ground_Explorer.py", title="Ground Explorer", icon=":material/stadium:"))

if AUTH_USER and AUTH_USER.is_admin:
    pages["Account & Settings"].append(st.Page("pages/14_Admin.py", title="Admin", icon=":material/admin_panel_settings:"))

pg = st.navigation(pages)

# Page access control
_PROTECTED_PAGES = {
    "Grid Solver": "grid_solver",
    "Advanced Search": "advanced_search",
    "Game Lab": "game_lab",
    "Database Health": "database_health",
}

if pg.title in _PROTECTED_PAGES:
    feature = _PROTECTED_PAGES[pg.title]
    if not accounts.can_access(AUTH_USER, feature):
        st.markdown(f"# {pg.title}")
        if AUTH_USER:
            st.warning("Your account does not currently have access to this feature.")
        else:
            st.info("Join or log in to use this feature.")
            login_tab, join_tab = st.tabs(["Log in", "Create account"])
            with login_tab:
                accounts_ui._login_form("gate")
            with join_tab:
                accounts_ui._join_form("gate")
        st.stop()

pg.run()
"""

with open("app.py", "w", encoding="utf-8") as f:
    f.write(app_content)
