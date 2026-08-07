"""Scheduled and administrator-triggered database refreshes.

The Streamlit process never writes a live sports database.  It starts this
module in a separate process; each sport's existing builder remains the
authority for fetching, validating and atomically replacing its database.

Ubuntu systemd timers call the same CLI as the Admin page. Annual jobs are
scheduled weekly, while the ``scheduled`` command applies a calendar guard so
only the intended post-event date performs work.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import data_paths

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs" / "database_updates"
STATUS_PATH = LOG_DIR / "status.json"
CHECK_STATUS_PATH = LOG_DIR / "check-status.json"
LOCK_PATH = LOG_DIR / "update.lock"
SPORT_KEYS = ("afl", "nba", "mlb", "nfl")
EVENTS = ("regular", "brownlow-awards", "grand-final-awards", "full")
KEEP_BACKUPS = 5
STARTING_LOCK_MAX_SECONDS = 120
RUNNING_LOCK_MAX_SECONDS = 7 * 24 * 60 * 60
DEFAULT_STEP_TIMEOUT_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class Step:
    label: str
    argv: tuple[str, ...]
    optional: bool = False


def last_saturday_in_september(year: int) -> dt.date:
    day = dt.date(year, 9, 30)
    return day - dt.timedelta(days=(day.weekday() - 5) % 7)


def brownlow_refresh_date(year: int) -> dt.date:
    """Tuesday after the Monday preceding the AFL Grand Final."""
    return last_saturday_in_september(year) - dt.timedelta(days=4)


def grand_final_refresh_date(year: int) -> dt.date:
    return last_saturday_in_september(year) + dt.timedelta(days=1)


def event_is_due(event: str, today: dt.date | None = None) -> bool:
    today = today or dt.datetime.now().astimezone().date()
    if event == "brownlow-awards":
        return today == brownlow_refresh_date(today.year)
    if event == "grand-final-awards":
        return today == grand_final_refresh_date(today.year)
    return True


def _python(*args: str) -> tuple[str, ...]:
    return (sys.executable, *args)


def _configured_command(name: str) -> tuple[str, ...] | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    return tuple(shlex.split(value, posix=os.name != "nt"))


def _build_steps(sport: str, db: str | None = None) -> list[Step]:
    db = db or data_paths.default_db(sport)
    if sport == "afl":
        return [Step("Fetch and rebuild AFL", _python(
            "-m", "afl.build_db", "--db", db, "--refresh"))]
    if sport == "nba":
        source = os.environ.get("SPORTS_DATA_NBA_SOURCE", "csv").strip().lower()
        if source not in {"csv", "bbr", "nba_api"}:
            raise ValueError("SPORTS_DATA_NBA_SOURCE must be csv, bbr, or nba_api")
        argv = list(_python(
            "-m", "nba.build_nba_db", "--db", db, "--source", source))
        source_root = os.environ.get("SPORTS_DATA_NBA_SOURCE_ROOT", "").strip()
        if source_root:
            argv.extend(("--source-root", source_root))
        if source == "nba_api":
            argv.append("--refresh")
        return [Step(f"Fetch and rebuild NBA ({source})", tuple(argv))]
    if sport == "mlb":
        return [Step(
            "Refresh Retrosheet and rebuild MLB",
            _python("-m", "mlb.build_mlb_db", "--db", db,
                    "--refresh-retrosheet"),
        )]
    if sport == "nfl":
        return [
            Step("Fetch and rebuild NFL", _python(
                "-m", "nfl.build_db", "--db", db,
                "--all-history", "--replace")),
            Step("Patch NFL application tables", _python(
                "-m", "utils.nfl.patch_nfl_db", "--db", db)),
        ]
    raise ValueError(f"unknown sport: {sport}")


def _award_steps(db: str | None = None,
                 refresh_official_history: bool = True) -> list[Step]:
    """Refresh sources whose automated use is supported, then load local CSVs.

    AFL Tables is deliberately absent: this repository documents that its
    automated-client restrictions prohibit direct scheduled scraping.  A
    separately authorised source command can be supplied by the operator;
    the conservative CSV loader then imports only the files it produced.
    """
    db = db or data_paths.default_db("afl")
    steps = []
    if refresh_official_history:
        steps.append(Step("Refresh official All-Australian history", _python(
            "-m", "utils.afl.load_all_australian_history", "--refresh",
            "--db", db), optional=True))
    steps.extend([
        Step("Refresh AFL captains", _python(
            "-m", "afl.scrape_afl_captains", "--refresh", "--load",
            "--db", db), optional=True),
        Step("Refresh AFL Hall of Fame", _python(
            "-m", "afl.scrape_hall_of_fame", "--refresh"), optional=True),
        Step("Load AFL Hall of Fame", _python(
            "-m", "utils.afl.load_hall_of_fame", "--db", db), optional=True),
        Step("Refresh teams of the century", _python(
            "-m", "afl.scrape_teams_of_the_century", "--refresh"), optional=True),
        Step("Load teams of the century", _python(
            "-m", "utils.afl.load_teams_of_the_century", "--db", db),
            optional=True),
    ])
    custom = _configured_command("SPORTS_DATA_AFL_AWARDS_FETCH_CMD")
    if custom:
        steps.append(Step("Fetch configured AFL awards source", custom))
    steps.extend([
        Step("Load Brownlow CSVs", _python(
            "-m", "utils.afl.load_brownlow", "--db", db, "--report"),
            optional=True),
        Step("Load Draftguru awards", _python(
            "-m", "utils.afl.load_draftguru", "--db", db), optional=True),
        Step("Link Draftguru draft rows", _python(
            "-m", "afl.link_draft", "--db", db), optional=True),
        Step("Link Draftguru people", _python(
            "-m", "afl.link_people", "--db", db), optional=True),
    ])
    return steps


def plan(event: str, sports: Iterable[str],
         db_paths: dict[str, str] | None = None) -> list[tuple[str, Step]]:
    chosen = tuple(dict.fromkeys(sports))
    unknown = set(chosen) - set(SPORT_KEYS)
    if unknown:
        raise ValueError(f"unknown sports: {', '.join(sorted(unknown))}")

    out: list[tuple[str, Step]] = []
    targets = db_paths or {}
    if event in {"regular", "full"}:
        for sport in chosen:
            out.extend((sport, step) for step in _build_steps(
                sport, targets.get(sport)))
    elif event == "grand-final-awards" and "afl" in chosen:
        # The Sunday job must add the Grand Final score before refreshing the
        # award layers.  Brownlow Tuesday does not need a second core rebuild.
        out.extend(("afl", step) for step in _build_steps(
            "afl", targets.get("afl")))
    if event in {"brownlow-awards", "grand-final-awards", "full"} and "afl" in chosen:
        out.extend(("afl", step) for step in _award_steps(
            targets.get("afl"), refresh_official_history=(
                event == "brownlow-awards")))

    # Fix/check/add is common and ordered after all source-specific work.
    for sport in chosen:
        db = targets.get(sport) or data_paths.default_db(sport)
        if sport == "afl":
            out.append((sport, Step("Repair AFL derived tables", _python(
                "-m", "utils.shared.repair_database", "--db", db))))
        out.append((sport, Step("Recompute obscurity", _python(
            "-m", "utils.shared.recompute_obscurity", "--sport", sport, "--db", db))))
        out.append((sport, Step("Optimise database", _python(
            "-m", "utils.shared.optimise_database", "--db", db, "--apply"), optional=True)))
        out.append((sport, Step("Strict database health check", _python(
            "health.py", "--sport", sport, "--db", db, "--strict"))))
    return out


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def read_status() -> dict:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def read_check_status() -> dict:
    try:
        return json.loads(CHECK_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update_is_active() -> bool:
    """Whether the cross-process update lock still belongs to live work."""
    return LOCK_PATH.exists() and _lock_is_active(_read_lock())


def database_file_status(sports: Iterable[str] = SPORT_KEYS) -> dict[str, dict]:
    """Return cheap live-file metadata without opening a database."""
    result = {}
    for sport in tuple(dict.fromkeys(sports)):
        path = Path(data_paths.default_db(sport))
        entry = {"path": str(path), "exists": path.exists()}
        if path.exists():
            stat = path.stat()
            entry.update({
                "bytes": stat.st_size,
                "modified_ns": stat.st_mtime_ns,
                "modified_at": dt.datetime.fromtimestamp(
                    stat.st_mtime, tz=dt.timezone.utc
                ).astimezone().isoformat(),
            })
        result[sport] = entry
    return result


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a harmless existence probe on Windows: the
        # CPython Windows implementation delegates ordinary signals to
        # TerminateProcess. Query the process handle instead.
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _lock_age_seconds(owner: dict) -> float | None:
    try:
        started = dt.datetime.fromisoformat(str(owner["started_at"]))
        if started.tzinfo is None:
            started = started.astimezone()
        return max(
            0.0,
            (dt.datetime.now().astimezone() - started).total_seconds(),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _lock_is_active(owner: dict) -> bool:
    age = _lock_age_seconds(owner)
    if owner.get("state") == "starting":
        return age is not None and age <= STARTING_LOCK_MAX_SECONDS
    try:
        pid = int(owner.get("pid", 0))
    except (TypeError, ValueError):
        return False
    return (
        age is not None
        and age <= RUNNING_LOCK_MAX_SECONDS
        and _pid_is_running(pid)
    )


def _read_lock() -> dict:
    try:
        value = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _acquire_lock() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        owner = _read_lock()
        reservation = os.environ.pop("SPORTS_DATA_UPDATE_RESERVATION", "")
        if reservation and secrets.compare_digest(
                reservation, str(owner.get("reservation", ""))):
            _write_json(LOCK_PATH, {
                "pid": os.getpid(),
                "started_at": dt.datetime.now().astimezone().isoformat(),
            })
            return
        if _lock_is_active(owner):
            if owner.get("state") == "starting":
                raise RuntimeError("database update is starting")
            raise RuntimeError(
                f"database update already running (PID {owner.get('pid')})")
        LOCK_PATH.unlink(missing_ok=True)
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError("database update already running") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid(), "started_at": dt.datetime.now().astimezone().isoformat()}, handle)


def _staging_paths(sports: Iterable[str]) -> dict[str, Path]:
    return {
        sport: Path(data_paths.default_db(sport)).with_suffix(
            Path(data_paths.default_db(sport)).suffix + ".update-building"
        )
        for sport in sports
    }


def _rebuild_sports(event: str, sports: Iterable[str]) -> set[str]:
    chosen = set(sports)
    if event in {"regular", "full"}:
        return chosen
    if event == "grand-final-awards" and "afl" in chosen:
        return {"afl"}
    return set()


def _prepare_staging(event: str, sports: Iterable[str],
                     paths: dict[str, Path]) -> None:
    rebuilt = _rebuild_sports(event, sports)
    for sport in sports:
        staging = paths[sport]
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.unlink(missing_ok=True)
        if sport not in rebuilt:
            live = Path(data_paths.default_db(sport))
            if not live.exists():
                raise RuntimeError(f"No live {sport.upper()} database at {live}")
            shutil.copy2(live, staging)


def _backup_and_promote(sport: str, staging: Path,
                        keep: int = KEEP_BACKUPS) -> str | None:
    """Atomically promote a validated staged database, retaining backups."""
    if not staging.exists():
        raise RuntimeError(f"staged {sport.upper()} database was not created")
    with closing(sqlite3.connect(
            f"file:{staging}?mode=ro", uri=True)) as con:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(
            f"staged {sport.upper()} database failed integrity check: {integrity}")

    live = Path(data_paths.default_db(sport))
    backup = None
    if live.exists() and keep > 0:
        folder = live.parent / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        backup = folder / f"{live.stem}-{stamp}{live.suffix}"
        shutil.copy2(live, backup)
        backups = sorted(
            folder.glob(f"{live.stem}-*{live.suffix}"),
            key=lambda item: item.stat().st_mtime_ns,
            reverse=True,
        )
        for stale in backups[keep:]:
            stale.unlink(missing_ok=True)
    os.replace(staging, live)
    return str(backup) if backup else None


def _database_snapshot(sport: str, *, quick: bool = False) -> dict:
    path = Path(data_paths.default_db(sport))
    result = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        return result
    stat = path.stat()
    result.update({
        "bytes": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
        "modified_at": dt.datetime.fromtimestamp(
            stat.st_mtime, tz=dt.timezone.utc
        ).astimezone().isoformat(),
    })
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as con:
            pragma = "quick_check" if quick else "integrity_check"
            result["integrity"] = con.execute(f"PRAGMA {pragma}").fetchone()[0]
            table_names = {
                row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            result["tables"] = len(table_names)
            if "players" in table_names:
                result["players"] = con.execute(
                    "SELECT COUNT(*) FROM players"
                ).fetchone()[0]
            if "games" in table_names:
                result["records"] = con.execute(
                    "SELECT COUNT(*) FROM games"
                ).fetchone()[0]
                columns = {
                    row[1] for row in con.execute("PRAGMA table_info(games)")
                }
                if "season" in columns:
                    lo, hi = con.execute(
                        "SELECT MIN(season), MAX(season) FROM games"
                    ).fetchone()
                    result.update({"season_min": lo, "season_max": hi})
    except sqlite3.Error as exc:
        result["integrity"] = f"error: {exc}"
    return result


def check_databases(sports: Iterable[str] = SPORT_KEYS) -> dict:
    """Inspect live databases without fetching sources or changing data."""
    chosen = tuple(dict.fromkeys(sports))
    unknown = set(chosen) - set(SPORT_KEYS)
    if unknown:
        raise ValueError(f"unknown sports: {', '.join(sorted(unknown))}")

    checked_at = dt.datetime.now().astimezone().isoformat()
    databases = {sport: _database_snapshot(sport, quick=True)
                 for sport in chosen}
    failures = [
        sport for sport, snapshot in databases.items()
        if not snapshot.get("exists") or snapshot.get("integrity") != "ok"
    ]
    result = {
        "state": "failed" if failures else "complete",
        "checked_at": checked_at,
        "sports": list(chosen),
        "databases": databases,
        "failures": failures,
        "mode": "read_only",
    }
    # Persist only the diagnostic report. The sports database files are
    # opened mode=ro and are never downloaded, rebuilt, or promoted here.
    _write_json(CHECK_STATUS_PATH, result)
    return result


def run_job(event: str, sports: Iterable[str], trigger: str = "cli",
            dry_run: bool = False) -> int:
    chosen = tuple(dict.fromkeys(sports))
    steps = plan(event, chosen)
    if dry_run:
        for sport, step in steps:
            print(f"[{sport}] {step.label}: {subprocess.list2cmdline(step.argv)}")
        return 0

    _acquire_lock()
    stamp = dt.datetime.now().astimezone()
    log_path = LOG_DIR / f"{stamp:%Y%m%d-%H%M%S}-{event}.log"
    staging = _staging_paths(chosen)
    status = {
        "state": "running", "event": event, "trigger": trigger,
        "sports": list(chosen), "pid": os.getpid(),
        "started_at": stamp.isoformat(), "log_path": str(log_path),
        "steps": [], "before": {s: _database_snapshot(s) for s in chosen},
    }
    _write_json(STATUS_PATH, status)
    required_failure = False
    optional_failure = False
    failed_sports = set()
    try:
        _prepare_staging(event, chosen, staging)
        steps = plan(
            event, chosen,
            {sport: str(path) for sport, path in staging.items()},
        )
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            for sport, step in steps:
                if sport in failed_sports:
                    status["steps"].append({
                        "sport": sport, "label": step.label,
                        "returncode": None, "optional": step.optional,
                        "seconds": 0, "state": "skipped",
                    })
                    _write_json(STATUS_PATH, status)
                    continue
                started = time.monotonic()
                log.write(f"\n[{dt.datetime.now().astimezone().isoformat()}] [{sport}] {step.label}\n")
                log.write(f"$ {subprocess.list2cmdline(step.argv)}\n")
                log.flush()
                try:
                    timeout = int(os.environ.get(
                        "SPORTS_DATA_UPDATE_STEP_TIMEOUT_SECONDS",
                        DEFAULT_STEP_TIMEOUT_SECONDS,
                    ))
                    if timeout <= 0:
                        raise ValueError
                except ValueError as exc:
                    raise RuntimeError(
                        "SPORTS_DATA_UPDATE_STEP_TIMEOUT_SECONDS must be a "
                        "positive integer"
                    ) from exc
                try:
                    completed = subprocess.run(
                        step.argv, cwd=ROOT, stdout=log,
                        stderr=subprocess.STDOUT, text=True, check=False,
                        timeout=timeout,
                    )
                    returncode = completed.returncode
                except subprocess.TimeoutExpired:
                    returncode = 124
                    log.write(
                        f"Step timed out after {timeout} seconds and was stopped.\n"
                    )
                    log.flush()
                record = {
                    "sport": sport, "label": step.label,
                    "returncode": returncode,
                    "optional": step.optional,
                    "seconds": round(time.monotonic() - started, 2),
                    "state": "complete" if returncode == 0 else "failed",
                }
                status["steps"].append(record)
                _write_json(STATUS_PATH, status)
                if returncode:
                    if step.optional:
                        optional_failure = True
                    else:
                        required_failure = True
                        failed_sports.add(sport)

        status["promotions"] = {}
        for sport in chosen:
            if sport in failed_sports:
                status["promotions"][sport] = {
                    "state": "retained_live",
                    "staging": str(staging[sport]),
                }
                continue
            try:
                backup = _backup_and_promote(sport, staging[sport])
            except (OSError, RuntimeError, sqlite3.Error) as exc:
                required_failure = True
                failed_sports.add(sport)
                status["promotions"][sport] = {
                    "state": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "staging": str(staging[sport]),
                }
            else:
                status["promotions"][sport] = {
                    "state": "promoted", "backup": backup,
                }
        status["after"] = {s: _database_snapshot(s) for s in chosen}
        status["state"] = (
            "failed" if required_failure else
            "complete_with_warnings" if optional_failure else "complete"
        )
        status["finished_at"] = dt.datetime.now().astimezone().isoformat()
        _write_json(STATUS_PATH, status)
        return 1 if required_failure else 0
    except Exception as exc:
        status.update({
            "state": "failed", "error": f"{type(exc).__name__}: {exc}",
            "finished_at": dt.datetime.now().astimezone().isoformat(),
        })
        _write_json(STATUS_PATH, status)
        raise
    finally:
        LOCK_PATH.unlink(missing_ok=True)


def start_background(event: str = "full", sports: Iterable[str] = SPORT_KEYS,
                     trigger: str = "admin") -> int:
    """Start an update without tying its lifetime to a Streamlit rerun."""
    chosen = tuple(dict.fromkeys(sports))
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        if _lock_is_active(_read_lock()):
            raise RuntimeError(
                "A database update is already running or starting.")
        LOCK_PATH.unlink(missing_ok=True)
    reservation = secrets.token_urlsafe(24)
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError("A database update is already running or starting.") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({
            "pid": os.getpid(), "state": "starting",
            "reservation": reservation,
            "started_at": dt.datetime.now().astimezone().isoformat(),
        }, handle)
    _write_json(STATUS_PATH, {
        "state": "starting", "event": event, "trigger": trigger,
        "sports": list(chosen), "pid": None,
        "started_at": dt.datetime.now().astimezone().isoformat(),
    })

    launcher_log = None
    try:
        launcher_log = (LOG_DIR / "launcher.log").open("a", encoding="utf-8")
        argv = [sys.executable, "-m", "database_updates", "run",
                "--event", event, "--sports", *chosen, "--trigger", trigger]
        child_env = os.environ.copy()
        child_env["SPORTS_DATA_UPDATE_RESERVATION"] = reservation
        kwargs = {
            "cwd": ROOT, "stdin": subprocess.DEVNULL,
            "stdout": launcher_log, "stderr": subprocess.STDOUT,
            "env": child_env,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(argv, **kwargs)
        return process.pid
    except Exception:
        LOCK_PATH.unlink(missing_ok=True)
        raise
    finally:
        if launcher_log is not None:
            launcher_log.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run an update in the foreground")
    run.add_argument("--event", choices=EVENTS, default="regular")
    run.add_argument("--sports", nargs="+", choices=SPORT_KEYS, default=list(SPORT_KEYS))
    run.add_argument("--trigger", default="cli")
    run.add_argument("--only-if-due", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    scheduled = sub.add_parser(
        "scheduled", help="run a guarded update from an operating-system timer")
    scheduled.add_argument(
        "event", choices=("regular", "brownlow-awards", "grand-final-awards"))
    sub.add_parser("status", help="print the last update status")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "status":
        print(json.dumps(read_status(), indent=2))
        return 0
    if args.command == "scheduled":
        if not event_is_due(args.event):
            print(
                f"{args.event} is not due on "
                f"{dt.datetime.now().astimezone().date().isoformat()}; skipped"
            )
            return 0
        sports = ("afl",) if args.event != "regular" else SPORT_KEYS
        return run_job(args.event, sports, trigger="systemd")
    if args.only_if_due and not event_is_due(args.event):
        today = dt.datetime.now().astimezone().date().isoformat()
        print(f"{args.event} is not due on {today}; skipped")
        return 0
    return run_job(args.event, args.sports, trigger=args.trigger, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
