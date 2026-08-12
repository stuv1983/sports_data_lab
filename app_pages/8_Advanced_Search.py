"""Advanced Search hosts two complementary modes.

The query builder works on any allowlisted table the database has and was
the page's sole mode for a while; the player-search language is the
URL-addressable compiler the README documents (``?q=club:Fitzroy``), with
did-you-mean suggestions and per-sport examples. A deep link carrying a
query opens in the mode that can honour it -- ``?q=`` means Player search,
``?qb=`` means the query builder -- and a *changed* deep link re-routes an
existing session, while an unchanged URL never overrides the mode the
reader picked by hand.
"""

import streamlit as st

import advanced_search
import query_builder

SPORT = st.session_state.SPORT
con = st.session_state.con

BUILDER, PLAYER = "Query builder", "Player search"

st.markdown("# Advanced Search")

_mode_key = SPORT.k("advanced_mode")
_seen_key = SPORT.k("advanced_url_seen")

_player_query = advanced_search.url_query()
_builder_token = st.query_params.get("qb") or ""
_current_url = (("qb", _builder_token) if _builder_token
                else ("q", _player_query))

if _mode_key not in st.session_state:
    st.session_state[_mode_key] = (BUILDER if _builder_token
                                   else PLAYER if _player_query
                                   else BUILDER)
elif _current_url != st.session_state.get(_seen_key):
    if _builder_token:
        st.session_state[_mode_key] = BUILDER
    elif _player_query:
        st.session_state[_mode_key] = PLAYER
st.session_state[_seen_key] = _current_url

mode = st.segmented_control(
    "Mode", [BUILDER, PLAYER], key=_mode_key,
    label_visibility="collapsed", persist_state="session") or BUILDER

if mode == PLAYER:
    advanced_search.search_page(SPORT, con, heading=False)
else:
    query_builder.page(SPORT, heading=False)
