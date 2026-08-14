"""The charts this app draws, and the palette they draw them in.

Every sport's pages go through here so a career reads the same shape
whichever database is loaded, and so the colours are decided once. The
palette is the validated categorical default from the data-visualisation
reference: eight hues in a fixed order, checked for the lightness band,
the chroma floor, colour-vision separation and contrast against both
surfaces. Slot one is blue and slot two orange, in that order, always --
a hue follows the entity it names, never its position in a list, so a
filter that drops a series must never repaint the one that remains.

Two halves. The first draws one career or one match, and is what the
player and match cards have always called. The second -- from
`season_trend_chart` down -- draws a whole competition for the Visual
Explorer, and every builder there takes a frame SQL has already reduced
to a row per season, per bin or per cell. None of them aggregates: a
builder that groups its own input is a second place for the arithmetic to
go wrong, and the denominators these charts need are only knowable in the
query. See visual_queries.py, which is where they are.

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

from typing import Sequence

import altair as alt
import pandas as pd
import streamlit as st

#: The validated categorical pair, light surface then dark. Both modes are
#: selected rather than flipped: the dark values are the same two hues
#: re-stepped for a dark background.
SERIES_LIGHT = ("#2a78d6", "#eb6834")
SERIES_DARK = ("#3987e5", "#d95926")

#: The same reference palette carried out to its full eight slots, for the
#: charts that name more than two entities -- several clubs across the same
#: seasons, say. The order is the palette's own and is the colour-vision
#: safety mechanism rather than a decoration: it was chosen so that every
#: *adjacent* pair clears the separation floor, which is the pairing a line
#: or a bar chart actually puts side by side. Slots one and two are the two
#: hues above, so a two-series chart drawn through here is the same chart
#: it always was.
CATEGORICAL_LIGHT = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                     "#e87ba4", "#008300", "#4a3aa7", "#e34948")
CATEGORICAL_DARK = ("#3987e5", "#d95926", "#199e70", "#c98500",
                    "#d55181", "#008300", "#9085e9", "#e66767")

#: Eight and no more. A ninth series is never a generated hue -- past the
#: eighth slot the colours stop being separable under colour-vision
#: deficiency, and a chart that keeps going is lying about how many things
#: a reader can tell apart. Callers cap their selection at this and say so.
MAX_SERIES = len(CATEGORICAL_LIGHT)

#: One hue, light to dark, for magnitude. The dark mode is the same hue
#: re-stepped rather than the light ramp inverted: on either surface the
#: "near zero" end is the step nearest that surface, so an empty cell
#: recedes and a full one carries weight.
SEQUENTIAL_LIGHT = ("#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5",
                    "#256abf", "#184f95", "#0d366b")
SEQUENTIAL_DARK = ("#0d366b", "#184f95", "#256abf", "#3987e5",
                   "#6da7ec", "#9ec5f4", "#cde2fb")

#: Grid and axis ink, kept recessive so the marks carry the chart.
_GRID = "#8a8a85"

#: Altair renders at this height unless a caller asks for another. Tall
#: enough to read a trend, short enough to sit inside a card.
HEIGHT = 200

#: The most marks a scatterplot may carry. Past this a point is not a
#: player any more, it is texture: the marks overplot into a solid block,
#: the browser slows to a crawl, and no individual point can be hovered or
#: clicked -- which is the entire reason the chart is interactive. A query
#: that would exceed it must aggregate, bin or raise its threshold instead
#: of drawing the overflow.
SCATTER_CAP = 4000


def _dark() -> bool:
    try:
        return str(st.get_option("theme.base")).lower() == "dark"
    except Exception:                                    # noqa: BLE001
        return False


def series_colours() -> tuple[str, str]:
    """The two categorical hues, stepped for the active theme."""
    return SERIES_DARK if _dark() else SERIES_LIGHT


def categorical_colours(count: int | None = None) -> list:
    """The first `count` categorical hues, stepped for the active theme.

    Slots are handed out in the palette's fixed order and never cycled, so
    a hue belongs to whichever entity took that slot. Callers assign the
    slot from a *stable* ordering of their own -- an alphabetical club
    list, not a leaderboard -- because a colour that follows rank repaints
    every surviving series the moment a filter drops one.
    """
    palette = CATEGORICAL_DARK if _dark() else CATEGORICAL_LIGHT
    if count is None:
        return list(palette)
    return list(palette[:max(0, min(int(count), MAX_SERIES))])


def sequential_range() -> list:
    """The single-hue magnitude ramp, low to high, for the active theme."""
    return list(SEQUENTIAL_DARK if _dark() else SEQUENTIAL_LIGHT)


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


# --------------------------------------------------- league-wide shapes
#
# Everything below draws a whole competition rather than one career, and
# every one of them takes a frame that SQL has already reduced -- a row per
# season, a row per bin, a row per cell. None of them aggregates: a builder
# that groups its own input is a second place for the arithmetic to be
# wrong, and the denominators these charts need (recorded games, not rows)
# are only knowable in the query.


def season_trend_chart(frame: pd.DataFrame, season_column: str,
                       value_column: str, label: str,
                       brush: str | None = None, colour: str | None = None):
    """One league-wide measure across every season, as a line.

    A line rather than the career chart's bars: a competition is a
    continuous thing that ran every year, so the slope between two seasons
    is real -- which is exactly the claim a career chart must not make
    about a player who missed one.

    A season the source has no row for is absent rather than zero, and
    Altair's default is to join across it. That is the wrong reading for a
    competition that did not run, so the line is drawn with
    ``interpolate="linear"`` over the seasons present and the gap is left
    for the reader to see in the axis.

    `brush` names an interval selection over the x axis, for a caller
    passing the chart to ``st.altair_chart(..., on_select="rerun")`` to
    read a season range back out of.
    """
    if frame.empty or value_column not in frame.columns:
        return None
    data = frame[[season_column, value_column]].copy()
    data[value_column] = pd.to_numeric(data[value_column], errors="coerce")
    data = data.dropna(subset=[value_column]).sort_values(season_column)
    if data.empty:
        return None

    colour = colour or series_colours()[0]
    chart = (
        alt.Chart(data)
        .mark_line(strokeWidth=2, color=colour,
                   point=alt.OverlayMarkDef(size=45, color=colour))
        .encode(
            x=alt.X(f"{season_column}:Q", axis=_axis(None),
                    scale=alt.Scale(nice=False, zero=False)),
            y=alt.Y(f"{value_column}:Q", axis=_axis(label),
                    scale=alt.Scale(zero=False, nice=True)),
            tooltip=[alt.Tooltip(f"{season_column}:Q", title="Season",
                                 format="d"),
                     alt.Tooltip(f"{value_column}:Q", title=label,
                                 format=",")],
        )
        .properties(height=HEIGHT)
    )
    if brush:
        chart = chart.add_params(
            alt.selection_interval(name=brush, encodings=["x"]))
    return chart


def multi_series_chart(frame: pd.DataFrame, x_column: str, y_column: str,
                       series_column: str, x_title: str, y_title: str,
                       order: Sequence | None = None,
                       id_column: str | None = None, selection: str | None = None,
                       zero: bool = False, rule_at: float | None = None,
                       reverse_y: bool = False):
    """Several named entities' values across the same axis, one line each.

    `order` is the entities the caller means to draw, in a *stable*
    ordering -- the picker's own list, alphabetical -- and never the
    current ranking. Each one keeps the slot its position in `order` gives
    it whether or not the data has a row for it, which is the guarantee
    that matters in practice: narrowing the seasons until a club has no
    row left must not repaint the clubs that still do. A colour handed out
    by position in the *data* does exactly that, and a reader who learned
    which line was Carlton is then reading someone else's season.

    Declines rather than truncating past `MAX_SERIES`. Eight is where the
    palette stops being separable under colour-vision deficiency, and a
    ninth line drawn in a ninth colour is a chart claiming a distinction
    its reader cannot make. The caller caps the selection instead.

    `rule_at` draws one recessive reference line -- a .500 record, a zero
    differential -- because "above or below this" is most of what a
    longitudinal team chart is asked.

    `reverse_y` puts 1 at the top, for a finishing position: on a ladder
    the small number is the good one, and an axis that climbs draws a
    premiership at the bottom of the chart.
    """
    if frame.empty or not {x_column, y_column, series_column} <= set(frame.columns):
        return None
    data = frame.copy()
    data[y_column] = pd.to_numeric(data[y_column], errors="coerce")
    data = data.dropna(subset=[y_column]).sort_values([series_column, x_column])
    if data.empty:
        return None

    intended = list(dict.fromkeys(
        order if order is not None else data[series_column]))
    if not intended or len(intended) > MAX_SERIES:
        return None
    # The slot comes from the intended order, so an entity the data has no
    # row for holds its colour rather than handing it to the next one.
    palette = categorical_colours(len(intended))
    present = set(data[series_column])
    pairs = [(name, palette[slot]) for slot, name in enumerate(intended)
             if name in present]
    if not pairs:
        return None
    names = [name for name, _ in pairs]
    colours = [colour for _, colour in pairs]
    tooltip = [alt.Tooltip(f"{series_column}:N", title=None),
               alt.Tooltip(f"{x_column}:Q", title=x_title, format="d"),
               alt.Tooltip(f"{y_column}:Q", title=y_title, format=",.3~f")]
    if id_column and id_column in data.columns:
        tooltip.append(alt.Tooltip(f"{id_column}:N", title="Id"))

    lines = (
        alt.Chart(data)
        .mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=40))
        .encode(
            x=alt.X(f"{x_column}:Q", axis=_axis(x_title),
                    scale=alt.Scale(nice=False, zero=False)),
            y=alt.Y(f"{y_column}:Q", axis=_axis(y_title),
                    scale=alt.Scale(zero=zero, nice=True,
                                    reverse=bool(reverse_y))),
            color=alt.Color(
                f"{series_column}:N",
                scale=alt.Scale(domain=names, range=colours),
                legend=alt.Legend(title=None, labelColor=_GRID, orient="top",
                                  columns=min(4, len(names)))),
            tooltip=tooltip,
        )
    )
    if selection:
        lines = lines.add_params(alt.selection_point(
            name=selection, fields=[series_column], on="click"))
    if rule_at is None:
        return lines.properties(height=HEIGHT + 40)
    rule = (alt.Chart(pd.DataFrame({"_rule": [float(rule_at)]}))
            .mark_rule(color=_GRID, strokeWidth=1, opacity=0.7)
            .encode(y="_rule:Q"))
    return (rule + lines).properties(height=HEIGHT + 40)


def distribution_chart(frame: pd.DataFrame, bin_column: str,
                       count_column: str, x_title: str,
                       series_column: str | None = None,
                       order: Sequence | None = None,
                       bin_width: float = 1.0, share: bool = False):
    """A pre-binned distribution, one bar per bin.

    The binning is the query's job, not this function's: a histogram of
    237,000 baseball games must never travel to the browser as 237,000
    rows, and the bin width belongs with the code that knows the scale of
    the thing being binned. What arrives here is already (bin, count).

    With `series_column` the bins are drawn side by side rather than
    stacked -- two eras, or home against away -- because a stack answers
    "how many altogether" and the question a distribution is asked is
    "what shape". `share` switches the axis to each series' own percentage,
    which is the only honest comparison when the two series count
    different numbers of games.
    """
    if frame.empty or not {bin_column, count_column} <= set(frame.columns):
        return None
    data = frame.copy()
    for column in (bin_column, count_column):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=[bin_column, count_column])
    if data.empty or not data[count_column].any():
        return None

    value, axis_title = count_column, "Matches"
    if share:
        group = ([series_column] if series_column
                 and series_column in data.columns else [])
        totals = (data.groupby(group)[count_column].transform("sum")
                  if group else data[count_column].sum())
        data["Share"] = (data[count_column] / totals * 100).round(2)
        value, axis_title = "Share", "% of matches"

    tooltip = [alt.Tooltip(f"{bin_column}:Q", title=x_title, format=",.0f"),
               alt.Tooltip(f"{count_column}:Q", title="Matches", format=",")]
    if share:
        tooltip.append(alt.Tooltip("Share:Q", title="Share", format=".1f"))

    # A 2px gap between neighbouring bars: the surface between two fills is
    # what keeps a run of bars from reading as one block.
    encoding = {
        "x": alt.X(f"{bin_column}:Q", axis=_axis(x_title),
                   scale=alt.Scale(nice=False),
                   bin=alt.Bin(binned=True, step=float(bin_width))),
        "x2": alt.X2(f"{bin_column}_end:Q"),
        "y": alt.Y(f"{value}:Q", axis=_axis(axis_title), stack=None),
        "tooltip": tooltip,
    }
    data[f"{bin_column}_end"] = data[bin_column] + float(bin_width)

    if series_column and series_column in data.columns:
        # Same slot rule as `multi_series_chart`: the colour is the
        # position in the intended order, so an era with no matches in it
        # does not hand its hue to the era that does.
        intended = list(dict.fromkeys(
            order if order is not None else data[series_column]))
        if not intended or len(intended) > MAX_SERIES:
            return None
        palette = categorical_colours(len(intended))
        present = set(data[series_column])
        pairs = [(name, palette[slot])
                 for slot, name in enumerate(intended) if name in present]
        if not pairs:
            return None
        encoding["color"] = alt.Color(
            f"{series_column}:N",
            scale=alt.Scale(domain=[name for name, _ in pairs],
                            range=[colour for _, colour in pairs]),
            legend=alt.Legend(title=None, labelColor=_GRID, orient="top"))
        encoding["tooltip"] = [alt.Tooltip(f"{series_column}:N",
                                           title=None)] + tooltip
        mark = alt.Chart(data).mark_bar(opacity=0.68, stroke=None)
    else:
        mark = alt.Chart(data).mark_bar(color=series_colours()[0],
                                        opacity=0.9, stroke=None)
    return mark.encode(**encoding).properties(height=HEIGHT + 40)


def ranked_bar_chart(frame: pd.DataFrame, name_column: str,
                     value_column: str, value_title: str,
                     tooltip_columns: Sequence = (),
                     selection: str | None = None, limit: int = 20):
    """A ranked list drawn as horizontal bars, longest at the top.

    Horizontal because the labels are names -- grounds, players -- and a
    name rotated to fit under a vertical bar is a name nobody reads.

    One colour for every bar. Shading them by their own length would
    double-encode the value as hue, spend the only free channel on
    something the bar already says, and put a value ramp on categories
    that have no order but the one this chart just gave them.
    """
    if frame.empty or not {name_column, value_column} <= set(frame.columns):
        return None
    data = frame.copy()
    data[value_column] = pd.to_numeric(data[value_column], errors="coerce")
    data = (data.dropna(subset=[value_column])
            .sort_values(value_column, ascending=False)
            .head(max(1, int(limit))))
    if data.empty:
        return None

    order = list(data[name_column])
    tooltip = [alt.Tooltip(f"{name_column}:N", title=None),
               alt.Tooltip(f"{value_column}:Q", title=value_title,
                           format=",")]
    tooltip += [alt.Tooltip(f"{column}:N", title=column)
                for column in tooltip_columns if column in data.columns]
    chart = (
        alt.Chart(data)
        .mark_bar(height=12, color=series_colours()[0],
                  cornerRadiusTopRight=4, cornerRadiusBottomRight=4)
        .encode(
            y=alt.Y(f"{name_column}:N", sort=order, axis=_axis(None)),
            x=alt.X(f"{value_column}:Q", axis=_axis(value_title)),
            tooltip=tooltip,
        )
        .properties(height=max(HEIGHT, 22 * len(order)))
    )
    if selection:
        chart = chart.add_params(alt.selection_point(
            name=selection, fields=[name_column], on="click"))
    return chart


def coverage_heatmap(frame: pd.DataFrame, x_column: str, y_column: str,
                     value_column: str, x_title: str,
                     y_order: Sequence | None = None,
                     value_title: str = "Recorded",
                     selection: str | None = None):
    """A grid of magnitudes -- how much of each thing exists, when.

    One hue light to dark, because the value is a quantity and a rainbow
    would invent categories in a continuous scale. A cell the source has
    no row for is left out of `frame` entirely rather than passed as zero:
    "the competition did not play that season" and "the competition played
    and recorded none of this" are different facts, and only the second one
    is a zero. An absent cell therefore shows the surface through it.
    """
    if frame.empty or not {x_column, y_column, value_column} <= set(frame.columns):
        return None
    data = frame.copy()
    data[value_column] = pd.to_numeric(data[value_column], errors="coerce")
    data = data.dropna(subset=[value_column])
    if data.empty:
        return None

    rows = list(y_order) if y_order is not None else list(
        dict.fromkeys(data[y_column]))
    rows = [row for row in rows if row in set(data[y_column])]
    if not rows:
        return None

    chart = (
        alt.Chart(data)
        .mark_rect(stroke=None)
        .encode(
            x=alt.X(f"{x_column}:O", axis=alt.Axis(
                title=x_title, grid=False, labelColor=_GRID,
                titleColor=_GRID, tickColor=_GRID, domainColor=_GRID,
                labelAngle=0, labelOverlap="greedy")),
            y=alt.Y(f"{y_column}:N", sort=rows, axis=_axis(None)),
            color=alt.Color(
                f"{value_column}:Q",
                scale=alt.Scale(range=sequential_range(), domain=[0, 100]),
                legend=alt.Legend(title=value_title, labelColor=_GRID,
                                  titleColor=_GRID, orient="top",
                                  gradientLength=140, format=".0f")),
            tooltip=[alt.Tooltip(f"{y_column}:N", title=None),
                     alt.Tooltip(f"{x_column}:O", title=x_title),
                     alt.Tooltip(f"{value_column}:Q", title=value_title,
                                 format=".1f")],
        )
        # 18px a row keeps a 22-stat grid readable without scrolling and
        # still leaves each cell a target worth hovering.
        .properties(height=max(HEIGHT, 18 * len(rows)))
    )
    if selection:
        chart = chart.add_params(alt.selection_point(
            name=selection, fields=[x_column, y_column], on="click"))
    return chart
