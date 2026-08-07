#!/usr/bin/env python3
"""Parse cached club sources and load the club data layer into SQLite.

This importer is idempotent. It keeps the visible club catalogue at exactly 18
current clubs and rebuilds source-derived rows inside one transaction.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Iterable

try:
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover - exercised by users without dep
    raise SystemExit("Missing dependency. Run: python -m pip install beautifulsoup4") from exc

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from utils.afl.club_sources import (CLUBS, DEFAULT_RAW_DIR, STAT_HEADERS,
                                    clean_text, fallback_name_key,
                                    selected_clubs, source_name_to_display,
                                    source_path)
from data_paths import default_db  # noqa: E402  (needs the path above)

#: Resolved through data_paths so this loader writes to the same file the
#: application reads. Hardcoding a path here is what previously split the
#: club layer away from the rest of the database.
DEFAULT_DB = Path(default_db("afl"))
TRUSTED = {"unique", "resolved"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_number(value: Any, *, integer: bool = False):
    text = clean_text(value).replace(",", "")
    if not text or text in {"-", "—", "n/a", "N/A"}:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group(0))
    return int(number) if integer else number


def read_html(path: Path) -> BeautifulSoup:
    data = path.read_bytes()
    return BeautifulSoup(data.decode("windows-1252", errors="replace"), "html.parser")


def header_texts(table) -> list[str]:
    row = table.find("thead")
    row = row.find("tr") if row else table.find("tr")
    if not row:
        return []
    return [clean_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]


def body_rows(table) -> Iterable[tuple[list[str], list[Any]]]:
    body = table.find("tbody") or table
    for row in body.find_all("tr", recursive=False):
        cells = row.find_all(["td", "th"], recursive=False)
        if not cells or row.find_parent("thead"):
            continue
        yield [clean_text(c.get_text(" ", strip=True)) for c in cells], cells


def player_href(cell) -> str | None:
    link = cell.find("a", href=True) if cell else None
    return link.get("href") if link else None


def parse_player_totals(path: Path) -> tuple[list[dict], list[dict]]:
    soup = read_html(path)
    tables = [t for t in soup.find_all("table") if "Player" in header_texts(t)]
    if not tables:
        raise ValueError("no Player Totals table found")
    parsed: list[list[dict]] = []
    for table in tables[:2]:
        headers = header_texts(table)
        records = []
        for values, cells in body_rows(table):
            if len(values) != len(headers) or not values[0] or values[0] == "Player":
                continue
            raw = dict(zip(headers, values))
            record = {
                "player_name_source": values[0],
                "player_name": source_name_to_display(values[0]),
                "player_url": player_href(cells[0]),
                "raw_row_json": json.dumps(raw, ensure_ascii=False, sort_keys=True),
            }
            for source, target in STAT_HEADERS.items():
                if source in raw:
                    record[target] = parse_number(raw[source], integer=False)
            if "GM" in raw:
                record["games"] = parse_number(raw["GM"], integer=True)
            records.append(record)
        parsed.append(records)
    totals = parsed[0]
    averages = parsed[1] if len(parsed) > 1 else []
    return totals, averages


def parse_wdl(value: str) -> tuple[int | None, int | None, int | None, int | None]:
    match = re.search(r"(\d+)\s*\((\d+)-(\d+)-(\d+)\)", clean_text(value))
    if not match:
        return parse_number(value, integer=True), None, None, None
    return tuple(int(v) for v in match.groups())  # type: ignore[return-value]


def parse_all_time(path: Path) -> list[dict]:
    soup = read_html(path)
    table = next((t for t in soup.find_all("table")
                  if "Games (W-D-L)" in header_texts(t)), None)
    if table is None:
        raise ValueError("no All Time Player List table found")
    headers = header_texts(table)
    records = []
    for values, cells in body_rows(table):
        if len(values) != len(headers) or len(values) < 11:
            continue
        raw = dict(zip(headers, values))
        games, wins, draws, losses = parse_wdl(raw.get("Games (W-D-L)", ""))
        records.append({
            "cap_number": parse_number(raw.get("Cap"), integer=True),
            "jumper_number": clean_text(raw.get("#")) or None,
            "player_name_source": raw.get("Player", ""),
            "player_name": source_name_to_display(raw.get("Player", "")),
            "player_url": player_href(cells[headers.index("Player")]),
            "dob": clean_text(raw.get("DOB")) or None,
            "height_cm": parse_number(raw.get("HT"), integer=True),
            "weight_kg": parse_number(raw.get("WT"), integer=True),
            "games": games, "wins": wins, "draws": draws, "losses": losses,
            "goals": parse_number(raw.get("Goals"), integer=True),
            "seasons_text": clean_text(raw.get("Seasons")) or None,
            "debut_age_text": clean_text(raw.get("Debut")) or None,
            "last_age_text": clean_text(raw.get("Last")) or None,
            "raw_row_json": json.dumps(raw, ensure_ascii=False, sort_keys=True),
        })
    return records


def table_rows(table) -> list[Any]:
    """Return this table's rows in document order across mixed sections.

    AFL Tables' raw response can contain a direct heading row followed by a
    ``tbody`` containing the header/data rows. Browser "save complete" output
    normalises all rows into one ``tbody``. Selecting every row whose nearest
    table is the current table supports both forms without absorbing rows from
    any nested table.
    """
    return [
        row for row in table.find_all("tr")
        if row.find_parent("table") is table
    ]


def row_cells(row) -> list[Any]:
    """Cells owned by one row, tolerant of legacy/malformed HTML."""
    direct = row.find_all(["th", "td"], recursive=False)
    if direct:
        return direct
    return [
        cell for cell in row.find_all(["th", "td"])
        if cell.find_parent("tr") is row
    ]


def heading_before(table) -> str:
    # Prefer an actual heading element. Legacy AFL Tables pages sometimes use
    # plain text or a bold element immediately before the table, so inspect a
    # small preceding-text window as a fallback and accept only a record title.
    heading = table.find_previous(["h1", "h2", "h3", "h4", "b", "strong"])
    if heading:
        text = clean_text(heading.get_text(" ", strip=True))
        if record_scope_and_stat(text)[0]:
            return text
    for node in table.find_all_previous(string=True, limit=30):
        text = clean_text(node)
        if record_scope_and_stat(text)[0]:
            return text
    return ""


def record_scope_and_stat(heading: str) -> tuple[str | None, str | None]:
    text = clean_text(heading)
    match = re.search(r"Most\s+(.+?)\s+In\s+A\s+(Season|Game)", text, re.I)
    if not match:
        return None, None
    stat = re.sub(r"[^a-z0-9]+", "_", match.group(1).casefold()).strip("_")
    return match.group(2).casefold(), stat


def first_present(raw: dict[str, str], *names: str) -> str | None:
    lowered = {k.casefold(): v for k, v in raw.items()}
    for name in names:
        value = lowered.get(name.casefold())
        if value not in (None, ""):
            return value
    return None


def _last_record_heading(text: str) -> str:
    """Return the final AFL Tables record title in a text window."""
    matches = list(re.finditer(
        r"Most\s+.+?\s+In\s+A\s+(?:Season|Game)",
        clean_text(text),
        flags=re.I,
    ))
    return clean_text(matches[-1].group(0)) if matches else ""


def record_table_fragments(path: Path) -> list[tuple[str, str]]:
    """Pair each record heading with the table that follows it.

    The browser-saved AFL Tables page puts ``Most ... In A Season/Game`` in
    the first table row. The live response fetched by ``urllib`` puts that
    title immediately *before* the table. Supporting only the first form made
    every downloaded records page validate but parse as zero rows.

    Each return item is ``(heading, isolated_table_html)``. Isolating table
    blocks keeps malformed legacy markup in one table from swallowing later
    tables, while the preceding text window supports the live page layout.
    """
    data = path.read_bytes()
    try:
        raw = data.decode("utf-8")
    except UnicodeDecodeError:
        raw = data.decode("windows-1252", errors="replace")

    blocks: list[tuple[str, str]] = []
    table_pattern = re.compile(r"<table\b[^>]*>.*?</table\s*>", re.I | re.S)
    for match in table_pattern.finditer(raw):
        fragment = match.group(0)
        table_text = clean_text(
            BeautifulSoup(fragment, "html.parser").get_text(" ", strip=True)
        )
        heading = _last_record_heading(table_text)

        # Live AFL Tables responses place the title before the table rather
        # than inside it. Use the closest preceding title, not the previous
        # table. A 12 KB window is comfortably larger than the title/link
        # wrapper but smaller than a complete preceding record table.
        if not heading:
            prefix = raw[max(0, match.start() - 12000):match.start()]
            prefix_text = BeautifulSoup(
                prefix, "html.parser"
            ).get_text(" ", strip=True)
            heading = _last_record_heading(prefix_text)

        if record_scope_and_stat(heading)[0]:
            blocks.append((heading, fragment))

    # Last-resort DOM walk for a page whose table closing tags are malformed.
    # ``heading_before`` searches the preceding DOM nodes and therefore also
    # handles the live title-before-table layout.
    if not blocks:
        soup = BeautifulSoup(raw, "html.parser")
        for table in soup.find_all("table"):
            table_text = clean_text(table.get_text(" ", strip=True))
            heading = _last_record_heading(table_text) or heading_before(table)
            if record_scope_and_stat(heading)[0]:
                blocks.append((heading, str(table)))
    return blocks


def parse_record_fragment(fragment: str, heading_hint: str = "") -> list[dict]:
    """Parse one isolated paired-column record table.

    AFL Tables omits several closing ``</td>`` tags in the raw response. The
    standard BeautifulSoup ``html.parser`` therefore nests later cells inside
    the first cell. Parse row/cell boundaries from start tags instead: every
    new ``<td>`` or ``<th>`` starts the next cell, whether or not the previous
    cell was explicitly closed.
    """

    def fragment_text(markup: str) -> str:
        return clean_text(
            BeautifulSoup(markup, "html.parser").get_text(" ", strip=True)
        )

    def fragment_cells(row_markup: str) -> list[dict[str, str | None]]:
        pattern = re.compile(
            r"<(?P<tag>th|td)\b(?P<attrs>[^>]*)>"
            r"(?P<body>.*?)(?=<(?:th|td)\b|</tr\s*>|$)",
            flags=re.I | re.S,
        )
        cells: list[dict[str, str | None]] = []
        for match in pattern.finditer(row_markup):
            body = match.group("body")
            href = re.search(
                r"<a\b[^>]*href\s*=\s*[\"']([^\"']+)",
                body,
                flags=re.I,
            )
            cells.append({
                "text": fragment_text(body),
                "href": href.group(1) if href else None,
            })
        return cells

    row_fragments = re.findall(
        r"<tr\b[^>]*>(.*?)</tr\s*>", fragment, flags=re.I | re.S
    )
    if not row_fragments:
        return []

    heading = clean_text(heading_hint)
    if not record_scope_and_stat(heading)[0]:
        for row_fragment in row_fragments[:5]:
            candidate = fragment_text(row_fragment)
            if record_scope_and_stat(candidate)[0]:
                heading = _last_record_heading(candidate)
                break
    scope, stat = record_scope_and_stat(heading)
    if not scope:
        return []

    header_row_index = None
    headers: list[str] = []
    for index, row_fragment in enumerate(row_fragments):
        cells = fragment_cells(row_fragment)
        candidate = [str(cell["text"] or "") for cell in cells]
        if any(value.casefold() == "player" for value in candidate):
            header_row_index = index
            headers = candidate
            break
    if header_row_index is None or not headers:
        return []

    player_positions = [
        index for index, value in enumerate(headers)
        if value.casefold() == "player"
    ]
    if not player_positions:
        return []

    groups: list[list[tuple[dict[str, str], list[dict[str, str | None]]]]] = [
        [] for _ in player_positions
    ]
    for row_fragment in row_fragments[header_row_index + 1:]:
        cells = fragment_cells(row_fragment)
        values = [str(cell["text"] or "") for cell in cells]
        if len(values) != len(headers):
            continue
        for group_index, offset in enumerate(player_positions):
            stop = (
                player_positions[group_index + 1]
                if group_index + 1 < len(player_positions)
                else len(headers)
            )
            segment_headers = headers[offset:stop]
            segment_values = values[offset:stop]
            segment_cells = cells[offset:stop]
            if segment_values and segment_values[0]:
                groups[group_index].append((
                    dict(zip(segment_headers, segment_values)),
                    segment_cells,
                ))

    source_rows = [item for group in groups for item in group]
    records: list[dict] = []
    for rank, (raw, cells) in enumerate(source_rows, start=1):
        player = first_present(raw, "Player")
        if not player:
            continue
        match_description = first_present(raw, "Match")
        season_value = first_present(raw, "Year", "Season")
        opponent = first_present(raw, "Opponent", "Opp")
        if match_description:
            year_match = re.search(r"\b(18|19|20)\d{2}\b", match_description)
            if year_match and not season_value:
                season_value = year_match.group(0)
            opponent_match = re.search(r"\bv\s+(.+)$", match_description, re.I)
            if opponent_match and not opponent:
                opponent = clean_text(opponent_match.group(1))
        value_text = first_present(raw, "#", "Total", "Value", "Stat", "Record")
        records.append({
            "scope": scope,
            "stat": stat,
            "source_heading": heading,
            "source_rank": rank,
            "player_name_source": player,
            "player_name": source_name_to_display(player),
            "player_url": cells[0].get("href") if cells else None,
            "source_team": first_present(raw, "TM", "Team", "Tm"),
            "value": parse_number(value_text),
            "games": parse_number(first_present(raw, "GM", "Games"), integer=True),
            "average": parse_number(first_present(raw, "Ave.", "Average", "Avg")),
            "season": parse_number(season_value, integer=True),
            "round": first_present(raw, "Round", "Rnd"),
            "opponent": opponent,
            "match_date": first_present(raw, "Date"),
            "match_description": match_description,
            "raw_row_json": json.dumps(raw, ensure_ascii=False, sort_keys=True),
        })
    return records


def parse_records(path: Path) -> list[dict]:
    """Parse every AFL Tables season/game record table from raw HTML."""
    fragments = record_table_fragments(path)
    records = [
        record
        for heading, fragment in fragments
        for record in parse_record_fragment(fragment, heading)
    ]
    if not records:
        data = path.read_bytes()
        try:
            raw = data.decode("utf-8")
        except UnicodeDecodeError:
            raw = data.decode("windows-1252", errors="replace")
        headings = len(re.findall(
            r"Most\s+.+?\s+In\s+A\s+(?:Season|Game)", raw,
            flags=re.I | re.S,
        ))
        tables = len(re.findall(r"<table\b", raw, flags=re.I))
        raise ValueError(
            "no Season/Game Record tables parsed "
            f"(source contains {headings} record headings and {tables} table tags)"
        )
    return records

def parse_wikipedia(path: Path) -> tuple[int | None, str | None, list[dict]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    parsed = payload.get("parse") or {}
    html = parsed.get("text") or ""
    # MediaWiki formatversion=1 wraps HTML in {"*": ...}; accept either form
    # so old caches remain importable if the API format is changed locally.
    if isinstance(html, dict):
        html = html.get("*") or ""
    soup = BeautifulSoup(str(html), "html.parser")
    table = next((
        candidate for candidate in soup.find_all("table")
        if "infobox" in (candidate.get("class") or [])
    ), None)
    fields: list[dict] = []
    if table:
        for row in table_rows(table):
            # Direct children avoid treating labels from nested mini-tables as
            # top-level club fields. Fall back for legacy malformed markup.
            cells = row_cells(row)
            label = next((cell for cell in cells if cell.name == "th"), None)
            value = next((cell for cell in cells if cell.name == "td"), None)
            if not label or not value:
                continue
            field_label = clean_text(label.get_text(" ", strip=True))
            key = re.sub(
                r"[^a-z0-9]+", "_", field_label.casefold()
            ).strip("_")
            if not key:
                continue
            fields.append({
                "field_key": key,
                "field_label": field_label,
                "field_value": clean_text(value.get_text(" ", strip=True)),
                "field_html": str(value),
            })
    return parsed.get("revid"), clean_text(parsed.get("displaytitle")), fields


DDL = """
CREATE TABLE IF NOT EXISTS clubs (
    club_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    abbreviation TEXT NOT NULL,
    db_club_now TEXT NOT NULL,
    wikipedia_title TEXT NOT NULL,
    wikipedia_url TEXT NOT NULL,
    afltables_slug TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS club_source_snapshots (
    club_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_path TEXT NOT NULL,
    fetched_at TEXT,
    sha256 TEXT NOT NULL,
    revision_id INTEGER,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (club_id, source_type)
);
CREATE TABLE IF NOT EXISTS club_wikipedia_fields (
    club_id TEXT NOT NULL,
    field_key TEXT NOT NULL,
    field_label TEXT NOT NULL,
    field_value TEXT,
    field_html TEXT,
    revision_id INTEGER,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (club_id, field_key)
);
CREATE TABLE IF NOT EXISTS club_player_totals (
    row_id INTEGER PRIMARY KEY,
    club_id TEXT NOT NULL, player_name_source TEXT NOT NULL,
    player_name TEXT NOT NULL, player_url TEXT, player_id INTEGER,
    match_status TEXT NOT NULL, candidate_count INTEGER NOT NULL,
    games INTEGER, kicks REAL, marks REAL, handballs REAL, disposals REAL,
    goals REAL, behinds REAL, hitouts REAL, tackles REAL, rebounds REAL,
    inside50s REAL, clearances REAL, clangers REAL, frees_for REAL,
    frees_against REAL, brownlow REAL, contested REAL, uncontested REAL,
    contested_marks REAL, marks_i50 REAL, one_percenters REAL, bounces REAL,
    goal_assists REAL, raw_row_json TEXT NOT NULL, imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS club_player_averages (
    row_id INTEGER PRIMARY KEY,
    club_id TEXT NOT NULL, player_name_source TEXT NOT NULL,
    player_name TEXT NOT NULL, player_url TEXT, player_id INTEGER,
    match_status TEXT NOT NULL, candidate_count INTEGER NOT NULL,
    games INTEGER, kicks REAL, marks REAL, handballs REAL, disposals REAL,
    goals REAL, behinds REAL, hitouts REAL, tackles REAL, rebounds REAL,
    inside50s REAL, clearances REAL, clangers REAL, frees_for REAL,
    frees_against REAL, brownlow REAL, contested REAL, uncontested REAL,
    contested_marks REAL, marks_i50 REAL, one_percenters REAL, bounces REAL,
    goal_assists REAL, raw_row_json TEXT NOT NULL, imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS club_player_register (
    row_id INTEGER PRIMARY KEY,
    club_id TEXT NOT NULL, cap_number INTEGER, jumper_number TEXT,
    player_name_source TEXT NOT NULL, player_name TEXT NOT NULL,
    player_url TEXT, player_id INTEGER, match_status TEXT NOT NULL,
    candidate_count INTEGER NOT NULL, dob TEXT, height_cm INTEGER,
    weight_kg INTEGER, games INTEGER, wins INTEGER, draws INTEGER,
    losses INTEGER, goals INTEGER, seasons_text TEXT, debut_age_text TEXT,
    last_age_text TEXT, raw_row_json TEXT NOT NULL, imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS club_player_records (
    record_id INTEGER PRIMARY KEY,
    club_id TEXT NOT NULL, scope TEXT NOT NULL, stat TEXT NOT NULL,
    source_heading TEXT NOT NULL, source_rank INTEGER NOT NULL,
    player_name_source TEXT NOT NULL, player_name TEXT NOT NULL,
    player_url TEXT, player_id INTEGER, match_status TEXT NOT NULL,
    candidate_count INTEGER NOT NULL, source_team TEXT, value REAL,
    games INTEGER, average REAL, season INTEGER, round TEXT,
    opponent TEXT, match_date TEXT, match_description TEXT,
    raw_row_json TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_club_totals_player ON club_player_totals(player_id);
CREATE INDEX IF NOT EXISTS ix_club_register_player ON club_player_register(player_id);
CREATE INDEX IF NOT EXISTS ix_club_records_player ON club_player_records(player_id);
CREATE INDEX IF NOT EXISTS ix_club_records_lookup ON club_player_records(club_id, scope, stat);
"""


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def make_name_key(value: str) -> str:
    # Use one source-independent key on both sides. The project's stored
    # ``name_key`` includes spaces, while saved AFL Tables names arrive as
    # ``Surname, Given``; recomputing avoids format drift between builds.
    key = fallback_name_key(source_name_to_display(value))
    # Explicit source spelling corrections are deliberately narrow.
    aliases = {"jonathonross": "jonathanross"}
    return aliases.get(key, key)


def build_link_index(con: sqlite3.Connection, club) -> dict[str, list[dict]]:
    if not table_exists(con, "players"):
        return {}
    columns = {r[1] for r in con.execute("PRAGMA table_info(players)")}
    select = ["player_id", "player"]
    for optional in ("name_key", "dob", "birth_year", "clubs_now",
                     "debut_season", "final_season"):
        if optional in columns:
            select.append(optional)
    rows = [dict(zip(select, row)) for row in con.execute(
        f"SELECT {', '.join(select)} FROM players"
    )]
    eligible_ids: set[int] | None = None
    if table_exists(con, "games"):
        game_cols = {r[1] for r in con.execute("PRAGMA table_info(games)")}
        if {"player_id", "club_now"} <= game_cols:
            eligible_ids = {r[0] for r in con.execute(
                "SELECT DISTINCT player_id FROM games WHERE club_now=?",
                (club.db_club_now,),
            )}
    out: dict[str, list[dict]] = {}
    for row in rows:
        if eligible_ids is not None and row["player_id"] not in eligible_ids:
            continue
        key = make_name_key(row["player"])
        out.setdefault(key, []).append(row)
    return out


def link_record(record: dict, index: dict[str, list[dict]]) -> None:
    candidates = list(index.get(make_name_key(record["player_name"]), []))
    season = record.get("season")
    if season is not None and len(candidates) > 1:
        in_era = [
            candidate for candidate in candidates
            if (candidate.get("debut_season") is None
                or int(candidate["debut_season"]) <= int(season))
            and (candidate.get("final_season") is None
                 or int(candidate["final_season"]) >= int(season))
        ]
        if in_era:
            candidates = in_era
    dob = record.get("dob")
    if dob and len(candidates) > 1:
        exact = [c for c in candidates if clean_text(c.get("dob")) == clean_text(dob)]
        if exact:
            candidates = exact
        else:
            year = parse_number(dob, integer=True)
            by_year = [c for c in candidates if c.get("birth_year") == year]
            if by_year:
                candidates = by_year
    record["candidate_count"] = len(candidates)
    if len(candidates) == 1:
        record["player_id"] = candidates[0]["player_id"]
        record["match_status"] = "unique"
    elif not candidates:
        record["player_id"] = None
        record["match_status"] = "unmatched"
    else:
        record["player_id"] = None
        record["match_status"] = "ambiguous"


def insert_dicts(con: sqlite3.Connection, table: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    columns = list(rows[0])
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
        f"({', '.join('?' for _ in columns)})"
    )
    con.executemany(sql, [[row.get(col) for col in columns] for row in rows])
    return len(rows)


def source_meta(path: Path, source_type: str) -> dict:
    meta_path = path.parent / "sources.metadata.json"
    if meta_path.exists():
        try:
            return (json.loads(meta_path.read_text(encoding="utf-8"))
                    .get(source_type, {}))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def load(db_path: Path, raw_dir: Path, club_ids: list[str] | None = None,
         *, verbose: bool = True, details: bool = False) -> dict:
    clubs = selected_clubs(club_ids)
    imported_at = utc_now()
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    summary = {
        "clubs": 0, "wikipedia_fields": 0, "totals": 0, "averages": 0,
        "register": 0, "records": 0, "missing_files": [], "errors": [],
        "links": {"unique": 0, "ambiguous": 0, "unmatched": 0},
        "cross_source_mismatches": 0,
    }
    try:
        con.execute("PRAGMA foreign_keys=OFF")
        con.executescript(DDL)
        con.execute("BEGIN")
        selected_ids = [c.club_id for c in clubs]
        marks = ",".join("?" for _ in selected_ids)
        for table in (
            "club_source_snapshots", "club_wikipedia_fields",
            "club_player_totals", "club_player_averages",
            "club_player_register", "club_player_records",
        ):
            con.execute(f"DELETE FROM {table} WHERE club_id IN ({marks})", selected_ids)

        for club in clubs:
            con.execute(
                "INSERT OR REPLACE INTO clubs VALUES (?,?,?,?,?,?,?,?,?)",
                (club.club_id, club.name, club.abbreviation, club.db_club_now,
                 club.wikipedia_title, club.wikipedia_url, club.afltables_slug,
                 1, imported_at),
            )
            summary["clubs"] += 1
            index = build_link_index(con, club)
            parsers = {
                "wikipedia": parse_wikipedia,
                "afltables_player_totals": parse_player_totals,
                "afltables_all_time": parse_all_time,
                "afltables_records": parse_records,
            }
            for source_type, parser in parsers.items():
                path = source_path(raw_dir, club, source_type)
                if not path.exists():
                    summary["missing_files"].append(str(path))
                    continue
                try:
                    meta = source_meta(path, source_type)
                    revision_id = meta.get("revision_id")
                    con.execute(
                        "INSERT INTO club_source_snapshots VALUES (?,?,?,?,?,?,?,?)",
                        (club.club_id, source_type,
                         meta.get("source_url") or {
                             "wikipedia": club.wikipedia_url,
                             "afltables_player_totals": club.afltables_player_totals_url,
                             "afltables_records": club.afltables_records_url,
                             "afltables_all_time": club.afltables_all_time_url,
                         }[source_type], str(path), meta.get("fetched_at"),
                         sha256_file(path), revision_id, imported_at),
                    )
                    if source_type == "wikipedia":
                        revision_id, _, rows = parser(path)
                        for row in rows:
                            row.update(club_id=club.club_id,
                                       revision_id=revision_id,
                                       imported_at=imported_at)
                        summary["wikipedia_fields"] += insert_dicts(
                            con, "club_wikipedia_fields", rows)
                    elif source_type == "afltables_player_totals":
                        totals, averages = parser(path)
                        for collection, table, key in (
                            (totals, "club_player_totals", "totals"),
                            (averages, "club_player_averages", "averages"),
                        ):
                            for row in collection:
                                link_record(row, index)
                                summary["links"][row["match_status"]] += 1
                                row.update(club_id=club.club_id,
                                           imported_at=imported_at)
                            summary[key] += insert_dicts(con, table, collection)
                    elif source_type == "afltables_all_time":
                        rows = parser(path)
                        for row in rows:
                            link_record(row, index)
                            summary["links"][row["match_status"]] += 1
                            row.update(club_id=club.club_id,
                                       imported_at=imported_at)
                        summary["register"] += insert_dicts(
                            con, "club_player_register", rows)
                    else:
                        rows = parser(path)
                        for row in rows:
                            link_record(row, index)
                            summary["links"][row["match_status"]] += 1
                            row.update(club_id=club.club_id,
                                       imported_at=imported_at)
                        summary["records"] += insert_dicts(
                            con, "club_player_records", rows)
                except Exception as exc:
                    summary["errors"].append(
                        f"{club.club_id}/{source_type}: {type(exc).__name__}: {exc}"
                    )
                    if details:
                        print(f"ERROR {summary['errors'][-1]}", file=sys.stderr)
        mismatch_sql = f"""
            SELECT COUNT(*)
            FROM club_player_totals t
            JOIN club_player_register r
              ON r.club_id=t.club_id
             AND ((t.player_url IS NOT NULL AND r.player_url=t.player_url)
                  OR (t.player_url IS NULL AND r.player_url IS NULL
                      AND r.player_name_source=t.player_name_source))
            WHERE t.club_id IN ({marks})
              AND (COALESCE(t.games,-1)<>COALESCE(r.games,-1)
                   OR COALESCE(t.goals,-1)<>COALESCE(r.goals,-1))
        """
        summary["cross_source_mismatches"] = con.execute(
            mismatch_sql, selected_ids
        ).fetchone()[0]
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    if verbose:
        print_summary(summary, db_path)
    return summary


def print_summary(summary: dict, db_path: Path) -> None:
    print(f"\nClub data loaded into {db_path}")
    print(f"  clubs              {summary['clubs']:8,}")
    print(f"  Wikipedia fields   {summary['wikipedia_fields']:8,}")
    print(f"  player totals      {summary['totals']:8,}")
    print(f"  player averages    {summary['averages']:8,}")
    print(f"  player register    {summary['register']:8,}")
    print(f"  season/game records{summary['records']:8,}")
    print("  links")
    for key, value in summary["links"].items():
        print(f"    {key:16} {value:8,}")
    print(f"  missing files      {len(summary['missing_files']):8,}")
    print(f"  parse errors       {len(summary['errors']):8,}")
    print(f"  total/register diff{summary['cross_source_mismatches']:8,}")


def refresh_default(db_path: str | Path | None = None,
                    verbose: bool = True) -> bool:
    target_db = Path(db_path) if db_path is not None else DEFAULT_DB
    if not DEFAULT_RAW_DIR.exists() or not target_db.exists():
        return False
    available = any(source_path(DEFAULT_RAW_DIR, club, "wikipedia").exists()
                    for club in CLUBS)
    if not available:
        return False
    load(target_db, DEFAULT_RAW_DIR, verbose=verbose)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    ap.add_argument("--club", action="append", dest="clubs")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--details", action="store_true")
    args = ap.parse_args(argv)
    if not args.db.exists():
        ap.error(f"database not found: {args.db}")
    try:
        summary = load(args.db, args.raw_dir, args.clubs,
                       verbose=True, details=args.details)
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.details:
        for path in summary["missing_files"]:
            print(f"missing  {path}")
        for error in summary["errors"]:
            print(f"error    {error}")
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
