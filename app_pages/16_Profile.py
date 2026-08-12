from copy import deepcopy
from datetime import datetime

import streamlit as st

import accounts
import theme
import ui_preferences


SPORT = st.session_state.SPORT
user = accounts.get_user(st.session_state.get("auth_user_id"))
if not user:
    st.warning("Log in to view your profile and customize the app.")
    st.stop()


def display_date(value):
    if not value:
        return "Not yet"
    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y")
    except (TypeError, ValueError):
        return str(value)


preferences = ui_preferences.normalise(accounts.get_user_preferences(user.id))
activity = accounts.get_user_activity_summary(user.id)
stats = accounts.get_user_stats(user.id)

st.markdown("# My profile")
st.caption("Your account, playing history, appearance, and navigation settings.")

with st.container(border=True):
    st.markdown(f"### {user.display_name}")
    st.caption(user.email)
    summary = st.columns(4)
    summary[0].metric("Role", user.role.title())
    summary[1].metric("Member since", display_date(user.created_at))
    summary[2].metric("Saved grids", activity["saved_grids"])
    summary[3].metric("Games played", activity["games_played"])
    st.caption(
        f"Last login: {display_date(user.last_login_at)} · "
        f"Last game: {display_date(activity['last_played_at'])}"
    )

st.markdown("## Playing history")
if not stats:
    st.caption("No games played yet. Open Play grids or Game lab to get started.")
else:
    gridley = stats.get("gridley")
    if gridley:
        with st.container(border=True):
            st.markdown("### Gridley")
            columns = st.columns(3)
            columns[0].metric("Games played", gridley["games_played"])
            columns[1].metric("Average score", f"{gridley['avg_score']} / 9")
            columns[2].metric("Highest score", f"{gridley['top_score']} / 9")

    other_games = [(game, data) for game, data in stats.items()
                   if game != "gridley"]
    if other_games:
        with st.container(border=True):
            st.markdown("### Game lab")
            for game, data in other_games:
                columns = st.columns(3)
                columns[0].metric(
                    f"{game.replace('_', ' ').title()} games",
                    data["games_played"],
                )
                columns[1].metric("High score", data["top_score"])
                columns[2].metric("Average", data["avg_score"])

st.markdown("## Appearance")
with st.container(border=True):
    st.caption(
        f"Choose how {SPORT.label} looks. Appearance is saved to your account."
    )
    appearance = theme.controls(st, SPORT.key)
    if st.button(
        "Save appearance", type="primary", icon=":material/save:",
        key=SPORT.k("profile_save_appearance"),
    ):
        updated = deepcopy(preferences)
        updated["appearance"][SPORT.key] = appearance
        accounts.save_user_preferences(user.id, updated)
        st.toast("Appearance saved", icon=":material/check_circle:")

st.markdown("## Navigation and layout")
with st.container(border=True):
    st.caption(
        "Hide tools you do not use and move navigation sections into the "
        "order that suits you. Home and My profile always stay available."
    )
    nav = preferences["navigation"]
    draft_key = "profile_section_order"
    owner_key = "profile_section_order_owner"
    if st.session_state.get(owner_key) != user.id:
        for widget_key in (
            "profile_visible_pages", "profile_show_database_status",
            "profile_section_to_move",
        ):
            st.session_state.pop(widget_key, None)
        st.session_state[draft_key] = list(nav["section_order"])
        st.session_state[owner_key] = user.id

    hidden_options = [
        page_id for page_id in ui_preferences.PAGE_LABELS
        if page_id not in ui_preferences.PINNED_PAGES
        and (user.is_admin or page_id not in {"admin", "database_health"})
    ]
    visible_pages = st.multiselect(
        "Visible navigation pages",
        options=hidden_options,
        default=[page_id for page_id in hidden_options
                 if page_id not in nav["hidden_pages"]],
        format_func=lambda page_id: ui_preferences.PAGE_LABELS[page_id],
        key="profile_visible_pages",
    )

    order = st.session_state[draft_key]
    selected_section = st.selectbox(
        "Section to move", order, key="profile_section_to_move"
    )
    with st.container(horizontal=True):
        move_earlier = st.button(
            "Move earlier", icon=":material/arrow_upward:",
            disabled=order.index(selected_section) == 0,
        )
        move_later = st.button(
            "Move later", icon=":material/arrow_downward:",
            disabled=order.index(selected_section) == len(order) - 1,
        )
    if move_earlier or move_later:
        index = order.index(selected_section)
        other = index - 1 if move_earlier else index + 1
        order[index], order[other] = order[other], order[index]
        st.rerun()
    st.caption("Current section order: " + " → ".join(order))

    show_status = st.toggle(
        "Show database status in the sidebar",
        value=nav["show_database_status"],
        key="profile_show_database_status",
    )
    if st.button(
        "Save layout", type="primary", icon=":material/save:",
        key="profile_save_layout",
    ):
        updated = deepcopy(preferences)
        updated["navigation"] = {
            "section_order": list(order),
            "hidden_pages": [page_id for page_id in hidden_options
                             if page_id not in visible_pages],
            "show_database_status": show_status,
        }
        accounts.save_user_preferences(user.id, updated)
        st.toast("Layout saved", icon=":material/check_circle:")
        st.rerun()
