"""Ballpark Explorer for MLB."""
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


PLAYER_METRICS = {
    "Seasons": ("COUNT(*)", None),
    "Home Runs": ("SUM(g.home_runs)", "home_runs"),
    "Hits": ("SUM(g.hits)", "hits"),
    "RBIs": ("SUM(g.rbis)", "rbis"),
    "Stolen bases": ("SUM(g.stolen_bases)", "stolen_bases"),
    "Strikeouts": ("SUM(g.strikeouts)", "strikeouts"),
}

STATUS_FILTERS = {
    "All games": "1=1",
    "Postseason": "g.is_postseason=1",
}

@st.cache_data(show_spinner=False, max_entries=20)
def _grounds(revision, _con) -> pd.DataFrame:
    # Use distinct dates to roughly estimate matches since MLB has no matches table
    return pd.read_sql_query(
        "SELECT venue AS Arena, COUNT(DISTINCT date) AS Matches FROM games WHERE venue IS NOT NULL GROUP BY venue ORDER BY Matches DESC", _con)

@st.cache_data(show_spinner=False, max_entries=100)
def _leaders(ground, year_from, year_to, status, metric, min_games, revision, _con) -> pd.DataFrame:
    if metric == "Seasons":
        expression = "COUNT(*)"
        stat = None
    else:
        expression, stat = PLAYER_METRICS[metric]
    
    availability = f"AND g.{stat} IS NOT NULL" if stat else ""
    return pd.read_sql_query(
        f"""SELECT g.player AS Player, COUNT(*) AS Games,
                   SUM(g.home_runs) AS Home_Runs,
                   MIN(g.season) AS First, MAX(g.season) AS Last,
                   GROUP_CONCAT(DISTINCT g.club_hist) AS Teams,
                   {expression} AS Value, g.player_id AS PlayerID
              FROM games g
             WHERE g.venue=? AND g.season BETWEEN ? AND ?
               AND {STATUS_FILTERS[status]} {availability}
             GROUP BY g.player_id
            HAVING COUNT(*) >= ?
             ORDER BY Value DESC, Games DESC, Player LIMIT 200""",
        _con, params=(ground, int(year_from), int(year_to), int(min_games)))

def ground_explorer_page(sport, con: sqlite3.Connection) -> None:
    st.markdown("# Ballpark Explorer")
    revision = _revision(sport.db)
    grounds = _grounds(revision, con)
    if grounds.empty:
        st.info("No ballparks found.")
        return

    options = grounds["Arena"].tolist()
    ground = st.selectbox("Ballpark", options)

    seasons = con.execute("SELECT MIN(season), MAX(season) FROM games WHERE venue=?", (ground,)).fetchone()
    if not seasons or not seasons[0]:
        st.info("No games found for this ballpark.")
        return

    lo, hi = st.select_slider("Seasons", options=list(range(int(seasons[0]), int(seasons[1]) + 1)), value=(int(seasons[0]), int(seasons[1])))

    view = st.segmented_control("View", ["Player leaders", "Records"], default="Player leaders")

    if view == "Player leaders":
        q1, q2 = st.columns([1.4, 1])
        status = q1.selectbox("Match status", list(STATUS_FILTERS))
        min_games = q2.number_input("Minimum games (seasons)", min_value=1, value=1, step=1)
        st.caption("Open any leaderboard below. Closed leaderboards are not queried.")
        
        for metric, (_, stat) in PLAYER_METRICS.items():
            leader_box = st.expander(
                f"{metric} leaders", expanded=(metric == "Seasons"),
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
                    "Player", "Value", "Games", "Home_Runs", "First", "Last", "Teams",
                ]]
                visible = visible.rename(columns={"Value": metric})
                visible = visible.loc[:, ~visible.columns.duplicated()]
                st.caption("Select a player for their complete career.")
                components.clickable_player_table(visible, leaders["PlayerID"].tolist(), sport, con, key=f"leaders_{ground}_{metric}")
                
    elif view == "Records":
        st.caption("Ballpark records calculated from available player season data.")
        
        record_box_1 = st.expander("Most career Home Runs", expanded=True, icon=":material/sports_score:")
        with record_box_1:
            career_hr = pd.read_sql_query(
                """SELECT player AS Player, SUM(home_runs) AS "Home Runs", COUNT(*) AS Seasons
                     FROM games WHERE venue=? AND home_runs IS NOT NULL GROUP BY player_id ORDER BY "Home Runs" DESC LIMIT 10""", con, params=(ground,))
            if not career_hr.empty:
                components.clickable_entity_table(career_hr, sport, con, key=f"rec_chr_{ground}")
                
        record_box_2 = st.expander("Most career Strikeouts", expanded=False, icon=":material/sports_score:")
        with record_box_2:
            career_so = pd.read_sql_query(
                """SELECT player AS Player, SUM(strikeouts) AS Strikeouts, COUNT(*) AS Seasons
                     FROM games WHERE venue=? AND strikeouts IS NOT NULL GROUP BY player_id ORDER BY Strikeouts DESC LIMIT 10""", con, params=(ground,))
            if not career_so.empty:
                components.clickable_entity_table(career_so, sport, con, key=f"rec_cso_{ground}")
                
        record_box_3 = st.expander("Most Home Runs in a season", expanded=False, icon=":material/target:")
        with record_box_3:
            season_hr = pd.read_sql_query(
                """SELECT player AS Player, season AS Season, home_runs AS "Home Runs", club_now AS Team
                     FROM games WHERE venue=? AND home_runs IS NOT NULL ORDER BY home_runs DESC LIMIT 10""", con, params=(ground,))
            if not season_hr.empty:
                components.clickable_entity_table(season_hr, sport, con, key=f"rec_shr_{ground}")
