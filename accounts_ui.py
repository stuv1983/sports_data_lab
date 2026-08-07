import streamlit as st
import accounts
import database_updates
import db_pool


def _login_form(prefix="sidebar"):
    with st.form(f"{prefix}_login_form"):
        email = st.text_input("Email", key=f"{prefix}_login_email")
        password = st.text_input(
            "Password", type="password", key=f"{prefix}_login_password")
        submitted = st.form_submit_button("Log in", type="primary")
    if submitted:
        try:
            user = accounts.authenticate(email, password)
            if user is None:
                st.error("Email or password is incorrect, or this account is disabled.")
            else:
                st.session_state["auth_user_id"] = user.id
                st.rerun()
        except accounts.AccountError as exc:
            st.error(str(exc))


def _join_form(prefix="sidebar"):
    with st.form(f"{prefix}_join_form"):
        name = st.text_input("Display name", key=f"{prefix}_join_name")
        email = st.text_input("Email", key=f"{prefix}_join_email")
        password = st.text_input(
            "Password", type="password", key=f"{prefix}_join_password",
            help="Use at least 10 characters.")
        submitted = st.form_submit_button("Create account", type="primary")
    if submitted:
        try:
            user, first_admin = accounts.register(name, email, password)
        except accounts.AccountError as exc:
            st.error(str(exc))
        else:
            if first_admin:
                st.success("Account created. As the first member, you are the administrator. Please check your email (or logs/emails.txt) to verify your account.")
            else:
                st.success("Account created. Please check your email (or logs/emails.txt) to verify your account before logging in.")


def account_page(user):
    st.markdown("# Account")
    if user:
        st.markdown(f"### {user.display_name}")
        st.caption(f"{user.email} · {user.role}")
        st.info("Your grids are saved to this account and are available on every sport's Grid Solver page.")
        if st.button("Log out", key="account_page_logout"):
            st.session_state.pop("auth_user_id", None)
            st.rerun()
    else:
        st.caption("Join to use Grid Solver and other advanced features, and to save your grids.")
        login_tab, join_tab = st.tabs(["Log in", "Create account"])
        with login_tab:
            _login_form("page")
        with join_tab:
            _join_form("page")


def admin_page(user):
    st.markdown("# Access administration")
    st.caption("Admins always retain access. Choose members, selected accounts, or admins only for each feature.")
    policies = accounts.feature_policies()
    users = accounts.list_users()

    st.markdown("### Feature access")
    for feature, (label, _default) in accounts.FEATURES.items():
        current = policies.get(feature, _default)
        col1, col2 = st.columns([2, 3])
        col1.markdown(f"**{label}**")
        choice = col2.selectbox(
            f"Who can use {label}", accounts.AUDIENCES,
            index=accounts.AUDIENCES.index(current), key=f"policy_{feature}",
            format_func=lambda value: {
                "public": "Public (Everyone)",
                "member": "All members", "selected": "Selected members",
                "admin": "Admins only"}[value], label_visibility="collapsed")
        if choice != current:
            accounts.set_feature_policy(user.id, feature, choice)
            st.rerun()
        if choice == "selected":
            granted = accounts.feature_grants(feature)
            member_options = [u for u in users if u.active and not u.is_admin]
            picked = st.multiselect(
                f"Selected accounts for {label}", member_options,
                default=[u for u in member_options if u.id in granted],
                format_func=lambda u: f"{u.display_name} · {u.email}",
                key=f"grants_{feature}")
            picked_ids = {u.id for u in picked}
            if picked_ids != (granted & {u.id for u in member_options}):
                for member in member_options:
                    accounts.set_feature_grant(
                        user.id, feature, member.id, member.id in picked_ids)
                st.rerun()

    st.markdown("### Members")
    for member in users:
        with st.expander(f"{member.display_name} · {member.email}"):
            c1, c2, c3 = st.columns([2, 2, 1])
            role = c1.selectbox(
                "Role", ["member", "admin"],
                index=0 if member.role == "member" else 1,
                key=f"user_role_{member.id}")
            active = c2.checkbox("Active", value=member.active,
                                 key=f"user_active_{member.id}")
            if c3.button("Update", key=f"user_update_{member.id}"):
                try:
                    accounts.set_user_access(
                        user.id, member.id, role=role, active=active)
                except accounts.AccountError as exc:
                    st.error(str(exc))
                else:
                    st.success("Access updated.")
                    st.rerun()

    st.markdown("### Database updates")
    st.caption(
        "Runs the existing fetchers and builders for AFL, NBA, MLB and NFL, "
        "then repairs, optimises and checks each database. The update runs in "
        "the background and the live app keeps serving the last validated files."
    )

    status = database_updates.read_status()
    if status:
        state = status.get("state", "unknown")
        message = {
            "running": "A database update is running.",
            "complete": "The last database update completed.",
            "complete_with_warnings": "The database update completed, but one or more optional award sources were unavailable. See the steps below.",
            "failed": "The last database update failed; the live validated databases were retained where the builder supports atomic replacement.",
        }.get(state, f"Last update state: {state}")
        if state == "complete":
            st.success(message)
        elif state == "complete_with_warnings":
            st.warning(message)
        elif state == "failed":
            st.error(message)
        else:
            st.info(message)
        started = status.get("started_at", "unknown")
        finished = status.get("finished_at")
        st.caption(
            f"Event: {status.get('event', 'unknown')} · Started: {started}"
            + (f" · Finished: {finished}" if finished else "")
        )
        if status.get("steps"):
            with st.expander("Update steps", expanded=state == "failed"):
                for step in status["steps"]:
                    result = "ok" if step.get("returncode") == 0 else "failed"
                    if step.get("optional") and result == "failed":
                        result = "optional source unavailable"
                    st.write(
                        f"{step.get('sport', '').upper()} · {step.get('label')} · "
                        f"{result} · {step.get('seconds', 0):g}s"
                    )
        if status.get("log_path"):
            st.code(status["log_path"], language=None)

    with st.form("admin_database_update_form"):
        password = st.text_input(
            "Confirm your admin password", type="password",
            help="A fresh password check is required before starting a database write."
        )
        submitted = st.form_submit_button(
            "Update all databases", type="primary", icon=":material/sync:"
        )
    if submitted:
        try:
            confirmed = accounts.authenticate(user.email, password)
        except accounts.AccountError as exc:
            st.error(str(exc))
        else:
            if confirmed is None or confirmed.id != user.id or not confirmed.is_admin:
                st.error("Password confirmation failed.")
            else:
                try:
                    pid = database_updates.start_background()
                except RuntimeError as exc:
                    st.warning(str(exc))
                else:
                    st.success(f"Database update started in the background (PID {pid}).")
                    st.rerun()

    controls = st.container(horizontal=True)
    if controls.button("Refresh update status", icon=":material/refresh:"):
        st.rerun()
    if controls.button(
        "Reload updated databases", icon=":material/restart_alt:",
        disabled=status.get("state") == "running",
        help="Closes this session's old read handles and reruns the app. A server reboot is not normally required.",
    ):
        db_pool.close_all()
        st.cache_data.clear()
        st.rerun()

    with st.expander("Automatic schedule"):
        st.write("Regular scores and statistics: Friday, Saturday, Sunday and Monday at 12:10 am Sydney time.")
        st.write("Brownlow and awards: 1:00 am on the Tuesday after Brownlow night (22 September in 2026).")
        st.write("Grand Final and final awards: 1:00 am on the Sunday after the last Saturday in September (27 September in 2026).")
        st.caption(
            "Windows Task Scheduler runs missed starts when the computer resumes and prevents overlapping jobs."
        )
