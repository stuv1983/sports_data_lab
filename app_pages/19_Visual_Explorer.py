"""Visual Explorer -- the database drawn rather than listed.

Every other Explore page answers a question with rows. This one answers
the questions whose answer is a shape: when a competition grew, what a
career looked like from one end to the other, whether a club's decade was
a decline or a plateau, how often a match is close, and which statistics
the record actually holds for which seasons.

WHERE THE WORK HAPPENS
----------------------
Not here. The aggregation is SQLite's, in visual_queries.py, and the
drawing is Altair's, in charts.py. This file picks the section, collects
the filters, hands them over, and wires what a reader clicks to the
detail cards components.py already owns. That split is what keeps the
page honest about grain and denominators: every number on screen was
computed by a query that knows what one row of that sport's `games` table
actually is.

WHAT A SECTION MAY OFFER
------------------------
`visual_queries.capabilities` measures the open database once per
revision, and a section renders only what its answer allows. The MLB is
the case that makes this necessary rather than tidy: a row of its `games`
is a player's *season* with one club, because Lahman has no box scores.
So its trajectory charts are labelled per season, its rate axes divide by
the games a row stands for, and it is offered no single-game view at all
-- while its match distributions work fine, because the Retrosheet game
logs loaded beside Lahman are genuinely game-level.

STATE
-----
Every widget key goes through `SPORT.k("visual", ...)`, which prefixes the
sport. That is not only a namespace: `sports.picker` drops every key
beginning with the previous sport's prefix when the reader switches
sports, so an AFL stat selection cannot survive into an NBA render and
pick a column that database has never heard of.
"""

import pandas as pd
import streamlit as st

import charts
import components
import labels
import ui_widgets
import visual_queries as vq

SPORT = st.session_state.SPORT
con = st.session_state.con
DB_REVISION = st.session_state.DB_REVISION
SCHEMA = SPORT.schema
V = SPORT.vocab

CAPS = vq.capabilities(SPORT.key, DB_REVISION, con)

SECTIONS = ["Overview", "Players", "Teams", "Matches", "Venues", "Awards"]


def k(*parts):
    """A widget key namespaced to this sport and this page."""
    return SPORT.k("visual", *parts)


def _season_range(key_part, span, label="Seasons"):
    """A season-range slider over the span the data actually covers.

    Returned as a tuple for the query layer, or () for "everything" --
    which lets a caller skip the BETWEEN entirely rather than binding the
    full range and asking SQLite to filter nothing.
    """
    if not span or span[0] >= span[1]:
        return ()
    low, high = int(span[0]), int(span[1])

    # A range applied from the Overview brush has to survive into a
    # section whose slider has already been drawn once. Streamlit ignores
    # a widget's `default` the moment its key holds a value, so the key is
    # dropped -- before the widget is created, which is the only point it
    # is legal -- and the applied range recorded as consumed so this fires
    # once per apply rather than on every rerun.
    applied = st.session_state.get(k(key_part, "applied"))
    consumed = k(key_part, "consumed")
    if applied and st.session_state.get(consumed) != applied:
        st.session_state.pop(k(key_part), None)
        st.session_state[consumed] = applied

    default = applied or (low, high)
    default = (max(low, int(default[0])), min(high, int(default[1])))
    chosen = st.slider(label, low, high, default, key=k(key_part))
    return () if chosen == (low, high) else (int(chosen[0]), int(chosen[1]))


def _brushed_seasons(event, name):
    """The season range a reader dragged out of a chart, or None.

    Altair reports an interval selection as the encoded field's extent.
    A click that selects nothing comes back as an empty dict, which is not
    a range and must not be read as one.
    """
    selection = getattr(event, "selection", None) or {}
    interval = selection.get(name) or {}
    values = interval.get("Season") if isinstance(interval, dict) else None
    if not values or len(values) < 2:
        return None
    low, high = sorted(float(value) for value in values[:2])
    return round(low), round(high)


def _empty(message, hint=""):
    st.info(message)
    if hint:
        st.caption(hint)


# ------------------------------------------------------------- heading

st.markdown("# Visual Explorer")
st.caption(f"{SPORT.label.replace(' Data Lab', '')} drawn by "
           f"{V.season}, {V.club}, career and {V.game} — every chart "
           "aggregated in the database, not in the browser.")

with st.expander("What this database can be asked to draw", expanded=False):
    grain = ("one row per player per "
             f"{V.game}" if CAPS.player_game_grain
             else f"one row per player per {V.season}")
    st.caption(f"`{SCHEMA.games}` holds {grain}"
               + (f", covering {CAPS.season_range[0]}–{CAPS.season_range[1]}."
                  if CAPS.season_range else "."))
    for label, ready, note in CAPS.summary():
        icon = ":material/check_circle:" if ready else ":material/block:"
        st.markdown(f"{icon} **{label}** — {note}")
    for label, hint in CAPS.missing.items():
        st.caption(f"{label}: {hint}")

section = st.segmented_control(
    "Section", SECTIONS, default="Overview", key=k("section"),
    label_visibility="collapsed")


# ------------------------------------------------------------ overview

@st.fragment
def _activity_section(activity):
    """League participation by season, with a season-range brush.

    A fragment because the brush reruns on every drag: isolating it keeps
    a dragged range from re-rendering the coverage heatmap below, which is
    the most expensive thing on the page.
    """
    available = [name for name in vq.ACTIVITY_METRICS
                 if name in activity.columns
                 and activity[name].notna().any()]
    if not available:
        _empty("This database records no per-season activity to draw.")
        return

    metric = st.segmented_control(
        "Measure", available, default=available[0], key=k("activity_metric"),
        format_func=lambda name: vq.activity_label(SPORT, CAPS, name))
    metric = metric or available[0]
    label = vq.activity_label(SPORT, CAPS, metric)

    chart = charts.season_trend_chart(activity, "Season", metric, label,
                                      brush="season_span")
    if chart is None:
        _empty(f"No {label.lower()} recorded.")
        return
    event = st.altair_chart(chart, on_select="rerun", width="stretch",
                            key=k("activity_chart", metric))
    st.caption(f"{label} in each {V.season} the database covers. "
               "Drag across the chart to pick a range of "
               f"{V.season}s, then apply it to the other sections.")

    brushed = _brushed_seasons(event, "season_span")
    if brushed:
        low, high = brushed
        with st.container(horizontal=True, vertical_alignment="center"):
            st.caption(f"Selected **{low}–{high}**")
            if st.button("Apply to filters", key=k("apply_brush"),
                         icon=":material/filter_alt:", type="tertiary"):
                # Written to the *applied* keys the sliders seed from, not
                # to the slider keys themselves: assigning to a live
                # widget's key while that widget is on screen is the one
                # thing Streamlit forbids outright.
                for part in ("player_span", "team_span", "match_span",
                             "landscape_span"):
                    st.session_state[k(part, "applied")] = (low, high)
                st.rerun()


@st.fragment
def _coverage_section():
    """Which statistics the record holds, season by season.

    A fragment for the same reason as the activity chart above: its query
    is the page's heaviest, and nothing else on the Overview should be
    able to make it run again.
    """
    st.markdown("#### Data coverage")
    if not CAPS.coverage:
        _empty("This database records no statistics to measure coverage of.")
        return

    default = list(CAPS.stats[:12])
    chosen = st.multiselect(
        "Statistics", list(CAPS.stats), default=default,
        format_func=labels.title, key=k("coverage_stats"))
    if not chosen:
        st.caption("Pick at least one statistic.")
        return

    frame = vq.stat_coverage(SPORT.key, DB_REVISION, con, tuple(chosen))
    chart = charts.coverage_heatmap(
        frame, "Season", "Statistic", "Coverage", V.season.capitalize(),
        y_order=vq.coverage_rows(CAPS, chosen),
        value_title="% of rows recorded", selection="coverage_cell")
    if chart is None:
        _empty("Nothing to measure under those statistics.")
        return
    event = st.altair_chart(chart, on_select="rerun", width="stretch",
                            key=k("coverage_chart"))
    st.caption(
        f"The share of that {V.season}'s `{SCHEMA.games}` rows carrying a "
        "value. A pale cell is a statistic nobody recorded yet, not a "
        f"{V.season} of zeroes; a {V.season} the competition did not play "
        "has no cell at all. Click a cell for the numbers behind it.")

    picked = (getattr(event, "selection", None) or {}).get("coverage_cell") or []
    if picked:
        rows = pd.DataFrame(picked)
        detail = frame.merge(rows[["Season", "Statistic"]].drop_duplicates(),
                             on=["Season", "Statistic"], how="inner")
        if not detail.empty:
            shown = detail.rename(columns={
                "Rows_": f"{SCHEMA.games.capitalize()} rows",
                "Coverage": "% recorded"})
            components.clickable_season_table(
                shown, shown["Season"].tolist(), SPORT, con,
                key=k("coverage_pick"), hide_index=True)


if section == "Overview":
    st.markdown("#### League activity")
    if not CAPS.league_activity:
        _empty("This database has no seasons to summarise.")
    else:
        activity = vq.league_activity(SPORT.key, DB_REVISION, con)
        if activity.empty:
            _empty("No activity recorded.")
        else:
            _activity_section(activity)
            latest = activity.iloc[-1]
            with st.container(horizontal=True):
                for name in vq.ACTIVITY_METRICS:
                    if name in activity.columns and pd.notna(latest.get(name)):
                        st.metric(vq.activity_label(SPORT, CAPS, name),
                                  f"{int(latest[name]):,}",
                                  help=f"In {int(latest['Season'])}",
                                  border=True)
    st.divider()
    _coverage_section()


# ------------------------------------------------------------- players

elif section == "Players":
    st.markdown("#### Career trajectory")
    if not CAPS.player_trajectory:
        _empty("This database records no statistics to trace a career with.")
    else:
        left, right = st.columns([2, 1])
        with left:
            picked = ui_widgets.player_picker(
                k("player"), sport=SPORT, db_revision=DB_REVISION,
                label="Player")
        with right:
            stat = st.selectbox("Statistic", list(CAPS.stats),
                                format_func=labels.title, key=k("player_stat"))

        # `stat_era_warning` is the Grid Solver's sentence -- it ends "cannot
        # satisfy this square" and fires even for a statistic recorded from
        # the competition's first season. Here the same fact is a coverage
        # note, and only worth making when the statistic really does start
        # after the data does.
        first = SPORT.stat_available_from(stat)
        if first and CAPS.season_range and first > CAPS.season_range[0]:
            st.caption(f"⚠ {labels.title(stat)} was not recorded before "
                       f"{first}. Earlier {V.season}s of a career show no "
                       "bar rather than a zero.")

        if picked is None:
            st.caption("Pick a player to draw their career.")
        else:
            pid, name = picked
            seasons = vq.player_seasons(SPORT.key, DB_REVISION, con, pid, stat)
            if seasons.empty:
                _empty(f"{name} has no recorded {labels.words(stat)}.")
            elif stat in CAPS.rate_stats:
                # A rate is not summed and not averaged across clubs: one
                # mark per row, exactly as the source recorded it.
                chart = charts.multi_series_chart(
                    seasons.assign(Series=labels.title(stat)),
                    "Season", "Value", "Series",
                    V.season.capitalize(), labels.title(stat))
                if chart is not None:
                    st.altair_chart(chart, width="stretch",
                                    key=k("player_rate", stat))
                st.caption(
                    f"{labels.title(stat)} is a rate, so it is shown as the "
                    "source recorded it and never added up or averaged "
                    f"across {V.clubs} — combining two would need innings "
                    "these tables do not carry.")
                components.clickable_entity_table(
                    seasons, SPORT, con, key=k("player_rate_rows", stat),
                    hide_index=True)
            else:
                rate_label = f"Per {V.game}"
                seasons = vq.with_rate(seasons, rate_label)
                unit = (V.game if CAPS.player_game_grain
                        else f"{V.game} the {V.season} row stands for")
                totals, rates = st.columns(2)
                with totals:
                    chart = charts.career_chart(
                        seasons, "Season", "Total", labels.title(stat))
                    if chart is not None:
                        st.altair_chart(chart, width="stretch",
                                        key=k("player_total", stat))
                    st.caption(f"{labels.title(stat)} each {V.season}.")
                with rates:
                    chart = charts.career_chart(
                        seasons, "Season", rate_label,
                        f"{labels.title(stat)} per {V.game}",
                        colour=charts.series_colours()[1])
                    if chart is not None:
                        st.altair_chart(chart, width="stretch",
                                        key=k("player_rate", stat))
                    st.caption(f"Per {unit} the statistic was recorded in.")

                # Two charts rather than one with two axes. Games and a
                # per-game rate are different sizes, and plotting them
                # against each other invents a relationship.
                st.caption(
                    f"A {V.season} missing from the axis is one "
                    f"{name} did not play; a {V.season} with no recorded "
                    f"{labels.words(stat)} is left out rather than drawn "
                    "as a zero.")
                shown = seasons.rename(columns={
                    "Total": labels.title(stat),
                    "Played": V.games.capitalize(),
                    "Recorded": f"{V.games.capitalize()} recorded"})
                components.clickable_entity_table(
                    shown, SPORT, con, key=k("player_rows", stat),
                    hide_index=True)

            with st.container(horizontal=True):
                components.player_button(
                    f"Open {name}'s full card", SPORT, con, pid,
                    key=k("player_card", pid))

    st.divider()
    st.markdown("#### Volume against efficiency")
    if not CAPS.volume_efficiency:
        _empty("This database records no statistics to compare.")
    else:
        controls = st.columns([1.4, 1, 1])
        landscape_stat = controls[0].selectbox(
            "Statistic", [s for s in CAPS.stats if s not in CAPS.rate_stats],
            format_func=labels.title, key=k("landscape_stat"))
        floor = controls[1].number_input(
            f"Minimum {V.games} recorded", min_value=1, max_value=1000,
            value=100 if CAPS.player_game_grain else 300, step=10,
            key=k("landscape_floor"),
            help="A rate over a handful of games is noise; this is the "
                 "sample below which a career is left off the chart.")
        marks = controls[2].number_input(
            "Careers shown", min_value=100, max_value=charts.SCATTER_CAP,
            value=1500, step=100, key=k("landscape_marks"),
            help=f"Capped at {charts.SCATTER_CAP:,}: past a few thousand "
                 "marks no single point can be hovered or clicked.")

        with st.container():
            span = _season_range("landscape_span", CAPS.season_range)
        frame = vq.volume_efficiency(
            SPORT.key, DB_REVISION, con, landscape_stat, span,
            int(floor), int(marks))
        volume_label, rate_label = vq.efficiency_labels(SPORT, landscape_stat)
        chart = charts.quadrant_chart(
            frame, volume_label, rate_label, "Player",
            x_title=volume_label, y_title=rate_label, id_column="PlayerID")
        if chart is None:
            _empty("No career clears that threshold in those "
                   f"{V.season}s.")
        else:
            event = st.altair_chart(chart, on_select="rerun", width="stretch",
                                    key=k("landscape", landscape_stat))
            st.caption(
                f"{len(frame):,} careers, each with at least {int(floor):,} "
                f"{V.games} the statistic was recorded in. The rate divides "
                f"by those recorded {V.games} and not by {V.games} played, "
                "so a career that began before the statistic did is not "
                "penalised for the seasons nobody counted. Dashed lines are "
                "the medians of the field shown. Click a point to open that "
                "player.")
            chosen = [row.get("PlayerID")
                      for row in (getattr(event, "selection", None) or {})
                      .get("quadrant", [])
                      if row.get("PlayerID") is not None]
            if chosen:
                rows = frame[frame["PlayerID"].isin(chosen)]
                components.clickable_player_table(
                    rows.drop(columns=["PlayerID"]),
                    rows["PlayerID"].tolist(), SPORT, con,
                    key=k("landscape_pick", landscape_stat))


# --------------------------------------------------------------- teams

elif section == "Teams":
    st.markdown(f"#### {V.clubs.capitalize()} over time")
    if not CAPS.team_trends:
        _empty(f"This database has no {V.club} records to trace.",
               SPORT.past_games_hint)
    else:
        options = vq.team_options(SPORT.key, DB_REVISION, con)
        if not options:
            _empty(f"No {V.clubs} recorded.")
        else:
            default = options[:min(4, len(options))]
            teams = st.multiselect(
                V.clubs.capitalize(), options, default=default,
                key=k("teams"),
                max_selections=charts.MAX_SERIES,
                help=f"Up to {charts.MAX_SERIES}. Past that the palette "
                     "stops being separable under colour-vision "
                     "deficiency, and a ninth line would claim a "
                     "distinction a reader cannot make.")

            metrics = dict(vq.TEAM_METRICS)
            if CAPS.team_rank_column:
                metrics[CAPS.team_rank_label] = ("Rank", None, False)
            controls = st.columns([1.3, 1])
            measure = controls[0].selectbox(
                "Measure", list(metrics), key=k("team_metric"))
            include_finals = False
            if CAPS.team_postseason_toggle:
                include_finals = controls[1].toggle(
                    f"Include {V.postseason}", value=False,
                    key=k("team_finals"),
                    help=f"Off, the chart is the {V.season} proper.")
            span = _season_range("team_span",
                                 CAPS.season_range or CAPS.match_season_range)

            if not teams:
                st.caption(f"Pick at least one {V.club}.")
            else:
                frame = vq.team_seasons(
                    SPORT.key, DB_REVISION, con, tuple(teams), span,
                    include_finals)
                column, rule, zero = metrics[measure]
                if frame.empty or column not in frame.columns:
                    _empty(f"No records for those {V.clubs} in those "
                           f"{V.season}s.")
                else:
                    # The colour order is the *selection* in the league's
                    # own alphabetical order, not the whole league (which
                    # runs to 141 clubs for one build) and not the
                    # leaderboard. Narrowing the seasons until a club has
                    # no row left therefore leaves the others' hues alone.
                    order = [club for club in options if club in set(teams)]
                    chart = charts.multi_series_chart(
                        frame, "Season", column, "Team",
                        V.season.capitalize(), measure,
                        order=order, selection="team",
                        zero=zero, rule_at=rule,
                        reverse_y=(column == "Rank"))
                    if chart is None:
                        _empty("Nothing to draw for that measure.")
                    else:
                        event = st.altair_chart(
                            chart, on_select="rerun", width="stretch",
                            key=k("team_chart", measure))
                        source = ("the competition's own season table"
                                  if CAPS.team_season_table
                                  else f"aggregated {V.game} results")
                        st.caption(
                            f"{measure} by {V.season}, from {source}. "
                            f"Colour follows the {V.club} and not its "
                            "position, so filtering the list never "
                            "repaints the lines that remain. Click a line "
                            f"to open that {V.club}.")
                        picked = [row.get("Team") for row in
                                  (getattr(event, "selection", None) or {})
                                  .get("team", []) if row.get("Team")]
                        shown = (frame[frame["Team"].isin(picked)]
                                 if picked else frame)
                        # Every row names both a season and a club, so the
                        # table opens either card -- which is why the
                        # click on the line only has to narrow it.
                        components.clickable_season_table(
                            shown, shown["Season"].tolist(), SPORT, con,
                            clubs=shown["Team"].tolist(),
                            key=k("team_rows", measure), hide_index=True)


# ------------------------------------------------------------- matches

elif section == "Matches":
    st.markdown(f"#### {V.game.capitalize()} margins")
    if not CAPS.match_distributions:
        _empty(f"This database has no {V.game}-level {V.club} rows, so it "
               "cannot say how close a "
               f"{V.game} was.", SPORT.past_games_hint)
    else:
        width = vq.margin_bin_width(SPORT.key, DB_REVISION, con)
        splits = ["None", "Era"]
        if CAPS.home_away:
            splits.append("Home and away")
        controls = st.columns([1.4, 1])
        split = controls[0].segmented_control(
            "Compare", splits, default="None", key=k("margin_split"))
        split = split or "None"
        share = controls[1].toggle(
            "As a share", value=split != "None", key=k("margin_share"),
            help="Two eras played different numbers of matches, so only "
                 "the percentages compare.")
        span = _season_range("match_span",
                             CAPS.match_season_range or CAPS.season_range)

        if split == "Home and away":
            frame = vq.home_away_margins(SPORT.key, DB_REVISION, con,
                                         width, span)
            chart = charts.distribution_chart(
                frame, "Bin", "Matches", "Margin", series_column="Side",
                order=["Home", "Away"], bin_width=width, share=share)
            note = (f"Every {V.game} counted twice, once from each side, so "
                    "the gap between the two curves is the home ground's "
                    f"worth. {V.postseason.capitalize()} are left out "
                    "entirely rather than folded into one side: they have "
                    "no home team.")
        else:
            frame = vq.margin_distribution(SPORT.key, DB_REVISION, con,
                                           width, span, split)
            series = "Era" if split == "Era" else None
            order = (sorted(frame["Era"].unique())
                     if series and not frame.empty else None)
            chart = charts.distribution_chart(
                frame, "Bin", "Matches", "Winning margin",
                series_column=series, order=order, bin_width=width,
                share=share)
            note = (f"The winning margin of each {V.game}, in bins of "
                    f"{width}. A draw is a margin of nought.")
            if split == "Era":
                note += (" The two series are the first and last thirds of "
                         f"the {V.season}s in range.")

        if chart is None:
            _empty(f"No {V.games} in those {V.season}s.")
        else:
            st.altair_chart(chart, width="stretch",
                            key=k("margin_chart", split, share))
            st.caption(note)

    st.divider()
    st.markdown("#### Scoring by season")
    if not CAPS.match_distributions:
        _empty(f"No {V.game}-level scores to summarise.")
    else:
        scoring = vq.scoring_by_season(
            SPORT.key, DB_REVISION, con,
            _season_range("scoring_span",
                          CAPS.match_season_range or CAPS.season_range))
        if scoring.empty:
            _empty(f"No {V.games} in those {V.season}s.")
        else:
            measures = ["Average total score", "Average winning margin"]
            measure = st.segmented_control(
                "Measure", measures, default=measures[0],
                key=k("scoring_metric")) or measures[0]
            chart = charts.season_trend_chart(
                scoring, "Season", measure, measure)
            if chart is None:
                _empty("Nothing to draw.")
            else:
                st.altair_chart(chart, width="stretch",
                                key=k("scoring_chart", measure))
                st.caption(
                    f"Per {V.game}, across every {V.game} of that "
                    f"{V.season}. The total is both sides added together, "
                    f"counted once per {V.game}.")
                components.clickable_season_table(
                    scoring, scoring["Season"].tolist(), SPORT, con,
                    key=k("scoring_rows"), hide_index=True)


# -------------------------------------------------------------- venues

elif section == "Venues":
    st.markdown(f"#### Busiest {V.venues}")
    if not CAPS.venue_charts:
        # Named rather than blank: the NBA build has the column and fills
        # in under two per cent of it, and "no data" and "almost no data"
        # want different answers from a reader.
        share = (f"Only {CAPS.venue_coverage:.1f}% of this database's "
                 f"{V.game} rows name one" if CAPS.venues
                 else f"This database records no {V.venue} on its "
                      f"{V.game} rows")
        _empty(f"{share}, so a busiest-{V.venues} chart would rank the few "
               "matches somebody happened to record rather than the "
               f"{V.venues}.", SPORT.past_games_hint)
    else:
        controls = st.columns([1, 2])
        top = controls[0].number_input(
            f"{V.venues.capitalize()} shown", min_value=5, max_value=40,
            value=15, step=5, key=k("venue_top"))
        with controls[1]:
            span = _season_range("venue_span",
                                 CAPS.match_season_range or CAPS.season_range)
        frame = vq.busiest_venues(SPORT.key, DB_REVISION, con, span, int(top))
        if frame.empty:
            _empty(f"No {V.games} in those {V.season}s.")
        else:
            chart = charts.ranked_bar_chart(
                frame, "Venue", "Matches", f"{V.games.capitalize()} hosted",
                tooltip_columns=["From", "To"], selection="venue",
                limit=int(top))
            event = st.altair_chart(chart, on_select="rerun", width="stretch",
                                    key=k("venue_chart"))
            st.caption(
                f"Counted once per {V.game}, not once per {V.club}: the "
                "source holds a row for each side. Click a bar to trace "
                f"that {V.venue} through the {V.season}s.")
            picked = [row.get("Venue") for row in
                      (getattr(event, "selection", None) or {})
                      .get("venue", []) if row.get("Venue")]
            components.clickable_entity_table(
                frame, SPORT, con, key=k("venue_rows"), hide_index=True)

            if picked:
                st.markdown(f"#### {', '.join(picked[:charts.MAX_SERIES])}")
                over_time = vq.venue_by_season(
                    SPORT.key, DB_REVISION, con,
                    tuple(picked[:charts.MAX_SERIES]), span)
                measures = [name for name in vq.VENUE_METRICS
                            if name in over_time.columns
                            and over_time[name].notna().any()]
                measure = st.segmented_control(
                    "Measure", measures, default=measures[0] if measures
                    else None, key=k("venue_metric")) or (
                        measures[0] if measures else None)
                chart = charts.multi_series_chart(
                    over_time, "Season", measure, "Venue",
                    V.season.capitalize(), measure,
                    order=sorted(picked)) if measure else None
                if chart is None:
                    _empty(f"Nothing recorded for that {V.venue}.")
                else:
                    st.altair_chart(chart, width="stretch",
                                    key=k("venue_trend", measure))
                    st.caption(
                        f"A {V.season} with no mark is one the {V.venue} "
                        "did not host, which is a fact worth seeing rather "
                        "than a zero to plot.")


# -------------------------------------------------------------- awards

elif section == "Awards":
    st.markdown("#### Awards")
    if not CAPS.award_charts:
        _empty("This database has no awards table loaded.",
               SPORT.loader_hints.get("awards_available", ""))
    else:
        catalogue = vq.award_options(SPORT.key, DB_REVISION, con)
        if catalogue.empty:
            _empty("No awards recorded.")
        else:
            names = dict(zip(catalogue["Key"], catalogue["Award"]))
            controls = st.columns([2, 1])
            award = controls[0].selectbox(
                "Award", list(catalogue["Key"]),
                format_func=lambda key: str(names.get(key, key)),
                key=k("award"))
            top = controls[1].number_input(
                "Recipients shown", min_value=5, max_value=40, value=15,
                step=5, key=k("award_top"))
            row = catalogue[catalogue["Key"] == award].iloc[0]
            with st.container(horizontal=True):
                st.metric("Records", f"{int(row['Records']):,}", border=True)
                st.metric("First", f"{int(row['From'])}", border=True)
                st.metric("Latest", f"{int(row['To'])}", border=True)

            span = _season_range("award_span",
                                 (int(row["From"]), int(row["To"])))
            by_season = vq.award_by_season(SPORT.key, DB_REVISION, con,
                                           award, span)
            chart = charts.season_trend_chart(
                by_season, "Season", "Recipients", "Recipients")
            if chart is None:
                _empty("Nothing recorded in those seasons.")
            else:
                st.altair_chart(chart, width="stretch",
                                key=k("award_chart", award))
                st.caption(
                    "How many rows the source files for that award each "
                    f"{V.season} — one for a medal, a whole team for a "
                    f"selection. A {V.season} with no mark is one the "
                    "source has nothing for, which for an award that ran "
                    "continuously is a gap in the data rather than a "
                    f"{V.season} nobody won it.")

            st.markdown("#### Most decorated")
            leaders = vq.award_leaders(SPORT.key, DB_REVISION, con, award,
                                       span, int(top))
            if leaders.empty:
                _empty("No named recipients recorded for that award.")
            else:
                chart = charts.ranked_bar_chart(
                    leaders, "Recipient", "Awards", "Times won",
                    tooltip_columns=["First", "Last"], limit=int(top))
                if chart is not None:
                    st.altair_chart(chart, width="stretch",
                                    key=k("award_leaders", award))
                if "PlayerID" in leaders.columns:
                    components.clickable_player_table(
                        leaders.drop(columns=["PlayerID"]),
                        leaders["PlayerID"].tolist(), SPORT, con,
                        key=k("award_rows", award))
                else:
                    st.caption(
                        "This build's awards table names its winners as "
                        "text with no link to a player, so a row here "
                        "opens no card.")
                    components.clickable_entity_table(
                        leaders, SPORT, con, key=k("award_rows", award),
                        hide_index=True)
