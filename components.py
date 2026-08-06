"""Shared clickable-table-to-overlay widgets, for any page with a table.

Grid Solver was the first page to let a click on a results row open a
detail dialog over the board (app.py's show_player_dialog). This module is
that pattern lifted out so Past Games, Advanced Search and Stats Explorer
can offer the same thing without a fourth copy of the same twelve lines,
and so a future overlay kind only has to be added once.

There are four kinds now, one per thing a row can be: a player, a match, a
season (optionally a club's season) and a club. A table opens the overlay
for whatever its rows *are* -- Streamlit's dataframe selects a row, not a
cell, so a row naming both a player and a season has to pick one, and the
right one is the subject of the table.

One dialog per script run
-------------------------
Streamlit allows exactly one dialog to open per rerun, so an overlay body
must never itself contain a clickable table. Renderers that appear both on
a page and inside a dialog take a `nested` flag and fall back to a plain
table when it is set -- see explore.render_player_profile.
"""

from __future__ import annotations

from typing import Callable, Sequence

import streamlit as st

import explore
import overlays


#: Session-state keys tracking which table's selection is the live one.
_ROWS = "_overlay_rows"          # widget key -> the row it currently holds
_ACTIVE = "_overlay_active"      # widget key whose selection changed last


def _select(df, key, dataframe_kwargs):
    """Draw a selectable table; return the picked row when it owns the overlay.

    A page can carry several clickable tables, and a dataframe holds its
    selection until it is clicked again. Select a season, dismiss the
    overview, then select a player and *both* tables have a selection --
    which used to take the page down, because Streamlit allows exactly one
    dialog per script run.

    So the table whose selection just changed owns the overlay, and the
    others stand down until they are clicked again. When the change is
    noticed after some earlier table has already claimed the run, the run
    is restarted: on the second pass the earlier table is no longer the
    active one and the click the reader actually made is the one that
    opens.

    Not opening on every rerun is a fix in its own right. The old version
    reopened a dismissed dialog the moment anything else on the page was
    touched, because the row underneath it was still selected.
    """
    kwargs = {"hide_index": True, "width": "stretch"}
    kwargs.update(dataframe_kwargs)
    table = st.dataframe(
        df, on_select="rerun", selection_mode="single-row", key=key, **kwargs)
    picked = table.selection.rows if table and table.selection else []
    row = picked[0] if picked else None

    rows = st.session_state.setdefault(_ROWS, {})
    if rows.get(key, "unset") != row:
        rows[key] = row
        if row is not None:
            previous = st.session_state.get(_ACTIVE)
            st.session_state[_ACTIVE] = key
            if previous is not None and previous != key:
                st.rerun()
        elif st.session_state.get(_ACTIVE) == key:
            st.session_state[_ACTIVE] = None

    if row is None or st.session_state.get(_ACTIVE) != key:
        return None
    return row


# --------------------------------------------------------------- player

@st.dialog("Player", width="large")
def _player_dialog(sport, con, pid, key_prefix):
    explore.render_player_profile(sport, con, pid, key_prefix=key_prefix,
                                  heading_level="###", nested=True)


def clickable_player_table(df, player_ids: Sequence, sport, con, key: str,
                           key_prefix: str | None = None, **dataframe_kwargs):
    """Render `df` and open a player dialog when a row is clicked.

    `player_ids` must align with `df`'s rows position-for-position -- it is
    typically a hidden id column pulled off the frame before display, the
    same way Grid Solver keeps `pids` alongside the columns it shows.

    An id may be None. An award roll lists every winner, including the ones
    whose name could not be resolved to a player in this database, and
    those rows are still worth showing -- they just have no career to open.
    """
    row = _select(df, key, dataframe_kwargs)
    if row is None:
        return
    pid = player_ids[row] if row < len(player_ids) else None
    if pid is None:
        st.info("That row could not be linked to a player in this database, "
                "so there is no career to show.")
        return
    _player_dialog(sport, con, pid, key_prefix or key)


# ---------------------------------------------------------------- match

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
    row = _select(df, key, dataframe_kwargs)
    if row is not None:
        _match_dialog(matches[row], render_body or _default_match_body)


# ----------------------------------------------------------------- game

@st.dialog("Game", width="large")
def _game_dialog(sport, con, record, stat):
    overlays.game_card(sport, con, record, stat=stat)


def clickable_game_table(df, sport, con, key: str, stat=None,
                         **dataframe_kwargs):
    """Render a table of `games` rows, each opening its own scorecard.

    Unlike the match table this needs no parallel list: a game row carries
    everything the card shows, so the clicked row of the frame *is* the
    record.
    """
    row = _select(df, key, dataframe_kwargs)
    if row is not None:
        _game_dialog(sport, con, df.iloc[row].to_dict(), stat)


# --------------------------------------------------------------- season

@st.dialog("Season", width="large")
def _season_dialog(sport, con, season, club):
    overlays.season_overview(sport, con, season, club=club)


def clickable_season_table(df, seasons: Sequence, sport, con, key: str,
                           clubs: Sequence | None = None,
                           **dataframe_kwargs):
    """Render `df` and open a season overview when a row is clicked.

    `seasons` aligns with the rows. `clubs` optionally does too: a row of a
    player's career or of a club's record names a club as well as a season,
    and the overview then shows that club's part in the season alongside
    the champion.
    """
    row = _select(df, key, dataframe_kwargs)
    if row is None:
        return
    season = seasons[row] if row < len(seasons) else None
    if season is None:
        return
    club = clubs[row] if clubs is not None and row < len(clubs) else None
    _season_dialog(sport, con, season, club)


# ----------------------------------------------------------------- club

@st.dialog("Club", width="large")
def _club_dialog(sport, con, club):
    overlays.club_overview(sport, con, club)


def clickable_club_table(df, clubs: Sequence, sport, con, key: str,
                         **dataframe_kwargs):
    """Render `df` and open a club overview when a row is clicked."""
    row = _select(df, key, dataframe_kwargs)
    if row is None:
        return
    club = clubs[row] if row < len(clubs) else None
    if club:
        _club_dialog(sport, con, club)
