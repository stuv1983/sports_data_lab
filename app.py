import os
import sqlite3
import streamlit as st

import accounts
import accounts_ui
import auth_session
import branding
import db_pool
import sports
import theme
import ui_preferences

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

# Before the user is read: a tab opened from an already-logged-in browser
# starts with an empty session_state and has to recover the log in from its
# cookie, or the whole page below is built for a stranger.
auth_session.restore()

AUTH_USER = accounts.get_user(st.session_state.get("auth_user_id"))
USER_PREFERENCES = ui_preferences.normalise(
    accounts.get_user_preferences(AUTH_USER.id) if AUTH_USER else {}
)


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

if USER_PREFERENCES["navigation"]["show_database_status"]:
    with st.sidebar.expander("Database status", expanded=False):
        _status_rows, _layer_hints = _cached_status(SPORT.key, DB_REVISION)
        lines = "<br>".join(
            f"{label}: <b>{value}</b>" for label, value in _status_rows
        )
        st.markdown(
            f"<div class='status-row'>{lines}</div>", unsafe_allow_html=True
        )
        for _label, hint in _layer_hints:
            st.caption(hint)
        if SPORT.has_club_explorer and not LAYERS.club_data and SPORT.club_data_hint:
            st.caption(SPORT.club_data_hint)
        if (SPORT.has_club_explorer and not LAYERS.family_relationships
                and SPORT.family_hint):
            st.caption(SPORT.family_hint)

PALETTE = theme.current_palette(
    st, SPORT.key, USER_PREFERENCES, AUTH_USER.id if AUTH_USER else None
)
st.markdown(theme.css(PALETTE), unsafe_allow_html=True)

# The favicon is set above by set_page_config; this adds what it cannot --
# the iOS home-screen icon and the web-app meta tags, tinted to the palette
# the reader actually chose.
branding.apply(st, SPORT, theme_color=PALETTE.get("board", ""))

with st.sidebar.expander(f"Account · {AUTH_USER.display_name}" if AUTH_USER else "Join or log in", expanded=False):
    if AUTH_USER:
        st.caption(f"{AUTH_USER.email} · {AUTH_USER.role}")
        if st.button("Log out", key="account_logout"):
            # Retires the browser's token too: a log out that left the
            # cookie standing would log this tab out and the next one
            # straight back in.
            auth_session.forget()
            st.rerun()
    else:
        login_tab, join_tab = st.tabs(["Log in", "Join"])
        with login_tab:
            accounts_ui._login_form()
        with join_tab:
            accounts_ui._join_form()

# Page access control
_PROTECTED_PAGES = {
    "Play grids": "play_grids",
    "Grid solver": "grid_solver",
    "Advanced search": "advanced_search",
    "Game lab": "game_lab",
    "Database health": "database_health",
}


def _gated_page(feature, path, title, icon):
    """A protected page, hidden from the menu when it is out of reach.

    Hidden, not absent. A page left out of the catalogue is a URL
    Streamlit has never heard of, so opening one in a second tab -- which
    starts as a fresh session, before the cookie has been read back --
    answered "page cannot be found" instead of the log in gate below.
    Registering it means the URL always resolves; the gate under
    ``st.navigation`` is what decides whether the page may run, exactly as
    before, so this hides a door rather than unlocking one.
    """
    return st.Page(
        path, title=title, icon=icon,
        visibility=("visible" if accounts.can_access(AUTH_USER, feature)
                    else "hidden"),
    )


# Build the authorized catalogue first. Member preferences only filter and
# reorder this list; they never grant access to a protected page.
page_entries = {
    "Discover": [
        ("home", st.Page("app_pages/1_Home.py", title="Home", icon=":material/home:")),
        ("random_discovery", st.Page("app_pages/10_Random_Discovery.py", title="Random discovery", icon=":material/shuffle:")),
    ],
    "Explore": [
        ("player_search", st.Page("app_pages/3_Player_Search.py", title="Player search", icon=":material/person_search:")),
        ("stats_explorer", st.Page("app_pages/9_Stats_Explorer.py", title="Stats explorer", icon=":material/bar_chart:")),
        ("visual_explorer", st.Page("app_pages/19_Visual_Explorer.py", title="Visual explorer", icon=":material/insights:")),
        ("club_explorer", st.Page("app_pages/4_Club_Explorer.py", title=f"{SPORT.vocab.club.capitalize()} explorer", icon=":material/shield:")),
        ("past_games", st.Page("app_pages/6_Past_Games.py", title=f"Past {SPORT.vocab.games}", icon=":material/history:")),
        ("awards", st.Page("app_pages/7_Awards.py", title="Awards", icon=":material/emoji_events:")),
    ],
    "Play": [],
    "Account & settings": [
        ("profile", st.Page("app_pages/16_Profile.py", title="My profile", icon=":material/person:")),
        ("account", st.Page("app_pages/2_Account.py", title="Account", icon=":material/account_circle:")),
    ]
}

if SPORT.has_ground_explorer:
    # Index 4 keeps Ground explorer directly after Club explorer,
    # which Visual Explorer's entry above pushed along by one.
    page_entries["Explore"].insert(4, ("ground_explorer", st.Page("app_pages/5_Ground_Explorer.py", title="Ground explorer", icon=":material/stadium:")))

if SPORT.has_draft_page:
    page_entries["Explore"].append(("draft", st.Page("app_pages/18_Draft.py", title="Draft", icon=":material/how_to_vote:")))
    
page_entries["Explore"].insert(1, ("advanced_search", _gated_page(
    _PROTECTED_PAGES["Advanced search"], "app_pages/8_Advanced_Search.py",
    "Advanced search", ":material/manage_search:")))

page_entries["Play"].append(("grid_solver", _gated_page(
    _PROTECTED_PAGES["Grid solver"], "app_pages/11_Grid_Solver.py",
    "Grid solver", ":material/grid_on:")))
page_entries["Play"].append(("play_grids", _gated_page(
    _PROTECTED_PAGES["Play grids"], "app_pages/15_Play_Grids.py",
    "Play grids", ":material/sports_esports:")))
page_entries["Play"].append(("leaderboards", st.Page("app_pages/17_Leaderboards.py", title="Leaderboards", icon=":material/leaderboard:")))
page_entries["Play"].append(("game_lab", _gated_page(
    _PROTECTED_PAGES["Game lab"], "app_pages/12_Game_Lab.py",
    "Game lab", ":material/science:")))

page_entries["Account & settings"].append(("database_health", _gated_page(
    _PROTECTED_PAGES["Database health"], "app_pages/13_Database_Health.py",
    "Database health", ":material/health_and_safety:")))

# Admin carries its own "administrator access required" check, which is why
# it can be registered for everyone and merely hidden here.
page_entries["Account & settings"].append((
    "admin",
    st.Page("app_pages/14_Admin.py", title="Admin",
            icon=":material/admin_panel_settings:",
            visibility=("visible" if AUTH_USER and AUTH_USER.is_admin
                        else "hidden"))))

pages = ui_preferences.apply_navigation(page_entries, USER_PREFERENCES)
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
