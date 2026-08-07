import streamlit as st
import explore
explore.home_page(st.session_state.SPORT, st.session_state.con, st.session_state.DRAFT_OK, st.session_state.AWARDS_OK)
