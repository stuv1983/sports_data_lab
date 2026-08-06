"""Regression tests for the shared table-to-overlay click dispatcher."""

from streamlit.testing.v1 import AppTest

import pandas as pd

import components


def test_button_click_is_consumed_by_its_table():
    app = AppTest.from_string(
        """
import pandas as pd
import streamlit as st
import components

st.session_state["click"] = {"row": 1, "label": "Bob"}
components._queue_click("click", "players", "player", "Player")
event = components._select(
    pd.DataFrame({"Player": ["Alice", "Bob"]}),
    "players",
    {},
    action_columns={"Player": "player"},
)
st.write(f"picked={event['row']}:{event['action']}")
"""
    ).run()

    assert not app.exception
    assert app.markdown[-1].value == "picked=1:player"
    assert "_overlay_pending" not in app.session_state


def test_click_for_another_table_is_not_consumed():
    app = AppTest.from_string(
        """
import pandas as pd
import streamlit as st
import components

st.session_state["click"] = {"row": 0, "label": "Open"}
components._queue_click("click", "matches", "open", "_Open")
event = components._select(
    pd.DataFrame({"Player": ["Alice"]}),
    "players",
    {},
    action_columns={"Player": "player"},
)
st.write(f"picked={event}")
"""
    ).run()

    assert not app.exception
    assert app.markdown[-1].value == "picked=None"
    assert app.session_state["_overlay_pending"] == {
        "key": "matches",
        "row": 0,
        "action": "open",
        "column": "_Open",
        "label": "Open",
    }


def test_player_name_column_can_be_the_action():
    app = AppTest.from_string(
        """
import pandas as pd
import streamlit as st
import components

st.session_state["click"] = {"row": 0, "label": "A Hall of Famer"}
components._queue_click("click", "hall", "player", "Name")
event = components._select(
    pd.DataFrame({"Name": ["A Hall of Famer"], "Inducted": [2000]}),
    "hall",
    {},
    action_columns={"Name": "player"},
)
st.write(f"picked={event['row']}")
"""
    ).run()

    assert not app.exception
    assert app.markdown[-1].value == "picked=0"
    assert list(app.dataframe[0].value.columns) == ["Name", "Inducted"]


def test_entity_columns_include_player_club_and_each_season_column():
    frame = pd.DataFrame({
        "Player": ["A"], "Club": ["Carlton"], "Season": [2024],
        "First": [2010], "Last": [2024],
    })

    assert components._entity_columns(frame, player=True) == {
        "Player": "player",
        "Season": "season",
        "First": "season",
        "Last": "season",
        "Club": "club",
    }


def test_multi_club_columns_use_one_arrow_compatible_list_type():
    values = components._club_action_column(pd.Series([
        "Carlton", "Carlton, St Kilda", None,
    ])).tolist()

    assert values == [["Carlton"], ["Carlton", "St Kilda"], []]


def test_season_histories_offer_each_year_as_an_action():
    values = components._season_action_column(pd.Series([
        "2022,2023,2024", "1995", None,
    ])).tolist()

    assert values == [["2022", "2023", "2024"], ["1995"], []]


def test_unresolved_player_click_stays_visible_and_explains_no_card():
    app = AppTest.from_string(
        """
import pandas as pd
import streamlit as st
import components

st.session_state["click"] = {"row": 0, "label": "Unresolved Player"}
components._queue_click("click", "unresolved", "player", "Player")
components.clickable_player_table(
    pd.DataFrame({"Player": ["Unresolved Player"]}),
    [float("nan")], None, None, key="unresolved",
)
"""
    ).run()

    assert not app.exception
    assert "no career to show" in app.info[0].value


def test_overlay_history_pushes_deduplicates_and_goes_back():
    app = AppTest.from_string(
        """
import streamlit as st
import components

components._push_card({"kind": "club", "club": "Carlton", "label": "Carlton"})
components._push_card({"kind": "club", "club": "Carlton", "label": "Carlton"})
components._push_card({"kind": "player", "pid": 7, "label": "Player Seven"})
components._back_overlay()
st.write(st.session_state["_overlay_stack"])
"""
    ).run()

    assert not app.exception
    assert app.session_state["_overlay_stack"] == [
        {"kind": "club", "club": "Carlton", "label": "Carlton"}
    ]
