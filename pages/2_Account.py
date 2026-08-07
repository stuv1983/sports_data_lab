import streamlit as st
import accounts_ui
import accounts
user = accounts.get_user(st.session_state.get("auth_user_id"))
accounts_ui.account_page(user)
