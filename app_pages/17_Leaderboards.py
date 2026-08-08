import streamlit as st
import pandas as pd
import accounts
import theme

st.set_page_config(page_title="Leaderboards - AFL Data Lab", layout="centered")

SPORT = st.session_state.get("SPORT")
if not SPORT:
    st.error("Sport not initialized.")
    st.stop()

st.markdown("## Global Leaderboards")

game_types = ["gridley", "guess_career", "higher_lower", "threshold_challenge"]
tabs = st.tabs([g.replace("_", " ").title() for g in game_types])

for i, game in enumerate(game_types):
    with tabs[i]:
        leaders = accounts.get_leaderboard(game)
        if not leaders:
            st.info(f"No scores logged for {game.replace('_', ' ').title()} yet. Be the first!")
        else:
            df = pd.DataFrame(leaders)
            df.index += 1
            df.columns = ["Player", "Top Score", "Last Played (UTC)"]
            # Convert ISO string to readable date
            df["Last Played (UTC)"] = pd.to_datetime(df["Last Played (UTC)"]).dt.strftime('%Y-%m-%d %H:%M')
            st.dataframe(df, use_container_width=True)
