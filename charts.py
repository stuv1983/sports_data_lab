"""The charts this app draws, and the palette they draw them in.

Every sport's pages go through here so a career reads the same shape
whichever database is loaded, and so the colours are decided once. The
palette is the validated categorical default from the data-visualisation
reference: two hues, checked for the lightness band, the chroma floor,
colour-vision separation and contrast against both surfaces. Slot one is
blue and slot two orange, in that order, always -- a hue follows the
entity it names, never its position in a list, so a filter that drops a
series must never repaint the one that remains.

Two rules are worth stating because breaking them is easy and quiet:

Never two y-scales on one chart. Games and goals are different sizes and
plotting them against each other invents a relationship the data does not
have, so a career draws as two charts side by side instead.

A break in a career is a fact. A player who missed a season has no bar
that year, not a zero, and the season is still on the axis -- the gap is
the thing worth seeing. The same honesty rule runs through every builder
here: a value the source never recorded is excluded, never counted as
zero, so a rolling average is an average of recorded games and a missing
percentile is a missing bar with a note, not a 0th-percentile slander.
"""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

#: The validated categorical pair, light surface then dark. Both modes are
#: selected rather than flipped: the dark values are the same two hues
#: re-stepped for a dark background.
SERIES_LIGHT = ("#2a78d6", "#eb6834")
SERIES_DARK = ("#3987e5", "#d95926")

#: Grid and axis ink, kept recessive so the marks carry the chart.
_GRID = "#8a8a85"

#: Altair renders at this height unless a caller asks for another. Tall
#: enough to read a trend, short enough to sit inside a card.
HEIGHT = 200


def series_colours() -> tuple[str, str]:
    """The two categorical hues, stepped for the active theme."""
    try:
        base = st.get_option("theme.base")
    except Exception:                                    # noqa: BLE001
        base = None
    return SERIES_DARK if str(base).lower() == "dark" else SERIES_LIGHT


def _axis(title: str) -> alt.Axis:
    return alt.Axis(title=title, grid=False, labelColor=_GRID,
                    titleColor=_GRID, tickColor=_GRID, domainColor=_GRID)


def career_chart(seasons: pd.DataFrame, season_column: str, value_column: str,
                 label: str, colour: str | None = None):
    """One measure across a career, a bar per season.

    Bars rather than a line: a season is a discrete thing a player either
    played or did not, and a line between two seasons draws a slope
    through a year that may not exist. Seasons a player missed are absent
    rather than zero, and the axis still spans them, so the gap shows.
    """
    if seasons.empty or value_column not in seasons.columns:
        return None
    frame = seasons[[season_column, value_column]].copy()
    frame[value_column] = pd.to_numeric(frame[value_column], errors="coerce")
    frame = frame.dropna(subset=[value_column])
    if frame.empty or not frame[value_column].any():
        return None
    # One row per season: a player who changed clubs mid-season has two
    # rows for it, and the career total for that season is the sum.
    frame = (frame.groupby(season_column, as_index=False)[value_column]
             .sum())

    colour = colour or series_colours()[0]
    return (
        alt.Chart(frame)
        .mark_bar(size=10, cornerRadiusTopLeft=4, cornerRadiusTopRight=4,
                  color=colour)
        .encode(
            x=alt.X(f"{season_column}:O", axis=_axis(None)),
            y=alt.Y(f"{value_column}:Q", axis=_axis(label)),
            tooltip=[alt.Tooltip(f"{season_column}:O", title="Season"),
                     alt.Tooltip(f"{value_column}:Q", title=label,
                                 format=",")],
        )
        .properties(height=HEIGHT)
    )


def rolling_form_chart(frame: pd.DataFrame, order_column: str,
                       value_column: str, label: str, window: int,
                       x_title: str | None = None,
                       ordinal_x: bool = False):
    """One stat per game with its rolling average laid over the top.

    Bars are the recorded games; the line is the mean of the last `window`
    *recorded* games. Games where the stat was never recorded are excluded
    before the window is taken -- averaging zeroes into a pre-1965 career
    would invent form the source never measured -- and the line starts
    only once a full window exists (`min_periods=window`), so its first
    `window - 1` games show no line rather than a lie built from less.

    One y-scale: the bars and the line measure the same thing in the same
    unit, which is the only arrangement an overlay is honest in.
    """
    if frame.empty or value_column not in frame.columns:
        return None
    data = frame[[order_column, value_column]].copy()
    data[value_column] = pd.to_numeric(data[value_column], errors="coerce")
    data = data.dropna(subset=[value_column]).sort_values(order_column)
    if data.empty:
        return None
    data["Rolling"] = (data[value_column]
                       .rolling(window, min_periods=window).mean().round(2))

    blue, orange = series_colours()
    x_type = "O" if ordinal_x else "Q"
    x = alt.X(f"{order_column}:{x_type}", axis=_axis(x_title),
              **({} if ordinal_x else {"scale": alt.Scale(nice=False)}))
    bars = (
        alt.Chart(data)
        .mark_bar(size=2, color=blue, opacity=0.75)
        .encode(
            x=x,
            y=alt.Y(f"{value_column}:Q", axis=_axis(label)),
            tooltip=[alt.Tooltip(f"{order_column}:{x_type}",
                                 title=x_title or order_column),
                     alt.Tooltip(f"{value_column}:Q", title=label)],
        )
    )
    line = (
        alt.Chart(data)
        .mark_line(strokeWidth=2, color=orange)
        .encode(
            x=x,
            y=alt.Y("Rolling:Q", axis=_axis(label)),
            tooltip=[alt.Tooltip(f"{order_column}:{x_type}",
                                 title=x_title or order_column),
                     alt.Tooltip("Rolling:Q",
                                 title=f"{window}-{x_title or 'game'} avg")],
        )
    )
    return (bars + line).properties(height=HEIGHT + 20)


def percentile_profile_chart(frame: pd.DataFrame, names: tuple,
                             attribute_column: str = "Attribute",
                             percentile_column: str = "Percentile",
                             player_column: str = "Player",
                             value_column: str = "Value"):
    """Two players' league percentiles across the sport's headline stats.

    A grouped horizontal bar per attribute does the radar's job without
    the radar's distortions: the axis is the same 0-100 percentile for
    every attribute, so lengths compare honestly, and an attribute the
    era never recorded for a player is simply absent for them rather
    than drawn at zero.
    """
    if frame.empty:
        return None
    blue, orange = series_colours()
    order = list(dict.fromkeys(frame[attribute_column]))
    return (
        alt.Chart(frame)
        .mark_bar(height=10, cornerRadiusTopRight=4,
                  cornerRadiusBottomRight=4)
        .encode(
            y=alt.Y(f"{attribute_column}:N", sort=order, axis=_axis(None)),
            yOffset=alt.YOffset(f"{player_column}:N",
                                sort=list(names)),
            x=alt.X(f"{percentile_column}:Q",
                    scale=alt.Scale(domain=[0, 100]),
                    axis=_axis("League percentile")),
            color=alt.Color(
                f"{player_column}:N",
                scale=alt.Scale(domain=list(names), range=[blue, orange]),
                legend=alt.Legend(title=None, labelColor=_GRID,
                                  orient="top")),
            tooltip=[alt.Tooltip(f"{player_column}:N"),
                     alt.Tooltip(f"{attribute_column}:N"),
                     alt.Tooltip(f"{value_column}:Q", title="Per game",
                                 format=".2f"),
                     alt.Tooltip(f"{percentile_column}:Q",
                                 title="Percentile", format=".0f")],
        )
        .properties(height=max(HEIGHT, 34 * len(order)))
    )


def quadrant_chart(frame: pd.DataFrame, x_column: str, y_column: str,
                   name_column: str, x_title: str, y_title: str,
                   label_top: int = 10, id_column: str | None = None):
    """Volume against efficiency, quartered by the medians of the shown set.

    The reference lines are medians of what is plotted, so the quadrants
    always split the field being looked at rather than an all-time one.
    Only the `label_top` most extreme points carry a name -- a label on
    every point is a list wearing a chart's clothes; everyone else is a
    hover away. Pass `id_column` to attach a selection parameter named
    "quadrant" for `st.altair_chart(..., on_select="rerun")`.
    """
    if frame.empty:
        return None
    data = frame.copy()
    for column in (x_column, y_column):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=[x_column, y_column])
    if data.empty:
        return None
    x_mid = float(data[x_column].median())
    y_mid = float(data[y_column].median())
    # The most notable points: furthest from the medians once each axis is
    # scaled to its own spread, so one big axis does not drown the other.
    x_span = (data[x_column].max() - data[x_column].min()) or 1.0
    y_span = (data[y_column].max() - data[y_column].min()) or 1.0
    distance = (((data[x_column] - x_mid) / x_span) ** 2
                + ((data[y_column] - y_mid) / y_span) ** 2)
    data["_labelled"] = distance.rank(ascending=False) <= label_top

    blue, _ = series_colours()
    tooltip = [alt.Tooltip(f"{name_column}:N"),
               alt.Tooltip(f"{x_column}:Q", title=x_title, format=","),
               alt.Tooltip(f"{y_column}:Q", title=y_title, format=".2f")]
    points = (
        alt.Chart(data)
        .mark_circle(size=70, color=blue, opacity=0.75,
                     stroke="#ffffff", strokeWidth=0.5)
        .encode(x=alt.X(f"{x_column}:Q", axis=_axis(x_title),
                        scale=alt.Scale(zero=False, nice=True)),
                y=alt.Y(f"{y_column}:Q", axis=_axis(y_title),
                        scale=alt.Scale(zero=False, nice=True)),
                tooltip=tooltip)
    )
    if id_column:
        points = points.add_params(alt.selection_point(
            name="quadrant", fields=[id_column], on="click"))
    rules = alt.Chart(pd.DataFrame({"x": [x_mid]})).mark_rule(
        color=_GRID, strokeDash=[4, 4]).encode(x="x:Q")
    rules_y = alt.Chart(pd.DataFrame({"y": [y_mid]})).mark_rule(
        color=_GRID, strokeDash=[4, 4]).encode(y="y:Q")
    names_layer = (
        alt.Chart(data[data["_labelled"]])
        .mark_text(align="left", dx=7, dy=-5, fontSize=11, color=_GRID)
        .encode(x=f"{x_column}:Q", y=f"{y_column}:Q",
                text=f"{name_column}:N")
    )
    return (rules + rules_y + points + names_layer).properties(
        height=HEIGHT * 2)


def progression_chart(rows, home: str, away: str, period_word: str = "break"):
    """The running score of one match, both sides, break by break.

    A line, because the score at each break is one thing measured
    repeatedly, and the shape of a comeback is the point. Nought-all at
    the first bounce is prepended so the first period is a climb from the
    start rather than a mark floating above it.

    `rows` is `_period_rows`' output: (period, home tuple, away tuple),
    each tuple starting with the running points.
    """
    if not rows:
        return None
    blue, orange = series_colours()
    records = [{"Break": 0, "Side": side, "Points": 0}
               for side in (home, away)]
    for period, home_side, away_side in rows:
        records.append({"Break": period, "Side": home,
                        "Points": home_side[0]})
        records.append({"Break": period, "Side": away,
                        "Points": away_side[0]})
    frame = pd.DataFrame(records)
    if frame["Points"].nunique() <= 1:
        return None

    # The two sides are named in the legend and by the colour, and a
    # tooltip gives the number -- so no value is printed on every point.
    return (
        alt.Chart(frame)
        # Altair sizes a point by area, so 60px² is a mark about 8.7px
        # across -- the floor at which a break is still a target worth
        # hovering rather than a dot to aim at.
        .mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=60))
        .encode(
            x=alt.X("Break:Q", axis=_axis(period_word.capitalize()),
                    scale=alt.Scale(nice=False)),
            y=alt.Y("Points:Q", axis=_axis("Score")),
            color=alt.Color(
                "Side:N",
                scale=alt.Scale(domain=[home, away], range=[blue, orange]),
                legend=alt.Legend(title=None, labelColor=_GRID)),
            tooltip=["Side:N", alt.Tooltip("Points:Q", title="Score")],
        )
        .properties(height=HEIGHT)
    )
