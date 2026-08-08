import streamlit as st
import accounts
import theme

st.set_page_config(page_title="My Profile - AFL Data Lab", layout="centered")

SPORT = st.session_state.get("SPORT")
if not SPORT:
    st.error("Sport not initialized.")
    st.stop()

user_id = st.session_state.get("auth_user_id")
user = accounts.get_user(user_id) if user_id else None
if not user:
    st.warning("You must be logged in to view your profile.")
    st.stop()

st.markdown(f"## {user.display_name}'s Profile")
st.markdown(f"**Role:** {user.role.title()}")

stats = accounts.get_user_stats(user.id)

if not stats:
    st.info("You haven't played any games yet. Head over to Play Grids or Game Lab to get started!")
else:
    st.markdown("### Gridley")
    gridley = stats.get("gridley")
    if gridley:
        col1, col2, col3 = st.columns(3)
        col1.metric("Games Played", gridley["games_played"])
        col2.metric("Average Score", f"{gridley['avg_score']} / 9")
        col3.metric("Highest Score", f"{gridley['top_score']} / 9")
    else:
        st.write("No Gridley games played yet.")
    
    st.markdown("---")
    st.markdown("### Game Lab")
    found_other = False
    for game, data in stats.items():
        if game != "gridley":
            found_other = True
            c1, c2, c3 = st.columns(3)
            c1.metric(f"{game.replace('_', ' ').title()} Games", data['games_played'])
            c2.metric("High Score", data['top_score'])
            c3.metric("Average", data['avg_score'])
    if not found_other:
         st.write("No Game Lab modes played yet.")
