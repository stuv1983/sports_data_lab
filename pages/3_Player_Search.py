import streamlit as st
import explore
import ui_widgets
from functools import partial
picker = partial(ui_widgets.player_picker, sport=st.session_state.SPORT, db_revision=st.session_state.DB_REVISION)
explore.player_page(st.session_state.SPORT, st.session_state.con, picker)
