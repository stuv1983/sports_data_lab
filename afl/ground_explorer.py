"""Ground Explorer: venue history, records, matchups and player leaders."""
from __future__ import annotations

import os
import sqlite3

import pandas as pd
import streamlit as st

import components


ANY = "Any"
PLAYER_METRICS = {
    "Games": ("COUNT(*)", None),
    "Wins": ("SUM(g.result='W')", None),
    "Goals": ("SUM(g.goals)", "goals"),
    "Behinds": ("SUM(g.behinds)", "behinds"),
    "Score (points)": (
        "SUM(COALESCE(g.goals, 0) * 6 + COALESCE(g.behinds, 0))", "goals"),
    "Marks": ("SUM(g.marks)", "marks"),
    "Disposals": ("SUM(g.disposals)", "disposals"),
    "Kicks": ("SUM(g.kicks)", "kicks"),
    "Handballs": ("SUM(g.handballs)", "handballs"),
    "Tackles": ("SUM(g.tackles)", "tackles"),
    "Hitouts": ("SUM(g.hitouts)", "hitouts"),
    "Clearances": ("SUM(g.clearances)", "clearances"),
    "Inside 50s": ("SUM(g.inside50s)", "inside50s"),
    "Rebound 50s": ("SUM(g.rebounds)", "rebounds"),
    "Contested possessions": ("SUM(g.contested)", "contested"),
    "Contested marks": ("SUM(g.contested_marks)", "contested_marks"),
    "Goal assists": ("SUM(g.goal_assists)", "goal_assists"),
    "One percenters": ("SUM(g.one_percenters)", "one_percenters"),
    "Brownlow votes": ("SUM(g.brownlow)", "brownlow"),
}
STATUS_FILTERS = {
    "All games": "1=1",
    "Wins": "g.result='W'",
    "Finals": "g.is_final=1",
    "Finals wins": "g.is_final=1 AND g.result='W'",
}


def _revision(db):
    try:
        stat = os.stat(db)
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


@st.cache_data(show_spinner=False, max_entries=20)
def _grounds(revision, _con) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT venue AS Ground, in_use AS Used, games AS Matches, "
        "average_score AS 'Average score', scores_100 AS '100+', profile_url "
        "FROM venue_summary ORDER BY games DESC, venue", _con)


@st.cache_data(show_spinner=False, max_entries=60)
def _clubs(ground, revision, _con) -> list[str]:
    return [row[0] for row in _con.execute(
        "SELECT club_now, COUNT(*) FROM games WHERE venue=? "
        "GROUP BY club_now ORDER BY COUNT(*) DESC, club_now", (ground,))]


@st.cache_data(show_spinner=False, max_entries=60)
def _team_records(ground, revision, _con) -> pd.DataFrame:
    return pd.read_sql_query(
        """SELECT team AS Club, played AS P, wins AS W, draws AS D,
                  losses AS L, points_for AS "For", points_against AS Against,
                  percentage AS "%", win_percentage AS "Win %",
                  scores_100_for AS "100+ for", scores_100_against AS "100+ against"
             FROM venue_team_records WHERE venue=? ORDER BY rank""",
        _con, params=(ground,))


@st.cache_data(show_spinner=False, max_entries=100)
def _matches(ground, year_from, year_to, club, opponent, revision,
             _con) -> pd.DataFrame:
    where = ["venue=?", "season BETWEEN ? AND ?"]
    params: list = [ground, int(year_from), int(year_to)]
    for team in (club, opponent):
        if team != ANY:
            where.append("(home_team_now=? OR away_team_now=?)")
            params.extend([team, team])
    return pd.read_sql_query(
        """SELECT season AS Season, round AS Round, match_date AS Date,
                  home_team AS Home, away_team AS Away,
                  CAST(home_score AS INTEGER) || '–' ||
                    CAST(away_score AS INTEGER) AS Score,
                  margin AS Margin, venue AS Ground, attendance AS Crowd,
                  CASE data_status WHEN 'player_stats' THEN 'Complete'
                    WHEN 'partial_player_stats' THEN 'Partial'
                    WHEN 'score_only' THEN 'Score only' ELSE data_status END AS Status
             FROM matches WHERE """ + " AND ".join(where) +
        " ORDER BY match_date DESC LIMIT 1000",
        _con, params=tuple(params)).dropna(axis=1, how="all")


@st.cache_data(show_spinner=False, max_entries=100)
def _leaders(ground, year_from, year_to, status, metric, min_games,
             revision, _con) -> pd.DataFrame:
    expression, stat = PLAYER_METRICS[metric]
    availability = f"AND g.{stat} IS NOT NULL" if stat else ""
    return pd.read_sql_query(
        f"""SELECT g.player AS Player, COUNT(*) AS Games,
                   SUM(g.result='W') AS Wins,
                   SUM(g.goals) AS Goals, SUM(g.marks) AS Marks,
                   SUM(g.disposals) AS Disposals,
                   SUM(g.brownlow) AS "Brownlow votes",
                   MIN(g.season) AS First, MAX(g.season) AS Last,
                   GROUP_CONCAT(DISTINCT g.club_hist) AS Clubs,
                   {expression} AS Value, g.player_id AS PlayerID
              FROM games g
             WHERE g.venue=? AND g.season BETWEEN ? AND ?
               AND {STATUS_FILTERS[status]} {availability}
             GROUP BY g.player_id
            HAVING COUNT(*) >= ?
             ORDER BY Value DESC, Games DESC, Player LIMIT 200""",
        _con, params=(ground, int(year_from), int(year_to), int(min_games)))


@st.cache_data(show_spinner=False, max_entries=60)
def _head_to_head(ground, club_a, club_b, year_from, year_to, revision,
                  _con):
    rows = pd.read_sql_query(
        """SELECT season AS Season, round AS Round, match_date AS Date,
                  home_team AS Home, away_team AS Away,
                  CAST(home_score AS INTEGER) || '–' ||
                    CAST(away_score AS INTEGER) AS Score,
                  CASE WHEN winner=home_team THEN home_team_now
                       WHEN winner=away_team THEN away_team_now
                       ELSE winner END AS Winner,
                  margin AS Margin, venue AS Ground,
                  attendance AS Crowd
             FROM matches
            WHERE venue=? AND season BETWEEN ? AND ?
              AND ((home_team_now=? AND away_team_now=?)
                OR (home_team_now=? AND away_team_now=?))
            ORDER BY match_date DESC""",
        _con, params=(ground, int(year_from), int(year_to),
                      club_a, club_b, club_b, club_a))
    wins_a = int((rows["Winner"].str.casefold() == club_a.casefold()).sum()) if len(rows) else 0
    wins_b = int((rows["Winner"].str.casefold() == club_b.casefold()).sum()) if len(rows) else 0
    return rows, wins_a, wins_b, len(rows) - wins_a - wins_b


@st.cache_data(show_spinner=False, max_entries=60)
def _venue_records(ground, revision, _con):
    records = pd.read_sql_query(
        """SELECT CASE category WHEN 'biggest_win' THEN 'Biggest win'
                    WHEN 'highest_score' THEN 'Highest score'
                    ELSE 'Lowest score' END AS Record,
                  record_value AS Value, team AS Team, team_score AS Score,
                  opponent AS Opponent, opponent_score AS "Opponent score",
                  match_date AS Date
             FROM venue_match_records WHERE venue=? ORDER BY category, rank""",
        _con, params=(ground,))
    careers = pd.read_sql_query(
        """SELECT CASE category WHEN 'most_games' THEN 'Games' ELSE 'Goals' END AS Record,
                  record_value AS Value, player AS Player, clubs AS Clubs
             FROM venue_player_records WHERE venue=? ORDER BY category, rank""",
        _con, params=(ground,))
    single = pd.read_sql_query(
        """SELECT CASE category WHEN 'most_goals_game' THEN 'Goals in a game'
                    ELSE 'Disposals in a game' END AS Record,
                  record_value AS Value, player AS Player,
                  match_description AS Match
             FROM venue_player_game_records WHERE venue=? ORDER BY category, rank""",
        _con, params=(ground,))
    return records, careers, single


def ground_explorer_page(sport, con: sqlite3.Connection) -> None:
    """Render the searchable ground section."""
    st.markdown("# Ground Explorer")
    st.caption(
        "Search every VFL/AFL ground: overall records, head-to-head results, "
        "player leaders, match history and imported AFL Tables records.")
    if not con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='venue_summary'").fetchone():
        st.info("Ground profiles are not loaded. Run `python -m utils.afl.load_venues --fetch`.")
        return

    revision = _revision(sport.db)
    grounds = _grounds(revision, con)
    options = grounds["Ground"].tolist()
    f1, f2 = st.columns([1.8, 2.2])
    ground = f1.selectbox("Ground", options, key="ground_pick")
    seasons = con.execute(
        "SELECT MIN(season), MAX(season) FROM matches WHERE venue=?",
        (ground,)).fetchone()
    lo, hi = f2.select_slider(
        "Seasons", options=list(range(int(seasons[0]), int(seasons[1]) + 1)),
        value=(int(seasons[0]), int(seasons[1])), key="ground_years")

    summary = grounds.loc[grounds["Ground"] == ground].iloc[0]
    with st.container(horizontal=True):
        st.metric("Used", summary["Used"], border=True)
        st.metric("Matches", f"{int(summary['Matches']):,}", border=True)
        st.metric("Average score", f"{summary['Average score']:.2f}", border=True)
        st.metric("Scores of 100+", f"{int(summary['100+']):,}", border=True)

    view = st.segmented_control(
        "Ground view",
        ["Overall", "Head to head", "Player leaders", "Matches", "Records"],
        default="Overall", key="ground_view")
    clubs = _clubs(ground, revision, con)

    if view == "Overall":
        st.markdown("### Club records")
        records = _team_records(ground, revision, con)
        components.clickable_entity_table(
            records, sport, con, key=f"ground_overall_{ground}",
            column_config={"%": st.column_config.NumberColumn(format="%.2f"),
                           "Win %": st.column_config.NumberColumn(format="%.2f")})
        st.link_button("AFL Tables ground profile", summary["profile_url"],
                       icon=":material/open_in_new:", type="tertiary")

    elif view == "Head to head":
        a1, a2 = st.columns(2)
        club_a = a1.selectbox("First club", clubs, key="ground_h2h_a")
        choices = [club for club in clubs if club != club_a]
        club_b = a2.selectbox("Second club", choices, key="ground_h2h_b")
        rows, wins_a, wins_b, draws = _head_to_head(
            ground, club_a, club_b, lo, hi, revision, con)
        with st.container(horizontal=True):
            st.metric("Played", len(rows), border=True)
            st.metric(f"{club_a} wins", wins_a, border=True)
            st.metric("Draws", draws, border=True)
            st.metric(f"{club_b} wins", wins_b, border=True)
        if rows.empty:
            st.info("These clubs did not meet at this ground in the selected seasons.")
        else:
            components.clickable_entity_table(
                rows.drop(columns=["Winner"]), sport, con,
                key=f"ground_h2h_{ground}_{club_a}_{club_b}",
                column_config={"Crowd": st.column_config.NumberColumn(format="%d")})

    elif view == "Player leaders":
        q1, q2 = st.columns([1.4, 1])
        status = q1.selectbox(
            "Match status", list(STATUS_FILTERS), key="ground_status")
        min_games = q2.number_input(
            "Minimum games", min_value=1, value=1, step=1,
            key="ground_min_games")
        st.caption(
            "Open any leaderboard below. Closed leaderboards are not queried.")

        for metric, (_, stat) in PLAYER_METRICS.items():
            leader_box = st.expander(
                f"{metric} leaders", expanded=(metric == "Games"),
                key=f"ground_leader_box_{metric}",
                icon=":material/leaderboard:", on_change="rerun")
            if not leader_box.open:
                continue
            with leader_box:
                if stat:
                    warning = sport.stat_era_warning(stat, season_from=lo)
                    if warning:
                        st.caption(f"⚠ {warning}")
                leaders = _leaders(
                    ground, lo, hi, status, metric, min_games, revision, con)
                if leaders.empty:
                    st.info("No players meet those filters.")
                    continue
                visible = leaders.drop(columns=["PlayerID"])
                visible = visible[[
                    "Player", "Value", "Games", "Wins", "Goals", "Marks",
                    "Disposals", "Brownlow votes", "First", "Last", "Clubs",
                ]]
                visible = visible.rename(columns={"Value": metric})
                # Avoid duplicate columns when the ranking metric is also
                # included as a standard context column.
                visible = visible.loc[:, ~visible.columns.duplicated()]
                st.caption("Select a player for their complete career.")
                components.clickable_player_table(
                    visible, leaders["PlayerID"].tolist(), sport, con,
                    key=f"ground_leaders_{ground}_{metric}_{status}")

    elif view == "Matches":
        m1, m2 = st.columns(2)
        club = m1.selectbox("Club", [ANY, *clubs], key="ground_match_club")
        opponents = [value for value in clubs if value != club]
        opponent = m2.selectbox("Opponent", [ANY, *opponents],
                                key="ground_match_opponent")
        rows = _matches(ground, lo, hi, club, opponent, revision, con)
        st.caption(f"{len(rows):,} matches; showing at most 1,000.")
        components.clickable_entity_table(
            rows, sport, con, key=f"ground_matches_{ground}",
            column_config={"Crowd": st.column_config.NumberColumn(format="%d")})

    else:
        match_records, careers, single = _venue_records(ground, revision, con)
        st.caption("Open or close each record book independently.")
        record_books = (
            ("Biggest wins", match_records, "Biggest win",
             ":material/trophy:"),
            ("Highest scores", match_records, "Highest score",
             ":material/trending_up:"),
            ("Lowest scores", match_records, "Lowest score",
             ":material/trending_down:"),
            ("Most career games", careers, "Games",
             ":material/calendar_month:"),
            ("Most career goals", careers, "Goals",
             ":material/sports_score:"),
            ("Most goals in a game", single, "Goals in a game",
             ":material/target:"),
            ("Most disposals in a game", single, "Disposals in a game",
             ":material/stat_3:"),
        )
        for index, (title, frame, value, icon) in enumerate(record_books):
            record_box = st.expander(
                title, expanded=(index == 0),
                key=f"ground_record_box_{index}", icon=icon,
                on_change="rerun")
            if not record_box.open:
                continue
            with record_box:
                subset = frame.loc[frame["Record"] == value].drop(
                    columns=["Record"])
                if subset.empty:
                    st.info("No imported records are available for this ground.")
                else:
                    components.clickable_entity_table(
                        subset, sport, con,
                        key=f"ground_records_{ground}_{index}")
