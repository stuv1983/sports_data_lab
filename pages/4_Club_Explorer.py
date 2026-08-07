import streamlit as st
from afl import club_explorer
club_explorer.club_explorer_page(st.session_state.SPORT, st.session_state.con)
