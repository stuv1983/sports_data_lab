import datetime as dt
import sqlite3

import streamlit as st

import accounts
import auth_session
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


def _elapsed_time(started_at, finished_at=None):
    """Format an update runtime without exposing timestamp arithmetic in UI."""
    if not started_at:
        return "Unknown"
    try:
        import datetime as dt

        started = dt.datetime.fromisoformat(str(started_at))
        finished = (dt.datetime.fromisoformat(str(finished_at))
                    if finished_at else dt.datetime.now().astimezone())
        if started.tzinfo is None:
            started = started.astimezone()
        if finished.tzinfo is None:
            finished = finished.astimezone()
        seconds = max(0, int((finished - started).total_seconds()))
    except (TypeError, ValueError):
        return "Unknown"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _sport_progress_rows(status):
    """One operational summary row per sport in the current/last job."""
    sports = status.get("sports") or []
    try:
        planned = database_updates.plan(status.get("event", "full"), sports)
    except (TypeError, ValueError):
        planned = []
    totals = {sport: 0 for sport in sports}
    for sport, _step in planned:
        totals[sport] = totals.get(sport, 0) + 1
    completed = {sport: [] for sport in sports}
    for step in status.get("steps", []):
        completed.setdefault(step.get("sport"), []).append(step)
    current = status.get("current_step") or {}
    promotions = status.get("promotions") or {}
    rows = []
    for sport in sports:
        steps = completed.get(sport, [])
        done = len(steps)
        required_failed = any(
            step.get("state") == "failed" and not step.get("optional")
            for step in steps
        )
        promotion = promotions.get(sport, {}).get("state")
        if promotion == "promoted":
            state = "Updated"
        elif promotion == "retained_live":
            state = "Live database retained"
        elif promotion == "failed" or required_failed:
            state = "Failed"
        elif current.get("sport") == sport:
            state = "Running"
        elif status.get("state") in {"starting", "running"}:
            state = "Waiting"
        else:
            state = "Finished"
        detail = (current.get("label") if current.get("sport") == sport else
                  steps[-1].get("label") if steps else "Not started")
        total = totals.get(sport, 0)
        rows.append({
            "Sport": sport.upper(),
            "Status": state,
            "Progress": min(done / total, 1.0) if total else 0.0,
            "Steps": f"{done} / {total}" if total else str(done),
            "Current or last step": detail,
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


def _days_ago(days):
    if days is None:
        return "Never"
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    return f"{days} days ago"


def _render_rising_star_currency(currency):
    """How current the nominations are, without running anything.

    The panel used to show only the outcome of the last check, which meant
    an administrator who had never run one saw nothing at all and had no
    way to tell whether the award was current short of triggering a job.
    """
    if not currency:
        return
    state = currency.get("state")
    if state == "not loaded":
        st.info(
            "No Rising Star nominations are loaded. Run a check below, or "
            "rebuild the AFL database."
        )
        return
    if state == "unreadable":
        st.warning(f"Could not read the nominations: {currency.get('summary')}")
        return
    if state == "empty":
        st.info("The nominations table is present but holds no rows.")
        return

    season = currency.get("season")
    latest_round = currency.get("latest_round")
    with st.container(horizontal=True):
        st.metric(
            f"Latest {season} nomination",
            "None yet" if latest_round is None else f"Round {latest_round}",
            border=True,
            help=("The newest nomination in the live database. Rounds carry "
                  "no date, so this says what is loaded, not how recent it is."),
        )
        st.metric(
            f"Nominations in {season}", f"{currency.get('season_nominations', 0):,}",
            border=True,
            help=f"{currency.get('total', 0):,} across every season.",
        )
        st.metric(
            "Source last checked", _days_ago(currency.get("days_since_check")),
            border=True,
            help="Wikipedia is checked every Monday, and whenever this "
                 "page's button is used.",
        )

    if currency.get("latest_player"):
        st.caption(
            f"Newest: **{currency['latest_player']}** "
            f"({currency.get('latest_club', 'unknown club')}), round "
            f"{latest_round}, from {currency.get('latest_source', 'unknown')}"
            + ("" if currency.get("latest_linked")
               else " - not linked to a player, so the solver cannot see it")
            + ". Sources: "
            + ", ".join(f"{name} {count:,}" for name, count
                        in sorted(currency.get("sources", {}).items()))
            + "."
        )

    if currency.get("stale"):
        st.warning(
            "The season is underway and the source has not been checked "
            f"successfully since {_days_ago(currency.get('days_since_check')).lower()}"
            ". A nomination is announced every round, so at least one is "
            "probably missing. Run a check below, and confirm the Monday "
            "timer is installed."
        )
    elif not currency.get("in_season"):
        st.caption(
            "Between seasons: no further nomination is due until the next "
            "season starts."
        )


def _render_manual_round_progress(status, kind, label):
    """Where a running load is up to, in phases rather than in seconds.

    The job runs detached, so the only thing the browser knows about it is
    what the status file says. It names every phase as it begins, which is
    what turns "it is running" into "rebuilding the ladder, ten of eleven".
    The phases are nowhere near equal -- the last one is about two thirds
    of the minute -- so a bar that rests there is working rather than
    stuck, and the label is what distinguishes the two.
    """
    step = status.get("phase_step") or 0
    total = status.get("phase_total") or 0
    phase = status.get("phase") or "Starting"
    elapsed = _elapsed_time(status.get("started_at"))

    with st.container(border=True):
        st.markdown(f"**{kind} of {label} is running**")
        if total:
            st.progress(min(step / total, 1.0),
                        text=f"{phase} — step {step} of {total}")
        else:
            st.progress(0.0, text="Starting")
        detail = st.columns(3)
        detail[0].metric("Elapsed", elapsed, border=True)
        detail[1].metric("Phase", f"{step} / {total}" if total else "—",
                         border=True)
        detail[2].metric(
            "Typical", "about 1 min" if not status.get("dry_run")
            else "a few seconds", border=True)
        st.caption(
            "This page follows the job by itself; there is no need to "
            "refresh. Nothing reaches the live database until every phase "
            "has passed."
        )


def _elapsed_seconds(started_at) -> int | None:
    """Seconds since a status timestamp, or None if it cannot be read."""
    if not started_at:
        return None
    try:
        started = dt.datetime.fromisoformat(str(started_at))
        if started.tzinfo is None:
            started = started.astimezone()
        return max(0, int(
            (dt.datetime.now().astimezone() - started).total_seconds()))
    except (TypeError, ValueError):
        return None


def _round_load_stalled(status) -> bool:
    """Whether a job that says it is running has actually gone.

    The status file is the only evidence a detached job leaves, so a
    process killed mid-load leaves 'running' behind for ever. The lock is
    the second opinion -- but it is taken by the child a moment after the
    parent writes 'starting', so a job is only called stalled once it has
    had long enough to take it.
    """
    if (status or {}).get("state") not in {"starting", "running"}:
        return False
    elapsed = _elapsed_seconds(status.get("started_at"))
    if elapsed is None or elapsed < 30:
        return False
    return not database_updates.update_is_active()


@st.fragment(run_every=2)
def _live_manual_round_status():
    """Poll the running load, then hand back to the ordinary page.

    Rendered only while a job is actually running, and its last act is to
    rerun the whole app -- which drops this fragment and shows the finished
    report. Without that the page would keep polling a job that ended, and
    a job that died would be polled until the tab was closed.
    """
    status = database_updates.read_manual_round_status()
    if _round_load_stalled(status):
        st.error(
            "The load stopped unexpectedly and its process is no longer "
            "running. Nothing was written to the live database — the round "
            "is applied to a staged copy and promoted only on success.",
            icon=":material/error:")
        _render_manual_round_status({**status, "state": "failed",
                                     "error": "the process is gone"})
        return
    _render_manual_round_status(status)
    if (status or {}).get("state") not in {"starting", "running"}:
        st.rerun(scope="app")


def _render_manual_round_status(status):
    """The loader's own report, which is what the operator actually needs.

    It names which file paired with which fixture, which source name
    resolved to which player, and exactly what it refused and why. A
    success/failure banner alone would throw away the useful part.
    """
    if not status:
        return
    state = status.get("state")
    kind = "Check" if status.get("dry_run") else "Load"
    label = f"round {status.get('round')}, {status.get('season')}"
    if state in {"starting", "running"}:
        _render_manual_round_progress(status, kind, label)
    elif state == "failed":
        st.error(f"{kind} of {label} failed: {status.get('error')}")
        st.caption("Nothing was written to the live database.")
    elif state == "complete" and status.get("dry_run"):
        st.success(f"{label} checked. Nothing was written — rerun the "
                   "command without `--dry-run` to apply it.")
    elif state == "complete":
        st.success(f"{label} loaded and the AFL database was replaced. Use "
                   "**Reload updated databases** above to pick it up.")
    report = status.get("report")
    if report:
        with st.expander(f"{kind} report", expanded=state == "failed"):
            st.code(report, language="text")


def _render_manual_rounds(summary):
    """Which hand-entered rounds the database is carrying, and their state."""
    if not summary:
        return
    rounds = summary.get("rounds") or []
    if summary.get("latest_round") is not None:
        st.caption(
            f"Latest game in the database: round {summary['latest_round']}, "
            f"{summary.get('latest_season')} "
            f"({summary.get('latest_date', 'date unknown')})."
        )
    if not rounds:
        st.caption("No hand-entered rounds are stored. Every round in the "
                   "database came from the upstream dataset.")
        return
    st.dataframe([{
        "Season": row["season"],
        "Round": row["round"],
        # A stored round the rebuild now produces itself is redundant, not
        # wrong: --apply-only already defers to the upstream rows. Saying
        # so is how the operator knows it is safe to forget.
        "Upstream now has it": "Yes" if row.get("upstream_has") else "No",
    } for row in rounds], width="stretch", hide_index=True)
    if summary.get("redundant"):
        st.caption(
            f"{summary['redundant']} stored round(s) are now published "
            "upstream and can be forgotten below."
        )


def _render_rising_star_edits():
    """The hand-entered rows, read-only, so an edit can be found.

    Adding, amending or undoing one happens offline through
    `utils.afl.rising_star_manual` -- the web process never writes.
    """
    from utils.afl import rising_star_manual as manual

    try:
        entries = manual.read_entries()
    except OSError as exc:
        st.warning(f"Could not read hand-entered nominations: {exc}")
        return
    if not entries:
        return
    st.caption(f"{len(entries)} hand-entered row(s):")
    st.dataframe([{
        "Season": row.get("season"),
        "Round": row.get("round_number") or "—",
        "Player": row.get("player"),
        "Club": row.get("club") or "—",
        "Ineligible": "Yes" if str(row.get("ineligible")) == "1" else "",
        "Votes": row.get("votes") or "",
        "Winner": "Yes" if str(row.get("is_season_winner")) == "1" else "",
        "Edited by": row.get("edited_by"),
    } for row in entries], width="stretch", hide_index=True)


def _render_manual_round_section(active):
    """Follow hand-entered rounds; entering one happens offline.

    afl/build_db.py does not scrape AFL Tables -- its robots.txt disallows
    it -- so game data arrives through the cached fitzRoy dataset, which
    lags the live season by a round or two. Hand-entered rounds close that
    gap, but the web process is strictly read-only: it accepts no uploads
    and starts no load. Rounds are checked and loaded from the command
    line (the same loader the desktop window uses), and this tab shows
    what is stored and how the last load went.
    """
    st.markdown("#### Hand-entered round results")
    st.caption(
        "For a round that has been played but not yet published upstream. "
        "Rounds are loaded offline from the command line; this page only "
        "reports what is stored and follows a load's progress."
    )

    try:
        summary = database_updates.manual_rounds()
    except (OSError, sqlite3.Error, ImportError) as exc:
        summary = {}
        st.warning(f"Could not read stored rounds: {exc}")
    _render_manual_rounds(summary)

    st.markdown("##### Load a round from the command line")
    st.caption(
        "Put the round summary and one CSV per match in a folder, copied "
        "from the AFL Tables match pages. Run the check first; the real "
        "load stages a copy of the database and promotes it only if the "
        "loader accepts the round."
    )
    st.code(
        "python database_updates.py manual-round-load "
        "--dir <folder> --season <year> --round <round> --dry-run\n"
        "python database_updates.py manual-round-load "
        "--dir <folder> --season <year> --round <round>",
        language="bash",
    )

    # Poll only while there is something to poll. When the fragment sees
    # the job finish it reruns the app, which lands here on the other
    # branch and shows the loader's report.
    running = database_updates.read_manual_round_status()
    if (running or {}).get("state") in {"starting", "running"}:
        _live_manual_round_status()
    else:
        _render_manual_round_status(running)

    redundant = [row for row in (summary.get("rounds") or [])
                 if row.get("upstream_has")]
    if redundant:
        with st.expander("Rounds the upstream dataset now carries"):
            st.caption(
                "Dropping a stored round is safe once the rebuild produces "
                "it: the upstream rows are already the authority, and "
                "forgetting only removes the hand-entered copy underneath "
                "them. Run offline:"
            )
            for row in redundant:
                st.code(
                    "python database_updates.py manual-round-forget "
                    f"--season {row['season']} --round {row['round']}",
                    language="bash",
                )


def _render_rising_star_scan(status):
    if not status:
        return
    state = status.get("state")
    started = status.get("started_at")
    if state == "failed":
        st.error(status.get("error", "The Rising Star check failed."))
        st.caption(
            f"Failed: {_display_time(status.get('finished_at'))} - The live "
            "AFL database was left unchanged."
        )
        return
    if state in {"starting", "running"}:
        st.info(
            f"A Rising Star check is {state} "
            f"({_elapsed_time(started)} so far). It normally takes a few "
            "seconds; use **Refresh status** above to see the result."
        )
        return
    if state != "complete":
        return

    result = status.get("result", {})
    season = status.get("season") or result.get("season")
    added = result.get("new_nominations") or []
    if added:
        st.success(
            f"Added {len(added)} new {season} nomination"
            f"{'' if len(added) == 1 else 's'}: "
            + ", ".join(f"{item['player']} ({item['club']}, round "
                        f"{item['round']})" for item in added)
        )
    elif result.get("note"):
        st.info(result["note"])
    elif status.get("promoted"):
        # Nothing new was published, but the database was behind what the
        # source file already said -- a previous run wrote the file and
        # failed to load it. Saying "no change" here would be wrong.
        st.success(
            f"No new nomination was published, but the database was behind "
            f"the source and has been reloaded up to round "
            f"{result.get('latest_round', 'unknown')}."
        )
    else:
        st.info(
            f"Checked: no new {season} nomination has been published since "
            "the last check."
        )
    if status.get("promoted"):
        st.caption(
            "The AFL database was replaced. Use **Reload updated databases** "
            "above to pick it up in this session."
        )
    st.caption(
        f"Finished: {_display_time(status.get('finished_at'))} - "
        f"Took {_elapsed_time(started, status.get('finished_at'))} - "
        f"Latest round at the source: {result.get('latest_round', 'unknown')} - "
        f"Triggered by: {status.get('trigger', 'unknown')}"
    )


def _login_form(prefix="sidebar"):
    with st.form(f"{prefix}_login_form"):
        email = st.text_input("Email", key=f"{prefix}_login_email")
        password = st.text_input(
            "Password", type="password", key=f"{prefix}_login_password")
        submitted = st.form_submit_button("Log in", type="primary")
    pending_key = f"{prefix}_resend_email"
    if submitted:
        # Cleared on every attempt, before anything can set it again. The
        # button below acts on whatever this key holds, so an address left
        # over from an earlier attempt -- or from an earlier session on this
        # browser -- would quietly be the one that got mailed.
        st.session_state.pop(pending_key, None)
        try:
            user = accounts.authenticate(email, password)
            if user is None:
                st.error("Email or password is incorrect, or this account is disabled.")
            else:
                # Not a bare session_state write: remember() also issues
                # the cookie that carries this log in into the next tab.
                auth_session.remember(user)
                st.rerun()
        except accounts.AccountError as exc:
            st.error(str(exc))
            # Verification links expire after a day. Offer a new one here, or
            # an account whose link went stale can neither log in nor register
            # again with the same address.
            st.session_state[pending_key] = email

    if st.session_state.get(pending_key):
        if st.button("Resend verification email", key=f"{prefix}_resend"):
            accounts.resend_verification(st.session_state.pop(pending_key))
            st.success("If that address needs verifying, a fresh link is on "
                       "its way, unless one was sent in the last few minutes "
                       "-- that one is still good. Check your email (or "
                       "logs/emails.txt).")


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
    """One page, grouped by what a thing is rather than when it was added.

    It had grown into a single column holding access control, four kinds
    of database job and a schedule, so finding the round loader meant
    scrolling past everything else and the running-job banner scrolled
    away from the button that started it. The jobs are now grouped by the
    data they touch, and the statuses every tab needs are read once here.
    """
    st.markdown("# Administration")

    notice = st.session_state.pop("database_update_notice", None)
    if notice:
        getattr(st, notice.get("kind", "info"))(notice.get("message", ""))

    status = database_updates.read_status()
    check_status = database_updates.read_check_status()
    gridley_status = database_updates.read_gridley_scan_status()
    rising_star_status = database_updates.read_rising_star_status()
    round_status = database_updates.read_manual_round_status()
    state = status.get("state", "unknown") if status else "unknown"

    def running(job):
        return (job or {}).get("state") in {"starting", "running"}

    # Every job takes the same lock, so any one of them running has to
    # disable all the buttons. Watching only the main update's status left
    # them enabled during a Gridley scan, and clicking one produced "a
    # database update is already running" instead of a disabled control.
    active = (
        (state in {"starting", "running"} or running(gridley_status)
         or running(rising_star_status) or running(round_status))
        and database_updates.update_is_active()
    )

    _admin_banner(active, state, status, gridley_status, rising_star_status,
                  round_status)

    tabs = st.tabs([
        "Databases", "Match data", "Rising Star", "Grids", "Access",
        "Schedule",
    ])
    with tabs[0]:
        _admin_databases_tab(user, status, check_status, state, active)
    with tabs[1]:
        _render_manual_round_section(active)
    with tabs[2]:
        _admin_rising_star_tab(user, rising_star_status, active)
    with tabs[3]:
        _admin_grids_tab(gridley_status, active)
    with tabs[4]:
        _admin_access_tab(user)
    with tabs[5]:
        _admin_schedule_tab()


def _admin_banner(active, state, status, gridley_status, rising_star_status,
                  round_status) -> None:
    """What is running right now, above the tabs.

    A job started on one tab is followed from whichever tab the operator
    happens to be on, because the thing they want to know -- is it safe to
    start another one -- is not a property of the tab they are looking at.
    """
    if not active:
        return
    for label, job in (("Database update", status),
                       ("Round load", round_status),
                       ("Gridley scan", gridley_status),
                       ("Rising Star check", rising_star_status)):
        if (job or {}).get("state") in {"starting", "running"}:
            st.warning(
                f"**{label} in progress** — started "
                f"{_elapsed_time(job.get('started_at'))} ago. The update "
                "lock holds every other database job until it finishes.",
                icon=":material/hourglass_top:")
            return
    st.warning("A database job is in progress.",
               icon=":material/hourglass_top:")


def _admin_schedule_tab() -> None:
    st.markdown("#### Automatic schedule")
    st.caption("Every job below can also be run early from the command "
               "line -- its tab shows the exact command -- rather than "
               "waiting for a timer.")
    st.dataframe([
        {"Job": "Scores and statistics",
         "Runs": "Fri, Sat, Sun, Mon at 12:10 am Sydney"},
        {"Job": "Gridley board scan", "Runs": "Daily at 6:30 am Sydney"},
        {"Job": "Rising Star nominations", "Runs": "Monday at 8:00 am Sydney"},
        {"Job": "Brownlow and awards",
         "Runs": "1:00 am the Tuesday after Brownlow night "
                 "(22 September in 2026)"},
        {"Job": "Grand Final and final awards",
         "Runs": "1:00 am the Sunday after the last Saturday in September "
                 "(27 September in 2026)"},
    ], width="stretch", hide_index=True)
    st.caption(
        "Ubuntu systemd timers run missed starts after downtime and the "
        "update lock prevents overlapping jobs."
    )


def _admin_access_tab(user):
    policies = accounts.feature_policies()
    users = accounts.list_users()

    st.markdown("#### Feature access")
    st.caption("Admins always retain access. Choose members, selected "
               "accounts, or admins only for each feature.")
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

    st.markdown("#### Members")
    for member in users:
        with st.expander(f"{member.display_name} · {member.email}"):
            c1, c2, c3 = st.columns([2, 2, 1])
            role = c1.selectbox(
                "Role", ["member", "admin"],
                index=0 if member.role == "member" else 1,
                key=f"user_role_{member.id}")
            member_active = c2.checkbox("Active", value=member.active,
                                        key=f"user_active_{member.id}")
            if c3.button("Update", key=f"user_update_{member.id}"):
                try:
                    accounts.set_user_access(
                        user.id, member.id, role=role, active=member_active)
                except accounts.AccountError as exc:
                    st.error(str(exc))
                else:
                    st.success("Access updated.")
                    st.rerun()


def _admin_databases_tab(user, status, check_status, state, active):
    st.caption(
        "Refresh, validate and inspect AFL, NBA, MLB and NFL data. Updates run "
        "in the background against staging files, so the app keeps serving the "
        "last validated database until each replacement is ready."
    )
    if status:
        completed_steps = status.get(
            "completed_steps", len(status.get("steps", [])))
        total_steps = status.get("total_steps")
        if total_steps is None:
            try:
                total_steps = len(database_updates.plan(
                    status.get("event", "full"),
                    status.get("sports", database_updates.SPORT_KEYS),
                ))
            except (TypeError, ValueError):
                total_steps = 0
        state_label = {
            "starting": "Starting", "running": "Running",
            "complete": "Complete", "complete_with_warnings": "Warnings",
            "failed": "Failed",
        }.get(state, str(state).replace("_", " ").title())
        summary = st.columns(4, vertical_alignment="center")
        summary[0].metric("Job status", state_label, border=True)
        summary[1].metric(
            "Scope", f"{len(status.get('sports', []))} sport(s)", border=True)
        summary[2].metric(
            "Progress", f"{completed_steps} / {total_steps or '?'} steps",
            border=True)
        summary[3].metric(
            "Elapsed",
            _elapsed_time(status.get("started_at"), status.get("finished_at")),
            border=True)
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
            if total_steps:
                current = status.get("current_step") or {}
                progress_text = f"{completed_steps} of {total_steps} steps finished"
                if current:
                    sport_prefix = (
                        f"{current.get('sport').upper()}: "
                        if current.get("sport") else "")
                    progress_text += (
                        f" - {sport_prefix}{current.get('label', 'Working')}")
                st.progress(
                    min(completed_steps / total_steps, 1.0),
                    text=progress_text,
                )
                st.caption(
                    "The status file updates before and after every step. "
                    "Use **Refresh status** below to see the latest result."
                )
        if status.get("error"):
            st.error(status["error"])
        sport_rows = _sport_progress_rows(status)
        if sport_rows:
            st.dataframe(
                sport_rows,
                column_config={
                    "Progress": st.column_config.ProgressColumn(
                        "Progress", min_value=0.0, max_value=1.0,
                        format="percent",
                    ),
                },
                width="stretch", hide_index=True,
            )
        if status.get("steps"):
            with st.expander(
                "Step history", icon=":material/list_alt:",
                expanded=state == "failed",
            ):
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

    with st.container(border=True):
        st.markdown("#### Run a database update (offline)")
        st.caption(
            "The web process is strictly read-only and cannot start a "
            "database write. Updates run from the command line (or their "
            "scheduled timers) against staging files, and this page follows "
            "their progress. `regular` is the routine scores-and-statistics "
            "update; the awards events add slower AFL award imports and are "
            "normally only needed after Brownlow night or the Grand Final."
        )
        st.code(
            "python database_updates.py run --event regular "
            "--sports afl nba mlb nfl\n"
            "python database_updates.py run --event brownlow-awards --sports afl\n"
            "python database_updates.py run --event grand-final-awards --sports afl",
            language="bash",
        )

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
    if controls.button("Refresh status", icon=":material/refresh:"):
        st.rerun()
    if controls.button(
        "Reload updated databases", icon=":material/restart_alt:",
        disabled=active,
        help="Closes this session's old read handles and reruns the app. A server reboot is not normally required.",
    ):
        db_pool.close_all()
        st.cache_data.clear()
        # st.connection stores its SQLConnection (engine and pool inside)
        # in cache_resource; clearing it disposes those engines, so no
        # pooled handle keeps reading the replaced file's old inode.
        st.cache_resource.clear()
        st.rerun()

    _render_database_check(check_status)


def _admin_grids_tab(gridley_status, active):
    st.markdown("#### Gridley game scan")
    st.caption(
        "Checks Gridley's public daily AFL board feed from the newest saved "
        "date through today. New boards are validated in a copy before the "
        "AFL database is atomically replaced. This does not scan Immaculate "
        "Grid. The scan runs on its own daily timer; to pick up today's "
        "board early, run it offline -- the web process is read-only and "
        "cannot start it:"
    )
    st.code("python database_updates.py gridley-scan", language="bash")
    _render_gridley_scan(gridley_status)


def _admin_rising_star_tab(user, rising_star_status, active):
    st.markdown("#### AFL Rising Star nominations")
    st.caption(
        "Reads this season's nominations from Wikipedia, which publishes the "
        "weekly nomination within a day. FootyWire stays the source for the "
        "nominee's match statistics, and keeps every round it already has. "
        "This also runs on a Monday timer, so the button is for picking up "
        "this week's nomination early."
    )
    try:
        rising_star_currency = database_updates.rising_star_currency()
    except (OSError, sqlite3.Error) as exc:
        rising_star_currency = {"state": "unreadable", "summary": str(exc)}
    _render_rising_star_currency(rising_star_currency)
    st.caption(
        "To pick up this week's nomination early, run the scan offline -- "
        "the web process is read-only and cannot start it:"
    )
    st.code("python database_updates.py rising-star-scan", language="bash")
    _render_rising_star_scan(rising_star_status)

    with st.expander("Hand-entered nominations"):
        st.caption(
            "Hand-entered nominations are stored as a source file and "
            "re-applied on every load, so a rebuild replays them rather "
            "than losing them. Adding, amending or removing one happens "
            "offline:"
        )
        st.code("python -m utils.afl.rising_star_manual --help",
                language="bash")
        _render_rising_star_edits()
