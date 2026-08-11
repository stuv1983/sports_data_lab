import os
import sqlite3
import streamlit as st

import accounts
import accounts_ui
import branding
import db_pool
import sports
import theme

_key = st.session_state.get("sport", sports.DEFAULT)
_pre = sports.get(_key)
st.set_page_config(page_title=f"Sports Data Lab — {_pre.label.replace(' Data Lab', '')}", page_icon=branding.page_icon(_pre), layout="wide", initial_sidebar_state="expanded")

# Before the verify handler below: a verification link opened on a fresh
# install must find the users table, not "no such table".
accounts.ensure_schema()

# Consume the token, then drop it from the URL. Left in place it is checked
# again on every later rerun, and the second check always fails -- successful
# verification clears the token -- so the first click a reader made anywhere
# on the page turned "verified!" into "invalid or expired link".
if "verify" in st.query_params:
    st.session_state["verify_result"] = accounts.verify_email(
        st.query_params["verify"])
    del st.query_params["verify"]

_verified = st.session_state.pop("verify_result", None)
if _verified is True:
    st.success("Your email has been verified! You can now log in.")
elif _verified is False:
    st.error("Invalid or expired verification link. Use **Resend verification "
             "email** on the log in form to get a fresh one.")


SPORT = sports.picker(st)
st.session_state.SPORT = SPORT

if not SPORT.exists():
    st.title(f"{SPORT.icon}  {SPORT.label}")
    st.info(SPORT.missing_db_hint or f"{SPORT.label} is coming soon. Pick another sport in the sidebar.")
    st.stop()

def db_revision(db):
    # The path rides in the revision so every cache keyed on it -- result
    # frames, picker options -- is distinct per database file, not merely
    # per (mtime, size) coincidence. Every revision helper in the app
    # returns this same shape; db_pool keys on (db, revision) either way.
    stat = os.stat(db)
    return str(db), stat.st_mtime_ns, stat.st_size

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


@st.cache_data(show_spinner=False)
def _cached_layers(sport_key, revision):
    """Layer probes cost a handful of queries; run them once per revision."""
    sport = sports.get(sport_key)
    return sport.layers(db_pool.get_con(sport.db, revision))


@st.cache_data(show_spinner=False)
def _cached_status(sport_key, revision):
    """The sidebar's status rows COUNT(*) the games table -- once, not per rerun."""
    sport = sports.get(sport_key)
    status_con = db_pool.get_con(sport.db, revision)
    return sport.status(status_con), list(sport.missing_layer_hints(status_con))


LAYERS = _cached_layers(SPORT.key, DB_REVISION)
st.session_state.DRAFT_OK = LAYERS.draft
st.session_state.AWARDS_OK = LAYERS.awards
st.session_state.CLUB_DATA_OK = LAYERS.club_data
st.session_state.FAMILY_RELATIONSHIPS_OK = LAYERS.family_relationships
st.session_state.AVAILABLE = LAYERS.builders

st.sidebar.markdown(f"<div class='brand'>{SPORT.label}</div><div class='brand-sub'>SEARCH · EXPLORE · PLAY</div>", unsafe_allow_html=True)

with st.sidebar.expander("Database status", expanded=False):
    _status_rows, _layer_hints = _cached_status(SPORT.key, DB_REVISION)
    lines = "<br>".join(f"{label}: <b>{value}</b>" for label, value in _status_rows)
    st.markdown(f"<div class='status-row'>{lines}</div>", unsafe_allow_html=True)
    for _label, hint in _layer_hints:
        st.caption(hint)
    if SPORT.has_club_explorer and not LAYERS.club_data and SPORT.club_data_hint:
        st.caption(SPORT.club_data_hint)
    if SPORT.has_club_explorer and not LAYERS.family_relationships and SPORT.family_hint:
        st.caption(SPORT.family_hint)

PALETTE = theme.controls(st, SPORT.key)
st.markdown(theme.css(PALETTE), unsafe_allow_html=True)

# The favicon is set above by set_page_config; this adds what it cannot --
# the iOS home-screen icon and the web-app meta tags, tinted to the palette
# the reader actually chose.
branding.apply(st, SPORT, theme_color=PALETTE.get("board", ""))

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

# Page access control
_PROTECTED_PAGES = {
    "Play Grids": "play_grids",
    "Grid Solver": "grid_solver",
    "Advanced Search": "advanced_search",
    "Game Lab": "game_lab",
    "Database Health": "database_health",
}

# Build Navigation
pages = {
    "Discover": [
        st.Page("app_pages/1_Home.py", title="Home", icon=":material/home:"),
        st.Page("app_pages/10_Random_Discovery.py", title="Random Discovery", icon=":material/shuffle:"),
    ],
    "Explore": [
        st.Page("app_pages/3_Player_Search.py", title="Player Search", icon=":material/person_search:"),
        st.Page("app_pages/9_Stats_Explorer.py", title="Stats Explorer", icon=":material/bar_chart:"),
        st.Page("app_pages/4_Club_Explorer.py", title=f"{SPORT.vocab.club.capitalize()} Explorer", icon=":material/shield:"),
        st.Page("app_pages/6_Past_Games.py", title=f"Past {SPORT.vocab.games.capitalize()}", icon=":material/history:"),
        st.Page("app_pages/7_Awards.py", title="Awards", icon=":material/emoji_events:"),
    ],
    "Play": [],
    "Account & Settings": [
        st.Page("app_pages/16_Profile.py", title="My Profile", icon=":material/person:"),
        st.Page("app_pages/2_Account.py", title="Account", icon=":material/account_circle:"),
    ]
}

if SPORT.has_ground_explorer:
    pages["Explore"].insert(3, st.Page("app_pages/5_Ground_Explorer.py", title="Ground Explorer", icon=":material/stadium:"))

if SPORT.has_draft_page:
    pages["Explore"].append(st.Page("app_pages/18_Draft.py", title="Draft", icon=":material/how_to_vote:"))
    
if accounts.can_access(AUTH_USER, _PROTECTED_PAGES["Advanced Search"]):
    pages["Explore"].insert(1, st.Page("app_pages/8_Advanced_Search.py", title="Advanced Search", icon=":material/manage_search:"))

if accounts.can_access(AUTH_USER, _PROTECTED_PAGES["Grid Solver"]):
    pages["Play"].append(st.Page("app_pages/11_Grid_Solver.py", title="Grid Solver", icon=":material/grid_on:"))
if accounts.can_access(AUTH_USER, _PROTECTED_PAGES["Play Grids"]):
    pages["Play"].append(st.Page("app_pages/15_Play_Grids.py", title="Play Grids", icon=":material/sports_esports:"))
pages["Play"].append(st.Page("app_pages/17_Leaderboards.py", title="Leaderboards", icon=":material/leaderboard:"))
if accounts.can_access(AUTH_USER, _PROTECTED_PAGES["Game Lab"]):
    pages["Play"].append(st.Page("app_pages/12_Game_Lab.py", title="Game Lab", icon=":material/science:"))
    
if not pages["Play"]:
    del pages["Play"]

if accounts.can_access(AUTH_USER, _PROTECTED_PAGES["Database Health"]):
    pages["Account & Settings"].append(st.Page("app_pages/13_Database_Health.py", title="Database Health", icon=":material/health_and_safety:"))

if AUTH_USER and AUTH_USER.is_admin:
    pages["Account & Settings"].append(st.Page("app_pages/14_Admin.py", title="Admin", icon=":material/admin_panel_settings:"))

pg = st.navigation(pages)

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
