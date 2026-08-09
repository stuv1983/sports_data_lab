"""Stadium Explorer for NFL."""
from __future__ import annotations
import os
import sqlite3
import pandas as pd
import streamlit as st
import components


def _revision(db):
    try:
        stat = os.stat(db)
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


ANY = "Any"
PLAYER_METRICS = {
    "Games": ("COUNT(DISTINCT g.game_id)", None),
    "Wins": ("SUM(g.result='W')", None),
    "Touchdowns": ("SUM(g.touchdowns)", "touchdowns"),
    "Passing yards": ("SUM(g.passing_yards)", "passing_yards"),
    "Rushing yards": ("SUM(g.rushing_yards)", "rushing_yards"),
    "Receiving yards": ("SUM(g.receiving_yards)", "receiving_yards"),
}

STATUS_FILTERS = {
    "All games": "1=1",
    "Wins": "g.result='W'",
    "Playoffs": "g.is_playoff=1",
}

@st.cache_data(show_spinner=False, max_entries=20)
def _grounds(revision, _con) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT stadium AS Arena, COUNT(*) AS Matches FROM matches GROUP BY stadium ORDER BY Matches DESC", _con)

@st.cache_data(show_spinner=False, max_entries=60)
def _clubs(ground, revision, _con) -> list[str]:
    return [row[0] for row in _con.execute(
        "SELECT home_team, COUNT(*) FROM matches WHERE stadium=? "
        "GROUP BY home_team ORDER BY COUNT(*) DESC, home_team", (ground,))]

@st.cache_data(show_spinner=False, max_entries=60)
def _head_to_head(ground, club_a, club_b, year_from, year_to, revision, _con):
    rows = pd.read_sql_query(
        """SELECT season AS Season, week AS Round, gameday AS Date,
                  home_team AS Home, away_team AS Away,
                  CAST(home_score AS INTEGER) || '–' ||
                    CAST(away_score AS INTEGER) AS Score,
                  CASE WHEN home_score > away_score THEN home_team
                       WHEN away_score > home_score THEN away_team
                       ELSE 'Draw' END AS Winner,
                  ABS(home_score - away_score) AS Margin, stadium AS Ground
             FROM matches
            WHERE stadium=? AND season BETWEEN ? AND ?
              AND ((home_team=? AND away_team=?)
                OR (home_team=? AND away_team=?))
            ORDER BY gameday DESC""",
        _con, params=(ground, int(year_from), int(year_to),
                      club_a, club_b, club_b, club_a))
    wins_a = int((rows["Winner"] == club_a).sum()) if len(rows) else 0
    wins_b = int((rows["Winner"] == club_b).sum()) if len(rows) else 0
    return rows, wins_a, wins_b, len(rows) - wins_a - wins_b

@st.cache_data(show_spinner=False, max_entries=100)
def _matches(ground, year_from, year_to, club, opponent, revision, _con) -> pd.DataFrame:
    where = ["stadium=?", "season BETWEEN ? AND ?"]
    params: list = [ground, int(year_from), int(year_to)]
    for team in (club, opponent):
        if team != ANY:
            where.append("(home_team=? OR away_team=?)")
            params.extend([team, team])
    return pd.read_sql_query(
        """SELECT season AS Season, week AS Round, gameday AS Date,
                  home_team AS Home, away_team AS Away,
                  CAST(home_score AS INTEGER) || '–' ||
                    CAST(away_score AS INTEGER) AS Score,
                  ABS(home_score - away_score) AS Margin, stadium AS Ground
             FROM matches WHERE """ + " AND ".join(where) +
        " ORDER BY gameday DESC LIMIT 1000",
        _con, params=tuple(params)).dropna(axis=1, how="all")

@st.cache_data(show_spinner=False, max_entries=100)
def _leaders(ground, year_from, year_to, status, metric, min_games, revision, _con) -> pd.DataFrame:
    if metric == "Games" or metric == "Wins":
        expression = "COUNT(DISTINCT g.game_id)" if metric == "Games" else "SUM(g.result='W') / COUNT(g.game_id) * COUNT(DISTINCT g.game_id)"
        stat = None
    else:
        expression, stat = PLAYER_METRICS[metric]
    
    availability = f"AND g.{stat} IS NOT NULL" if stat else ""
    return pd.read_sql_query(
        f"""SELECT g.player_name AS Player, COUNT(DISTINCT g.game_id) AS Games,
                   SUM(g.result='W') / COUNT(g.game_id) * COUNT(DISTINCT g.game_id) AS Wins,
                   SUM(g.touchdowns) AS Touchdowns,
                   MIN(g.season) AS First, MAX(g.season) AS Last,
                   GROUP_CONCAT(DISTINCT g.club_hist) AS Teams,
                   {expression} AS Value, g.player_id AS PlayerID
              FROM games g
             WHERE g.venue=? AND g.season BETWEEN ? AND ?
               AND {STATUS_FILTERS[status]} {availability}
             GROUP BY g.player_id
            HAVING COUNT(DISTINCT g.game_id) >= ?
             ORDER BY Value DESC, Games DESC, Player LIMIT 200""",
        _con, params=(ground, int(year_from), int(year_to), int(min_games)))

def ground_explorer_page(sport, con: sqlite3.Connection) -> None:
    st.markdown("# Stadium Explorer")
    revision = _revision(sport.db)
    grounds = _grounds(revision, con)
    if grounds.empty:
        st.info("No stadiums found.")
        return
        
    options = grounds["Arena"].tolist()
    ground = st.selectbox("Stadium", options)
    
    seasons = con.execute("SELECT MIN(season), MAX(season) FROM matches WHERE stadium=?", (ground,)).fetchone()
    if not seasons or not seasons[0]:
        st.info("No matches found for this stadium.")
        return
        
    lo, hi = st.select_slider("Seasons", options=list(range(int(seasons[0]), int(seasons[1]) + 1)), value=(int(seasons[0]), int(seasons[1])))
    
    view = st.segmented_control("View", ["Head to head", "Player leaders", "Matches", "Records"], default="Player leaders")

    clubs = _clubs(ground, revision, con)

    if view == "Head to head":
        a1, a2 = st.columns(2)
        if len(clubs) < 2:
            st.info("Not enough clubs have played here to show head-to-head.")
        else:
            club_a = a1.selectbox("First team", clubs)
            choices = [club for club in clubs if club != club_a]
            club_b = a2.selectbox("Second team", choices)
            rows, wins_a, wins_b, draws = _head_to_head(ground, club_a, club_b, lo, hi, revision, con)
            with st.container(horizontal=True):
                st.metric("Played", len(rows), border=True)
                st.metric(f"{club_a} wins", wins_a, border=True)
                st.metric("Draws", draws, border=True)
                st.metric(f"{club_b} wins", wins_b, border=True)
            if rows.empty:
                st.info("These teams did not meet at this stadium in the selected seasons.")
            else:
                components.clickable_entity_table(
                    rows.drop(columns=["Winner"]), sport, con, key=f"h2h_{ground}_{club_a}_{club_b}")
                    
    elif view == "Matches":
        m1, m2 = st.columns(2)
        club = m1.selectbox("Team", [ANY, *clubs])
        opponents = [value for value in clubs if value != club]
        opponent = m2.selectbox("Opponent", [ANY, *opponents])
        rows = _matches(ground, lo, hi, club, opponent, revision, con)
        st.caption(f"{len(rows):,} matches; showing at most 1,000.")
        components.clickable_entity_table(rows, sport, con, key=f"matches_{ground}_{club}_{opponent}")

    elif view == "Player leaders":
        q1, q2 = st.columns([1.4, 1])
        status = q1.selectbox("Match status", list(STATUS_FILTERS))
        min_games = q2.number_input("Minimum games", min_value=1, value=1, step=1)
        st.caption("Open any leaderboard below. Closed leaderboards are not queried.")
        
        for metric, (_, stat) in PLAYER_METRICS.items():
            leader_box = st.expander(
                f"{metric} leaders", expanded=(metric == "Games"),
                key=f"ground_leader_box_{metric}",
                icon=":material/leaderboard:", on_change="rerun")
            if not leader_box.open:
                continue
            with leader_box:
                leaders = _leaders(ground, lo, hi, status, metric, min_games, revision, con)
                if leaders.empty:
                    st.info("No players meet those filters.")
                    continue
                visible = leaders.drop(columns=["PlayerID"])
                visible = visible[[
                    "Player", "Value", "Games", "Wins", "Touchdowns", "First", "Last", "Teams",
                ]]
                visible = visible.rename(columns={"Value": metric})
                visible = visible.loc[:, ~visible.columns.duplicated()]
                st.caption("Select a player for their complete career.")
                components.clickable_player_table(visible, leaders["PlayerID"].tolist(), sport, con, key=f"leaders_{ground}_{metric}")

    elif view == "Records":
        st.caption("Stadium records calculated from available matches and player games.")
        
        record_box_1 = st.expander("Biggest wins", expanded=True, icon=":material/trophy:")
        with record_box_1:
            biggest_wins = pd.read_sql_query(
                """SELECT gameday AS Date, home_team AS Home, away_team AS Away,
                          CAST(home_score AS INTEGER) || '–' || CAST(away_score AS INTEGER) AS Score,
                          ABS(home_score - away_score) AS Margin
                     FROM matches WHERE stadium=? ORDER BY Margin DESC LIMIT 10""", con, params=(ground,))
            if not biggest_wins.empty:
                components.clickable_entity_table(biggest_wins, sport, con, key=f"rec_bw_{ground}")
            else:
                st.info("No match data available.")
                
        record_box_2 = st.expander("Most career games", expanded=False, icon=":material/calendar_month:")
        with record_box_2:
            career_games = pd.read_sql_query(
                """SELECT player_name AS Player, COUNT(DISTINCT game_id) AS Games, MIN(season) AS First, MAX(season) AS Last
                     FROM games WHERE venue=? GROUP BY player_id ORDER BY Games DESC LIMIT 10""", con, params=(ground,))
            if not career_games.empty:
                components.clickable_entity_table(career_games, sport, con, key=f"rec_cg_{ground}")
                
        record_box_3 = st.expander("Most career touchdowns", expanded=False, icon=":material/sports_score:")
        with record_box_3:
            career_tds = pd.read_sql_query(
                """SELECT player_name AS Player, SUM(touchdowns) AS Touchdowns, COUNT(DISTINCT game_id) AS Games
                     FROM games WHERE venue=? AND touchdowns IS NOT NULL GROUP BY player_id ORDER BY Touchdowns DESC LIMIT 10""", con, params=(ground,))
            if not career_tds.empty:
                components.clickable_entity_table(career_tds, sport, con, key=f"rec_ct_{ground}")
                
        record_box_4 = st.expander("Most passing yards in a game", expanded=False, icon=":material/target:")
        with record_box_4:
            game_pass = pd.read_sql_query(
                """SELECT player_name AS Player, passing_yards AS "Passing Yards", date AS Date, opponent_team AS Opponent
                     FROM games WHERE venue=? AND passing_yards IS NOT NULL ORDER BY passing_yards DESC LIMIT 10""", con, params=(ground,))
            if not game_pass.empty:
                components.clickable_entity_table(game_pass, sport, con, key=f"rec_gp_{ground}")

        record_box_5 = st.expander("Most rushing yards in a game", expanded=False, icon=":material/target:")
        with record_box_5:
            game_rush = pd.read_sql_query(
                """SELECT player_name AS Player, rushing_yards AS "Rushing Yards", date AS Date, opponent_team AS Opponent
                     FROM games WHERE venue=? AND rushing_yards IS NOT NULL ORDER BY rushing_yards DESC LIMIT 10""", con, params=(ground,))
            if not game_rush.empty:
                components.clickable_entity_table(game_rush, sport, con, key=f"rec_gr_{ground}")
