import sqlite3

import streamlit as st

import accounts
import database_updates
import db_pool


def _display_time(value):
    if not value:
        return "Unknown"
    try:
        import datetime as dt

        parsed = dt.datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.astimezone().strftime("%d %b %Y, %I:%M:%S %p %Z")
    except (TypeError, ValueError):
        return str(value)


def _display_size(value):
    if value is None:
        return "Unknown"
    return f"{int(value) / 1_048_576:,.1f} MB"


def _change_value(previous, current, key):
    old, new = previous.get(key), current.get(key)
    if old is None or new is None:
        return "-"
    delta = int(new) - int(old)
    if key == "bytes":
        return f"{_display_size(new)} ({delta / 1_048_576:+,.1f} MB)"
    return f"{int(new):,} ({delta:+,})"


#: The events an administrator can start by hand, in plain words.
#: brownlow-awards and grand-final-awards are deliberately absent: both are
#: calendar-guarded jobs that do nothing away from their one due date, so
#: offering them here would only produce a job that silently skips.
_UPDATE_EVENTS = {
    "regular": "Scores and statistics",
    "full": "Everything including awards",
}

#: How a freshness verdict reads in the table.
_CURRENCY_LABELS = {
    "current": "up to date",
    "behind": "BEHIND",
    "unknown": "not measured",
}


def _currency(snapshot):
    """The answer to "is this sport's data current", not "when was the file
    written". A rebuild from a feed that stopped three weeks ago leaves a
    fresh timestamp on stale data, so the two are shown side by side."""
    fresh = snapshot.get("freshness") or {}
    state = fresh.get("state", "unknown")
    label = _CURRENCY_LABELS.get(state, state)
    summary = fresh.get("summary")
    return f"{label} - {summary}" if summary else label


def _database_rows(snapshots, *, include_check=False):
    rows = []
    for sport, snapshot in snapshots.items():
        row = {
            "Sport": sport.upper(),
            "Data currency": _currency(snapshot),
            "Last database update": _display_time(snapshot.get("modified_at")),
            "Size": _display_size(snapshot.get("bytes")),
        }
        if include_check:
            row.update({
                "Integrity": snapshot.get("integrity", "not checked"),
                "Players": snapshot.get("players", "-"),
                "Data rows": snapshot.get("records", "-"),
                "Latest season": snapshot.get("season_max", "-"),
            })
        rows.append(row)
    return rows


def _change_rows(before, after):
    rows = []
    for sport, current in after.items():
        previous = before.get(sport, {})
        old_season = previous.get("season_max")
        new_season = current.get("season_max")
        season = str(new_season or "-")
        if old_season is not None and new_season != old_season:
            season = f"{old_season} -> {new_season}"
        rows.append({
            "Sport": sport.upper(),
            "Players after update": _change_value(
                previous, current, "players"
            ),
            "Data rows after update": _change_value(
                previous, current, "records"
            ),
            "Latest season": season,
            "Database size": _change_value(previous, current, "bytes"),
        })
    return rows


def _render_database_check(check):
    if not check:
        return
    st.markdown("#### Last read-only check")
    if check.get("state") == "complete":
        st.success("All selected database files are present and passed SQLite's quick integrity check.")
    else:
        failed = ", ".join(
            sport.upper() for sport in check.get("failures", [])
        ) or "unknown"
        st.error(f"Database check failed for: {failed}.")
    # Being behind is a separate verdict from being broken: a stale
    # database passes every integrity check and still answers every query.
    stale = check.get("stale") or []
    if stale:
        st.warning(
            "Data is behind for: "
            + ", ".join(sport.upper() for sport in stale)
            + ". The file is intact, but no recent games have loaded — run "
              "an update for those sports and check the step log if it "
              "keeps happening."
        )
    st.caption(
        f"Checked: {_display_time(check.get('checked_at'))} - "
        "Read-only: no sources were downloaded and no sports database was changed."
    )
    st.dataframe(
        _database_rows(check.get("databases", {}), include_check=True),
        width="stretch", hide_index=True,
    )


def _render_gridley_scan(status):
    if not status:
        return
    result = status.get("result", {})
    state = status.get("state")
    if state == "failed":
        st.error(status.get("error", "The Gridley scan failed."))
        return
    if state == "starting":
        st.info("A Gridley scan is starting.")
        return
    if state != "complete":
        st.info("A Gridley scan is running.")
        return
    changes = result.get("inserted", 0) + result.get("updated", 0)
    if changes:
        st.success(
            f"Gridley scan saved {result.get('inserted', 0)} new board(s) "
            f"and refreshed {result.get('updated', 0)} board(s)."
        )
        if status.get("promoted"):
            st.caption(
                "The AFL database was replaced. Use **Reload updated "
                "databases** above to pick it up in this session."
            )
    elif result.get("checked") and (
            result.get("unavailable") == result.get("checked")):
        st.warning(
            "Gridley did not return a readable board for any checked date. "
            "Nothing was written; try again later or review server connectivity."
        )
    else:
        st.info("Gridley scan completed. No new or changed boards were found.")
    st.caption(
        f"Finished: {_display_time(status.get('finished_at'))} - "
        f"Dates checked: {result.get('checked', 0)} - "
        f"Unavailable: {result.get('unavailable', 0)}"
    )
    boards = [{
        "Gridley": (f"#{board['grid_num']}" if board.get("grid_num") else "-"),
        "Date": board.get("date"),
        "Result": board.get("action"),
        "Rows": " / ".join(board.get("rows", [])),
        "Columns": " / ".join(board.get("cols", [])),
    } for board in result.get("boards", [])]
    if boards:
        st.dataframe(boards, width="stretch", hide_index=True)


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
            _user, first_admin = accounts.register(name, email, password)
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

    notice = st.session_state.pop("database_update_notice", None)
    if notice:
        getattr(st, notice.get("kind", "info"))(notice.get("message", ""))

    status = database_updates.read_status()
    check_status = database_updates.read_check_status()
    gridley_status = database_updates.read_gridley_scan_status()
    state = status.get("state", "unknown") if status else "unknown"
    gridley_state = (gridley_status.get("state", "unknown")
                     if gridley_status else "unknown")
    # Both jobs take the same lock, so both have to count here. Watching
    # only the main update's status left every button enabled while a
    # Gridley scan was running, and clicking one produced "a database
    # update is already running" instead of a disabled control.
    active = (
        (state in {"starting", "running"}
         or gridley_state in {"starting", "running"})
        and database_updates.update_is_active()
    )
    if status:
        message = {
            "starting": "A database update is starting.",
            "running": "A database update is running.",
            "complete": "The last database update completed.",
            "complete_with_warnings": "The database update completed, but one or more optional award sources were unavailable. See the steps below.",
            "failed": "The last database update failed; the live validated databases were retained where the builder supports atomic replacement.",
        }.get(state, f"Last update state: {state}")
        if state in {"starting", "running"} and not active:
            st.error(
                "The last update stopped unexpectedly and its process is no "
                "longer running. Review the failed step or log below."
            )
        elif state == "complete":
            st.success(message)
        elif state == "complete_with_warnings":
            st.warning(message)
        elif state == "failed":
            st.error(message)
        else:
            st.info(message)
        started = _display_time(status.get("started_at"))
        finished = _display_time(status.get("finished_at"))
        timing = f"Event: {status.get('event', 'unknown')} - Started: {started}"
        if status.get("finished_at"):
            timing += f" - Finished: {finished}"
        st.caption(timing)
        if active:
            try:
                expected_steps = len(database_updates.plan(
                    status.get("event", "full"),
                    status.get("sports", database_updates.SPORT_KEYS),
                ))
            except (TypeError, ValueError):
                expected_steps = 0
            finished_steps = len(status.get("steps", []))
            if expected_steps:
                st.progress(
                    min(finished_steps / expected_steps, 1.0),
                    text=(f"{finished_steps} of {expected_steps} update steps "
                          "finished. Refresh for the latest result."),
                )
        if status.get("error"):
            st.error(status["error"])
        if status.get("steps"):
            with st.expander("Update steps", expanded=state == "failed"):
                for step in status["steps"]:
                    if step.get("state") == "skipped":
                        result = "skipped after required failure"
                    else:
                        result = "ok" if step.get("returncode") == 0 else "failed"
                    if (step.get("optional") and result == "failed"):
                        result = "optional source unavailable"
                    st.write(
                        f"{step.get('sport', '').upper()} · {step.get('label')} · "
                        f"{result} · {step.get('seconds', 0):g}s"
                    )
        if status.get("promotions"):
            with st.expander("Database promotion results", expanded=state == "failed"):
                for sport, promotion in status["promotions"].items():
                    promotion_state = promotion.get("state")
                    if promotion_state == "promoted":
                        detail = "validated database promoted"
                        if promotion.get("backup"):
                            detail += f"; backup: {promotion['backup']}"
                        st.success(f"{sport.upper()}: {detail}")
                    elif promotion_state == "retained_live":
                        st.warning(
                            f"{sport.upper()}: live database retained; failed "
                            f"staging file: {promotion.get('staging', 'unknown')}"
                        )
                    else:
                        st.error(
                            f"{sport.upper()}: promotion failed; live database "
                            f"retained. {promotion.get('error', '')}"
                        )
        if status.get("log_path"):
            st.caption("Full update log")
            st.code(status["log_path"], language=None)

        if status.get("after"):
            st.markdown("#### What the update found")
            st.dataframe(
                _change_rows(status.get("before", {}), status["after"]),
                width="stretch", hide_index=True,
            )

    st.markdown("#### Live database files")
    st.caption(
        "The timestamp is when each validated live database was last replaced."
    )
    st.dataframe(
        _database_rows(database_updates.database_file_status()),
        width="stretch", hide_index=True,
    )

    with st.form("admin_database_update_form"):
        st.caption(
            "Scores and statistics refreshes every selected sport from its "
            "own source. Everything including awards adds the AFL award "
            "layers, which is much slower and only worth running after "
            "Brownlow night or the Grand Final."
        )
        scope = st.columns(2)
        event = scope[0].selectbox(
            "What to refresh", _UPDATE_EVENTS,
            format_func=lambda key: _UPDATE_EVENTS[key],
        )
        sports = scope[1].multiselect(
            "Sports", database_updates.SPORT_KEYS,
            default=list(database_updates.SPORT_KEYS),
            format_func=str.upper,
        )
        password = st.text_input(
            "Confirm your admin password", type="password",
            help="A fresh password check is required before starting a database write."
        )
        submitted = st.form_submit_button(
            "Start update", type="primary", icon=":material/sync:",
            disabled=active,
        )
    if submitted:
        try:
            confirmed = accounts.authenticate(user.email, password)
        except accounts.AccountError as exc:
            st.error(str(exc))
        else:
            if confirmed is None or confirmed.id != user.id or not confirmed.is_admin:
                st.error("Password confirmation failed.")
            elif not sports:
                st.error("Choose at least one sport to refresh.")
            else:
                try:
                    pid = database_updates.start_background(
                        event=event, sports=sports)
                except (RuntimeError, ValueError) as exc:
                    st.error(str(exc))
                else:
                    st.session_state["database_update_notice"] = {
                        "kind": "success",
                        "message": (
                            f"{_UPDATE_EVENTS[event]} accepted for "
                            f"{', '.join(s.upper() for s in sports)} and "
                            f"started in the background (PID {pid})."
                        ),
                    }
                    st.rerun()

    controls = st.container(horizontal=True)
    if controls.button(
        "Check databases now", icon=":material/fact_check:",
        help=("Runs read-only file, row-count and integrity checks. It does "
              "not download sources or add anything to a database."),
    ):
        with st.status("Checking live databases...", expanded=True) as progress:
            try:
                check_status = database_updates.check_databases()
            except (OSError, ValueError, sqlite3.Error) as exc:
                progress.update(label="Database check failed", state="error")
                st.error(f"{type(exc).__name__}: {exc}")
            else:
                if check_status.get("state") == "complete":
                    progress.update(label="Database check complete", state="complete")
                else:
                    progress.update(
                        label="One or more database checks failed", state="error"
                    )
    if controls.button("Refresh update status", icon=":material/refresh:"):
        st.rerun()
    if controls.button(
        "Reload updated databases", icon=":material/restart_alt:",
        disabled=active,
        help="Closes this session's old read handles and reruns the app. A server reboot is not normally required.",
    ):
        db_pool.close_all()
        st.cache_data.clear()
        st.rerun()

    _render_database_check(check_status)

    st.markdown("#### Gridley game scan")
    st.caption(
        "Checks Gridley's public daily AFL board feed from the newest saved "
        "date through today. New boards are validated in a copy before the "
        "AFL database is atomically replaced. This does not scan Immaculate "
        "Grid. It also runs on its own daily timer, so this button is for "
        "picking up today's board early rather than for routine upkeep."
    )
    if st.button(
        "Scan Gridley for new games", icon=":material/grid_view:",
        disabled=active,
        help="Checks at most 31 dates and keeps Gridley's real board numbers.",
    ):
        # Detached, like the main update. Run inline this made up to 31
        # sequential HTTP requests inside the script run, which blocks the
        # page and loses the job if the websocket times out first.
        try:
            pid = database_updates.start_gridley_scan_background()
        except (OSError, RuntimeError, ValueError) as exc:
            st.error(f"{type(exc).__name__}: {exc}")
        else:
            st.session_state["database_update_notice"] = {
                "kind": "success",
                "message": (
                    f"Gridley scan started in the background (PID {pid}). "
                    "Use Refresh update status to follow it."
                ),
            }
            st.rerun()
    _render_gridley_scan(gridley_status)

    with st.expander("Automatic schedule"):
        st.write("Regular scores and statistics: Friday, Saturday, Sunday and Monday at 12:10 am Sydney time.")
        st.write("Gridley board scan: every day at 6:30 am Sydney time.")
        st.write("Brownlow and awards: 1:00 am on the Tuesday after Brownlow night (22 September in 2026).")
        st.write("Grand Final and final awards: 1:00 am on the Sunday after the last Saturday in September (27 September in 2026).")
        st.caption(
            "Ubuntu systemd timers run missed starts after downtime and the "
            "update lock prevents overlapping jobs."
        )
