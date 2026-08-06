"""Regression tests for the shared table-to-overlay click dispatcher."""

from streamlit.testing.v1 import AppTest


def test_button_click_is_consumed_by_its_table():
    app = AppTest.from_string(
        """
import pandas as pd
import streamlit as st
import components

st.session_state["click"] = {"row": 1, "label": "Bob"}
components._queue_click("click", "players")
row = components._select(
    pd.DataFrame({"Player": ["Alice", "Bob"]}),
    "players",
    {},
    action_column="Player",
)
st.write(f"picked={row}")
"""
    ).run()

    assert not app.exception
    assert app.markdown[-1].value == "picked=1"
    assert "_overlay_pending" not in app.session_state


def test_click_for_another_table_is_not_consumed():
    app = AppTest.from_string(
        """
import pandas as pd
import streamlit as st
import components

st.session_state["click"] = {"row": 0, "label": "Open"}
components._queue_click("click", "matches")
row = components._select(
    pd.DataFrame({"Player": ["Alice"]}),
    "players",
    {},
    action_column="Player",
)
st.write(f"picked={row}")
"""
    ).run()

    assert not app.exception
    assert app.markdown[-1].value == "picked=None"
    assert app.session_state["_overlay_pending"] == {
        "key": "matches",
        "row": 0,
    }


def test_player_name_column_can_be_the_action():
    app = AppTest.from_string(
        """
import pandas as pd
import streamlit as st
import components

st.session_state["click"] = {"row": 0, "label": "A Hall of Famer"}
components._queue_click("click", "hall")
row = components._select(
    pd.DataFrame({"Name": ["A Hall of Famer"], "Inducted": [2000]}),
    "hall",
    {},
    action_column="Name",
)
st.write(f"picked={row}")
"""
    ).run()

    assert not app.exception
    assert app.markdown[-1].value == "picked=0"
    assert list(app.dataframe[0].value.columns) == ["Name", "Inducted"]
