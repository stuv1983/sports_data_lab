import streamlit as st
import pandas as pd
import accounts

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
            # Renamed by key, not by position: assigning df.columns
            # wholesale relabels whatever order the query happened to
            # SELECT in, so a change there would silently retitle a column.
            df = pd.DataFrame(leaders).rename(columns={
                "display_name": "Player",
                "score": "Top Score",
                "played_at": "Set On (UTC)",
            })
            df.index += 1
            df["Set On (UTC)"] = pd.to_datetime(
                df["Set On (UTC)"]).dt.strftime("%Y-%m-%d %H:%M")
            st.dataframe(df, width="stretch")
