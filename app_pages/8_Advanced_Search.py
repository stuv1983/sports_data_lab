"""Advanced Search hosts two complementary modes.

The query builder works on any table the database has and was the page's
sole mode for a while; the player-search language is the URL-addressable
compiler the README documents (``?q=club:Fitzroy``), with did-you-mean
suggestions and per-sport examples. A deep link carrying a query opens in
the mode that can honour it.
"""

import streamlit as st

import advanced_search
import query_builder

SPORT = st.session_state.SPORT
con = st.session_state.con

BUILDER, PLAYER = "Query builder", "Player search"

st.markdown("# Advanced Search")

_mode_key = SPORT.k("advanced_mode")
if _mode_key not in st.session_state:
    st.session_state[_mode_key] = (PLAYER if advanced_search.url_query()
                                   else BUILDER)
mode = st.segmented_control(
    "Mode", [BUILDER, PLAYER], key=_mode_key,
    label_visibility="collapsed") or BUILDER

if mode == PLAYER:
    advanced_search.search_page(SPORT, con, heading=False)
else:
    query_builder.page(SPORT, heading=False)
