"""Shared clickable-table-to-overlay widgets, for any page with a table.

Grid Solver was the first page to let a click on a results row open a
detail dialog over the board (app.py's show_player_dialog). This module is
that pattern lifted out so Past Games, Advanced Search and Stats Explorer
can offer the same thing without a fourth copy of the same twelve lines,
and so a future overlay kind only has to be added once.

There are five kinds now, one per thing a row can be: a player, a match, an
individual game, a season (optionally a club's season) and a club. A table
opens the overlay
for whatever its rows *are*: player, season and club names are action cells,
while matches and individual games have an Open action. A row naming both a
player and a season makes its subject clickable, so an award roll opens the
player while a season record opens the season.

One navigable dialog
--------------------
Streamlit allows exactly one dialog to open per rerun. Every card therefore
uses the same dialog and pushes the next player, club, season, match or game
onto a session-local history stack. Tables inside a card remain clickable;
their actions replace the dialog body, and Back restores the previous card.
"""

from __future__ import annotations

from typing import Callable, Mapping, Sequence

import pandas as pd
import streamlit as st

import explore
import overlays


#: A ButtonColumn callback runs before Streamlit reruns the script. It leaves
#: the clicked table and row here for that table to consume during the rerun.
_PENDING = "_overlay_pending"
_STACK = "_overlay_stack"


def _queue_click(event_key: str, table_key: str, action: str,
                 column: str) -> None:
    """Remember one transient ButtonColumn click for the following rerun."""
    event = st.session_state.get(event_key)
    if event is not None and event.get("row") is not None:
        st.session_state[_PENDING] = {
            "key": table_key,
            "row": int(event["row"]),
            "action": action,
            "column": column,
            "label": event.get("label"),
        }


def _club_actions(value):
    """Turn a club-history cell into one button or a menu of club buttons."""
    if value is None or (not isinstance(value, (list, tuple, set))
                         and pd.isna(value)):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    values = [part.strip() for part in text.replace("|", ",").split(",")]
    values = [part for part in values if part]
    return values if len(values) > 1 else values[0]


def _club_action_column(series):
    """Keep Arrow types uniform when any row needs a multi-club menu."""
    values = series.map(_club_actions)
    if any(isinstance(value, list) for value in values):
        return values.map(
            lambda value: value if isinstance(value, list)
            else ([value] if value else [])
        )
    return values


def _season_action_column(series):
    """Render a comma-separated season history as a button menu."""
    def split(value):
        if isinstance(value, (list, tuple, set)):
            return [str(part).strip() for part in value if str(part).strip()]
        if value is None or pd.isna(value):
            return []
        values = [part.strip() for part in str(value).replace("|", ",").split(",")]
        return [part for part in values if part]

    values = series.map(split)
    if any(len(value) > 1 for value in values):
        return values
    return values.map(lambda value: value[0] if value else "")


def _entity_columns(df, *, player=False, season=True, clubs=True) -> dict:
    """Return visible dataframe columns that represent overlay entities."""
    actions = {}
    if player:
        for column in ("Player", "Name"):
            if column in df.columns:
                actions[column] = "player"
                break
    if season:
        for column in (
            "Season", "Seasons", "Year", "First", "Last", "From", "To",
            "Debut", "Final",
        ):
            if column in df.columns:
                actions[column] = "season"
    if clubs:
        for column in (
            "Club", "Team", "Clubs", "Teams", "For", "Opponent",
            "Home", "Away", "Source team", "Current club", "Drafted by",
        ):
            if column in df.columns:
                actions[column] = "club"
    return actions


def _select(df, key, dataframe_kwargs,
            action_columns: Mapping[str, str] | None = None,
            add_open: bool = False):
    """Draw a table and return the entity action that was clicked.

    Streamlit dataframe selections persist after a dialog is dismissed. That
    makes clicking the same row again unreliable and means several tables can
    all retain selections at once. ButtonColumn events are transient instead:
    every click fires, including a second click on the same player or season,
    and only the table named by the callback can consume the event.

    ``action_columns`` maps visible columns to ``player``, ``club`` or
    ``season``. Several cells in one row can therefore open different cards.
    ``add_open`` adds a compact action for the row itself (a match or game).
    """
    frame = df.copy()
    kwargs = {"hide_index": True, "width": "stretch"}
    kwargs.update(dataframe_kwargs)

    actions = dict(action_columns or {})
    if add_open:
        open_column = "_Open"
        while open_column in frame.columns:
            open_column = "_" + open_column
        frame.insert(0, open_column, ":material/open_in_new: Open")
        actions = {open_column: "open", **actions}
        if "column_order" in kwargs:
            kwargs["column_order"] = [
                open_column, *list(kwargs["column_order"])
            ]

    column_config = dict(kwargs.pop("column_config", {}) or {})
    for position, (column, action) in enumerate(actions.items()):
        if column not in frame.columns:
            continue
        if action == "club":
            frame[column] = _club_action_column(frame[column])
        elif action == "season":
            frame[column] = _season_action_column(frame[column])
        else:
            # Button labels must be text. Keep parallel ids/seasons untouched;
            # only the displayed copy is converted.
            frame[column] = frame[column].map(
                lambda value: "" if value is None else str(value)
            )
        event_key = f"{key}__action_{position}"
        column_config[column] = st.column_config.ButtonColumn(
            "" if action == "open" else column,
            width="small" if action == "open" else None,
            pinned=position == 0,
            alignment="left",
            type="tertiary",
            on_click=_queue_click,
            args=(event_key, key, action, column),
            key=event_key,
        )
    st.dataframe(frame, key=key, column_config=column_config, **kwargs)

    pending = st.session_state.get(_PENDING)
    if not pending or pending.get("key") != key:
        return None
    del st.session_state[_PENDING]
    row = pending.get("row")
    if not isinstance(row, int) or not 0 <= row < len(frame):
        return None
    return pending


# --------------------------------------------------------------- player

def _clear_overlay() -> None:
    st.session_state.pop(_STACK, None)


def _back_overlay() -> None:
    stack = st.session_state.get(_STACK, [])
    if len(stack) > 1:
        stack.pop()


def _push_card(card: dict) -> None:
    stack = st.session_state.setdefault(_STACK, [])
    if not stack or stack[-1] != card:
        stack.append(card)
        if len(stack) > 30:
            del stack[:-30]


def _card_label(card: dict) -> str:
    return str(card.get("label") or card.get("kind", "Details")).strip()


def _open_card(card: dict, sport, con, *, nested: bool) -> None:
    _push_card(card)
    if nested:
        st.rerun(scope="fragment")
    else:
        _details_dialog(sport, con)


@st.dialog("Details", width="large", on_dismiss=_clear_overlay)
def _details_dialog(sport, con):
    """Render the current card and keep a back-stack inside one dialog."""
    stack = st.session_state.get(_STACK, [])
    if not stack:
        return
    current = stack[-1]

    with st.container(horizontal=True, vertical_alignment="center"):
        if len(stack) > 1:
            st.button(
                "Back", icon=":material/arrow_back:", key="overlay_back",
                on_click=_back_overlay,
            )
        st.caption("  /  ".join(_card_label(card) for card in stack[-4:]))

    kind = current["kind"]
    if kind == "player":
        explore.render_player_profile(
            sport, con, current["pid"],
            key_prefix=current.get("key_prefix", "overlay"),
            heading_level="###", nested=True,
        )
    elif kind == "club":
        overlays.club_overview(
            sport, con, current["club"], nested=True,
        )
    elif kind == "season":
        overlays.season_overview(
            sport, con, current["season"], club=current.get("club"),
            nested=True,
        )
    elif kind == "game":
        overlays.game_card(
            sport, con, current["record"], stat=current.get("stat"),
            nested=True,
        )
    elif kind == "match":
        render_body = current.get("render_body") or _default_match_body
        match = current["match"]
        render_body(match)
        _match_links(sport, con, match)


def player_button(label: str, sport, con, pid, key: str,
                  key_prefix: str | None = None, nested: bool = False) -> None:
    """Render a native button that opens one player's card."""
    if st.button(label, key=key, icon=":material/person:", type="tertiary"):
        _open_card({"kind": "player", "pid": pid, "label": label,
                    "key_prefix": key_prefix or key},
                   sport, con, nested=nested)


def card_links(sport, con, *, key_prefix: str, player_id=None,
               player=None, season=None, clubs: Sequence = ()) -> None:
    """Compact in-card links that navigate within the active dialog."""
    with st.container(horizontal=True):
        if player_id is not None and not pd.isna(player_id):
            label = str(player or "Player")
            if st.button(label, icon=":material/person:",
                         key=f"{key_prefix}_player", type="tertiary"):
                _open_card({"kind": "player", "pid": player_id,
                            "label": label,
                            "key_prefix": f"{key_prefix}_player_card"},
                           sport, con, nested=True)
        if season is not None and not pd.isna(season):
            if st.button(str(season), icon=":material/calendar_month:",
                         key=f"{key_prefix}_season", type="tertiary"):
                _open_card({"kind": "season", "season": season,
                            "label": str(season)}, sport, con, nested=True)
        seen = set()
        for position, club in enumerate(clubs):
            if not club or str(club) in seen:
                continue
            seen.add(str(club))
            label = str(club).replace("_", " ").title()
            if st.button(label, icon=":material/shield:",
                         key=f"{key_prefix}_club_{position}", type="tertiary"):
                _open_card({"kind": "club", "club": club, "label": label},
                           sport, con, nested=True)


def clickable_player_table(df, player_ids: Sequence, sport, con, key: str,
                           key_prefix: str | None = None, nested: bool = False,
                           **dataframe_kwargs):
    """Render `df` and open a player dialog when its player/name is clicked.

    `player_ids` must align with `df`'s rows position-for-position -- it is
    typically a hidden id column pulled off the frame before display, the
    same way Grid Solver keeps `pids` alongside the columns it shows.

    An id may be None. An award roll lists every winner, including the ones
    whose name could not be resolved to a player in this database, and
    those rows are still worth showing -- they just have no career to open.
    """
    event = _select(
        df, key, dataframe_kwargs,
        action_columns=_entity_columns(df, player=True),
    )
    if event is None:
        return
    row = event["row"]
    if event["action"] == "club":
        club = event.get("label")
        if club:
            _open_card({"kind": "club", "club": club, "label": club},
                       sport, con, nested=nested)
        return
    if event["action"] == "season":
        season = event.get("label")
        if season is not None and not pd.isna(season):
            _open_card({"kind": "season", "season": season,
                        "label": str(season)}, sport, con, nested=nested)
        return
    pid = player_ids[row] if row < len(player_ids) else None
    if pid is None or pd.isna(pid):
        st.info("That row could not be linked to a player in this database, "
                "so there is no career to show.")
        return
    label = str(df.iloc[row].get(event["column"], "Player"))
    _open_card({"kind": "player", "pid": pid, "label": label,
                "key_prefix": key_prefix or key},
               sport, con, nested=nested)


# ---------------------------------------------------------------- match

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


def _match_links(sport, con, match) -> None:
    """Entity navigation shown under either rich match renderer."""
    links = []
    for value in (getattr(match, "club_id", None),
                  getattr(match, "opponent_id", None)):
        if value and value not in links:
            links.append(value)
    with st.container(horizontal=True):
        season = getattr(match, "season", None)
        if season is not None:
            if st.button(str(season), icon=":material/calendar_month:",
                         key="match_link_season"):
                _open_card({"kind": "season", "season": season,
                            "label": str(season)}, sport, con, nested=True)
        for position, club in enumerate(links):
            label = str(club).replace("_", " ").title()
            if st.button(label, icon=":material/shield:",
                         key=f"match_link_club_{position}"):
                _open_card({"kind": "club", "club": club, "label": label},
                           sport, con, nested=True)


def clickable_match_table(df, matches: Sequence, key: str,
                          sport=None, con=None,
                          nested: bool = False,
                          render_body: Callable | None = None,
                          **dataframe_kwargs):
    """Render `df` and open a match dialog from its Open action.

    `matches` must align with `df`'s rows position-for-position, same
    convention as `clickable_player_table`. `render_body(match)` draws the
    dialog's contents; the default shows the fields every Match carries.
    """
    event = _select(
        df, key, dataframe_kwargs,
        action_columns=_entity_columns(df) if sport is not None and con is not None
        else {},
        add_open=True,
    )
    if event is None:
        return
    row = event["row"]
    if event["action"] == "club":
        club = event.get("label")
        if club:
            _open_card({"kind": "club", "club": club, "label": club},
                       sport, con, nested=nested)
    elif event["action"] == "season":
        season = event.get("label")
        if season is not None and not pd.isna(season):
            _open_card({"kind": "season", "season": season,
                        "label": str(season)}, sport, con, nested=nested)
    else:
        match = matches[row]
        _open_card({"kind": "match", "match": match,
                    "render_body": render_body,
                    "label": f"{getattr(match, 'season', '')} match".strip()},
                   sport, con, nested=nested)


# ----------------------------------------------------------------- game

def clickable_game_table(df, sport, con, key: str, stat=None,
                         nested: bool = False,
                         **dataframe_kwargs):
    """Render a table of `games` rows, each opening its own scorecard.

    Unlike the match table this needs no parallel list: a game row carries
    everything the card shows, so the clicked row of the frame *is* the
    record.
    """
    event = _select(
        df, key, dataframe_kwargs,
        action_columns=_entity_columns(df), add_open=True,
    )
    if event is None:
        return
    row = event["row"]
    if event["action"] == "club":
        club = event.get("label")
        if club:
            _open_card({"kind": "club", "club": club, "label": club},
                       sport, con, nested=nested)
    elif event["action"] == "season":
        season = event.get("label")
        if season is not None and not pd.isna(season):
            _open_card({"kind": "season", "season": season,
                        "label": str(season)}, sport, con, nested=nested)
    else:
        record = df.iloc[row].to_dict()
        _open_card({"kind": "game", "record": record, "stat": stat,
                    "label": f"{record.get('Season', '')} game".strip()},
                   sport, con, nested=nested)


# --------------------------------------------------------------- season

def clickable_season_table(df, seasons: Sequence, sport, con, key: str,
                           clubs: Sequence | None = None,
                           nested: bool = False,
                           **dataframe_kwargs):
    """Render `df` and open a season overview when its season is clicked.

    `seasons` aligns with the rows. `clubs` optionally does too: a row of a
    player's career or of a club's record names a club as well as a season,
    and the overview then shows that club's part in the season alongside
    the champion.
    """
    event = _select(
        df, key, dataframe_kwargs,
        action_columns=_entity_columns(df, clubs=True),
    )
    if event is None:
        return
    row = event["row"]
    if event["action"] == "club":
        club = event.get("label")
        if club:
            _open_card({"kind": "club", "club": club, "label": club},
                       sport, con, nested=nested)
        return
    season = (event.get("label") if event["action"] == "season"
              else seasons[row] if row < len(seasons) else None)
    if season is None or pd.isna(season):
        return
    club = clubs[row] if clubs is not None and row < len(clubs) else None
    _open_card({"kind": "season", "season": season, "club": club,
                "label": str(season)}, sport, con, nested=nested)


# ----------------------------------------------------------------- club

def clickable_club_table(df, clubs: Sequence, sport, con, key: str,
                         nested: bool = False,
                         **dataframe_kwargs):
    """Render `df` and open a club overview when its club name is clicked."""
    action_column = df.columns[0] if len(df.columns) else None
    actions = {action_column: "club"} if action_column is not None else {}
    event = _select(df, key, dataframe_kwargs, action_columns=actions)
    if event is None:
        return
    row = event["row"]
    club = clubs[row] if row < len(clubs) else None
    if club:
        _open_card({"kind": "club", "club": club, "label": str(club)},
                   sport, con, nested=nested)
