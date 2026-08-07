"""Scheduled and administrator-triggered database refreshes.

The Streamlit process never writes a live sports database.  It starts this
module in a separate process; each sport's existing builder remains the
authority for fetching, validating and atomically replacing its database.

Windows Task Scheduler calls the same CLI as the Admin page.  Annual jobs are
scheduled weekly and use ``--only-if-due`` so calendar drift cannot cause a
second refresh.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import data_paths


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs" / "database_updates"
STATUS_PATH = LOG_DIR / "status.json"
LOCK_PATH = LOG_DIR / "update.lock"
SPORT_KEYS = ("afl", "nba", "mlb", "nfl")
EVENTS = ("regular", "brownlow-awards", "grand-final-awards", "full")


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
    today = today or dt.date.today()
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


def _build_steps(sport: str) -> list[Step]:
    if sport == "afl":
        return [Step("Fetch and rebuild AFL", _python("-m", "afl.build_db", "--refresh"))]
    if sport == "nba":
        source = os.environ.get("SPORTS_DATA_NBA_SOURCE", "csv").strip().lower()
        if source not in {"csv", "bbr", "nba_api"}:
            raise ValueError("SPORTS_DATA_NBA_SOURCE must be csv, bbr, or nba_api")
        argv = list(_python("-m", "nba.build_nba_db", "--source", source))
        source_root = os.environ.get("SPORTS_DATA_NBA_SOURCE_ROOT", "").strip()
        if source_root:
            argv.extend(("--source-root", source_root))
        if source == "nba_api":
            argv.append("--refresh")
        return [Step(f"Fetch and rebuild NBA ({source})", tuple(argv))]
    if sport == "mlb":
        return [Step(
            "Refresh Retrosheet and rebuild MLB",
            _python("-m", "mlb.build_mlb_db", "--refresh-retrosheet"),
        )]
    if sport == "nfl":
        return [
            Step("Fetch and rebuild NFL", _python(
                "-m", "nfl.build_db", "--all-history", "--replace")),
            Step("Patch NFL application tables", _python(
                "-m", "utils.nfl.patch_nfl_db")),
        ]
    raise ValueError(f"unknown sport: {sport}")


def _award_steps() -> list[Step]:
    """Refresh sources whose automated use is supported, then load local CSVs.

    AFL Tables is deliberately absent: this repository documents that its
    automated-client restrictions prohibit direct scheduled scraping.  A
    separately authorised source command can be supplied by the operator;
    the conservative CSV loader then imports only the files it produced.
    """
    steps = [
        Step("Refresh AFL captains", _python(
            "-m", "afl.scrape_afl_captains", "--refresh", "--load"), optional=True),
        Step("Refresh AFL Hall of Fame", _python(
            "-m", "afl.scrape_hall_of_fame", "--refresh"), optional=True),
        Step("Load AFL Hall of Fame", _python(
            "-m", "utils.afl.load_hall_of_fame"), optional=True),
        Step("Refresh teams of the century", _python(
            "-m", "afl.scrape_teams_of_the_century", "--refresh"), optional=True),
        Step("Load teams of the century", _python(
            "-m", "utils.afl.load_teams_of_the_century"), optional=True),
    ]
    custom = _configured_command("SPORTS_DATA_AFL_AWARDS_FETCH_CMD")
    if custom:
        steps.append(Step("Fetch configured AFL awards source", custom))
    steps.extend([
        Step("Load Brownlow CSVs", _python(
            "-m", "utils.afl.load_brownlow", "--report"), optional=True),
        Step("Load Draftguru awards", _python(
            "-m", "utils.afl.load_draftguru"), optional=True),
        Step("Link Draftguru draft rows", _python(
            "-m", "afl.link_draft"), optional=True),
        Step("Link Draftguru people", _python(
            "-m", "afl.link_people"), optional=True),
    ])
    return steps


def plan(event: str, sports: Iterable[str]) -> list[tuple[str, Step]]:
    chosen = tuple(dict.fromkeys(sports))
    unknown = set(chosen) - set(SPORT_KEYS)
    if unknown:
        raise ValueError(f"unknown sports: {', '.join(sorted(unknown))}")

    out: list[tuple[str, Step]] = []
    if event in {"regular", "full"}:
        for sport in chosen:
            out.extend((sport, step) for step in _build_steps(sport))
    elif event == "grand-final-awards" and "afl" in chosen:
        # The Sunday job must add the Grand Final score before refreshing the
        # award layers.  Brownlow Tuesday does not need a second core rebuild.
        out.extend(("afl", step) for step in _build_steps("afl"))
    if event in {"brownlow-awards", "grand-final-awards", "full"} and "afl" in chosen:
        out.extend(("afl", step) for step in _award_steps())

    # Fix/check/add is common and ordered after all source-specific work.
    for sport in chosen:
        db = data_paths.default_db(sport)
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


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _acquire_lock() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            owner = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
            if _pid_is_running(int(owner.get("pid", 0))):
                raise RuntimeError(f"database update already running (PID {owner['pid']})")
        except (OSError, ValueError, TypeError, KeyError):
            pass
        LOCK_PATH.unlink(missing_ok=True)
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError("database update already running") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid(), "started_at": dt.datetime.now().astimezone().isoformat()}, handle)


def _database_snapshot(sport: str) -> dict:
    path = Path(data_paths.default_db(sport))
    result = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        return result
    stat = path.stat()
    result.update({"bytes": stat.st_size, "modified_ns": stat.st_mtime_ns})
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
            result["integrity"] = con.execute("PRAGMA integrity_check").fetchone()[0]
            result["tables"] = con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    except sqlite3.Error as exc:
        result["integrity"] = f"error: {exc}"
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
    status = {
        "state": "running", "event": event, "trigger": trigger,
        "sports": list(chosen), "pid": os.getpid(),
        "started_at": stamp.isoformat(), "log_path": str(log_path),
        "steps": [], "before": {s: _database_snapshot(s) for s in chosen},
    }
    _write_json(STATUS_PATH, status)
    required_failure = False
    optional_failure = False
    try:
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            for sport, step in steps:
                started = time.monotonic()
                log.write(f"\n[{dt.datetime.now().astimezone().isoformat()}] [{sport}] {step.label}\n")
                log.write(f"$ {subprocess.list2cmdline(step.argv)}\n")
                log.flush()
                completed = subprocess.run(
                    step.argv, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                    text=True, check=False,
                )
                record = {
                    "sport": sport, "label": step.label,
                    "returncode": completed.returncode,
                    "optional": step.optional,
                    "seconds": round(time.monotonic() - started, 2),
                }
                status["steps"].append(record)
                _write_json(STATUS_PATH, status)
                if completed.returncode:
                    if step.optional:
                        optional_failure = True
                    else:
                        required_failure = True
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
    if LOCK_PATH.exists() and read_status().get("state") == "running":
        raise RuntimeError("A database update is already running.")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    launcher_log = (LOG_DIR / "launcher.log").open("a", encoding="utf-8")
    argv = [sys.executable, "-m", "database_updates", "run", "--event", event,
            "--sports", *sports, "--trigger", trigger]
    kwargs = {"cwd": ROOT, "stdin": subprocess.DEVNULL,
              "stdout": launcher_log, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(argv, **kwargs)
    launcher_log.close()
    return process.pid


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run an update in the foreground")
    run.add_argument("--event", choices=EVENTS, default="regular")
    run.add_argument("--sports", nargs="+", choices=SPORT_KEYS, default=list(SPORT_KEYS))
    run.add_argument("--trigger", default="cli")
    run.add_argument("--only-if-due", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="print the last update status")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "status":
        print(json.dumps(read_status(), indent=2))
        return 0
    if args.only_if_due and not event_is_due(args.event):
        print(f"{args.event} is not due on {dt.date.today().isoformat()}; skipped")
        return 0
    return run_job(args.event, args.sports, trigger=args.trigger, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
