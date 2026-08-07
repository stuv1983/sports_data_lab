import streamlit as st
import accounts_ui
import accounts
user = accounts.get_user(st.session_state.get("auth_user_id"))
if user and user.is_admin:
    accounts_ui.admin_page(user)
else:
    st.error("Administrator access required.")
