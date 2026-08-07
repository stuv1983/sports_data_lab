import streamlit as st
import importlib
module = importlib.import_module(st.session_state.SPORT.awards_page_module or "afl.awards_page")
module.awards_page(st.session_state.SPORT, st.session_state.con)
