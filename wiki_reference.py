"""Staging and import for the Wikipedia sports scrape.

``wiki_sports_scraper.py`` writes a reference dataset for the NBA, NFL and
MLB: a catalogue of every current franchise, plus whatever the team, league
and Hall of Fame pages happened to say, extracted as long records. This
module validates that output and loads it into a sport's database.

It is reference material, not a statistics source. Nothing here replaces
``matches``, ``players`` or ``player_seasons``, and nothing here is written
into them.

Why staging, and not straight into normalised tables
----------------------------------------------------

The scrape's structured content lives in ``data_json``, whose keys come
from whatever columns the Wikipedia table happened to have. Profiling this
run found 73 distinct key sets across the NBA's table rows, 91 across the
NFL's and 26 across the MLB's -- including positional keys (``0``, ``1``,
``2``) where the table had no usable header, and keys carrying the team's
own name (``dallas_cowboys_hall_of_famers_players_inducted``) where the
heading was folded into the header row.

There is no cross-section schema to normalise against, and inventing one
from section names would be guesswork: "Retired numbers" has four different
column sets across the thirty MLB clubs alone. So the records are staged
whole, with their provenance, and ``profile()`` reports the key sets that a
later normalisation pass would be built from.

What is imported, and what is not
---------------------------------

The consolidated ``<sport>/team_stats.csv`` only. Each sport also writes
one file per team under ``<sport>/teams/``, holding exactly the same
records; importing both would duplicate every team reference row.

The ``_cache/`` tree is never imported. It is the archived MediaWiki
response for each page -- provenance and re-run material, not data.

Idempotency
-----------

Every staged record carries a ``source_record_hash``: a SHA-256 of its
sport, team, section, record type, label, value, canonicalised
``data_json`` and source page. Deliberately *not* of ``table_index`` and
``row_index``, which shift whenever an editor moves a table on the page and
would otherwise make every re-import look like new content.

Re-importing upserts on that hash and then removes the sport's staged rows
that the current batch did not touch, so the staging tables always describe
the scrape that was last loaded rather than accumulating the union of every
scrape ever run.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

#: The scrape's own default output root.
DEFAULT_ROOT = Path(r"C:\data_lab\wiki")

SPORTS = ("nba", "nfl", "mlb")

#: Current-franchise counts. A deviation is flagged rather than accepted:
#: the league pages are the source, and a table that has gained or lost a
#: row usually means the page changed shape, not that a league expanded.
EXPECTED_TEAMS = {"nba": 30, "nfl": 32, "mlb": 30}

TEAM_STAGE = "wiki_team_stage"
REFERENCE_STAGE = "wiki_reference_stage"
LOG_STAGE = "wiki_scrape_log_stage"
MAP_TABLE = "wiki_team_map"
BATCH_TABLE = "wiki_import_batch"

#: The three record files, and the ``dataset_type`` each is staged under.
#: Order matters only for the report; the hash keeps them distinct.
DATASETS = (
    ("league_stats.csv", "league"),
    ("hall_of_fame.csv", "hall_of_fame"),
    ("team_stats.csv", "team"),
)

RECORD_TYPES = ("table_row", "list_item", "paragraph")

#: Wikipedia names a franchise as it is named now; these databases were
#: built from sources that named four of them as they used to be. Each of
#: these is the same franchise under a later name, so they are matched
#: rather than left unresolved -- and recorded as `alias_match`, not
#: `matched`, so a review can see the join was not on the name itself.
ALIASES = {
    "mlb": {
        "Cleveland Guardians": "Cleveland Indians",      # renamed 2022
        "Miami Marlins": "Florida Marlins",              # renamed 2012
        "Los Angeles Angels": "Los Angeles Angels of Anaheim",   # 2016
        "Athletics": "Oakland Athletics",                # left Oakland 2025
    },
}

# CSV cells here carry whole Wikipedia paragraphs and JSON objects; the
# 131,072-character default is not enough for the widest of them.
csv.field_size_limit(10_000_000)


# --------------------------------------------------------------- schema

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {BATCH_TABLE} (
    import_batch_id  INTEGER PRIMARY KEY,
    sport            TEXT NOT NULL,
    source_root      TEXT NOT NULL,
    scrape_generated_at TEXT,
    scraper_version  TEXT,
    imported_at      TEXT NOT NULL,
    status           TEXT NOT NULL,
    notes            TEXT
);

CREATE TABLE IF NOT EXISTS {TEAM_STAGE} (
    sport            TEXT NOT NULL,
    team             TEXT NOT NULL,
    conference       TEXT,
    league           TEXT,
    division         TEXT,
    location         TEXT,
    city             TEXT,
    arena            TEXT,
    stadium          TEXT,
    capacity_raw     TEXT,
    capacity_numeric INTEGER,
    founded_raw      TEXT,
    founded_year     INTEGER,
    joined_raw       TEXT,
    joined_year      INTEGER,
    first_season_raw TEXT,
    first_season_year INTEGER,
    head_coach       TEXT,
    coordinates_raw  TEXT,
    team_page_title  TEXT,
    team_url         TEXT,
    source_page      TEXT,
    source_url       TEXT,
    source_revision  INTEGER,
    scraped_at_utc   TEXT,
    import_batch_id  INTEGER,
    PRIMARY KEY (sport, team)
);

CREATE TABLE IF NOT EXISTS {REFERENCE_STAGE} (
    wiki_record_id     INTEGER PRIMARY KEY,
    source_record_hash TEXT NOT NULL UNIQUE,
    sport              TEXT NOT NULL,
    team               TEXT,
    team_slug          TEXT,
    dataset_type       TEXT NOT NULL,
    section            TEXT,
    record_type        TEXT NOT NULL,
    table_index        INTEGER,
    row_index          INTEGER,
    label              TEXT,
    value              TEXT,
    data_json          TEXT,
    json_key_set       TEXT,
    source_page        TEXT,
    source_url         TEXT,
    source_revision    INTEGER,
    scraped_at_utc     TEXT,
    import_batch_id    INTEGER,
    CHECK (dataset_type IN ('team', 'league', 'hall_of_fame')),
    CHECK (record_type IN ('table_row', 'list_item', 'paragraph')),
    CHECK (json_valid(data_json))
);

CREATE INDEX IF NOT EXISTS ix_wiki_ref_team
    ON {REFERENCE_STAGE}(sport, team);
CREATE INDEX IF NOT EXISTS ix_wiki_ref_section
    ON {REFERENCE_STAGE}(sport, dataset_type, section);

CREATE TABLE IF NOT EXISTS {LOG_STAGE} (
    import_batch_id  INTEGER,
    timestamp        TEXT,
    sport            TEXT,
    item             TEXT,
    status           TEXT,
    message          TEXT,
    output_path      TEXT
);

CREATE TABLE IF NOT EXISTS {MAP_TABLE} (
    sport             TEXT NOT NULL,
    wiki_team_name    TEXT NOT NULL,
    wiki_page_title   TEXT,
    canonical_team_id TEXT,
    match_status      TEXT NOT NULL,
    match_method      TEXT,
    reviewed_at       TEXT,
    PRIMARY KEY (sport, wiki_team_name)
);
"""


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create the staging tables. Safe to call on a database that has them."""
    con.executescript(SCHEMA)
    con.commit()


# ------------------------------------------------------------- cleaning

_YEAR = re.compile(r"(1[6-9]\d{2}|20\d{2})")


def numeric(text: str | None) -> int | None:
    """A whole number from a Wikipedia cell, or None.

    Capacities arrive as ``19156`` here but a re-scrape can produce
    ``65,878 expandable to 71,000`` or ``Various``. Only a cell that is
    entirely a number, once separators are dropped, is trusted; anything
    with prose in it is left to the ``_raw`` column beside this one.
    """
    if text is None:
        return None
    cleaned = text.strip().replace(",", "").replace("\u00a0", "")
    return int(cleaned) if cleaned.isdigit() else None


def year(text: str | None) -> int | None:
    """The first four-digit year in a cell, or None.

    Founding years carry footnote markers (``1901*``), season ranges
    (``1946-47``) and, for the NFL, two of them at once: ``1960 (AFL) 1970
    (NFL)``. The first is the one meant in every case in this run -- when
    the franchise began -- so it is the one taken, and the raw cell is kept
    beside it for anything that needs the rest.
    """
    if not text:
        return None
    found = _YEAR.search(text)
    return int(found.group(1)) if found else None


def canonical_json(text: str | None) -> str:
    """``data_json`` with its keys sorted and its whitespace fixed.

    The hash is taken over this rather than the raw string so that a
    re-serialised but identical object is recognised as the same record.
    """
    try:
        return json.dumps(json.loads(text or "{}"), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return (text or "").strip()


def key_set(data_json: str | None) -> str:
    """The record's JSON keys, sorted and pipe-joined.

    This is the column a normalisation pass groups on: ``player|position|
    tenure`` is a table worth building, ``0|1|2|3`` is a table whose header
    Wikipedia did not give us.
    """
    try:
        loaded = json.loads(data_json or "{}")
    except (TypeError, ValueError):
        return ""
    if not isinstance(loaded, dict):
        return ""
    return "|".join(sorted(str(k) for k in loaded))


def record_hash(dataset_type: str, row: dict) -> str:
    """The idempotency key described in the module docstring."""
    parts = (
        dataset_type,
        row.get("sport", ""),
        row.get("team", ""),
        row.get("section", ""),
        row.get("record_type", ""),
        row.get("label", ""),
        row.get("value", ""),
        canonical_json(row.get("data_json")),
        row.get("source_page", ""),
    )
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def read_csv(path: Path) -> list[dict]:
    """Rows from a UTF-8-with-BOM CSV, or [] when the file is absent."""
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# ----------------------------------------------------------- validation

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

#: Files a sport must have written before its output can be imported.
REQUIRED_FILES = ("team.csv", "team_stats.csv", "league_stats.csv",
                  "hall_of_fame.csv", "metadata.json")


class Report:
    """Errors block the import; warnings are recorded and imported anyway."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)

    @property
    def ok(self) -> bool:
        return not self.errors

    def print(self, prefix: str = "  ") -> None:
        for line in self.notes:
            print(f"{prefix}{line}")
        for line in self.warnings:
            print(f"{prefix}WARN  {line}")
        for line in self.errors:
            print(f"{prefix}ERROR {line}")


def validate(root: Path, sport: str) -> Report:
    """Check one sport's scrape output before any of it is written.

    A completed run is not the same as a usable one -- the scraper records
    its own WARN and ERROR lines and still exits cleanly -- so every check
    the import depends on is made here, against the files rather than
    against the scraper's summary of them.
    """
    report = Report()
    folder = root / sport

    if not folder.is_dir():
        report.error(f"no {sport}/ directory under {root}")
        return report

    for name in REQUIRED_FILES:
        if not (folder / name).exists():
            report.error(f"missing {sport}/{name}")
    if not report.ok:
        return report

    # -- metadata --------------------------------------------------------
    meta = read_json(folder / "metadata.json")
    failures = meta.get("team_page_failures")
    if failures:
        report.error(f"{failures} team page(s) failed to scrape")
    for key in ("team_reference_rows", "hall_of_fame_rows",
                "league_reference_rows"):
        if not meta.get(key):
            # Legitimate after a source page changes shape, but never
            # something to import without somebody having looked.
            report.warn(f"metadata reports {key} = {meta.get(key)}")

    # -- team catalogue --------------------------------------------------
    teams = read_csv(folder / "team.csv")
    expected = EXPECTED_TEAMS.get(sport)
    if expected and len(teams) != expected:
        report.error(f"{len(teams)} teams in team.csv, expected {expected}")
    else:
        report.note(f"team.csv: {len(teams)} teams")

    named = [row.get("team", "").strip() for row in teams]
    if len(set(named)) != len(named):
        duplicated = [n for n, c in Counter(named).items() if c > 1]
        report.error(f"team.csv repeats: {', '.join(duplicated)}")

    # -- record files ----------------------------------------------------
    seen_hashes: dict[str, str] = {}
    for filename, dataset_type in DATASETS:
        rows = read_csv(folder / filename)
        if not rows:
            report.warn(f"{filename} has no records")
            continue

        bad_json = bad_type = no_source = bad_stamp = bad_revision = 0
        for row in rows:
            try:
                json.loads(row.get("data_json") or "")
            except (TypeError, ValueError):
                bad_json += 1
            if row.get("record_type") not in RECORD_TYPES:
                bad_type += 1
            if not (row.get("source_url") and row.get("source_page")):
                no_source += 1
            if not _ISO.match(row.get("scraped_at_utc") or ""):
                bad_stamp += 1
            revision = (row.get("source_revision") or "").strip()
            if revision and not revision.isdigit():
                bad_revision += 1

            digest = record_hash(dataset_type, row)
            if digest in seen_hashes:
                # Not fatal: the import upserts, so a repeat collapses into
                # the row already there rather than failing the batch.
                report.warn(f"{filename}: duplicate content hash for "
                            f"{row.get('team') or sport} / "
                            f"{row.get('section')}")
            seen_hashes[digest] = filename

        for count, what in ((bad_json, "rows with invalid data_json"),
                            (bad_type, "rows with an unknown record_type")):
            if count:
                report.error(f"{filename}: {count} {what}")
        for count, what in ((no_source, "rows with no source page or URL"),
                            (bad_stamp, "rows with a non-ISO scraped_at_utc"),
                            (bad_revision, "rows with a non-numeric revision")):
            if count:
                report.warn(f"{filename}: {count} {what}")

        report.note(f"{filename}: {len(rows)} records")

    return report


def read_scrape_log(root: Path, sport: str) -> list[dict]:
    """The root scrape log's rows for one sport.

    The log is written once for the whole run, so it is filtered here
    rather than looked for inside the sport's folder.
    """
    key = sport.lower()
    return [row for row in read_csv(root / "scrape_log.csv")
            if (row.get("sport") or "").strip().lower() == key]


# -------------------------------------------------------- team mapping

def map_teams(con: sqlite3.Connection, sport: str,
              teams: list[dict]) -> dict[str, str]:
    """Fill ``wiki_team_map`` and return wiki team name -> club_id.

    The database stays the authority on team identity. ``team_slug`` is
    never used as the key: it is derived from the displayed name and
    changes on a relocation, a rebrand or a Wikipedia page move.
    """
    canonical: dict[str, str] = {}
    for club_id, name, db_name in con.execute(
            "SELECT club_id, name, db_club_now FROM clubs"):
        for candidate in (name, db_name):
            if candidate:
                canonical.setdefault(candidate.strip(), club_id)

    aliases = ALIASES.get(sport, {})
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    resolved: dict[str, str] = {}

    for row in teams:
        wiki_name = (row.get("team") or "").strip()
        if not wiki_name:
            continue
        club_id = canonical.get(wiki_name)
        status, method = "matched", "exact name"
        if not club_id and wiki_name in aliases:
            club_id = canonical.get(aliases[wiki_name])
            status, method = "alias_match", f"alias of {aliases[wiki_name]}"
        if not club_id:
            status, method = "unmatched", None

        if club_id:
            resolved[wiki_name] = club_id
        con.execute(
            f"INSERT INTO {MAP_TABLE} (sport, wiki_team_name, "
            f" wiki_page_title, canonical_team_id, match_status, "
            f" match_method, reviewed_at) VALUES (?,?,?,?,?,?,?) "
            f"ON CONFLICT(sport, wiki_team_name) DO UPDATE SET "
            f" wiki_page_title = excluded.wiki_page_title, "
            f" canonical_team_id = excluded.canonical_team_id, "
            f" match_status = excluded.match_status, "
            f" match_method = excluded.match_method, "
            f" reviewed_at = excluded.reviewed_at",
            (sport, wiki_name, (row.get("team_page_title") or "").strip(),
             club_id, status, method, stamp))
    con.commit()
    return resolved


# -------------------------------------------------------------- import

def open_batch(con: sqlite3.Connection, sport: str, root: Path,
               meta: dict, run_meta: dict) -> int:
    """Record the import and return its batch id."""
    ensure_schema(con)
    cursor = con.execute(
        f"INSERT INTO {BATCH_TABLE} (sport, source_root, "
        f" scrape_generated_at, scraper_version, imported_at, status) "
        f"VALUES (?,?,?,?,?,'running')",
        (sport, str(root), meta.get("generated_at_utc"),
         run_meta.get("version"),
         dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
    con.commit()
    return int(cursor.lastrowid)


def close_batch(con: sqlite3.Connection, batch_id: int, status: str,
                notes: str = "") -> None:
    con.execute(f"UPDATE {BATCH_TABLE} SET status = ?, notes = ? "
                f"WHERE import_batch_id = ?", (status, notes, batch_id))
    con.commit()


def load_team_catalogue(con: sqlite3.Connection, sport: str,
                        teams: list[dict], batch_id: int) -> int:
    """Stage ``team.csv``, keeping every value as it was scraped.

    The parsed columns sit *beside* the raw ones rather than replacing
    them. ``founded`` reading ``1901*`` becomes ``founded_raw='1901*'`` and
    ``founded_year=1901``; the asterisk is a footnote about the franchise's
    disputed origin, and dropping it silently would lose the only sign that
    the year is contested.
    """
    con.execute(f"DELETE FROM {TEAM_STAGE} WHERE sport = ?", (sport,))
    written = 0
    for row in teams:
        capacity = (row.get("capacity") or "").strip()
        founded = (row.get("founded") or "").strip()
        joined = (row.get("joined") or "").strip()
        first_season = (row.get("first_season") or "").strip()
        con.execute(f"""
            INSERT INTO {TEAM_STAGE} (
                sport, team, conference, league, division, location, city,
                arena, stadium, capacity_raw, capacity_numeric,
                founded_raw, founded_year, joined_raw, joined_year,
                first_season_raw, first_season_year, head_coach,
                coordinates_raw, team_page_title, team_url, source_page,
                source_url, source_revision, scraped_at_utc, import_batch_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            sport, (row.get("team") or "").strip(),
            row.get("conference"), row.get("league"), row.get("division"),
            row.get("location"), row.get("city"),
            row.get("arena"), row.get("stadium"),
            capacity or None, numeric(capacity),
            founded or None, year(founded),
            joined or None, year(joined),
            first_season or None, year(first_season),
            row.get("head_coach"), row.get("coordinates"),
            row.get("team_page_title"), row.get("team_url"),
            row.get("source_page"), row.get("source_url"),
            numeric(row.get("source_revision")), row.get("scraped_at_utc"),
            batch_id))
        written += 1
    con.commit()
    return written


def load_records(con: sqlite3.Connection, sport: str, folder: Path,
                 batch_id: int) -> tuple[dict[str, int], int]:
    """Stage the three record files. Returns (rows per dataset, collapsed).

    ``collapsed`` counts records that hashed to one already staged in this
    batch -- the same fact twice on the page, or a scraper that emitted a
    row twice. They are absorbed rather than rejected, but reported,
    because a large number of them means the scrape, not the import.
    """
    counts: dict[str, int] = {}
    staged = 0

    for filename, dataset_type in DATASETS:
        rows = read_csv(folder / filename)
        counts[dataset_type] = len(rows)
        for row in rows:
            data_json = row.get("data_json") or "{}"
            con.execute(f"""
                INSERT INTO {REFERENCE_STAGE} (
                    source_record_hash, sport, team, team_slug, dataset_type,
                    section, record_type, table_index, row_index, label,
                    value, data_json, json_key_set, source_page, source_url,
                    source_revision, scraped_at_utc, import_batch_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_record_hash) DO UPDATE SET
                    table_index     = excluded.table_index,
                    row_index       = excluded.row_index,
                    source_revision = excluded.source_revision,
                    scraped_at_utc  = excluded.scraped_at_utc,
                    import_batch_id = excluded.import_batch_id
            """, (
                record_hash(dataset_type, row), sport,
                (row.get("team") or "").strip() or None,
                (row.get("team_slug") or "").strip() or None,
                dataset_type, row.get("section"), row.get("record_type"),
                numeric(row.get("table_index")), numeric(row.get("row_index")),
                row.get("label"), row.get("value"), data_json,
                key_set(data_json), row.get("source_page"),
                row.get("source_url"), numeric(row.get("source_revision")),
                row.get("scraped_at_utc"), batch_id))
            staged += 1
    con.commit()

    # Every CSV row touched a row of this batch; if fewer rows carry the
    # batch than were read, two of them hashed the same.
    touched = con.execute(
        f"SELECT COUNT(*) FROM {REFERENCE_STAGE} "
        f"WHERE sport = ? AND import_batch_id = ?",
        (sport, batch_id)).fetchone()[0]

    # Rows the current batch did not touch are no longer on Wikipedia's
    # pages; the staging tables describe the scrape that was last loaded.
    con.execute(f"DELETE FROM {REFERENCE_STAGE} "
                f"WHERE sport = ? AND import_batch_id <> ?", (sport, batch_id))
    con.commit()

    return counts, staged - touched


def load_log(con: sqlite3.Connection, sport: str, root: Path,
             batch_id: int) -> dict[str, int]:
    """Stage the scraper's own log lines for this sport."""
    con.execute(f"DELETE FROM {LOG_STAGE} WHERE sport = ?", (sport.upper(),))
    statuses: Counter = Counter()
    for row in read_scrape_log(root, sport):
        statuses[(row.get("status") or "").strip()] += 1
        con.execute(
            f"INSERT INTO {LOG_STAGE} (import_batch_id, timestamp, sport, "
            f" item, status, message, output_path) VALUES (?,?,?,?,?,?,?)",
            (batch_id, row.get("timestamp"), row.get("sport"),
             row.get("item"), row.get("status"), row.get("message"),
             row.get("output_path")))
    con.commit()
    return dict(statuses)


# ------------------------------------------------------------ profiling

def profile(con: sqlite3.Connection, sport: str,
            limit: int = 20) -> list[tuple]:
    """(section, record_type, key set, rows) for the staged table rows.

    The inventory a normalisation pass is designed from. Run it before
    creating any destination table: sections sharing a name across clubs
    frequently do not share columns.
    """
    return con.execute(f"""
        SELECT section, record_type, json_key_set, COUNT(*) AS rows
        FROM {REFERENCE_STAGE}
        WHERE sport = ? AND record_type = 'table_row'
        GROUP BY section, record_type, json_key_set
        ORDER BY rows DESC LIMIT ?
    """, (sport, limit)).fetchall()


def unresolved(con: sqlite3.Connection, sport: str) -> list[tuple]:
    """Wiki team names with no canonical club, for the import report."""
    return con.execute(
        f"SELECT wiki_team_name, match_status FROM {MAP_TABLE} "
        f"WHERE sport = ? AND canonical_team_id IS NULL "
        f"ORDER BY wiki_team_name", (sport,)).fetchall()


def orphan_records(con: sqlite3.Connection, sport: str) -> int:
    """Staged team records whose team maps to no club."""
    return con.execute(f"""
        SELECT COUNT(*) FROM {REFERENCE_STAGE} r
        WHERE r.sport = ? AND r.team IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM {MAP_TABLE} m
              WHERE m.sport = r.sport AND m.wiki_team_name = r.team
                AND m.canonical_team_id IS NOT NULL)
    """, (sport,)).fetchone()[0]
