"""Shared clickable-table-to-overlay widgets, for any page with a table.

Grid Solver was the first page to let a click on a results row open a
detail dialog over the board (app.py's show_player_dialog). This module is
that pattern lifted out so Past Games, Advanced Search and Stats Explorer
can offer the same thing without a fourth copy of the same twelve lines,
and so a future overlay kind only has to be added once.

There are seven kinds now: player, match, individual game, season, round,
venue and club. A table opens the overlay for whatever its rows *are*:
player, season and club names are action cells, while matches and
individual games have an Open action. A row naming both a player and a
season makes its subject clickable, so an award roll opens the player
while a season record opens the season.

The bodies themselves live in overlays.py, one function per kind. This
module decides *which* card a click means and keeps the history stack; it
draws none of them.

One navigable dialog
--------------------
Streamlit allows exactly one dialog to open per rerun. Every card therefore
uses the same dialog and pushes the next player, club, season, match or game
onto a session-local history stack. Tables inside a card remain clickable;
their actions replace the dialog body, and Back restores the previous card.
"""

from __future__ import annotations

from typing import Mapping, Sequence

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


def _club_actions(value, sport=None):
    """Turn a club-history cell into one button or a menu of club buttons.

    Era names collapse to one entry per club first: "Kangaroos, North
    Melbourne" is one club under two names, and a menu offering both reads
    as two. The club the reader gets is named as it was at the time --
    `Sport.collapse_clubs` keeps a lone era name and uses the current name
    only where the cell spans a rename.
    """
    if value is None or (not isinstance(value, (list, tuple, set))
                         and pd.isna(value)):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    values = [part.strip() for part in text.replace("|", ",").split(",")]
    values = [part for part in values if part]
    if sport is not None:
        values = sport.collapse_clubs(values)
    return values if len(values) > 1 else values[0]


def _club_action_column(series, sport=None):
    """Keep Arrow types uniform when any row needs a multi-club menu."""
    values = series.map(lambda value: _club_actions(value, sport))
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
        # Pair-comparison tables qualify the same entity with a player's
        # name ("Patrick Dangerfield club") so the two sides remain clear.
        for column in df.columns:
            key = str(column).casefold()
            if key.endswith(" club") or key.endswith(" opponent"):
                actions[column] = "club"
    for column in ("Round", "Rnd", "Rd"):
        if column in df.columns:
            actions[column] = "round"
    for column in ("Venue", "Ground"):
        if column in df.columns:
            actions[column] = "venue"
    return actions


def _select(df, key, dataframe_kwargs,
            action_columns: Mapping[str, str] | None = None,
            add_open: bool = False, sport=None):
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
            frame[column] = _club_action_column(frame[column], sport)
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
    elif kind == "round":
        overlays.round_overview(
            sport, con, current["season"], current["round"], nested=True,
        )
    elif kind == "venue":
        overlays.venue_overview(sport, con, current["venue"], nested=True)
    elif kind == "game":
        overlays.game_card(
            sport, con, current["record"], stat=current.get("stat"),
            nested=True,
        )
    elif kind == "match":
        overlays.match_overview(sport, con, current["match"], nested=True)


def player_button(label: str, sport, con, pid, key: str,
                  key_prefix: str | None = None, nested: bool = False) -> None:
    """Render a native button that opens one player's card."""
    if st.button(label, key=key, icon=":material/person:", type="tertiary"):
        _open_card({"kind": "player", "pid": pid, "label": label,
                    "key_prefix": key_prefix or key},
                   sport, con, nested=nested)


def card_links(sport, con, *, key_prefix: str, player_id=None,
               player=None, season=None, round_value=None, venue=None,
               clubs: Sequence = ()) -> None:
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
        if (season is not None and round_value is not None
                and not pd.isna(round_value)):
            label = f"R{round_value}" if str(round_value).isdigit() else str(round_value)
            if st.button(label, icon=":material/event:",
                         key=f"{key_prefix}_round", type="tertiary"):
                _open_card({"kind": "round", "season": season,
                            "round": round_value,
                            "label": f"{season} {label}"},
                           sport, con, nested=True)
        if venue is not None and not pd.isna(venue) and str(venue).strip():
            label = str(venue).strip()
            if st.button(label, icon=":material/stadium:",
                         key=f"{key_prefix}_venue", type="tertiary"):
                _open_card({"kind": "venue", "venue": label, "label": label},
                           sport, con, nested=True)
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


def _row_season(row):
    for column in ("Season", "Year", "season"):
        if column in row and row[column] is not None and not pd.isna(row[column]):
            return row[column]
    return None


def _handle_entity_event(event, df, sport, con, *, nested=False,
                         fallback_season=None) -> bool:
    """Open a common entity action; return False for the row's subject."""
    action, label = event["action"], event.get("label")
    row = df.iloc[event["row"]]
    if action == "club" and label:
        _open_card({"kind": "club", "club": label, "label": label},
                   sport, con, nested=nested)
        return True
    if action == "season" and label is not None and not pd.isna(label):
        _open_card({"kind": "season", "season": label, "label": str(label)},
                   sport, con, nested=nested)
        return True
    if action == "round" and label is not None and not pd.isna(label):
        season = fallback_season if fallback_season is not None else _row_season(row)
        if season is None:
            st.info("A season is needed to open that round.")
        else:
            round_label = f"R{label}" if str(label).isdigit() else str(label)
            _open_card({"kind": "round", "season": season, "round": label,
                        "label": f"{season} {round_label}"},
                       sport, con, nested=nested)
        return True
    if action == "venue" and label:
        _open_card({"kind": "venue", "venue": label, "label": label},
                   sport, con, nested=nested)
        return True
    return False


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
        action_columns=_entity_columns(df, player=True), sport=sport,
    )
    if event is None:
        return
    row = event["row"]
    if _handle_entity_event(event, df, sport, con, nested=nested):
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

def _match_label(match) -> str:
    """`'Adelaide vs Richmond'` for the dialog's breadcrumb.

    The breadcrumb is how a reader retraces a path several cards deep, and
    every match used to label itself '2026 match', so a trail through three
    of them read the same three times.
    """
    def field(*names):
        for name in names:
            if isinstance(match, Mapping):
                value = match.get(name)
            else:
                value = getattr(match, name, None)
            if value is not None and str(value).strip():
                return str(value).replace("_", " ").title()
        return ""

    home = field("club_id", "Home", "Club", "For")
    away = field("opponent_id", "Away", "Opponent")
    if home and away:
        return f"{home} vs {away}"
    season = field("season", "Season")
    return f"{season} match".strip() or "Match"


def clickable_match_table(df, matches: Sequence, key: str,
                          sport=None, con=None,
                          nested: bool = False,
                          **dataframe_kwargs):
    """Render `df` and open a match dialog from its Open action.

    `matches` must align with `df`'s rows position-for-position, same
    convention as `clickable_player_table`. Each entry is anything that
    names the match -- a `club_history.Match`, or a mapping carrying its
    id -- and the card is built from the database rather than from the
    columns the table happened to show, so every page that opens a match
    opens the same one.
    """
    event = _select(
        df, key, dataframe_kwargs,
        action_columns=_entity_columns(df) if sport is not None and con is not None
        else {},
        add_open=True, sport=sport,
    )
    if event is None:
        return
    row = event["row"]
    if not _handle_entity_event(event, df, sport, con, nested=nested):
        match = matches[row]
        _open_card({"kind": "match", "match": match,
                    "label": _match_label(match)},
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
        action_columns=_entity_columns(df), add_open=True, sport=sport,
    )
    if event is None:
        return
    row = event["row"]
    if not _handle_entity_event(event, df, sport, con, nested=nested):
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
        action_columns=_entity_columns(df, clubs=True), sport=sport,
    )
    if event is None:
        return
    row = event["row"]
    if (event["action"] != "season"
            and _handle_entity_event(event, df, sport, con, nested=nested)):
        return
    season = (event.get("label") if event["action"] == "season"
              else seasons[row] if row < len(seasons) else None)
    if season is None or pd.isna(season):
        return
    club = clubs[row] if clubs is not None and row < len(clubs) else None
    _open_card({"kind": "season", "season": season, "club": club,
                "label": str(season)}, sport, con, nested=nested)


def clickable_round_table(df, seasons: Sequence, sport, con, key: str,
                          nested: bool = False, **dataframe_kwargs):
    """Render a summary whose Round cells open results and voting detail."""
    round_column = next((c for c in ("Round", "Rnd", "Rd") if c in df), None)
    actions = {round_column: "round"} if round_column else {}
    event = _select(df, key, dataframe_kwargs, action_columns=actions,
                    sport=sport)
    if event is None:
        return
    row = event["row"]
    season = seasons[row] if row < len(seasons) else None
    _handle_entity_event(event, df, sport, con, nested=nested,
                         fallback_season=season)


def clickable_entity_table(df, sport, con, key: str, nested: bool = False,
                           **dataframe_kwargs):
    """Render a table where any season, round, venue or club cell opens."""
    event = _select(df, key, dataframe_kwargs,
                    action_columns=_entity_columns(df), sport=sport)
    if event is not None:
        _handle_entity_event(event, df, sport, con, nested=nested)


# ----------------------------------------------------------------- club

def clickable_club_table(df, clubs: Sequence, sport, con, key: str,
                         nested: bool = False,
                         **dataframe_kwargs):
    """Render `df` and open a club overview when its club name is clicked."""
    action_column = df.columns[0] if len(df.columns) else None
    actions = {action_column: "club"} if action_column is not None else {}
    event = _select(df, key, dataframe_kwargs, action_columns=actions,
                    sport=sport)
    if event is None:
        return
    row = event["row"]
    club = clubs[row] if row < len(clubs) else None
    if club:
        _open_card({"kind": "club", "club": club, "label": str(club)},
                   sport, con, nested=nested)
