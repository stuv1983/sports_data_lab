"""Keeping the databases current, on a timer and on demand.

Two questions the update pipeline did not answer:

*Is the data current?* Nothing measured it. A database rebuilt last night
from a feed that stopped three weeks ago has a fresh file timestamp, a
clean integrity check and stale contents, and the Admin page called that
healthy.

*Does the automatic path cover everything?* The Gridley board scan existed
only as an Admin button, so a board arrived only when somebody clicked.
"""

import datetime as dt
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

import database_updates as updates

TODAY = dt.date(2026, 8, 8)


def _games_db(rows, columns="season INTEGER, date TEXT"):
    con = sqlite3.connect(":memory:")
    con.execute(f"CREATE TABLE games ({columns})")
    if rows:
        marks = ", ".join("?" * len(rows[0]))
        con.executemany(f"INSERT INTO games VALUES ({marks})", rows)
    return con


# ------------------------------------------------------- measuring currency

def test_a_sport_with_real_dates_is_measured_in_days():
    con = _games_db([(2026, "2026-08-02"), (2026, "2026-07-26")])
    fresh = updates._freshness("afl", con, {"season", "date"}, TODAY)
    assert fresh["basis"] == "date"
    assert fresh["state"] == "current"
    assert fresh["latest_game_date"] == "2026-08-02"
    assert fresh["days_since_latest_game"] == 6


def test_an_off_season_gap_is_not_reported_as_behind():
    """The NFL sits idle from February to September every year, so the
    threshold has to clear a whole off-season or every summer is an alarm."""
    con = _games_db([(2025, "2026-02-08")])
    fresh = updates._freshness("nfl", con, {"season", "date"}, TODAY)
    assert fresh["days_since_latest_game"] == 181
    assert fresh["state"] == "current"


def test_a_feed_that_stopped_is_reported_as_behind():
    con = _games_db([(2024, "2024-08-02")])
    fresh = updates._freshness("afl", con, {"season", "date"}, TODAY)
    assert fresh["state"] == "behind"
    assert fresh["days_since_latest_game"] == 736


def test_a_season_granular_sport_is_measured_in_seasons():
    """MLB's Lahman import stamps every game in a season YYYY-04-01.

    All 2,201 games of the 2025 season share the date 2025-04-01, so a
    days-since-last-game reading would call a correctly loaded database
    sixteen months stale. Being one season behind is also normal: Lahman
    publishes a season only once it has finished.
    """
    fresh = updates._freshness(
        "mlb", _games_db([(2025, "2025-04-01")] * 3), {"season", "date"}, TODAY)
    assert fresh["basis"] == "season"
    assert fresh["state"] == "current"
    assert fresh["seasons_behind"] == 1


def test_a_season_granular_sport_that_missed_a_year_is_behind():
    fresh = updates._freshness(
        "mlb", _games_db([(2024, "2024-04-01")]), {"season", "date"}, TODAY)
    assert fresh["state"] == "behind"
    assert fresh["seasons_behind"] == 2


def test_the_date_basis_is_read_from_the_data_not_assumed():
    """One distinct date across a whole season is a placeholder, not a
    fixture list, whichever sport it belongs to."""
    fresh = updates._freshness(
        "afl", _games_db([(2026, "2026-04-01")] * 5), {"season", "date"}, TODAY)
    assert fresh["basis"] == "season"


def test_freshness_survives_a_games_table_without_dates():
    con = _games_db([(2026,)], columns="season INTEGER")
    assert updates._freshness("afl", con, {"season"}, TODAY)["basis"] == "season"


def test_freshness_survives_an_empty_games_table():
    empty = _games_db([], columns="season INTEGER, date TEXT")
    fresh = updates._freshness("afl", empty, {"season", "date"}, TODAY)
    assert fresh["state"] == "unknown"


def test_a_stale_database_is_reported_apart_from_a_broken_one(
        tmp_path, monkeypatch):
    """Behind is not broken. A stale database passes every integrity check
    and still answers every query, so it must not surface as a failure."""
    db = tmp_path / "afl.db"
    with closing(sqlite3.connect(db)) as con:
        con.execute("CREATE TABLE players (player_id INTEGER)")
        con.execute("CREATE TABLE games (season INTEGER, date TEXT)")
        con.executemany("INSERT INTO games VALUES (?, ?)",
                        [(2019, "2019-08-02"), (2019, "2019-07-26")])
        con.commit()
    monkeypatch.setattr(updates, "CHECK_STATUS_PATH", tmp_path / "check.json")
    monkeypatch.setattr(updates.data_paths, "default_db", lambda sport: str(db))

    report = updates.check_databases(["afl"])

    assert report["state"] == "complete"
    assert report["failures"] == []
    assert report["stale"] == ["afl"]
    assert report["databases"]["afl"]["freshness"]["state"] == "behind"


def test_the_live_file_listing_carries_currency_too(tmp_path, monkeypatch):
    """This is the table the Admin page shows, and its timestamp column
    records when the file was replaced -- not how recent the data is."""
    db = tmp_path / "afl.db"
    with closing(sqlite3.connect(db)) as con:
        con.execute("CREATE TABLE games (season INTEGER, date TEXT)")
        con.executemany("INSERT INTO games VALUES (?, ?)",
                        [(2026, "2026-08-02"), (2026, "2026-07-26")])
        con.commit()
    monkeypatch.setattr(updates.data_paths, "default_db", lambda sport: str(db))

    status = updates.database_file_status(["afl"])
    assert status["afl"]["freshness"]["latest_game_date"] == "2026-08-02"
    assert "freshness" not in updates.database_file_status(
        ["afl"], with_freshness=False)["afl"]


def test_the_check_command_reports_currency(capsys, tmp_path, monkeypatch):
    db = tmp_path / "afl.db"
    with closing(sqlite3.connect(db)) as con:
        con.execute("CREATE TABLE games (season INTEGER, date TEXT)")
        con.executemany("INSERT INTO games VALUES (?, ?)",
                        [(2026, "2026-08-02"), (2026, "2026-07-26")])
        con.commit()
    monkeypatch.setattr(updates, "CHECK_STATUS_PATH", tmp_path / "check.json")
    monkeypatch.setattr(updates.data_paths, "default_db", lambda sport: str(db))

    assert updates.main(["check", "--sports", "afl"]) == 0
    out = capsys.readouterr().out
    assert "currency=current" in out
    assert "2026-08-02" in out


# --------------------------------------------- the automatic Gridley path

def test_the_gridley_scan_is_reachable_from_a_timer(monkeypatch):
    called = {}
    monkeypatch.setattr(
        updates, "run_gridley_scan",
        lambda **kwargs: called.update(kwargs) or {"result": {}})
    assert updates.main(["scheduled", "gridley"]) == 0
    assert called == {"trigger": "systemd"}


def test_the_gridley_scan_has_a_command_line(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        updates, "run_gridley_scan",
        lambda **kwargs: seen.update(kwargs) or {"result": {"inserted": 0}})
    assert updates.main(
        ["gridley-scan", "--through", "2026-08-08", "--max-days", "7"]) == 0
    assert seen["through"] == dt.date(2026, 8, 8)
    assert seen["max_days"] == 7


def test_a_gridley_timer_is_installed_and_runs_daily():
    """Gridley publishes one board a day, so following the Friday-to-Monday
    scores schedule would miss most of them."""
    unit_dir = updates.ROOT / "deploy" / "systemd"
    timer = (unit_dir / "sports-data-lab-db-gridley.timer.in").read_text()
    assert "OnCalendar=*-*-* 06:30:00 Australia/Sydney" in timer
    assert "sports-data-lab-db-update@gridley.service" in timer
    installer = (updates.ROOT / "scripts"
                 / "install_database_update_systemd.sh").read_text()
    assert "sports-data-lab-db-gridley.timer" in installer
    tasks = (updates.ROOT / "scripts"
             / "install_database_update_tasks.ps1").read_text()
    assert "gridley-scan" in tasks


# ------------------------------------- administrator jobs run detached

def _detached(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(updates, "LOG_DIR", log_dir)
    monkeypatch.setattr(updates, "LOCK_PATH", log_dir / "update.lock")
    monkeypatch.setattr(updates, "STATUS_PATH", log_dir / "status.json")
    monkeypatch.setattr(
        updates, "GRIDLEY_SCAN_STATUS_PATH", log_dir / "gridley.json")
    spawned = []

    def fake_popen(argv, **kwargs):
        spawned.append(argv)
        assert kwargs["stdin"] is updates.subprocess.DEVNULL
        assert "SPORTS_DATA_UPDATE_RESERVATION" in kwargs["env"]
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(updates.subprocess, "Popen", fake_popen)
    return log_dir, spawned


def test_the_gridley_scan_hands_off_to_a_detached_child(
        tmp_path, monkeypatch):
    """Run inline it made up to 31 sequential HTTP requests inside a
    Streamlit script run, blocking the page and losing the job whenever
    the websocket timed out first."""
    log_dir, spawned = _detached(tmp_path, monkeypatch)

    assert updates.start_gridley_scan_background(max_days=7) == 4242

    assert spawned[-1][2:] == [
        "database_updates", "gridley-scan", "--max-days", "7",
        "--trigger", "admin"]
    status = json.loads((log_dir / "gridley.json").read_text(encoding="utf-8"))
    assert status["state"] == "starting"
    assert updates.LOCK_PATH.exists(), "the child's lock must be reserved"


def test_an_admin_update_can_target_one_event_and_one_sport(
        tmp_path, monkeypatch):
    """The button always started event="full" over all four sports, which
    is hours of work when the ask was "refresh the AFL scores"."""
    _log_dir, spawned = _detached(tmp_path, monkeypatch)

    assert updates.start_background("regular", ["afl"]) == 4242

    assert spawned[-1][2:] == [
        "database_updates", "run", "--event", "regular",
        "--sports", "afl", "--trigger", "admin"]


def test_an_admin_update_rejects_a_scope_it_cannot_run(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "LOG_DIR", tmp_path)
    monkeypatch.setattr(updates, "LOCK_PATH", tmp_path / "update.lock")
    for event, sports in (("nonsense", ["afl"]), ("regular", []),
                          ("regular", ["quidditch"])):
        try:
            updates.start_background(event, sports)
        except ValueError:
            continue
        raise AssertionError(f"accepted {event!r} with {sports!r}")


# ------------------------------- promoting past a running application

def test_a_rebuild_promotes_while_the_app_holds_the_database_open(
        tmp_path, monkeypatch):
    """Windows will not rename over a file another process holds open.

    db_pool gives the running app one read-only handle per thread with a
    256 MB memory map on it, which is doubly unrenameable, so every
    scheduled update failed with "stop the Streamlit server" on the very
    machine that serves the app. Promotion copies the staged contents
    through SQLite instead, which the attached readers take part in.
    """
    import db_pool

    live = tmp_path / "afl.db"
    staging = tmp_path / "afl.db.update-building"
    with closing(sqlite3.connect(live)) as con:
        con.execute("CREATE TABLE games (season INTEGER, date TEXT)")
        con.execute("INSERT INTO games VALUES (2025, '2025-08-02')")
        con.commit()
    with closing(sqlite3.connect(staging)) as con:
        con.execute("CREATE TABLE games (season INTEGER, date TEXT)")
        con.executemany("INSERT INTO games VALUES (?, ?)",
                        [(2026, "2026-08-02")] * 40)
        con.commit()
    monkeypatch.setattr(updates.data_paths, "default_db", lambda s: str(live))

    reader = db_pool.open_read_only(str(live))
    try:
        assert reader.execute("SELECT MAX(date) FROM games").fetchone()[0] \
            == "2025-08-02"

        updates._backup_and_promote("afl", staging)

        # The already-attached reader sees it without reconnecting.
        assert reader.execute("SELECT MAX(date) FROM games").fetchone()[0] \
            == "2026-08-02"
        assert reader.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 40
        assert reader.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not staging.exists()
    finally:
        reader.close()


def test_an_unlocked_promotion_still_takes_the_atomic_rename(
        tmp_path, monkeypatch):
    """The copy is the fallback, not the new normal: with nothing holding
    the file, os.replace is instant and atomic."""
    live = tmp_path / "afl.db"
    staging = tmp_path / "afl.db.update-building"
    for path, season in ((live, 2025), (staging, 2026)):
        with closing(sqlite3.connect(path)) as con:
            con.execute("CREATE TABLE games (season INTEGER)")
            con.execute("INSERT INTO games VALUES (?)", (season,))
            con.commit()
    monkeypatch.setattr(updates.data_paths, "default_db", lambda s: str(live))
    monkeypatch.setattr(
        updates, "_promote_in_place",
        lambda *a: pytest.fail("should not need the in-place copy"))

    updates._backup_and_promote("afl", staging)

    with closing(sqlite3.connect(live)) as con:
        assert con.execute("SELECT season FROM games").fetchone()[0] == 2026
