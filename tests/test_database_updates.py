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


def _rising_star_db(path: Path, latest_round: int) -> None:
    with closing(sqlite3.connect(path)) as con:
        con.execute("CREATE TABLE IF NOT EXISTS players (player_id INTEGER)")
        con.execute("CREATE TABLE IF NOT EXISTS games (season INTEGER)")
        con.execute("DROP TABLE IF EXISTS rising_star_nominees")
        con.execute(
            "CREATE TABLE rising_star_nominees ("
            "season INTEGER, round_number INTEGER, player TEXT)")
        con.executemany(
            "INSERT INTO rising_star_nominees VALUES (2026, ?, 'Someone')",
            [(number,) for number in range(latest_round + 1)])
        con.commit()


def _round_db(path: Path, stored=(), games=()) -> None:
    with closing(sqlite3.connect(path)) as con:
        con.execute("CREATE TABLE players (player_id INTEGER)")
        con.execute(
            "CREATE TABLE games (season INTEGER, round TEXT, date TEXT)")
        con.executemany("INSERT INTO games VALUES (?,?,?)", games)
        con.execute(
            "CREATE TABLE manual_round_games (season INTEGER, round TEXT)")
        con.executemany("INSERT INTO manual_round_games VALUES (?,?)", stored)
        con.commit()


def test_the_latest_game_is_found_by_date_not_by_round_name(tmp_path,
                                                            monkeypatch):
    """`round` is TEXT because finals are named, so MAX(round) lies.

    Ordering lexically calls round 9 later than round 23, and the panel
    told the operator the database stopped nine rounds earlier than it did.
    """
    live = tmp_path / "afl.db"
    _round_db(live, games=[
        (2026, "9", "2026-05-17"),
        (2026, "23", "2026-08-09"),
        (2026, "EF", "2026-09-05"),
    ])
    monkeypatch.setattr(updates.data_paths, "default_db", lambda sport: str(live))

    summary = updates.manual_rounds(live)

    assert summary["latest_round"] == "EF"
    assert summary["latest_date"] == "2026-09-05"


def test_stored_rounds_report_whether_upstream_has_caught_up(tmp_path,
                                                             monkeypatch):
    """A stored round the rebuild now produces is redundant, not wrong."""
    live = tmp_path / "afl.db"
    _round_db(live, stored=[(2026, "23"), (2026, "24")],
              games=[(2026, "23", "2026-08-09")])
    monkeypatch.setattr(updates.data_paths, "default_db", lambda sport: str(live))

    summary = updates.manual_rounds(live)

    assert [(row["season"], row["round"], row["upstream_has"])
            for row in summary["rounds"]] == [
        (2026, "23", True), (2026, "24", False)]
    assert summary["redundant"] == 1


def test_an_upload_replaces_the_previous_attempt(tmp_path, monkeypatch):
    """A stale file left behind is not obviously stale.

    Files are paired to fixtures by the club names inside them, so a
    leftover file from a previous upload would be picked up as part of the
    round rather than ignored.
    """
    monkeypatch.setattr(updates, "MANUAL_ROUND_UPLOADS", tmp_path / "uploads")

    first = updates.upload_round_files(2026, "23", [("summary.csv", b"one")])
    assert (first / "summary.csv").read_bytes() == b"one"

    second = updates.upload_round_files(
        2026, "23", [("fixed.csv", b"two")])
    assert second == first
    assert not (second / "summary.csv").exists()
    assert (second / "fixed.csv").read_bytes() == b"two"


def test_an_upload_cannot_write_outside_its_own_folder(tmp_path, monkeypatch):
    """Upload names come from the browser and are not to be trusted."""
    monkeypatch.setattr(updates, "MANUAL_ROUND_UPLOADS", tmp_path / "uploads")

    folder = updates.upload_round_files(
        2026, "23", [("../../escaped.csv", b"x"), ("ok.csv", b"y")])

    assert sorted(item.name for item in folder.iterdir()) == [
        "escaped.csv", "ok.csv"]
    assert not (tmp_path / "escaped.csv").exists()


def _manual_round_paths(tmp_path, monkeypatch, live):
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(updates, "LOG_DIR", log_dir)
    monkeypatch.setattr(updates, "LOCK_PATH", log_dir / "update.lock")
    monkeypatch.setattr(
        updates, "MANUAL_ROUND_STATUS_PATH", log_dir / "manual-round.json")
    monkeypatch.setattr(updates.data_paths, "default_db", lambda sport: str(live))


def test_a_checked_round_writes_nothing_and_keeps_the_report(tmp_path,
                                                             monkeypatch):
    live = tmp_path / "afl.db"
    _round_db(live, games=[(2026, "23", "2026-08-09")])
    _manual_round_paths(tmp_path, monkeypatch, live)
    from utils.afl import load_round_csv

    seen = {}

    def fake_load(db_path, folder, season, round_name, dry_run=False, **kwargs):
        seen.update(db=str(db_path), dry_run=dry_run)
        print("  18 fixtures, 9 game files paired")
        return 0

    monkeypatch.setattr(load_round_csv, "load", fake_load)
    folder = tmp_path / "round"
    folder.mkdir()

    status = updates.run_manual_round_load(folder, 2026, "23", dry_run=True)

    assert status["state"] == "complete"
    assert status["promoted"] is False
    assert "9 game files paired" in status["report"]
    # A dry run reads the live database; it never stages a copy.
    assert seen["db"] == str(live)
    assert seen["dry_run"] is True
    assert not live.with_suffix(".db.manual-round-building").exists()


def test_a_round_the_loader_refuses_leaves_the_live_database_alone(
        tmp_path, monkeypatch):
    """A LoadError is an expected verdict on the files, not a crash.

    The operator needs the report more than the exception, so the run ends
    as a recorded failure carrying both rather than raising.
    """
    live = tmp_path / "afl.db"
    _round_db(live, games=[(2026, "23", "2026-08-09")])
    _manual_round_paths(tmp_path, monkeypatch, live)
    from utils.afl import load_round_csv

    def fake_load(db_path, folder, season, round_name, dry_run=False, **kwargs):
        print("  score checks: player goals disagree with quarter totals")
        raise load_round_csv.LoadError(
            "player stats disagree with the round summary")

    monkeypatch.setattr(load_round_csv, "load", fake_load)
    folder = tmp_path / "round"
    folder.mkdir()
    before = live.read_bytes()

    status = updates.run_manual_round_load(folder, 2026, "23")

    assert status["state"] == "failed"
    assert "disagree" in status["error"]
    assert "score checks" in status["report"]
    assert live.read_bytes() == before
    assert not live.with_suffix(".db.manual-round-building").exists()
    assert not updates.LOCK_PATH.exists()


def test_a_loaded_round_is_staged_then_promoted(tmp_path, monkeypatch):
    live = tmp_path / "afl.db"
    _round_db(live, games=[(2026, "22", "2026-08-02")])
    _manual_round_paths(tmp_path, monkeypatch, live)
    from utils.afl import load_round_csv

    def fake_load(db_path, folder, season, round_name, dry_run=False, **kwargs):
        assert str(db_path).endswith(".manual-round-building")
        with closing(sqlite3.connect(db_path)) as con:
            con.execute("INSERT INTO games VALUES (2026, '23', '2026-08-09')")
            con.commit()
        print("  round written")
        return 0

    monkeypatch.setattr(load_round_csv, "load", fake_load)
    folder = tmp_path / "round"
    folder.mkdir()

    status = updates.run_manual_round_load(folder, 2026, "23")

    assert status["state"] == "complete"
    assert status["promoted"] is True
    with closing(sqlite3.connect(live)) as con:
        assert con.execute(
            "SELECT round FROM games ORDER BY date DESC LIMIT 1"
        ).fetchone() == ("23",)
    assert not updates.LOCK_PATH.exists()


def _currency_db(path: Path, season: int, latest_round: int) -> None:
    with closing(sqlite3.connect(path)) as con:
        con.execute(
            "CREATE TABLE rising_star_nominees ("
            "season INTEGER, round_number INTEGER, player TEXT, club TEXT, "
            "source TEXT, match_status TEXT)")
        con.executemany(
            "INSERT INTO rising_star_nominees VALUES (?,?,?,?,?,'unique')",
            [(season, number, f"Player {number}", "Geelong", "footywire")
             for number in range(latest_round)]
            + [(season, latest_round, "Jesse Dattoli", "Sydney", "wikipedia")])
        con.commit()


def test_currency_names_the_newest_nomination_and_where_it_came_from(
        tmp_path, monkeypatch):
    live = tmp_path / "afl.db"
    _currency_db(live, 2026, latest_round=22)
    monkeypatch.setattr(updates, "read_rising_star_status", lambda: {
        "state": "complete", "finished_at": "2026-08-10T08:00:00+10:00"})

    currency = updates.rising_star_currency(live, today=dt.date(2026, 8, 11))

    assert currency["state"] == "loaded"
    assert currency["season"] == 2026
    assert currency["latest_round"] == 22
    assert currency["latest_player"] == "Jesse Dattoli"
    assert currency["latest_source"] == "wikipedia"
    assert currency["latest_linked"] is True
    assert currency["season_nominations"] == 23
    assert currency["sources"] == {"footywire": 22, "wikipedia": 1}
    assert currency["days_since_check"] == 1
    assert currency["stale"] is False


def test_a_season_underway_and_long_unchecked_is_flagged_as_behind(
        tmp_path, monkeypatch):
    live = tmp_path / "afl.db"
    _currency_db(live, 2026, latest_round=15)
    monkeypatch.setattr(updates, "read_rising_star_status", lambda: {
        "state": "complete", "finished_at": "2026-07-01T08:00:00+10:00"})

    currency = updates.rising_star_currency(live, today=dt.date(2026, 8, 11))

    assert currency["in_season"] is True
    assert currency["days_since_check"] == 41
    assert currency["stale"] is True


def test_a_source_never_checked_during_the_season_is_flagged(tmp_path,
                                                             monkeypatch):
    live = tmp_path / "afl.db"
    _currency_db(live, 2026, latest_round=15)
    monkeypatch.setattr(updates, "read_rising_star_status", lambda: {})

    currency = updates.rising_star_currency(live, today=dt.date(2026, 8, 11))

    assert currency["days_since_check"] is None
    assert currency["stale"] is True


def test_a_completed_season_is_never_called_behind(tmp_path, monkeypatch):
    """No nomination is due between the Grand Final and next March.

    Warning every off-season day would train the reader to ignore the
    warning by the time it means something.
    """
    live = tmp_path / "afl.db"
    _currency_db(live, 2026, latest_round=24)
    monkeypatch.setattr(updates, "read_rising_star_status", lambda: {
        "state": "complete", "finished_at": "2026-09-21T08:00:00+10:00"})

    november = updates.rising_star_currency(live, today=dt.date(2026, 11, 30))
    assert november["in_season"] is False
    assert november["stale"] is False

    # And the following February, with the 2027 article not yet created.
    february = updates.rising_star_currency(live, today=dt.date(2027, 2, 1))
    assert february["stale"] is False


def test_currency_reports_plainly_when_nothing_is_loaded(tmp_path,
                                                         monkeypatch):
    monkeypatch.setattr(updates, "read_rising_star_status", lambda: {})
    empty = tmp_path / "afl.db"
    with closing(sqlite3.connect(empty)) as con:
        con.execute("CREATE TABLE players (player_id INTEGER)")
        con.commit()

    assert updates.rising_star_currency(
        empty, today=dt.date(2026, 8, 11))["state"] == "not loaded"
    assert updates.rising_star_currency(
        tmp_path / "missing.db", today=dt.date(2026, 8, 11)
    )["state"] == "not loaded"


def _rising_star_scan(tmp_path, monkeypatch, *, live, result,
                      writes_round=22):
    """Run a scan with the fetch and the reload replaced by fakes.

    The fake reload stands in for load_rising_star: it rewrites the
    nominations table of whatever database it is handed, so a test can see
    which file the reload actually targeted.
    """
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(updates, "LOG_DIR", log_dir)
    monkeypatch.setattr(updates, "LOCK_PATH", log_dir / "update.lock")
    monkeypatch.setattr(
        updates, "RISING_STAR_STATUS_PATH", log_dir / "rising-star.json")
    monkeypatch.setattr(updates.data_paths, "default_db",
                        lambda sport: str(live))

    from utils.afl import load_rising_star

    reloads = []

    def fake_load_sources(db, sources, verbose=True, **kwargs):
        reloads.append(str(db))
        _rising_star_db(Path(db), writes_round)
        return {"rows": 1, "trusted": 1}

    monkeypatch.setattr(load_rising_star, "load_sources", fake_load_sources)
    monkeypatch.setattr(load_rising_star, "default_sources", lambda: [])
    status = updates.run_rising_star_scan(
        season=2026, fetcher=lambda season: result)
    return status, reloads


def test_a_week_with_no_new_nomination_promotes_nothing(tmp_path, monkeypatch):
    """The common case: 51 weeks a year this must be a no-op."""
    live = tmp_path / "afl.db"
    _rising_star_db(live, latest_round=22)
    before = live.stat().st_mtime_ns

    status, reloads = _rising_star_scan(
        tmp_path, monkeypatch, live=live,
        result={"season": 2026, "changed": False, "added": 0,
                "latest_round": 22},
    )

    assert status["state"] == "complete"
    assert status["promoted"] is False
    assert reloads == []
    assert live.stat().st_mtime_ns == before
    assert not updates.LOCK_PATH.exists()


def test_a_new_nomination_is_loaded_and_promoted(tmp_path, monkeypatch):
    live = tmp_path / "afl.db"
    _rising_star_db(live, latest_round=21)

    status, reloads = _rising_star_scan(
        tmp_path, monkeypatch, live=live,
        result={"season": 2026, "changed": True, "added": 1,
                "latest_round": 22,
                "new_nominations": [{"round": 22, "player": "Jesse Dattoli",
                                     "club": "Sydney"}]},
    )

    assert status["state"] == "complete"
    assert status["promoted"] is True
    # The reload ran against staging, never against the live file.
    assert reloads and reloads[0].endswith(".rising-star-building")
    assert not live.with_suffix(".db.rising-star-building").exists()
    with closing(sqlite3.connect(live)) as con:
        assert con.execute(
            "SELECT MAX(round_number) FROM rising_star_nominees"
        ).fetchone()[0] == 22


def test_an_unchanged_file_still_reloads_when_the_database_is_behind(
        tmp_path, monkeypatch):
    """A run that wrote the CSV and then failed to load it must self-heal.

    Otherwise the source file says round 22 and the database stops at 21
    for as long as nobody looks, because every later week sees an
    unchanged file and concludes there is nothing to do.
    """
    live = tmp_path / "afl.db"
    _rising_star_db(live, latest_round=21)

    status, reloads = _rising_star_scan(
        tmp_path, monkeypatch, live=live,
        result={"season": 2026, "changed": False, "added": 0,
                "latest_round": 22},
    )

    assert status["promoted"] is True
    assert len(reloads) == 1


def test_a_forced_reload_publishes_work_the_season_check_cannot_see(
        tmp_path, monkeypatch):
    """Backfilling earlier seasons changes rows this season's check ignores.

    Without a way to say "reload anyway", the only route to publishing a
    backfill or a loader fix would be a full AFL rebuild.
    """
    live = tmp_path / "afl.db"
    _rising_star_db(live, latest_round=22)

    log_dir = tmp_path / "logs"
    monkeypatch.setattr(updates, "LOG_DIR", log_dir)
    monkeypatch.setattr(updates, "LOCK_PATH", log_dir / "update.lock")
    monkeypatch.setattr(
        updates, "RISING_STAR_STATUS_PATH", log_dir / "rising-star.json")
    monkeypatch.setattr(updates.data_paths, "default_db",
                        lambda sport: str(live))
    from utils.afl import load_rising_star

    reloads = []

    def fake_load_sources(db, sources, verbose=True, **kwargs):
        reloads.append(str(db))
        _rising_star_db(Path(db), 22)
        return {"rows": 1, "trusted": 1}

    monkeypatch.setattr(load_rising_star, "load_sources", fake_load_sources)
    monkeypatch.setattr(load_rising_star, "default_sources", lambda: [])
    unchanged = {"season": 2026, "changed": False, "latest_round": 22}

    quiet = updates.run_rising_star_scan(
        season=2026, fetcher=lambda season: unchanged)
    assert quiet["promoted"] is False
    assert reloads == []

    forced = updates.run_rising_star_scan(
        season=2026, fetcher=lambda season: unchanged, force=True)
    assert forced["promoted"] is True
    assert len(reloads) == 1


def test_a_season_with_no_article_yet_is_not_a_failed_run(tmp_path,
                                                          monkeypatch):
    """In February the next season's article has usually not been created."""
    from afl import fetch_wikipedia_rising_star as wiki

    live = tmp_path / "afl.db"
    _rising_star_db(live, latest_round=22)

    def missing(season):
        raise wiki.PageNotFound(f"{season}_AFL_Rising_Star")

    log_dir = tmp_path / "logs"
    monkeypatch.setattr(updates, "LOG_DIR", log_dir)
    monkeypatch.setattr(updates, "LOCK_PATH", log_dir / "update.lock")
    monkeypatch.setattr(
        updates, "RISING_STAR_STATUS_PATH", log_dir / "rising-star.json")
    monkeypatch.setattr(updates.data_paths, "default_db",
                        lambda sport: str(live))

    status = updates.run_rising_star_scan(season=2027, fetcher=missing)
    assert status["state"] == "complete"
    assert status["promoted"] is False
    assert not updates.LOCK_PATH.exists()


def test_a_reload_that_links_nobody_leaves_the_live_database_alone(
        tmp_path, monkeypatch):
    live = tmp_path / "afl.db"
    _rising_star_db(live, latest_round=21)
    from utils.afl import load_rising_star

    log_dir = tmp_path / "logs"
    monkeypatch.setattr(updates, "LOG_DIR", log_dir)
    monkeypatch.setattr(updates, "LOCK_PATH", log_dir / "update.lock")
    monkeypatch.setattr(
        updates, "RISING_STAR_STATUS_PATH", log_dir / "rising-star.json")
    monkeypatch.setattr(updates.data_paths, "default_db",
                        lambda sport: str(live))
    monkeypatch.setattr(load_rising_star, "default_sources", lambda: [])
    monkeypatch.setattr(
        load_rising_star, "load_sources",
        lambda db, sources, verbose=True, **kwargs: {"rows": 0, "trusted": 0})

    with pytest.raises(RuntimeError, match="linked no rows"):
        updates.run_rising_star_scan(
            season=2026,
            fetcher=lambda season: {"season": season, "changed": True,
                                    "latest_round": 22},
        )
    with closing(sqlite3.connect(live)) as con:
        assert con.execute(
            "SELECT MAX(round_number) FROM rising_star_nominees"
        ).fetchone()[0] == 21
    assert not updates.LOCK_PATH.exists()
    assert not live.with_suffix(".db.rising-star-building").exists()


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


# --------------------------------------------------- round-name allowlist
#
# `round_name` becomes a directory name that upload_round_files rmtree's,
# so it is allowlisted -- a round number or a finals code -- and the
# resolved folder must sit directly under the uploads root. Sanitising
# instead of rejecting is exactly how the traversal existed.

@pytest.mark.parametrize("hostile", [
    "../../etc", r"..\..\boot", "23/../..", "..", ".", "",
    "GF; rm -rf /", "23|x", "R23", "EF/../GF", "23\x00", "GRAND FINAL",
])
def test_a_round_name_off_the_allowlist_is_rejected(hostile, tmp_path,
                                                    monkeypatch):
    monkeypatch.setattr(updates, "MANUAL_ROUND_UPLOADS", tmp_path / "uploads")
    with pytest.raises(ValueError):
        updates.upload_round_files(2026, hostile, [("summary.csv", b"x")])
    assert not (tmp_path / "uploads").exists() or not any(
        (tmp_path / "uploads").iterdir()), "a rejected name touched the disk"


@pytest.mark.parametrize("fine", ["23", "1", "EF", "QF", "SF", "PF", "GF",
                                  "gf", " 23 "])
def test_allowlisted_round_names_land_directly_under_the_uploads_root(
        fine, tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    monkeypatch.setattr(updates, "MANUAL_ROUND_UPLOADS", root)
    folder = updates.upload_round_files(2026, fine, [("summary.csv", b"x")])
    assert folder.parent == root.resolve()
    assert folder.name.startswith("2026-")


def test_an_out_of_range_season_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "MANUAL_ROUND_UPLOADS", tmp_path / "uploads")
    for season in (1896, 2999):
        with pytest.raises(ValueError):
            updates.upload_round_files(season, "23", [("summary.csv", b"x")])


def test_an_upload_name_that_reduces_to_no_leaf_is_rejected(tmp_path,
                                                            monkeypatch):
    monkeypatch.setattr(updates, "MANUAL_ROUND_UPLOADS", tmp_path / "uploads")
    with pytest.raises(ValueError):
        updates.upload_round_files(2026, "23", [("..", b"x")])


# ------------------------------------------------ staging pre-seed
#
# The incident: a staged AFL rebuild starts from a fresh file, so
# manual_round_games -- the hand-entered round store the builder replays
# at the end of every rebuild -- was absent, the replay was a no-op, and
# the strict health check failed the build on the lost round's players.
# The store must ride *into* the fresh staging file before the builder
# runs, so the builder's own replay step can see it.

def _live_with_durables(path):
    with closing(sqlite3.connect(path)) as con:
        con.execute("CREATE TABLE manual_round_games (season INTEGER, "
                    "round TEXT, source_name TEXT, player_id INTEGER, "
                    "PRIMARY KEY (season, round, source_name))")
        con.execute("INSERT INTO manual_round_games VALUES "
                    "(2026, '23', 'Dattoli, Jesse', 13260)")
        con.execute("CREATE TABLE historic_grids (grid_id TEXT PRIMARY KEY)")
        con.execute("INSERT INTO historic_grids VALUES ('board-1')")
        con.execute("CREATE TABLE games (season INTEGER)")
        con.commit()
    return path


def test_a_rebuild_staging_is_seeded_with_the_manual_round_store(
        tmp_path, monkeypatch):
    live = _live_with_durables(tmp_path / "afl.db")
    stage = tmp_path / "afl.db.update-building"
    monkeypatch.setattr(updates.data_paths, "default_db",
                        lambda sport: str(live))

    updates._prepare_staging("regular", ["afl"], {"afl": stage})

    with closing(sqlite3.connect(stage)) as con:
        stored = con.execute(
            "SELECT season, round, player_id FROM manual_round_games"
        ).fetchall()
        grids = con.execute("SELECT grid_id FROM historic_grids").fetchall()
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert stored == [(2026, "23", 13260)], "the round store did not ride in"
    assert grids == [("board-1",)]
    assert "games" not in tables, \
        "only the durable tables may seed a fresh rebuild"


def test_a_first_ever_build_with_no_live_database_stays_clean(
        tmp_path, monkeypatch):
    live = tmp_path / "afl.db"          # never created
    stage = tmp_path / "afl.db.update-building"
    monkeypatch.setattr(updates.data_paths, "default_db",
                        lambda sport: str(live))

    updates._prepare_staging("regular", ["afl"], {"afl": stage})

    assert not stage.exists(), \
        "seeding must not conjure a staging file from a missing live one"
