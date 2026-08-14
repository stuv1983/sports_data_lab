import streamlit as st
import importlib
try:
    module = importlib.import_module(st.session_state.SPORT.draft_page_module)
    module.draft_page(st.session_state.SPORT, st.session_state.con)
except Exception as e:
    st.error(f"Draft not available: {e}")
