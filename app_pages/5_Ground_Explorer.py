import streamlit as st
import importlib
try:
    module = importlib.import_module(st.session_state.SPORT.ground_explorer_module)
    module.ground_explorer_page(st.session_state.SPORT, st.session_state.con)
except Exception as e:
    st.error(f"Ground Explorer not available: {e}")
