"""Shared clickable-table-to-overlay widgets, for any page with a table.

Grid Solver was the first page to let a click on a results row open a
detail dialog over the board (app.py's show_player_dialog). This module is
that pattern lifted out so Past Games, Advanced Search and Stats Explorer
can offer the same thing without a fourth copy of the same twelve lines,
and so a future overlay kind only has to be added once.

There are four kinds now, one per thing a row can be: a player, a match, a
season (optionally a club's season) and a club. A table opens the overlay
for whatever its rows *are*: player, season and club names are action cells,
while matches and individual games have an Open action. A row naming both a
player and a season makes its subject clickable, so an award roll opens the
player while a season record opens the season.

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


#: A ButtonColumn callback runs before Streamlit reruns the script. It leaves
#: the clicked table and row here for that table to consume during the rerun.
_PENDING = "_overlay_pending"


def _queue_click(event_key: str, table_key: str) -> None:
    """Remember one transient ButtonColumn click for the following rerun."""
    event = st.session_state.get(event_key)
    if event is not None and event.get("row") is not None:
        st.session_state[_PENDING] = {
            "key": table_key,
            "row": int(event["row"]),
        }


def _select(df, key, dataframe_kwargs, action_column: str | None = None):
    """Draw a table and return the row whose action cell was clicked.

    Streamlit dataframe selections persist after a dialog is dismissed. That
    makes clicking the same row again unreliable and means several tables can
    all retain selections at once. ButtonColumn events are transient instead:
    every click fires, including a second click on the same player or season,
    and only the table named by the callback can consume the event.

    When ``action_column`` is supplied, that visible value becomes the button
    (for example, Player or Season). Otherwise a compact Open column is added
    for rows such as matches and individual games.
    """
    frame = df.copy()
    kwargs = {"hide_index": True, "width": "stretch"}
    kwargs.update(dataframe_kwargs)

    if action_column not in frame.columns:
        action_column = "_Open"
        while action_column in frame.columns:
            action_column = "_" + action_column
        frame.insert(0, action_column, ":material/open_in_new: Open")
        if "column_order" in kwargs:
            kwargs["column_order"] = [
                action_column, *list(kwargs["column_order"])
            ]
        label = ""
        width = "small"
    else:
        # Button labels must be text. Keep the aligned seasons/player_ids
        # sequences untouched; only the displayed copy is converted.
        frame[action_column] = frame[action_column].map(
            lambda value: "" if value is None else str(value)
        )
        label = action_column
        width = None

    event_key = f"{key}__open"
    column_config = dict(kwargs.pop("column_config", {}) or {})
    column_config[action_column] = st.column_config.ButtonColumn(
        label,
        width=width,
        pinned=True,
        alignment="left",
        type="tertiary",
        on_click=_queue_click,
        args=(event_key, key),
        key=event_key,
    )
    st.dataframe(frame, key=key, column_config=column_config, **kwargs)

    pending = st.session_state.get(_PENDING)
    if not pending or pending.get("key") != key:
        return None
    del st.session_state[_PENDING]
    row = pending.get("row")
    return row if isinstance(row, int) and 0 <= row < len(frame) else None


# --------------------------------------------------------------- player

@st.dialog("Player", width="large")
def _player_dialog(sport, con, pid, key_prefix):
    explore.render_player_profile(sport, con, pid, key_prefix=key_prefix,
                                  heading_level="###", nested=True)


def clickable_player_table(df, player_ids: Sequence, sport, con, key: str,
                           key_prefix: str | None = None, **dataframe_kwargs):
    """Render `df` and open a player dialog when its player/name is clicked.

    `player_ids` must align with `df`'s rows position-for-position -- it is
    typically a hidden id column pulled off the frame before display, the
    same way Grid Solver keeps `pids` alongside the columns it shows.

    An id may be None. An award roll lists every winner, including the ones
    whose name could not be resolved to a player in this database, and
    those rows are still worth showing -- they just have no career to open.
    """
    action_column = next(
        (column for column in ("Player", "Name") if column in df.columns),
        None,
    )
    row = _select(df, key, dataframe_kwargs, action_column=action_column)
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
    """Render `df` and open a match dialog from its Open action.

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
    """Render `df` and open a season overview when its season is clicked.

    `seasons` aligns with the rows. `clubs` optionally does too: a row of a
    player's career or of a club's record names a club as well as a season,
    and the overview then shows that club's part in the season alongside
    the champion.
    """
    row = _select(df, key, dataframe_kwargs, action_column="Season")
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
    """Render `df` and open a club overview when its club name is clicked."""
    action_column = df.columns[0] if len(df.columns) else None
    row = _select(df, key, dataframe_kwargs, action_column=action_column)
    if row is None:
        return
    club = clubs[row] if row < len(clubs) else None
    if club:
        _club_dialog(sport, con, club)
