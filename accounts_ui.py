import datetime as dt
import sqlite3

import streamlit as st

import accounts
import data_paths
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
        st.info(f"{kind} of {label} is {state} "
                f"({_elapsed_time(status.get('started_at'))} so far). "
                "Applying a round takes about a minute.")
    elif state == "failed":
        st.error(f"{kind} of {label} failed: {status.get('error')}")
        st.caption("Nothing was written to the live database.")
    elif state == "complete" and status.get("dry_run"):
        st.success(f"{label} checked. Nothing was written — use **Load this "
                   "round** to apply it.")
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


def _rising_star_edit_form(user, active):
    """Nominate a player, or record a suspension or vote count.

    Players are chosen from a search rather than typed. A typed name that
    does not resolve loads as `unmatched` -- kept for audit, invisible to
    the solver -- so the nomination would look saved and answer nothing.
    Picking from the database cannot produce that.
    """
    from utils.afl import rising_star_manual as manual

    st.caption(
        "Hand-entered nominations are stored as a source file and re-applied "
        "on every load, so a rebuild replays them rather than losing them. "
        "A suspension or a vote count is recorded against the published "
        "nomination and keeps its match statistics."
    )

    term = st.text_input(
        "Find a player", key="rs_player_search",
        placeholder="Surname, or part of a name",
        help="Search the players already in the database.")
    if len(term.strip()) < 2:
        st.caption("Type at least two characters to search.")
        return

    try:
        con = db_pool.open_read_only(data_paths.default_db("afl"))
        try:
            matches = manual.search_players(con, term)
        finally:
            con.close()
    except (sqlite3.Error, OSError) as exc:
        st.error(f"Could not search players: {exc}")
        return
    if not matches:
        st.warning(f"No player matches {term!r}.")
        return

    chosen = st.selectbox(
        "Player", matches, format_func=lambda row: row["label"],
        key="rs_player_choice",
        help="Career span, games and clubs are shown because a name is not "
             "an identity — two Bailey Williamses played in 2026.")

    today = dt.date.today()
    with st.form("rising_star_edit"):
        fields = st.container(horizontal=True)
        season = fields.number_input(
            "Season", min_value=1993, max_value=today.year + 1,
            value=min(max(chosen["final_season"], 1993), today.year),
            step=1, format="%d", key="rs_season")
        round_number = fields.number_input(
            "Round", min_value=0, max_value=30, value=0, step=1,
            format="%d", key="rs_round",
            help="The round the nomination was for. Ignored when you are "
                 "only recording a suspension or votes.")
        club = fields.text_input("Club", value=(chosen["clubs"] or "").split(",")[0].strip(),
                                 key="rs_club")
        votes = fields.number_input(
            "Votes", min_value=0, max_value=200, value=0, step=1,
            format="%d", key="rs_votes",
            help="Final panel votes, published with the winner. Leave at 0 "
                 "to record none.")
        ineligible = st.checkbox(
            "Ineligible to win the Rising Star due to suspension",
            key="rs_ineligible",
            help="Records that a nominated player was later suspended and "
                 "so cannot win. The nomination itself stands.")
        winner = st.checkbox("Won the Rising Star this season", key="rs_winner")
        buttons = st.container(horizontal=True)
        nominate = buttons.form_submit_button(
            "Add nomination", icon=":material/add:", type="primary",
            disabled=active)
        annotate = buttons.form_submit_button(
            "Record against existing nomination", icon=":material/edit:",
            disabled=active)

    if not (nominate or annotate):
        return
    try:
        manual.upsert(
            int(season), chosen["player"],
            # The club is recorded either way. On an annotation it is what
            # tells two same-named nominees apart, and without it the
            # loader refuses to guess which one was suspended.
            club=club,
            # A round is what makes an entry a nomination. Passing it on an
            # annotation would turn "this nominee was suspended" into a
            # second nomination in whichever round the box happened to show.
            round_number=int(round_number) if nominate else None,
            ineligible=True if ineligible else None,
            votes=int(votes) or None,
            winner=True if winner else None,
            edited_by=getattr(user, "email", "admin"))
    except (OSError, ValueError) as exc:
        st.error(f"{type(exc).__name__}: {exc}")
        return

    try:
        database_updates.apply_rising_star_edits()
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        st.error(f"Saved the edit, but the database was not updated: {exc}")
        return
    st.session_state["database_update_notice"] = {
        "kind": "success",
        "message": (
            f"{chosen['player']} saved for {int(season)} and the AFL "
            "database was updated. Use Reload updated databases to pick it "
            "up in this session."
        ),
    }
    st.rerun()


def _render_rising_star_edits():
    """The hand-entered rows, so an edit can be found and undone."""
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

    labels = {
        f"{row.get('season')} {row.get('player')}": row.get("source_key")
        for row in entries
    }
    remove = st.selectbox("Undo an edit", ["—", *labels], key="rs_remove")
    if remove != "—" and st.button("Remove this edit",
                                   icon=":material/undo:", key="rs_remove_go"):
        manual.remove(labels[remove])
        try:
            database_updates.apply_rising_star_edits()
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            st.error(f"Removed the edit, but the reload failed: {exc}")
            return
        st.session_state["database_update_notice"] = {
            "kind": "success",
            "message": f"Removed the hand-entered row for {remove}.",
        }
        st.rerun()


def _render_manual_round_section(active):
    """Enter a round the upstream dataset has not published yet.

    afl/build_db.py does not scrape AFL Tables -- its robots.txt disallows
    it -- so game data arrives through the cached fitzRoy dataset, which
    lags the live season by a round or two. This is the supported way to
    close that gap, and it is the same loader the command line and the
    desktop window use, so all three agree on what a valid round is.

    Nothing here writes to the live database directly: a round is loaded
    into a staged copy and promoted only if the loader accepted it, and the
    parsed rows are stored so a rebuild replays them instead of losing
    them.
    """
    st.markdown("#### Hand-entered round results")
    st.caption(
        "For a round that has been played but not yet published upstream. "
        "Upload the round summary and one file per match, copied from the "
        "AFL Tables match pages. Player statistics, Brownlow votes where "
        "published, debutants and the ladder all follow from these files."
    )

    try:
        summary = database_updates.manual_rounds()
    except (OSError, sqlite3.Error, ImportError) as exc:
        summary = {}
        st.warning(f"Could not read stored rounds: {exc}")
    _render_manual_rounds(summary)

    today = dt.date.today()
    with st.form("manual_round_form"):
        fields = st.container(horizontal=True)
        season = fields.number_input(
            "Season", min_value=1897, max_value=today.year + 1,
            value=today.year, step=1, format="%d")
        round_name = fields.text_input(
            "Round", value="",
            help="The round as AFL Tables names it: 23, or a final such as EF.")
        # .csv only: the loader globs *.csv, so anything else would upload
        # successfully and then be invisible to it -- the worst of both.
        uploads = st.file_uploader(
            "Round summary and match files", accept_multiple_files=True,
            type=["csv"],
            help="One round summary plus one file per match. They are "
                 "paired to fixtures by the club names inside them, never "
                 "by filename, so a misnamed file cannot attach statistics "
                 "to the wrong match.")
        buttons = st.container(horizontal=True)
        check = buttons.form_submit_button(
            "Check this round", icon=":material/fact_check:")
        load = buttons.form_submit_button(
            "Load this round", icon=":material/upload_file:",
            disabled=active, type="primary")

    if check or load:
        if not str(round_name).strip():
            st.error("Enter the round, as AFL Tables names it.")
        elif not uploads:
            st.error("Upload the round summary and one file per match.")
        else:
            folder = database_updates.upload_round_files(
                int(season), str(round_name).strip(),
                [(item.name, item.getvalue()) for item in uploads])
            if check:
                # A dry run writes nothing, so it runs inline: the operator
                # is waiting for its verdict before deciding to load.
                with st.status("Checking the round...", expanded=True) as box:
                    status = database_updates.run_manual_round_load(
                        folder, int(season), str(round_name).strip(),
                        dry_run=True)
                    box.update(
                        label=("Round checked" if status.get("state") == "complete"
                               else "The round has problems"),
                        state=("complete" if status.get("state") == "complete"
                               else "error"))
                _render_manual_round_status(status)
                return
            try:
                pid = database_updates.start_manual_round_load_background(
                    folder, int(season), str(round_name).strip())
            except (OSError, RuntimeError, ValueError) as exc:
                st.error(f"{type(exc).__name__}: {exc}")
            else:
                st.session_state["database_update_notice"] = {
                    "kind": "success",
                    "message": (
                        f"Loading round {round_name}, {season} in the "
                        f"background (PID {pid}). It takes about a minute — "
                        "use Refresh status to follow it."
                    ),
                }
                st.rerun()

    _render_manual_round_status(database_updates.read_manual_round_status())

    redundant = [row for row in (summary.get("rounds") or [])
                 if row.get("upstream_has")]
    if redundant:
        with st.expander("Forget a round the upstream dataset now carries"):
            st.caption(
                "Dropping a stored round is safe once the rebuild produces "
                "it: the upstream rows are already the authority, and this "
                "only removes the hand-entered copy underneath them."
            )
            for row in redundant:
                if st.button(
                    f"Forget round {row['round']}, {row['season']}",
                    key=f"forget_{row['season']}_{row['round']}",
                    disabled=active, icon=":material/delete:",
                ):
                    try:
                        database_updates.forget_manual_round(
                            row["season"], row["round"])
                    except (OSError, RuntimeError, sqlite3.Error) as exc:
                        st.error(f"{type(exc).__name__}: {exc}")
                    else:
                        st.session_state["database_update_notice"] = {
                            "kind": "success",
                            "message": (
                                f"Round {row['round']}, {row['season']} is no "
                                "longer stored by hand."
                            ),
                        }
                        st.rerun()


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
        try:
            user = accounts.authenticate(email, password)
            if user is None:
                st.error("Email or password is incorrect, or this account is disabled.")
            else:
                st.session_state["auth_user_id"] = user.id
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
                       "its way. Check your email (or logs/emails.txt).")


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

    st.markdown("### Database operations")
    st.caption(
        "Refresh, validate and inspect AFL, NBA, MLB and NFL data. Updates run "
        "in the background against staging files, so the app keeps serving the "
        "last validated database until each replacement is ready."
    )

    notice = st.session_state.pop("database_update_notice", None)
    if notice:
        getattr(st, notice.get("kind", "info"))(notice.get("message", ""))

    status = database_updates.read_status()
    check_status = database_updates.read_check_status()
    gridley_status = database_updates.read_gridley_scan_status()
    rising_star_status = database_updates.read_rising_star_status()
    state = status.get("state", "unknown") if status else "unknown"
    gridley_state = (gridley_status.get("state", "unknown")
                     if gridley_status else "unknown")
    rising_star_state = (rising_star_status.get("state", "unknown")
                         if rising_star_status else "unknown")
    # Both jobs take the same lock, so both have to count here. Watching
    # only the main update's status left every button enabled while a
    # Gridley scan was running, and clicking one produced "a database
    # update is already running" instead of a disabled control.
    active = (
        (state in {"starting", "running"}
         or gridley_state in {"starting", "running"}
         or rising_star_state in {"starting", "running"})
        and database_updates.update_is_active()
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
        st.markdown("#### Start a database update")
        st.caption(
            "Scores and statistics is the routine update. The awards option "
            "adds slower AFL award imports and is normally only needed after "
            "Brownlow night or the Grand Final."
        )
        with st.form("admin_database_update_form", border=False):
            event = st.segmented_control(
                "Update type", list(_UPDATE_EVENTS), default="regular",
                format_func=lambda key: _UPDATE_EVENTS[key],
                selection_mode="single", width="stretch",
            )
            sports = st.pills(
                "Sports to update", database_updates.SPORT_KEYS,
                default=list(database_updates.SPORT_KEYS),
                selection_mode="multi", format_func=str.upper,
            )
            planned_steps = len(database_updates.plan(
                event, sports)) if event and sports else 0
            st.caption(
                f"Selected scope: {len(sports or [])} sport(s), "
                f"{planned_steps} validation and update steps. Each sport is "
                "promoted independently only after its required checks pass."
            )
            password = st.text_input(
                "Confirm your admin password", type="password",
                help=("A fresh password check is required before starting a "
                      "database write."),
            )
            submitted = st.form_submit_button(
                "Update selected databases", type="primary",
                icon=":material/sync:", disabled=active,
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
    if controls.button("Refresh status", icon=":material/refresh:"):
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
    if st.button(
        "Check for new Rising Star nominations", icon=":material/star:",
        disabled=active,
        help="One request. The AFL database is replaced only if a new "
             "nomination was published.",
    ):
        try:
            pid = database_updates.start_rising_star_scan_background()
        except (OSError, RuntimeError, ValueError) as exc:
            st.error(f"{type(exc).__name__}: {exc}")
        else:
            st.session_state["database_update_notice"] = {
                "kind": "success",
                "message": (
                    f"Rising Star check started in the background (PID {pid}). "
                    "It takes a few seconds -- use Refresh status to see what "
                    "it found."
                ),
            }
            st.rerun()
    _render_rising_star_scan(rising_star_status)

    with st.expander("Add or amend a Rising Star nomination by hand"):
        _rising_star_edit_form(user, active)
        _render_rising_star_edits()

    _render_manual_round_section(active)

    with st.expander("Automatic schedule"):
        st.write("Regular scores and statistics: Friday, Saturday, Sunday and Monday at 12:10 am Sydney time.")
        st.write("Gridley board scan: every day at 6:30 am Sydney time.")
        st.write("Rising Star nominations: Monday at 8:00 am Sydney time.")
        st.write("Brownlow and awards: 1:00 am on the Tuesday after Brownlow night (22 September in 2026).")
        st.write("Grand Final and final awards: 1:00 am on the Sunday after the last Saturday in September (27 September in 2026).")
        st.caption(
            "Ubuntu systemd timers run missed starts after downtime and the "
            "update lock prevents overlapping jobs."
        )
