import os

os.makedirs('pages', exist_ok=True)

pages = {
    '1_Home.py': '''import streamlit as st
import explore
explore.home_page(st.session_state.SPORT, st.session_state.con, st.session_state.DRAFT_OK, st.session_state.AWARDS_OK)
''',
    '2_Account.py': '''import streamlit as st
import accounts_ui
import accounts
user = accounts.get_user(st.session_state.get("auth_user_id"))
accounts_ui.account_page(user)
''',
    '3_Player_Search.py': '''import streamlit as st
import explore
import ui_widgets
from functools import partial
picker = partial(ui_widgets.player_picker, sport=st.session_state.SPORT, db_revision=st.session_state.DB_REVISION)
explore.player_page(st.session_state.SPORT, st.session_state.con, picker)
''',
    '4_Club_Explorer.py': '''import streamlit as st
from afl import club_explorer
club_explorer.club_explorer_page(st.session_state.SPORT, st.session_state.con)
''',
    '5_Ground_Explorer.py': '''import streamlit as st
import importlib
try:
    module = importlib.import_module(st.session_state.SPORT.ground_explorer_module)
    module.ground_explorer_page(st.session_state.SPORT, st.session_state.con)
except Exception as e:
    st.error(f"Ground Explorer not available: {e}")
''',
    '6_Past_Games.py': '''import streamlit as st
from afl import past_games
past_games.past_games_page(st.session_state.SPORT, st.session_state.con)
''',
    '7_Awards.py': '''import streamlit as st
import importlib
module = importlib.import_module(st.session_state.SPORT.awards_page_module or "afl.awards_page")
module.awards_page(st.session_state.SPORT, st.session_state.con)
''',
    '8_Advanced_Search.py': '''import streamlit as st
import advanced_search
advanced_search.search_page(st.session_state.SPORT, st.session_state.con)
''',
    '9_Stats_Explorer.py': '''import streamlit as st
import explore
explore.leaderboard_page(st.session_state.SPORT, st.session_state.con)
''',
    '10_Random_Discovery.py': '''import streamlit as st
import explore
explore.random_page(st.session_state.SPORT, st.session_state.con)
''',
    '12_Game_Lab.py': '''import streamlit as st
import explore
import ui_widgets
from functools import partial
picker = partial(ui_widgets.player_picker, sport=st.session_state.SPORT, db_revision=st.session_state.DB_REVISION)
explore.game_lab_page(st.session_state.SPORT, st.session_state.con, picker)
''',
    '13_Database_Health.py': '''import streamlit as st
import health
health.health_page(st.session_state.SPORT, st.session_state.con)
''',
    '14_Admin.py': '''import streamlit as st
import accounts_ui
import accounts
user = accounts.get_user(st.session_state.get("auth_user_id"))
if user and user.is_admin:
    accounts_ui.admin_page(user)
else:
    st.error("Administrator access required.")
'''
}

for filename, content in pages.items():
    with open(os.path.join('pages', filename), 'w') as f:
        f.write(content)
