"""Shared clickable-table-to-overlay widgets, for any page with a table.

Grid Solver was the first page to let a click on a results row open a
detail dialog over the board (app.py's show_player_dialog). This module is
that pattern lifted out so Past Games, Advanced Search and Stats Explorer
can offer the same thing without a fourth copy of the same twelve lines,
and so a future overlay kind only has to be added once.
"""

from __future__ import annotations

from typing import Callable, Sequence

import streamlit as st

import explore


@st.dialog("Player", width="large")
def _player_dialog(sport, con, pid, key_prefix):
    explore.render_player_profile(sport, con, pid, key_prefix=key_prefix,
                                  heading_level="###")


def clickable_player_table(df, player_ids: Sequence, sport, con, key: str,
                           key_prefix: str | None = None, **dataframe_kwargs):
    """Render `df` and open a player dialog when a row is clicked.

    `player_ids` must align with `df`'s rows position-for-position -- it is
    typically a hidden id column pulled off the frame before display, the
    same way Grid Solver keeps `pids` alongside the columns it shows.
    """
    kwargs = {"hide_index": True, "width": "stretch"}
    kwargs.update(dataframe_kwargs)
    table = st.dataframe(
        df, on_select="rerun", selection_mode="single-row", key=key,
        **kwargs)
    picked = table.selection.rows if table and table.selection else []
    if picked:
        _player_dialog(sport, con, player_ids[picked[0]],
                       key_prefix or key)


@st.dialog("Match", width="large")
def _match_dialog(match, render_body: Callable):
    render_body(match)


def _default_match_body(match) -> None:
    """Fallback dialog body when the caller has no richer renderer.

    Works off `afl.club_history.Match`'s public fields, which any
    club_history-backed sport already produces via `search_matches`.
    """
    home = getattr(match, "club_id", None)
    away = getattr(match, "opponent_id", None)
    st.markdown(f"### {home} vs {away}" if home and away else "### Match")
    c1, c2, c3 = st.columns(3)
    c1.metric("Season", getattr(match, "season", "—"))
    c2.metric("Round", getattr(match, "round", "—"))
    c3.metric("Score", getattr(match, "score", "—"))
    st.write(f"**Venue:** {getattr(match, 'venue', '—')}")
    date = getattr(match, "match_date", None)
    if date:
        st.write(f"**Date:** {date}")
    attendance = getattr(match, "attendance", None)
    if attendance:
        st.write(f"**Crowd:** {attendance:,}")
    result = getattr(match, "result", None)
    if result:
        st.write(f"**Result:** {result}")


def clickable_match_table(df, matches: Sequence, key: str,
                          render_body: Callable | None = None,
                          **dataframe_kwargs):
    """Render `df` and open a match dialog when a row is clicked.

    `matches` must align with `df`'s rows position-for-position, same
    convention as `clickable_player_table`. `render_body(match)` draws the
    dialog's contents; the default shows the fields every Match carries.
    """
    kwargs = {"hide_index": True, "width": "stretch"}
    kwargs.update(dataframe_kwargs)
    table = st.dataframe(
        df, on_select="rerun", selection_mode="single-row", key=key,
        **kwargs)
    picked = table.selection.rows if table and table.selection else []
    if picked:
        _match_dialog(matches[picked[0]], render_body or _default_match_body)
