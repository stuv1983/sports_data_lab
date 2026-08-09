import datetime as dt
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

import database_updates as updates


def test_2026_afl_annual_dates_match_announced_calendar():
    assert updates.last_saturday_in_september(2026) == dt.date(2026, 9, 26)
    assert updates.brownlow_refresh_date(2026) == dt.date(2026, 9, 22)
    assert updates.grand_final_refresh_date(2026) == dt.date(2026, 9, 27)


def test_every_regular_sport_fetches_and_ends_with_a_strict_health_check():
    """Each sport pulls from its source, then is checked strictly.

    "rebuild" used to stand in for the fetch, but MLB no longer rebuilds:
    its Lahman export is read once and never again, so the current season
    is loaded into the database that exists rather than replacing it.
    """
    planned = updates.plan("regular", updates.SPORT_KEYS)
    for sport in updates.SPORT_KEYS:
        labels = [step.label for key, step in planned if key == sport]
        assert any(word in label.lower()
                   for label in labels
                   for word in ("rebuild", "load")), (sport, labels)
        assert labels[-1] == "Strict database health check"


def test_annual_due_guards_only_match_the_intended_day():
    assert updates.event_is_due("brownlow-awards", dt.date(2026, 9, 22))
    assert not updates.event_is_due("brownlow-awards", dt.date(2026, 9, 29))
    assert updates.event_is_due("grand-final-awards", dt.date(2026, 9, 27))


def test_grand_final_job_rebuilds_scores_before_awards():
    planned = updates.plan("grand-final-awards", ["afl"])
    labels = [step.label for _, step in planned]
    assert labels[0] == "Fetch and rebuild AFL"
    assert "Load Brownlow CSVs" in labels


def test_plan_routes_database_steps_to_requested_staging_paths(tmp_path):
    targets = {
        sport: str(tmp_path / f"{sport}.db.update-building")
        for sport in updates.SPORT_KEYS
    }
    planned = updates.plan("full", updates.SPORT_KEYS, targets)

    routed = 0
    for sport, step in planned:
        if "--db" not in step.argv:
            continue
        routed += 1
        db_index = step.argv.index("--db") + 1
        assert step.argv[db_index] == targets[sport]
    assert routed >= 16


def _create_db(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as con:
        con.execute("CREATE TABLE marker (value TEXT)")
        con.execute("INSERT INTO marker VALUES (?)", (value,))
        con.commit()


def test_brownlow_staging_copies_live_but_regular_rebuild_starts_clean(
        tmp_path, monkeypatch):
    live = tmp_path / "afl.db"
    stage = tmp_path / "afl.db.update-building"
    _create_db(live, "live")
    monkeypatch.setattr(updates.data_paths, "default_db", lambda sport: str(live))

    updates._prepare_staging("brownlow-awards", ["afl"], {"afl": stage})
    with closing(sqlite3.connect(stage)) as con:
        assert con.execute("SELECT value FROM marker").fetchone()[0] == "live"

    updates._prepare_staging("regular", ["afl"], {"afl": stage})
    assert not stage.exists()


def test_validated_staging_is_promoted_and_live_database_is_backed_up(
        tmp_path, monkeypatch):
    live = tmp_path / "afl.db"
    stage = tmp_path / "afl.db.update-building"
    _create_db(live, "old")
    _create_db(stage, "new")
    monkeypatch.setattr(updates.data_paths, "default_db", lambda sport: str(live))

    backup = updates._backup_and_promote("afl", stage)

    assert backup is not None
    assert Path(backup).exists()
    assert not stage.exists()
    with closing(sqlite3.connect(live)) as con:
        assert con.execute("SELECT value FROM marker").fetchone()[0] == "new"
    with closing(sqlite3.connect(backup)) as con:
        assert con.execute("SELECT value FROM marker").fetchone()[0] == "old"


def test_check_only_reports_contents_without_changing_database(
        tmp_path, monkeypatch):
    db = tmp_path / "afl.db"
    with closing(sqlite3.connect(db)) as con:
        con.execute("CREATE TABLE players (player_id INTEGER)")
        con.execute("CREATE TABLE games (season INTEGER)")
        con.executemany("INSERT INTO players VALUES (?)", [(1,), (2,)])
        con.executemany("INSERT INTO games VALUES (?)", [(2025,), (2026,)])
        con.commit()
    before = db.read_bytes()
    modified_ns = db.stat().st_mtime_ns
    check_path = tmp_path / "check-status.json"
    monkeypatch.setattr(updates, "CHECK_STATUS_PATH", check_path)
    monkeypatch.setattr(updates.data_paths, "default_db", lambda sport: str(db))

    result = updates.check_databases(["afl"])

    assert result["state"] == "complete"
    assert result["mode"] == "read_only"
    assert result["databases"]["afl"]["players"] == 2
    assert result["databases"]["afl"]["records"] == 2
    assert result["databases"]["afl"]["season_max"] == 2026
    assert db.read_bytes() == before
    assert db.stat().st_mtime_ns == modified_ns
    assert updates.read_check_status() == result


def test_gridley_scan_promotes_new_board_atomically(tmp_path, monkeypatch):
    live = tmp_path / "afl.db"
    with closing(sqlite3.connect(live)) as con:
        con.execute("CREATE TABLE players (player_id INTEGER)")
        con.execute("CREATE TABLE games (season INTEGER)")
        con.commit()
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(updates, "LOG_DIR", log_dir)
    monkeypatch.setattr(updates, "LOCK_PATH", log_dir / "update.lock")
    monkeypatch.setattr(
        updates, "GRIDLEY_SCAN_STATUS_PATH", log_dir / "gridley-scan.json"
    )
    monkeypatch.setattr(updates.data_paths, "default_db", lambda sport: str(live))

    def fake_fetch(date):
        return {
            "grid_num": 1119,
            "rows": ["r1", "r2", "r3"],
            "cols": ["c1", "c2", "c3"],
        }

    status = updates.run_gridley_scan(
        through=dt.date(2026, 8, 8), fetcher=fake_fetch
    )

    assert status["state"] == "complete"
    assert status["promoted"] is True
    assert status["result"]["inserted"] == 1
    assert not updates.LOCK_PATH.exists()
    assert not live.with_suffix(".db.gridley-scan-building").exists()
    with closing(sqlite3.connect(live)) as con:
        assert con.execute(
            "SELECT grid_num, date FROM historic_grids"
        ).fetchall() == [(1119, "2026-08-08")]


def test_required_failure_skips_that_sport_and_does_not_block_others(
        tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(updates, "LOG_DIR", log_dir)
    monkeypatch.setattr(updates, "STATUS_PATH", log_dir / "status.json")
    monkeypatch.setattr(updates, "LOCK_PATH", log_dir / "update.lock")
    monkeypatch.setattr(
        updates.data_paths, "default_db",
        lambda sport: str(tmp_path / f"{sport}.db"),
    )

    def fake_plan(event, sports, db_paths=None):
        return [
            ("afl", updates.Step("AFL fails", ("afl-fail",))),
            ("afl", updates.Step("AFL skipped", ("afl-skip",))),
            ("nba", updates.Step("NBA succeeds", ("nba-ok",))),
        ]

    def fake_prepare(event, sports, paths):
        for sport in sports:
            _create_db(paths[sport], sport)

    calls = []
    observed_current_steps = []

    def fake_run(argv, **kwargs):
        calls.append(argv[0])
        live_status = json.loads(
            (log_dir / "status.json").read_text(encoding="utf-8"))
        observed_current_steps.append(live_status["current_step"])
        return SimpleNamespace(returncode=1 if argv[0] == "afl-fail" else 0)

    promoted = []
    monkeypatch.setattr(updates, "plan", fake_plan)
    monkeypatch.setattr(updates, "_prepare_staging", fake_prepare)
    monkeypatch.setattr(updates.subprocess, "run", fake_run)
    monkeypatch.setattr(
        updates, "_backup_and_promote",
        lambda sport, staging: promoted.append(sport),
    )

    assert updates.run_job("regular", ["afl", "nba"], trigger="test") == 1
    # The trailing call is _refresh_reference's own subprocess.run, fired
    # only for nba because only nba reached "promoted" -- afl's failure
    # skipped its promotion, and with it, its reference refresh.
    assert calls == ["afl-fail", "nba-ok", sys.executable]
    # The refresh call above runs after the step loop has already cleared
    # current_step, so it observes None -- unlike the two step calls, which
    # each observe the status this same job published for themselves.
    assert observed_current_steps == [
        {
            "sport": "afl", "label": "AFL fails", "step_number": 1,
            "started_at": observed_current_steps[0]["started_at"],
        },
        {
            "sport": "nba", "label": "NBA succeeds", "step_number": 3,
            "started_at": observed_current_steps[1]["started_at"],
        },
        None,
    ]
    assert promoted == ["nba"]
    status = json.loads((log_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["completed_steps"] == 3
    assert status["total_steps"] == 3
    assert status["current_step"] is None
    assert status["steps"][1]["state"] == "skipped"
    assert status["promotions"]["afl"]["state"] == "retained_live"
    assert status["promotions"]["nba"]["state"] == "promoted"


def test_running_state_is_visible_before_large_database_snapshots(
        tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(updates, "LOG_DIR", log_dir)
    monkeypatch.setattr(updates, "STATUS_PATH", log_dir / "status.json")
    monkeypatch.setattr(updates, "LOCK_PATH", log_dir / "update.lock")
    monkeypatch.setattr(
        updates.data_paths, "default_db", lambda sport: str(tmp_path / "afl.db"))

    def snapshot(_sport):
        visible = updates.read_status()
        assert visible["state"] == "running"
        assert visible["current_step"]["label"] == "Inspecting live databases"
        raise RuntimeError("stop after observing status")

    monkeypatch.setattr(updates, "_database_snapshot", snapshot)
    with pytest.raises(RuntimeError, match="stop after observing status"):
        updates.run_job("regular", ["afl"], trigger="test")


def test_failed_nested_builder_reports_the_real_diagnostic_database(tmp_path):
    staging = tmp_path / "nba.db.update-building"
    working = tmp_path / "nba.db.update-building.building"
    working.touch()
    Path(str(staging) + ".build-report.json").write_text(json.dumps({
        "status": "failed", "working_db": str(working),
    }), encoding="utf-8")

    assert updates._retained_staging_path(staging) == working


def test_scheduled_annual_command_exits_when_not_due(monkeypatch):
    monkeypatch.setattr(updates, "event_is_due", lambda event: False)
    monkeypatch.setattr(
        updates, "run_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ran job")),
    )
    assert updates.main(["scheduled", "brownlow-awards"]) == 0


def test_systemd_templates_use_sydney_time_and_persistent_timers():
    unit_dir = updates.ROOT / "deploy" / "systemd"
    service = (unit_dir / "sports-data-lab-db-update@.service.in").read_text()
    assert "database_updates scheduled %i" in service
    assert "TimeoutStartSec=8h" in service
    for timer in unit_dir.glob("*.timer.in"):
        content = timer.read_text()
        assert "Australia/Sydney" in content
        assert "Persistent=true" in content


# -------------------------------------- what a rebuild must not destroy

def _grid_db(path, boards):
    with closing(sqlite3.connect(path)) as con:
        con.execute("CREATE TABLE historic_grids (grid_num INTEGER PRIMARY "
                    "KEY, date TEXT, source TEXT)")
        con.executemany("INSERT INTO historic_grids VALUES (?,?,?)", boards)
        con.commit()
    return path


def test_a_rebuild_does_not_empty_the_captured_grid_library(tmp_path,
                                                            monkeypatch):
    """`afl.build_db` writes a database from nothing.

    The Gridley feed serves recent boards only, so a board captured on the
    day it was published is the only copy there will ever be. Promoting a
    fresh build over the live file took the library from thirteen boards
    down to whatever that morning's scan had found.
    """
    live = _grid_db(tmp_path / "afl.db",
                    [(1118, "2026-08-05", "Gridley"),
                     (1119, "2026-08-08", "Gridley")])
    staging = tmp_path / "afl.db.update-building"
    with closing(sqlite3.connect(staging)) as con:
        con.execute("CREATE TABLE games (player_id)")
        con.commit()

    carried = updates._carry_forward("afl", staging, live)

    assert carried == {"historic_grids": 2}
    with closing(sqlite3.connect(f"file:{staging}?mode=ro", uri=True)) as con:
        assert [row[0] for row in con.execute(
            "SELECT grid_num FROM historic_grids ORDER BY grid_num")] == [
            1118, 1119]


def test_a_board_the_rebuild_already_has_is_not_duplicated(tmp_path):
    """The scan writes to the live database and a rebuild may run after
    it, so the same board can exist on both sides."""
    live = _grid_db(tmp_path / "afl.db", [(1120, "2026-08-09", "Gridley")])
    staging = _grid_db(tmp_path / "afl.db.update-building",
                       [(1120, "2026-08-09", "Gridley")])

    updates._carry_forward("afl", staging, live)

    with closing(sqlite3.connect(f"file:{staging}?mode=ro", uri=True)) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM historic_grids").fetchone()[0] == 1


def test_only_data_with_no_link_into_the_rebuild_is_carried():
    """Hall of Fame and teams of the century hold `name_key` and link to
    players, and a rebuild reassigns player ids -- carrying those rows
    across would point them at the wrong people. They are reloaded from
    their own CSVs instead."""
    for tables in updates.CARRIED_TABLES.values():
        for forbidden in ("hall_of_fame", "team_selections", "players",
                          "games"):
            assert forbidden not in tables


def test_a_sport_with_nothing_to_carry_is_untouched(tmp_path):
    live = _grid_db(tmp_path / "nfl.db", [(1, "2026-08-09", "Gridley")])
    staging = tmp_path / "nfl.db.update-building"
    with closing(sqlite3.connect(staging)) as con:
        con.execute("CREATE TABLE games (player_id)")
        con.commit()

    assert updates._carry_forward("nfl", staging, live) == {}


def test_a_regular_rebuild_restores_the_layers_it_wipes():
    """A rebuild drops every table a separate loader owns. Leaving those
    to the awards event meant the Hall of Fame was missing from the live
    database from a regular update in August until Brownlow night."""
    labels = [step.label for sport, step in updates.plan("regular", ["afl"])
              if sport == "afl"]
    for expected in ("Load AFL Hall of Fame", "Load teams of the century",
                     "Load stat coverage"):
        assert expected in labels, expected
    assert labels.index("Fetch and rebuild AFL") < labels.index(
        "Load AFL Hall of Fame"), "a loader ran before the rebuild wiped it"


def test_nfl_club_history_is_projected_after_a_rebuild():
    """Without it the club pages read "Past games are not loaded"."""
    labels = [step.label for sport, step in updates.plan("regular", ["nfl"])]
    assert "Project NFL club history" in labels
    assert labels.index("Fetch and rebuild NFL") < labels.index(
        "Project NFL club history")
