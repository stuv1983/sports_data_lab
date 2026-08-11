#!/usr/bin/env python3
"""The chart builders' promises: NULL-honest maths, and None over noise.

These test the data half of charts.py -- what gets averaged, what gets
excluded, when a builder declines to draw -- not Vega output. The rules
under test are the module's stated ones: an unrecorded value is excluded,
never a zero; a rolling line begins only once a full window of *recorded*
entries exists; an empty or all-null frame is no chart at all.
"""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---

import altair as alt
import pandas as pd

import charts


def _layer_data(chart):
    """The data frames of a layered chart, outermost first.

    Altair hoists data shared by every layer up to the LayerChart, so a
    sub-layer's own `.data` may be Undefined; fall back to the parent's.
    """
    return [layer.data if layer.data is not alt.Undefined else chart.data
            for layer in chart.layer]


# ------------------------------------------------------ rolling form

def test_rolling_average_is_over_recorded_games_only():
    """A NULL game is excluded before the window is taken, not averaged
    in as zero: games 1-3 recorded, 4 unrecorded, 5-6 recorded -- the
    3-game window at game 6 averages games 3, 5 and 6."""
    frame = pd.DataFrame({
        "Game": [1, 2, 3, 4, 5, 6],
        "Value": [10, 20, 30, None, 40, 50],
    })
    chart = charts.rolling_form_chart(frame, "Game", "Value", "Disposals", 3)
    data = _layer_data(chart)[0]
    assert data["Game"].tolist() == [1, 2, 3, 5, 6]      # the NULL game is gone
    rolling = data.set_index("Game")["Rolling"]
    assert pd.isna(rolling.loc[1]) and pd.isna(rolling.loc[2])
    assert rolling.loc[3] == 20.0                        # (10+20+30)/3
    assert rolling.loc[6] == 40.0                        # (30+40+50)/3


def test_rolling_line_waits_for_a_full_window():
    frame = pd.DataFrame({"Game": [1, 2], "Value": [5, 7]})
    chart = charts.rolling_form_chart(frame, "Game", "Value", "Goals", 5)
    data = _layer_data(chart)[0]
    assert data["Rolling"].isna().all()   # never enough games for the window


def test_an_unrecorded_career_is_no_chart_not_a_flat_zero():
    frame = pd.DataFrame({"Game": [1, 2, 3], "Value": [None, None, None]})
    assert charts.rolling_form_chart(frame, "Game", "Value", "Tackles", 5) \
        is None
    assert charts.rolling_form_chart(pd.DataFrame(), "Game", "Value",
                                     "Tackles", 5) is None


# ------------------------------------------------- percentile profile

def test_percentile_profile_keeps_both_players_and_their_order():
    frame = pd.DataFrame([
        {"Player": "A", "Attribute": "Goals", "Value": 2.1, "Percentile": 88.0},
        {"Player": "B", "Attribute": "Goals", "Value": 0.4, "Percentile": 31.0},
        {"Player": "A", "Attribute": "Marks", "Value": 6.0, "Percentile": 70.0},
    ])
    chart = charts.percentile_profile_chart(frame, ("A", "B"))
    assert isinstance(chart, alt.Chart)
    # The colour scale is anchored to the two names in picked order, so a
    # missing attribute for one player can never repaint the other.
    spec = chart.to_dict()
    assert spec["encoding"]["color"]["scale"]["domain"] == ["A", "B"]
    assert charts.percentile_profile_chart(pd.DataFrame(), ("A", "B")) is None


# ------------------------------------------------------------ quadrant

def test_quadrant_labels_only_the_most_extreme_points():
    frame = pd.DataFrame({
        "Games": [100, 110, 105, 300, 20],
        "Rate": [1.0, 1.1, 1.05, 3.0, 0.1],
        "Player": ["mid1", "mid2", "mid3", "star", "cameo"],
    })
    chart = charts.quadrant_chart(frame, "Games", "Rate", "Player",
                                  "Games", "Per game", label_top=2)
    text_data = chart.layer[-1].data       # the text layer is last
    assert set(text_data["Player"]) == {"star", "cameo"}


def test_quadrant_drops_null_rows_and_declines_an_empty_field():
    frame = pd.DataFrame({
        "Games": [100, None],
        "Rate": [1.0, 2.0],
        "Player": ["kept", "no-volume"],
    })
    chart = charts.quadrant_chart(frame, "Games", "Rate", "Player",
                                  "Games", "Per game")
    assert chart.layer[2].data["Player"].tolist() == ["kept"]
    empty = pd.DataFrame({"Games": [None], "Rate": [None], "Player": ["x"]})
    assert charts.quadrant_chart(empty, "Games", "Rate", "Player",
                                 "Games", "Per game") is None


def test_quadrant_selection_param_only_when_asked():
    frame = pd.DataFrame({"Games": [10, 20], "Rate": [1.0, 2.0],
                          "Player": ["a", "b"], "PlayerID": [1, 2]})
    plain = charts.quadrant_chart(frame, "Games", "Rate", "Player",
                                  "Games", "Per game")
    wired = charts.quadrant_chart(frame, "Games", "Rate", "Player",
                                  "Games", "Per game", id_column="PlayerID")
    def param_names(chart):
        spec = chart.to_dict()
        scopes = [spec, *spec.get("layer", [])]   # Altair may hoist params
        return [p["name"] for scope in scopes
                for p in scope.get("params", [])]
    assert not param_names(plain)
    assert "quadrant" in param_names(wired)
