import streamlit as st
from afl import past_games
past_games.past_games_page(st.session_state.SPORT, st.session_state.con)
