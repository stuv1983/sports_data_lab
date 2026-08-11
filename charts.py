"""The two charts this app draws, and the palette they draw them in.

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
the thing worth seeing.
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
