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
GRIDLEY_SCAN_STATUS_PATH = LOG_DIR / "gridley-scan-status.json"
RISING_STAR_STATUS_PATH = LOG_DIR / "rising-star-status.json"
LOCK_PATH = LOG_DIR / "update.lock"
SPORT_KEYS = ("afl", "nba", "mlb", "nfl")
EVENTS = ("regular", "brownlow-awards", "grand-final-awards", "full")
KEEP_BACKUPS = 5

#: How long a sport may go with no new game before its data is behind
#: rather than simply between seasons. Each value is that sport's real
#: off-season plus a margin, so only a feed that has actually stopped
#: trips it: AFL runs March-September, the NBA October-June, the NFL
#: September-February.
STALE_AFTER_DAYS = {"afl": 200, "nba": 170, "nfl": 250}

#: Sports whose source publishes a season at a time rather than a game at
#: a time, measured in seasons behind instead of days.
#:
#: MLB is the whole reason this exists. Its Lahman import stamps every game
#: in a season YYYY-04-01 -- the 2025 season's 2,201 games all share the
#: date 2025-04-01 -- so a days-since-last-game reading would call a
#: correctly loaded database sixteen months stale. One season behind is
#: also normal, because Lahman publishes only after a season finishes.
STALE_AFTER_SEASONS = {"mlb": 2}
STARTING_LOCK_MAX_SECONDS = 120
RUNNING_LOCK_MAX_SECONDS = 7 * 24 * 60 * 60
DEFAULT_STEP_TIMEOUT_SECONDS = 6 * 60 * 60

#: How long a promotion waits for readers to finish a query before giving
#: up. Generous: the alternative to waiting is discarding a rebuild that
#: has already run to completion.
PROMOTE_TIMEOUT_SECONDS = 60


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
    # `rising-star` and `gridley` deliberately have no calendar guard. Both
    # promote only when their source actually changed, so running one on
    # the wrong day costs a request and does nothing -- while a day-of-week
    # guard would refuse the catch-up run that `Persistent=true` schedules
    # after the server was down on the intended day, which is the one run
    # that most needs to happen.
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
        # The three loaders after the rebuild are not awards work, even
        # though _award_steps runs two of them too. `afl.build_db` writes
        # a database from nothing, so every layer a separate loader owns
        # is absent from it -- and a regular update promoting that build
        # took the Hall of Fame, the teams of the century and the stat
        # coverage notes out of the live database until the next awards
        # run, which is in September. All three read checked-in CSVs, need
        # no network, and take about a second.
        #
        # Rebuilding them rather than carrying the old rows across is
        # deliberate: they hold `name_key` and link to players, and the
        # rebuild reassigns player ids. Stale rows would point at the
        # wrong people. See CARRIED_TABLES for the data that cannot be
        # rebuilt and so must be carried.
        return [
            Step("Fetch and rebuild AFL", _python(
                "-m", "afl.build_db", "--db", db, "--refresh")),
            Step("Load AFL Hall of Fame", _python(
                "-m", "utils.afl.load_hall_of_fame", "--db", db),
                optional=True),
            Step("Load teams of the century", _python(
                "-m", "utils.afl.load_teams_of_the_century", "--db", db),
                optional=True),
            Step("Load stat coverage", _python(
                "-m", "utils.shared.load_stat_coverage", "--sport", "afl",
                "--db", db), optional=True),
        ]
    if sport == "nba":
        # nba.build_nba_db --source live can build a full history, but
        # NBA.com's own game log reports the same numeric team_id for a
        # franchise across its entire history -- it has no notion that the
        # Thunder were the SuperSonics in 1985. A full rebuild from
        # --source live therefore cannot produce an historically-accurate
        # club_hist for any relocated or renamed franchise: every game,
        # however old, gets stamped with the modern name. See
        # utils/nba/load_current_season.py's module docstring for the
        # incident this caused.
        #
        # So NBA gets MLB's treatment: the full history comes from
        # --source csv (or a real scrape), built once, by hand -- see
        # _rebuild_sports, which excludes nba the same way it already
        # excludes mlb. This step only ever appends the season(s) still in
        # progress, from NBA.com's live game log, and never touches an
        # already-loaded season -- so it never has the live source's
        # historical-identity problem to begin with.
        return [
            Step("Load the current NBA season(s)", _python(
                "-m", "utils.nba.load_current_season", "--db", db)),
            _load_arenas_step("nba", db),
        ]
    if sport == "mlb":
        # The Lahman CSVs built this database once and are not read again.
        # Lahman publishes a season only after it ends, so the rebuild
        # this used to run could never produce a game the database did not
        # already have -- MLB sat a season behind from April to November
        # with no way to close the gap. The season in progress now comes
        # from MLB's own Stats API and is appended in place.
        #
        # Retrosheet is not refreshed here. Its loader takes no --db, so
        # in a staged update it would write to the live database instead
        # of the copy, and its game-log URL is pinned to gl1871_2025.zip
        # in any case -- it cannot yield a 2026 game to add.
        return [
            Step("Load the current MLB season from the Stats API",
                 _python("-m", "utils.mlb.load_statsapi", "--db", db)),
            _load_arenas_step("mlb", db),
        ]
    if sport == "nfl":
        return [
            Step("Fetch and rebuild NFL", _python(
                "-m", "nfl.build_db", "--db", db,
                "--all-history", "--replace")),
            # --no-reference: this step runs against a staging file beside
            # the live database -- see _refresh_reference, which redoes the
            # reference write against the live path once promotion (not
            # this step) actually succeeds. Same reasoning as NBA's
            # --no-reference above.
            Step("Patch NFL application tables", _python(
                "-m", "utils.nfl.patch_nfl_db", "--db", db,
                "--no-reference")),
            # Without this the club pages read "Past games are not
            # loaded". `nfl.build_db --replace` writes the schedule but
            # not the club-history rows projected from it, so a rebuild
            # left every club without a game list.
            Step("Project NFL club history", _python(
                "-m", "utils.nfl.load_club_history", "--db", db),
                optional=True),
            _load_arenas_step("nfl", db),
        ]
    raise ValueError(f"unknown sport: {sport}")


def _load_arenas_step(sport: str, db: str) -> Step:
    """Reload the Wikipedia arena/stadium reference tables.

    `mlb.build_mlb_db`, `nba.build_nba_db` and `nfl.build_db` know nothing
    of `arenas`/`arena_teams` -- a database they write (or the copy MLB's
    incremental step runs against) simply lacks them, silently dropping
    the table on the next promotion. Reading `data/arena/**/master_*.csv`
    back in takes under a second and needs no network, the same shape as
    the AFL Hall of Fame/teams-of-century loaders below.
    """
    return Step("Load arena reference data", _python(
        "-m", "utils.shared.load_arenas", "--sport", sport, "--db", db),
        optional=True)


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


def _write_json(path: Path, payload: dict, *, best_effort: bool = False) -> None:
    """Write ``payload`` to ``path`` atomically.

    Windows can hand back a transient ``PermissionError`` (WinError 5) when
    something else -- an antivirus scan, the Admin page's own
    ``read_status()`` -- has the destination open at the exact instant of
    ``os.replace``. A short retry rides that out. ``best_effort=True`` is for
    progress-reporting writes: one of those failing must never take down a
    rebuild that has real, possibly hours of, promotable work in progress --
    see run_job, where every step and the final promotion both write through
    here inside the same try block that would otherwise abort the whole job.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    attempts = 5
    for attempt in range(attempts):
        try:
            os.replace(temporary, path)
            return
        except OSError:
            if attempt == attempts - 1:
                if not best_effort:
                    raise
                print(f"warning: could not update {path} (left locked by "
                      "another process); continuing without this status "
                      "update", file=sys.stderr)
                temporary.unlink(missing_ok=True)
                return
            time.sleep(0.2 * (attempt + 1))


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


def read_gridley_scan_status() -> dict:
    try:
        return json.loads(GRIDLEY_SCAN_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def read_rising_star_status() -> dict:
    try:
        return json.loads(RISING_STAR_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update_is_active() -> bool:
    """Whether the cross-process update lock still belongs to live work."""
    return LOCK_PATH.exists() and _lock_is_active(_read_lock())


def database_file_status(sports: Iterable[str] = SPORT_KEYS,
                         *, with_freshness: bool = True) -> dict[str, dict]:
    """Live-file metadata, plus how current the data inside actually is.

    The file's timestamp alone answers the wrong question -- it records
    when the database was last replaced, not how recent the games in it
    are -- so the freshness probe rides along. It is two indexed
    aggregates per sport, about half a second for all four.
    """
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
            if with_freshness:
                try:
                    with closing(sqlite3.connect(
                            f"file:{path}?mode=ro", uri=True)) as con:
                        tables = {row[0] for row in con.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='table'")}
                        if "games" in tables:
                            columns = {row[1] for row in con.execute(
                                "PRAGMA table_info(games)")}
                            entry["freshness"] = _freshness(
                                sport, con, columns)
                except sqlite3.Error as exc:
                    entry["freshness"] = {
                        "state": "unknown", "summary": f"unreadable: {exc}"}
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


def _retained_staging_path(staging: Path) -> Path:
    """Resolve a builder's actual diagnostic file after a failed build.

    Most builders write directly to our staging path. The NBA builder adds
    its own atomic ``.building`` suffix and reports that path beside it; on
    failure the old UI linked to a staging file that never existed.
    """
    if staging.exists():
        return staging
    report = Path(str(staging) + ".build-report.json")
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
        working = Path(str(payload.get("working_db", "")))
        if working.exists():
            return working
    except (OSError, ValueError, TypeError):
        pass
    building = Path(str(staging) + ".building")
    return building if building.exists() else staging


def _rebuild_sports(event: str, sports: Iterable[str]) -> set[str]:
    chosen = set(sports)
    if event in {"regular", "full"}:
        # Neither ever rebuilds from scratch through this pipeline: each
        # has a static historical base (MLB's Lahman import, NBA's --source
        # csv/bbr) built once by hand, and every automated run only appends
        # the current season to whatever staging starts as a copy of.
        return chosen - {"mlb", "nba"}
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


def _promote_in_place(staging: Path, live: Path) -> None:
    """Copy a staged database over a live one that is still open.

    SQLite's own backup API, so the write is a transaction the readers
    already attached to the live file take part in, rather than a
    filesystem operation they can veto.
    """
    with closing(sqlite3.connect(
            f"file:{staging}?mode=ro", uri=True)) as source, \
            closing(sqlite3.connect(
                live, timeout=PROMOTE_TIMEOUT_SECONDS)) as target:
        target.execute(
            f"PRAGMA busy_timeout = {int(PROMOTE_TIMEOUT_SECONDS * 1000)}")
        source.backup(target)


#: Tables a rebuild cannot reproduce, and so must never destroy.
#:
#: `historic_grids` is captured, not derived. The Gridley feed serves
#: recent boards only, so a board scraped on the day it was published is
#: the only copy there will ever be -- and `afl.build_db` writes a
#: database from nothing, which does not include it. Promoting that build
#: silently emptied the captured library: it went from thirteen boards to
#: whatever the next morning's scan happened to find.
#:
#: Only for data with no link into the rebuilt tables. Anything carrying a
#: player id or `name_key` must be rebuilt by its own loader instead --
#: see the AFL branch of _build_steps -- because a rebuild reassigns those
#: ids and carried rows would point at the wrong people.
CARRIED_TABLES: dict[str, tuple[str, ...]] = {"afl": ("historic_grids",)}


def _table_columns(con, table: str, schema: str = "main") -> list[str]:
    return [row[1] for row in
            con.execute(f"PRAGMA {schema}.table_info({table})")]


def _carry_forward(sport: str, staging: Path, live: Path) -> dict[str, int]:
    """Copy the tables a rebuild cannot reproduce into the staged database.

    Runs before promotion, so what the health check validated is what is
    promoted plus rows the build never claimed to own.
    """
    tables = CARRIED_TABLES.get(sport, ())
    if not tables or not live.exists():
        return {}
    carried: dict[str, int] = {}
    with closing(sqlite3.connect(staging)) as con:
        con.execute("ATTACH DATABASE ? AS live", (str(live),))
        try:
            for table in tables:
                created = con.execute(
                    "SELECT sql FROM live.sqlite_master WHERE type='table' "
                    "AND name=?", (table,)).fetchone()
                if not created or not created[0]:
                    continue
                if not con.execute(
                        "SELECT 1 FROM main.sqlite_master WHERE type='table' "
                        "AND name=?", (table,)).fetchone():
                    con.execute(created[0])
                # Column-wise rather than SELECT *: if a later build ever
                # does write this table, the two shapes need not agree.
                shared = [column for column
                          in _table_columns(con, table, "live")
                          if column in _table_columns(con, table)]
                if not shared:
                    continue
                names = ", ".join(f'"{column}"' for column in shared)
                con.execute(
                    f"INSERT OR REPLACE INTO main.{table} ({names}) "
                    f"SELECT {names} FROM live.{table}")
                carried[table] = con.execute(
                    f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]
            con.commit()
        finally:
            con.execute("DETACH DATABASE live")
    return carried


#: Sports whose builder writes a measured reference sidecar (franchise
#: lineage, current team list, stat eras) beside its own database, keyed to
#: the CLI arguments that recompute it against an already-built database
#: without touching the database itself.
_REFERENCE_REFRESH: dict[str, tuple[str, ...]] = {
    "nba": ("-m", "nba.build_nba_db", "--reference-only"),
    "nfl": ("-m", "utils.nfl.patch_nfl_db", "--reference-only"),
}


def _refresh_reference(sport: str) -> None:
    """Recompute a promoted sport's reference sidecar against the live db.

    The build step for this sport ran with --no-reference, against a
    staging file the outer promotion above had not yet accepted -- see the
    comment beside --no-reference in _build_steps. Now that the staging
    file *is* the live database, redo that measurement for real. Best
    effort: the database itself already promoted successfully, and a
    reference file one rebuild out of date is far better than failing an
    otherwise-complete update over a sidecar file.
    """
    argv = _REFERENCE_REFRESH.get(sport)
    if not argv:
        return
    live = data_paths.default_db(sport)
    try:
        subprocess.run(_python(*argv, "--db", live), cwd=ROOT,
                       capture_output=True, text=True, check=True,
                       timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"warning: could not refresh {sport} reference data: {exc}",
              file=sys.stderr)


def _backup_and_promote(sport: str, staging: Path,
                        keep: int = KEEP_BACKUPS) -> str | None:
    """Atomically promote a validated staged database, retaining backups."""
    if not staging.exists():
        raise RuntimeError(f"staged {sport.upper()} database was not created")
    _carry_forward(sport, staging, Path(data_paths.default_db(sport)))
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
    try:
        os.replace(staging, live)
    except PermissionError:
        # Windows will not rename over a file another process holds open,
        # and the running app holds one read-only handle per thread with a
        # 256 MB memory map on it (db_pool.PRAGMAS), which is doubly
        # unrenameable. Requiring the server to be stopped made every
        # scheduled update fail on a machine that serves the app.
        #
        # Renaming is not the only way to promote. Copying the staged
        # *contents* into the live file through SQLite goes via the pager,
        # which coordinates with those readers rather than fighting the
        # filesystem, and it is transactional -- an interrupted copy rolls
        # back instead of leaving a torn file. Readers pick the new data
        # up on their next query without reconnecting.
        try:
            _promote_in_place(staging, live)
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"Could not overwrite {live.name}: {exc}. Another process "
                f"is holding a read transaction open on it; retry, or stop "
                f"the Streamlit server for this run."
            ) from exc
        staging.unlink(missing_ok=True)
    return str(backup) if backup else None


def _freshness(sport: str, con, columns: set[str],
               today: dt.date | None = None) -> dict:
    """How far behind the loaded data is, and whether that is expected.

    Neither the file's timestamp nor its row count answers "is this
    current". A database rebuilt last night from a feed that stopped three
    weeks ago has a fresh mtime and a clean integrity check, which is
    exactly the state this is here to catch.

    Whether dates are usable is decided from the data, not assumed: a
    season carrying one distinct date is a season-granular import whose
    date is a placeholder, and comparing it to today would be nonsense.
    """
    today = today or dt.datetime.now().astimezone().date()
    result: dict = {"basis": "unknown", "state": "unknown"}
    if "season" not in columns:
        result["summary"] = "no season column to measure against"
        return result

    latest_season = con.execute("SELECT MAX(season) FROM games").fetchone()[0]
    if latest_season is None:
        result["summary"] = "no games loaded"
        return result
    result["latest_season"] = latest_season

    # Is `date` a fixture date or a placeholder? The declaration above is
    # the authority, and this is the safety net for a sport nobody
    # declared: a season holding many games that all share one date is a
    # stamp, not a fixture list. The row count matters -- a season with a
    # single game loaded also has a single distinct date, and calling that
    # a placeholder would misread every season opening round.
    dated = False
    if "date" in columns:
        earliest, latest, played = con.execute(
            "SELECT MIN(date), MAX(date), COUNT(*) FROM games "
            "WHERE season = ?", (latest_season,)).fetchone()
        dated = earliest is not None and (played == 1 or earliest != latest)

    if sport not in STALE_AFTER_SEASONS and dated:
        latest = con.execute(
            "SELECT MAX(date) FROM games WHERE date IS NOT NULL").fetchone()[0]
        try:
            played = dt.date.fromisoformat(str(latest)[:10])
        except (TypeError, ValueError):
            result["summary"] = f"unreadable game date {latest!r}"
            return result
        days = (today - played).days
        limit = STALE_AFTER_DAYS.get(sport)
        result.update({
            "basis": "date",
            "latest_game_date": played.isoformat(),
            "days_since_latest_game": days,
            "stale_after_days": limit,
            "state": ("behind" if limit is not None and days > limit
                      else "current"),
            "summary": (f"last game {played.isoformat()}, {days} day"
                        f"{'' if days == 1 else 's'} ago"),
        })
        return result

    # Season-granular source.
    limit = STALE_AFTER_SEASONS.get(sport)
    behind = today.year - int(latest_season)
    result.update({
        "basis": "season",
        "seasons_behind": behind,
        "stale_after_seasons": limit,
        "state": ("behind" if limit is not None and behind >= limit
                  else "current"),
        "summary": (f"latest season loaded is {latest_season}"
                    + (f", {behind} behind {today.year}" if behind > 0 else "")),
    })
    return result


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
                result["freshness"] = _freshness(sport, con, columns)
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
    # Being behind is not a broken file, so it must not read as one: a
    # stale database still passes integrity and still serves every query.
    # It is reported alongside, not folded into, `failures`.
    stale = [
        sport for sport, snapshot in databases.items()
        if snapshot.get("freshness", {}).get("state") == "behind"
    ]
    result = {
        "state": "failed" if failures else "complete",
        "checked_at": checked_at,
        "sports": list(chosen),
        "databases": databases,
        "failures": failures,
        "stale": stale,
        "mode": "read_only",
    }
    # Persist only the diagnostic report. The sports database files are
    # opened mode=ro and are never downloaded, rebuilt, or promoted here.
    _write_json(CHECK_STATUS_PATH, result, best_effort=True)
    return result


def run_gridley_scan(*, through: dt.date | None = None, max_days: int = 31,
                      trigger: str = "admin", fetcher=None) -> dict:
    """Fetch new Gridley boards into a copy, then atomically promote it."""
    from utils import fetch_grids

    _acquire_lock()
    started = dt.datetime.now().astimezone()
    live = Path(data_paths.default_db("afl"))
    staging = live.with_suffix(live.suffix + ".gridley-scan-building")
    status = {
        "state": "running", "trigger": trigger,
        "started_at": started.isoformat(), "database": str(live),
    }
    _write_json(GRIDLEY_SCAN_STATUS_PATH, status, best_effort=True)
    try:
        if not live.exists():
            raise RuntimeError(f"No live AFL database at {live}")
        staging.unlink(missing_ok=True)
        shutil.copy2(live, staging)
        kwargs = {"through": through, "max_days": max_days}
        if fetcher is not None:
            kwargs["fetcher"] = fetcher
        result = fetch_grids.scan_gridley(staging, **kwargs)
        changes = result["inserted"] + result["updated"]
        if changes:
            backup = _backup_and_promote("afl", staging)
            promoted = True
        else:
            staging.unlink(missing_ok=True)
            backup = None
            promoted = False
        status.update({
            "state": "complete", "finished_at": dt.datetime.now(
                ).astimezone().isoformat(),
            "result": result, "promoted": promoted, "backup": backup,
            "after": _database_snapshot("afl", quick=True),
        })
        _write_json(GRIDLEY_SCAN_STATUS_PATH, status, best_effort=True)
        return status
    except Exception as exc:
        status.update({
            "state": "failed", "error": f"{type(exc).__name__}: {exc}",
            "finished_at": dt.datetime.now().astimezone().isoformat(),
        })
        _write_json(GRIDLEY_SCAN_STATUS_PATH, status, best_effort=True)
        raise
    finally:
        LOCK_PATH.unlink(missing_ok=True)


def _loaded_rising_star_round(database: Path, season: int) -> int | None:
    """The latest nomination round the live database actually holds."""
    if not database.exists():
        return None
    try:
        with closing(sqlite3.connect(
                f"file:{database}?mode=ro", uri=True)) as con:
            if not con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='rising_star_nominees'").fetchone():
                return None
            return con.execute(
                "SELECT MAX(round_number) FROM rising_star_nominees "
                "WHERE season = ?", (season,)).fetchone()[0]
    except sqlite3.Error:
        return None


def run_rising_star_scan(*, season: int | None = None, trigger: str = "admin",
                         fetcher=None) -> dict:
    """Refresh the season's Rising Star nominations from Wikipedia.

    The award adds one nomination a week all season. FootyWire, the richer
    source, may not be fetched automatically under its own terms, so the
    weekly catch-up comes from Wikipedia instead -- see
    ``afl/fetch_wikipedia_rising_star.py`` for why both sources coexist and
    ``utils/afl/load_rising_star.py`` for which one wins a round.

    Same shape as the Gridley scan: refresh a source file, rebuild into a
    copy, promote only if there is something new. A week with no new
    nomination costs one HTTP request and changes nothing.
    """
    from afl import fetch_wikipedia_rising_star as wiki
    from utils.afl import load_rising_star

    season = season or dt.datetime.now().astimezone().year
    _acquire_lock()
    started = dt.datetime.now().astimezone()
    live = Path(data_paths.default_db("afl"))
    staging = live.with_suffix(live.suffix + ".rising-star-building")
    status = {
        "state": "running", "trigger": trigger, "season": season,
        "started_at": started.isoformat(), "database": str(live),
    }
    _write_json(RISING_STAR_STATUS_PATH, status, best_effort=True)
    try:
        if not live.exists():
            raise RuntimeError(f"No live AFL database at {live}")
        try:
            result = (fetcher(season) if fetcher is not None
                      else wiki.refresh_season(season))
        except wiki.PageNotFound:
            status.update({
                "state": "complete", "promoted": False, "result": {
                    "season": season, "added": 0, "changed": False,
                    "note": f"no {season} Wikipedia article yet",
                },
                "finished_at": dt.datetime.now().astimezone().isoformat(),
            })
            _write_json(RISING_STAR_STATUS_PATH, status, best_effort=True)
            return status

        # An unchanged source file is not proof the database is current: a
        # previous run could have written the CSV and then failed to load
        # it. Compare what is loaded against what the file now says, so a
        # half-finished run repairs itself on the next Monday rather than
        # staying one nomination behind until someone notices.
        loaded_round = _loaded_rising_star_round(live, season)
        latest = result.get("latest_round")
        behind = (latest is not None
                  and (loaded_round is None or loaded_round < latest))
        if not result.get("changed") and not behind:
            status.update({
                "state": "complete", "promoted": False, "result": result,
                "finished_at": dt.datetime.now().astimezone().isoformat(),
            })
            _write_json(RISING_STAR_STATUS_PATH, status, best_effort=True)
            return status

        staging.unlink(missing_ok=True)
        shutil.copy2(live, staging)
        loaded = load_rising_star.load_sources(
            str(staging), load_rising_star.default_sources(), verbose=True)
        if not loaded.get("trusted"):
            raise RuntimeError(
                "the reloaded Rising Star table linked no rows to a player; "
                "the live database was left unchanged")
        backup = _backup_and_promote("afl", staging)
        status.update({
            "state": "complete", "promoted": True, "backup": backup,
            "result": result, "loaded": loaded,
            "finished_at": dt.datetime.now().astimezone().isoformat(),
            "after": _database_snapshot("afl", quick=True),
        })
        _write_json(RISING_STAR_STATUS_PATH, status, best_effort=True)
        return status
    except Exception as exc:
        status.update({
            "state": "failed", "error": f"{type(exc).__name__}: {exc}",
            "finished_at": dt.datetime.now().astimezone().isoformat(),
        })
        _write_json(RISING_STAR_STATUS_PATH, status, best_effort=True)
        raise
    finally:
        staging.unlink(missing_ok=True)
        LOCK_PATH.unlink(missing_ok=True)


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
        "steps": [], "completed_steps": 0, "total_steps": len(steps),
        "current_step": {
            "sport": None, "label": "Inspecting live databases",
            "step_number": 0,
            "started_at": stamp.isoformat(),
        },
        "before": {},
    }
    _write_json(STATUS_PATH, status, best_effort=True)
    required_failure = False
    optional_failure = False
    failed_sports = set()
    try:
        # Publish a live state before these snapshots scan several large
        # SQLite files. Previously the Admin page sat at "Starting, 0s" for
        # minutes even though the child process was doing real work.
        status["before"] = {s: _database_snapshot(s) for s in chosen}
        status["current_step"] = {
            "sport": None, "label": "Preparing staging databases",
            "step_number": 0,
            "started_at": dt.datetime.now().astimezone().isoformat(),
        }
        _write_json(STATUS_PATH, status, best_effort=True)
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
                    status["completed_steps"] = len(status["steps"])
                    _write_json(STATUS_PATH, status, best_effort=True)
                    continue
                started = time.monotonic()
                step_started_at = dt.datetime.now().astimezone().isoformat()
                status["current_step"] = {
                    "sport": sport,
                    "label": step.label,
                    "step_number": len(status["steps"]) + 1,
                    "started_at": step_started_at,
                }
                _write_json(STATUS_PATH, status, best_effort=True)
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
                    "started_at": step_started_at,
                    "finished_at": dt.datetime.now().astimezone().isoformat(),
                }
                status["steps"].append(record)
                status["completed_steps"] = len(status["steps"])
                status["current_step"] = None
                _write_json(STATUS_PATH, status, best_effort=True)
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
                    "staging": str(_retained_staging_path(staging[sport])),
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
                _refresh_reference(sport)
        status["after"] = {s: _database_snapshot(s) for s in chosen}
        status["state"] = (
            "failed" if required_failure else
            "complete_with_warnings" if optional_failure else "complete"
        )
        status["finished_at"] = dt.datetime.now().astimezone().isoformat()
        status["current_step"] = None
        # best_effort: every promotion decision above is already final: a
        # locked status file here must not turn a successful run into a
        # reported failure, and must not throw away completed promotions by
        # falling into the except block below.
        _write_json(STATUS_PATH, status, best_effort=True)
        return 1 if required_failure else 0
    except Exception as exc:
        status.update({
            "state": "failed", "error": f"{type(exc).__name__}: {exc}",
            "finished_at": dt.datetime.now().astimezone().isoformat(),
        })
        _write_json(STATUS_PATH, status, best_effort=True)
        raise
    finally:
        LOCK_PATH.unlink(missing_ok=True)


def _spawn_detached(command: list[str], status_path: Path,
                    status: dict) -> int:
    """Reserve the update lock, then hand it to a detached child process.

    Shared by every administrator-triggered job. Without it a job runs
    inside the Streamlit script run: the page blocks for as long as the
    work takes, and a websocket timeout kills the job halfway through.
    """
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
    _write_json(status_path, status, best_effort=True)

    launcher_log = None
    try:
        launcher_log = (LOG_DIR / "launcher.log").open("a", encoding="utf-8")
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
        process = subprocess.Popen(
            [sys.executable, "-m", "database_updates", *command], **kwargs)
        return process.pid
    except Exception:
        LOCK_PATH.unlink(missing_ok=True)
        raise
    finally:
        if launcher_log is not None:
            launcher_log.close()


def start_background(event: str = "full", sports: Iterable[str] = SPORT_KEYS,
                     trigger: str = "admin") -> int:
    """Start an update without tying its lifetime to a Streamlit rerun."""
    chosen = tuple(dict.fromkeys(sports))
    if not chosen:
        raise ValueError("choose at least one sport")
    unknown = set(chosen) - set(SPORT_KEYS)
    if unknown:
        raise ValueError(f"unknown sports: {', '.join(sorted(unknown))}")
    if event not in EVENTS:
        raise ValueError(f"unknown event: {event}")
    total_steps = len(plan(event, chosen))
    return _spawn_detached(
        ["run", "--event", event, "--sports", *chosen, "--trigger", trigger],
        STATUS_PATH,
        {
            "state": "starting", "event": event, "trigger": trigger,
            "sports": list(chosen), "pid": None,
            "started_at": dt.datetime.now().astimezone().isoformat(),
            "steps": [], "completed_steps": 0,
            "total_steps": total_steps, "current_step": None,
        },
    )


def start_gridley_scan_background(*, max_days: int = 31,
                                  trigger: str = "admin") -> int:
    """Start a Gridley scan detached from the Streamlit process.

    The scan makes up to `max_days` sequential HTTP requests, which is far
    too long to hold a script run open.
    """
    return _spawn_detached(
        ["gridley-scan", "--max-days", str(max_days), "--trigger", trigger],
        GRIDLEY_SCAN_STATUS_PATH,
        {
            "state": "starting", "trigger": trigger, "pid": None,
            "started_at": dt.datetime.now().astimezone().isoformat(),
        },
    )


def start_rising_star_scan_background(*, season: int | None = None,
                                      trigger: str = "admin") -> int:
    """Start a Rising Star refresh detached from the Streamlit process.

    Short work -- one request and a reload -- but it promotes a database,
    which means it takes the update lock and must not die with a rerun.
    """
    season = season or dt.datetime.now().astimezone().year
    return _spawn_detached(
        ["rising-star-scan", "--season", str(season), "--trigger", trigger],
        RISING_STAR_STATUS_PATH,
        {
            "state": "starting", "trigger": trigger, "pid": None,
            "season": season,
            "started_at": dt.datetime.now().astimezone().isoformat(),
        },
    )




def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run an update in the foreground")
    run.add_argument("--event", choices=EVENTS, default="regular")
    run.add_argument("--sports", nargs="+", choices=SPORT_KEYS, default=list(SPORT_KEYS))
    run.add_argument("--trigger", default="cli")
    run.add_argument("--only-if-due", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    scan = sub.add_parser(
        "gridley-scan", help="fetch new Gridley boards into the AFL database")
    scan.add_argument(
        "--through", default=None,
        help="last date to check, YYYY-MM-DD (default: today)")
    scan.add_argument("--max-days", type=int, default=31)
    scan.add_argument("--trigger", default="cli")
    rising = sub.add_parser(
        "rising-star-scan",
        help="refresh this season's Rising Star nominations from Wikipedia")
    rising.add_argument("--season", type=int, default=None)
    rising.add_argument("--trigger", default="cli")
    scheduled = sub.add_parser(
        "scheduled", help="run a guarded update from an operating-system timer")
    scheduled.add_argument(
        "event",
        choices=("regular", "brownlow-awards", "grand-final-awards", "gridley",
                 "rising-star"))
    sub.add_parser("status", help="print the last update status")
    check = sub.add_parser(
        "check", help="report currency and integrity without changing anything")
    check.add_argument("--sports", nargs="+", choices=SPORT_KEYS,
                       default=list(SPORT_KEYS))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "status":
        print(json.dumps(read_status(), indent=2))
        return 0
    if args.command == "check":
        report = check_databases(args.sports)
        for sport in report["sports"]:
            snapshot = report["databases"][sport]
            fresh = snapshot.get("freshness", {})
            print(f"{sport.upper():4} integrity={snapshot.get('integrity')} "
                  f"currency={fresh.get('state', 'unknown')} "
                  f"({fresh.get('summary', 'not measured')})")
        if report["stale"]:
            print("behind: " + ", ".join(s.upper() for s in report["stale"]))
        return 1 if report["failures"] else 0
    if args.command == "gridley-scan":
        through = (dt.date.fromisoformat(args.through)
                   if args.through else None)
        status = run_gridley_scan(
            through=through, max_days=args.max_days, trigger=args.trigger)
        print(json.dumps(status.get("result", {}), indent=2))
        return 0
    if args.command == "rising-star-scan":
        status = run_rising_star_scan(
            season=args.season, trigger=args.trigger)
        print(json.dumps(status.get("result", {}), indent=2))
        return 0
    if args.command == "scheduled":
        if not event_is_due(args.event):
            print(
                f"{args.event} is not due on "
                f"{dt.datetime.now().astimezone().date().isoformat()}; skipped"
            )
            return 0
        if args.event == "gridley":
            run_gridley_scan(trigger="systemd")
            return 0
        if args.event == "rising-star":
            run_rising_star_scan(trigger="systemd")
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
