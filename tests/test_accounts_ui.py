import datetime as dt

import accounts_ui
from streamlit.testing.v1 import AppTest


def test_elapsed_time_formats_running_and_completed_jobs(monkeypatch):
    started = dt.datetime(2026, 8, 9, 10, 0, tzinfo=dt.timezone.utc)
    finished = started + dt.timedelta(hours=1, minutes=7, seconds=12)

    assert accounts_ui._elapsed_time(
        started.isoformat(), finished.isoformat()) == "1h 7m"
    assert accounts_ui._elapsed_time(None) == "Unknown"


def test_sport_progress_identifies_current_and_safely_retained_databases(
        monkeypatch):
    monkeypatch.setattr(
        accounts_ui.database_updates,
        "plan",
        lambda event, sports: [
            ("afl", object()), ("afl", object()), ("nba", object())],
    )
    rows = accounts_ui._sport_progress_rows({
        "state": "running", "event": "regular", "sports": ["afl", "nba"],
        "steps": [{
            "sport": "afl", "label": "Fetch AFL", "state": "failed",
            "optional": False,
        }],
        "current_step": {"sport": "nba", "label": "Fetch NBA"},
        "promotions": {"afl": {"state": "retained_live"}},
    })

    assert rows[0] == {
        "Sport": "AFL", "Status": "Live database retained",
        "Progress": 0.5, "Steps": "1 / 2",
        "Current or last step": "Fetch AFL",
    }
    assert rows[1]["Status"] == "Running"
    assert rows[1]["Current or last step"] == "Fetch NBA"


def test_a_season_behind_on_nominations_says_so_on_the_page():
    """The warning is the whole point of the panel, so it is worth pinning."""
    app = AppTest.from_string('''
from types import SimpleNamespace
import accounts_ui as ui

saved = {
    "FEATURES": ui.accounts.FEATURES,
    "feature_policies": ui.accounts.feature_policies,
    "list_users": ui.accounts.list_users,
    "read_status": ui.database_updates.read_status,
    "read_check_status": ui.database_updates.read_check_status,
    "read_gridley_scan_status": ui.database_updates.read_gridley_scan_status,
    "read_rising_star_status": ui.database_updates.read_rising_star_status,
    "rising_star_currency": ui.database_updates.rising_star_currency,
    "read_manual_round_status": ui.database_updates.read_manual_round_status,
    "manual_rounds": ui.database_updates.manual_rounds,
    "update_is_active": ui.database_updates.update_is_active,
    "database_file_status": ui.database_updates.database_file_status,
}
try:
    ui.accounts.FEATURES = {}
    ui.accounts.feature_policies = lambda: {}
    ui.accounts.list_users = lambda: []
    ui.database_updates.read_status = lambda: {}
    ui.database_updates.read_check_status = lambda: {}
    ui.database_updates.read_gridley_scan_status = lambda: {}
    ui.database_updates.read_rising_star_status = lambda: {}
    ui.database_updates.rising_star_currency = lambda: {
        "state": "loaded", "season": 2026, "season_nominations": 15,
        "total": 757, "latest_round": 14, "latest_player": "Jasper Alger",
        "latest_club": "Richmond", "latest_source": "footywire",
        "latest_linked": True, "sources": {"footywire": 757},
        "days_since_check": 41, "in_season": True, "stale": True,
    }
    ui.database_updates.read_manual_round_status = lambda: {}
    ui.database_updates.manual_rounds = lambda: {"rounds": []}
    ui.database_updates.update_is_active = lambda: False
    ui.database_updates.database_file_status = lambda: {
        sport: {"exists": False} for sport in ("afl", "nba", "mlb", "nfl")
    }
    ui.admin_page(SimpleNamespace(id=1, email="admin@example.com", is_admin=True))
finally:
    ui.accounts.FEATURES = saved["FEATURES"]
    ui.accounts.feature_policies = saved["feature_policies"]
    ui.accounts.list_users = saved["list_users"]
    ui.database_updates.read_status = saved["read_status"]
    ui.database_updates.read_check_status = saved["read_check_status"]
    ui.database_updates.read_gridley_scan_status = saved["read_gridley_scan_status"]
    ui.database_updates.read_rising_star_status = saved["read_rising_star_status"]
    ui.database_updates.rising_star_currency = saved["rising_star_currency"]
    ui.database_updates.read_manual_round_status = saved["read_manual_round_status"]
    ui.database_updates.manual_rounds = saved["manual_rounds"]
    ui.database_updates.update_is_active = saved["update_is_active"]
    ui.database_updates.database_file_status = saved["database_file_status"]
''')

    app.run()

    assert not list(app.exception)
    warnings = [str(element.value) for element in app.warning]
    assert any("41 days ago" in text for text in warnings)
    assert any("Monday timer" in text for text in warnings)


def test_days_ago_reads_as_english_not_arithmetic():
    assert accounts_ui._days_ago(None) == "Never"
    assert accounts_ui._days_ago(0) == "Today"
    assert accounts_ui._days_ago(1) == "Yesterday"
    assert accounts_ui._days_ago(11) == "11 days ago"


def test_database_operations_console_renders_with_all_sports_selected():
    app = AppTest.from_string('''
from types import SimpleNamespace
import accounts_ui as ui

saved = {
    "FEATURES": ui.accounts.FEATURES,
    "feature_policies": ui.accounts.feature_policies,
    "list_users": ui.accounts.list_users,
    "read_status": ui.database_updates.read_status,
    "read_check_status": ui.database_updates.read_check_status,
    "read_gridley_scan_status": ui.database_updates.read_gridley_scan_status,
    "read_rising_star_status": ui.database_updates.read_rising_star_status,
    "rising_star_currency": ui.database_updates.rising_star_currency,
    "read_manual_round_status": ui.database_updates.read_manual_round_status,
    "manual_rounds": ui.database_updates.manual_rounds,
    "update_is_active": ui.database_updates.update_is_active,
    "database_file_status": ui.database_updates.database_file_status,
}
try:
    ui.accounts.FEATURES = {}
    ui.accounts.feature_policies = lambda: {}
    ui.accounts.list_users = lambda: []
    ui.database_updates.read_status = lambda: {
        "state": "complete", "event": "regular",
        "sports": ["afl", "nba", "mlb", "nfl"],
        "steps": [], "completed_steps": 0, "total_steps": 17,
        "started_at": "2026-08-09T10:00:00+10:00",
        "finished_at": "2026-08-09T10:01:00+10:00",
    }
    ui.database_updates.read_check_status = lambda: {}
    ui.database_updates.read_gridley_scan_status = lambda: {}
    ui.database_updates.read_rising_star_status = lambda: {
        "state": "complete", "season": 2026, "promoted": True,
        "trigger": "admin",
        "started_at": "2026-08-11T08:00:00+10:00",
        "finished_at": "2026-08-11T08:00:04+10:00",
        "result": {
            "season": 2026, "changed": True, "latest_round": 22,
            "new_nominations": [{"round": 22, "player": "Jesse Dattoli",
                                 "club": "Sydney"}],
        },
    }
    ui.database_updates.rising_star_currency = lambda: {
        "state": "loaded", "season": 2026, "season_nominations": 23,
        "total": 765, "latest_round": 22, "latest_player": "Jesse Dattoli",
        "latest_club": "Sydney", "latest_source": "wikipedia",
        "latest_linked": True,
        "sources": {"footywire": 763, "wikipedia": 2},
        "days_since_check": 0, "in_season": True, "stale": False,
    }
    ui.database_updates.read_manual_round_status = lambda: {}
    ui.database_updates.manual_rounds = lambda: {
        "rounds": [{"season": 2026, "round": "23", "upstream_has": True}],
        "latest_season": 2026, "latest_round": "23",
        "latest_date": "2026-08-09", "redundant": 1,
    }
    ui.database_updates.update_is_active = lambda: False
    ui.database_updates.database_file_status = lambda: {
        sport: {"exists": False} for sport in ("afl", "nba", "mlb", "nfl")
    }
    ui.admin_page(SimpleNamespace(id=1, email="admin@example.com", is_admin=True))
finally:
    ui.accounts.FEATURES = saved["FEATURES"]
    ui.accounts.feature_policies = saved["feature_policies"]
    ui.accounts.list_users = saved["list_users"]
    ui.database_updates.read_status = saved["read_status"]
    ui.database_updates.read_check_status = saved["read_check_status"]
    ui.database_updates.read_gridley_scan_status = saved["read_gridley_scan_status"]
    ui.database_updates.read_rising_star_status = saved["read_rising_star_status"]
    ui.database_updates.rising_star_currency = saved["rising_star_currency"]
    ui.database_updates.read_manual_round_status = saved["read_manual_round_status"]
    ui.database_updates.manual_rounds = saved["manual_rounds"]
    ui.database_updates.update_is_active = saved["update_is_active"]
    ui.database_updates.database_file_status = saved["database_file_status"]
''')

    app.run()

    assert not list(app.exception)
    assert app.pills[0].value == ["afl", "nba", "mlb", "nfl"]
    assert any(
        button.label == "Update selected databases" for button in app.button)
    assert any(
        button.label == "Check for new Rising Star nominations"
        for button in app.button)
    # How current the award is has to be legible without running a check.
    labels = [metric.label for metric in app.metric]
    assert "Latest 2026 nomination" in labels
    assert "Source last checked" in labels
    assert any("Round 22" == metric.value for metric in app.metric)
    assert any("Jesse Dattoli" in str(element.value)
               for element in app.success)
    # Hand-entered rounds are enterable here, not only from the desktop
    # window and the command line.
    assert any(button.label == "Load this round" for button in app.button)
    assert any(button.label == "Forget round 23, 2026"
               for button in app.button)


# --------------------------------------------------------------------------
# following a round load while it runs


#: Names `_admin_app` replaces on the shared modules, and must put back.
#: `database_updates` is imported once per session, so a stub left behind
#: here is a stub every later suite runs against -- which is exactly what
#: happened, and it took out ten tests in test_database_updates.py.
_ADMIN_STUBBED = (
    ("accounts", "FEATURES"), ("accounts", "feature_policies"),
    ("accounts", "list_users"),
    ("database_updates", "read_status"),
    ("database_updates", "read_check_status"),
    ("database_updates", "read_gridley_scan_status"),
    ("database_updates", "read_rising_star_status"),
    ("database_updates", "rising_star_currency"),
    ("database_updates", "read_manual_round_status"),
    ("database_updates", "manual_rounds"),
    ("database_updates", "update_is_active"),
    ("database_updates", "database_file_status"),
)


def _admin_app(extra_stubs: str = "", running_round: str = "{}"):
    """Render admin_page against stubbed job statuses, restoring them after."""
    saved = "\n".join(
        f'_saved[({module!r}, {name!r})] = getattr(ui.{module}, {name!r})'
        for module, name in _ADMIN_STUBBED)
    restored = "\n    ".join(
        f'setattr(ui.{module}, {name!r}, _saved[({module!r}, {name!r})])'
        for module, name in _ADMIN_STUBBED)
    return AppTest.from_string(f'''
from types import SimpleNamespace
import accounts_ui as ui

_saved = {{}}
{saved}
try:
    ui.accounts.FEATURES = {{}}
    ui.accounts.feature_policies = lambda: {{}}
    ui.accounts.list_users = lambda: []
    ui.database_updates.read_status = lambda: {{}}
    ui.database_updates.read_check_status = lambda: {{}}
    ui.database_updates.read_gridley_scan_status = lambda: {{}}
    ui.database_updates.read_rising_star_status = lambda: {{}}
    ui.database_updates.rising_star_currency = lambda: {{
        "state": "loaded", "season": 2026, "total": 757, "sources": {{}},
        "days_since_check": 1, "in_season": True, "stale": False,
    }}
    ui.database_updates.read_manual_round_status = lambda: {running_round}
    ui.database_updates.manual_rounds = lambda: {{"rounds": []}}
    ui.database_updates.update_is_active = lambda: True
    ui.database_updates.database_file_status = lambda: {{
        sport: {{"exists": False}} for sport in ("afl", "nba", "mlb", "nfl")
    }}
    {extra_stubs}
    ui.admin_page(SimpleNamespace(id=1, email="a@b.c", is_admin=True))
finally:
    {restored}
''')


def test_the_admin_page_groups_its_jobs_by_what_they_touch():
    """One column of everything is how the round loader got buried."""
    app = _admin_app()
    app.run()

    assert not list(app.exception)
    labels = [tab.label for tab in app.tabs]
    for expected in ("Databases", "Match data", "Rising Star", "Grids",
                     "Access", "Schedule"):
        assert expected in labels, f"{expected} tab is missing"


def test_a_running_load_reports_the_phase_it_is_on_not_just_the_seconds():
    running = ('{"state": "running", "season": 2026, "round": "22", '
               '"dry_run": False, "started_at": "2026-08-11T19:00:00+10:00", '
               '"phase": "Deriving matches", "phase_step": 7, '
               '"phase_total": 11}')
    app = _admin_app(running_round=running)
    app.run()

    assert not list(app.exception)
    # st.progress reports its value as a truncated percentage.
    bars = [element.value for element in app.get("progress")]
    assert bars, "a running load shows no progress bar"
    assert int(100 * 7 / 11) in bars

    metrics = {element.label: element.value for element in app.metric}
    assert metrics.get("Phase") == "7 / 11"
    assert "Elapsed" in metrics
    text = " ".join(str(element.value) for element in app.markdown)
    assert "Load of round 22, 2026 is running" in text


def test_a_job_in_progress_is_announced_above_the_tabs():
    """Started on one tab, followed from any of them."""
    running = ('{"state": "running", "season": 2026, "round": "22", '
               '"started_at": "2026-08-11T19:00:00+10:00"}')
    app = _admin_app(running_round=running)
    app.run()

    warnings = " ".join(str(element.value) for element in app.warning)
    assert "Round load in progress" in warnings
    assert "disabled until it finishes" in warnings


def test_nothing_running_means_no_banner_and_no_progress_bar():
    app = _admin_app()
    app.run()

    warnings = " ".join(str(element.value) for element in app.warning)
    assert "in progress" not in warnings


def test_a_load_whose_process_has_gone_is_not_polled_for_ever():
    """The status file is the only trace a detached job leaves behind."""
    old = (dt.datetime.now().astimezone() - dt.timedelta(minutes=5)).isoformat()
    status = {"state": "running", "started_at": old}
    saved = accounts_ui.database_updates.update_is_active
    try:
        accounts_ui.database_updates.update_is_active = lambda: False
        assert accounts_ui._round_load_stalled(status) is True
        # Still holding the lock means it is simply slow, not gone.
        accounts_ui.database_updates.update_is_active = lambda: True
        assert accounts_ui._round_load_stalled(status) is False
    finally:
        accounts_ui.database_updates.update_is_active = saved


def test_a_job_that_has_only_just_started_is_never_called_stalled():
    """The child takes the lock a moment after the parent says 'starting'."""
    just_now = dt.datetime.now().astimezone().isoformat()
    saved = accounts_ui.database_updates.update_is_active
    try:
        accounts_ui.database_updates.update_is_active = lambda: False
        assert accounts_ui._round_load_stalled(
            {"state": "starting", "started_at": just_now}) is False
    finally:
        accounts_ui.database_updates.update_is_active = saved


def test_a_finished_job_is_never_called_stalled():
    assert accounts_ui._round_load_stalled({"state": "complete"}) is False
    assert accounts_ui._round_load_stalled({}) is False
    assert accounts_ui._round_load_stalled(None) is False
