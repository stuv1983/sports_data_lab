#!/usr/bin/env python3
"""Import and conservatively link VFL/AFL club-captain records.

The importer keeps source text and audit metadata, but only exposes rows that
resolve to one AFL player. Identity matching handles punctuation, suffixes and
a small reviewed alias table; club and career evidence are still required.
Rows that remain ambiguous or unsupported never receive a ``player_id``.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import hashlib
import os
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from data_paths import captaincy_sources, default_db

HEADER_ALIASES = {
    "season": {"season", "year", "yr"},
    "club": {"club", "team", "side"},
    "player": {"player", "name", "captain", "player name"},
    "role": {"role", "position", "captaincy role", "type"},
    "source_url": {"source url", "source_url", "url"},
    "player_url": {"player url", "player_url", "wikipedia player url"},
    "source_page": {"source page", "source_page", "page"},
    "source_revision": {"source revision", "source_revision", "revision", "revid"},
    "source_period": {"source period", "source_period", "period", "years"},
    "source_notes": {"source notes", "source_notes", "notes"},
}
REQUIRED = {"season", "club", "player"}
TRUSTED_STATUSES = {"unique", "resolved"}
LINK_STATUSES = ("unique", "resolved", "ambiguous", "unmatched",
                 "unsupported_role")

# Source display names that differ from the AFL Tables display name.  These
# aliases only nominate an identity key; a link is still accepted only when
# one player also satisfies the club and career evidence.
NAME_ALIASES = {
    "willstuckey": "Bill Stuckey",
    "billstrickland": "Billy Strickland",
    "robertnash": "Bob Nash",
    "charlietyson": "Charles Tyson",
    "albypannam": "Albert Pannam",
    "williamrobinson": "Bill Robinson",
    "williamgriffith": "Billy Griffith",
    "davidsmith": "Dave Smith",
    "williamcmcclelland": "Bill McClelland",
    "albertchadwick": "Bert Chadwick",
    "johnnylewis": "John Lewis",
    "almantello": "Albert Mantello",
    "johnlawson": "Ivor Lawson",
    "hughjames": "Hughie James",
    "cyrillilburne": "Dooley Lilburne",
    "percybentley": "Perce Bentley",
    "herbhowson": "Bert Howson",
    "williamthomas": "Bill Thomas",
    "charliestanbridge": "Charles Stanbridge",
    "normanware": "Norm Ware",
}

# Reviewed source rows where the season/player pairing is wrong.  The source
# text remains in the audit table and the correction is recorded in ``notes``.
RECORD_OVERRIDES = {
    (1969, "melbourne", "hassamann"): "Tassie Johnson",
    (1970, "melbourne", "robertjohnson"): "Frank Davis",
    (2016, "essendon", "jobewatson"): "Brendon Goddard",
    (1926, "sydney", "charliepannam"): "Charles Pannam",
    (1927, "sydney", "charliepannam"): "Charles Pannam",
}

_SUFFIXES = {"jr", "junior", "sr", "senior", "ii", "iii", "iv"}


def clean_text(value: object) -> str:
    """Collapse Unicode and ordinary whitespace without changing wording."""
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip()


def text_key(value: object) -> str:
    """Readable normalised key used for headings, clubs and exact names."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("’", "'").replace("`", "'")
    text = re.sub(
        r"\((?:\s*c\s*|[^)]*(?:co[- ]captain|acting captain|captain)[^)]*)\)",
        " ", text, flags=re.I,
    )
    text = re.sub(r"\b(?:co[- ]captain|acting captain|captain)\b", " ",
                  text, flags=re.I)
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text).lower()
    return " ".join(text.split())


def identity_key(value: object) -> str:
    """Compact person key tolerant of apostrophes, initials and suffixes."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("’", "'").replace("`", "'")
    text = re.sub(
        r"\((?:\s*c\s*|[^)]*(?:co[- ]captain|acting captain|captain)[^)]*)\)",
        " ", text, flags=re.I,
    )
    text = re.sub(r"\b(?:co[- ]captain|acting captain|captain)\b", " ",
                  text, flags=re.I)
    text = re.sub(r"\b(?:jr|junior|sr|senior|ii|iii|iv)\.?\s*$",
                  " ", text, flags=re.I)
    return re.sub(r"[^a-zA-Z0-9]+", "", text).lower()


def split_player_names(value: object) -> list[str]:
    """Split a plain-text co-captain cell while preserving name suffixes."""
    text = clean_text(value)
    if not text:
        return []

    primary = [part.strip() for part in re.split(
        r"\s*(?:;|/|\band\b|\s&\s)\s*", text, flags=re.I
    ) if part.strip()]
    result: list[str] = []
    for part in primary:
        pieces = [piece.strip() for piece in part.split(",") if piece.strip()]
        if len(pieces) <= 1:
            result.append(part)
            continue
        if len(pieces) == 2 and text_key(pieces[1]) in _SUFFIXES:
            result.append(part)
        else:
            result.extend(pieces)
    return result


def is_captain_role(value: object) -> bool:
    """Accept captain/co-captain/acting captain, not vice/deputy roles."""
    role = unicodedata.normalize("NFKD", str(value or ""))
    role = "".join(ch for ch in role if not unicodedata.combining(ch))
    role = re.sub(r"[^a-zA-Z0-9]+", " ", role).lower()
    role = " ".join(role.split())
    return ("captain" in role and "vice captain" not in role
            and "deputy captain" not in role)


def _header_map(fieldnames: list[str]) -> dict[str, str]:
    normalised = {text_key(heading): heading for heading in fieldnames
                  if heading is not None}
    mapped: dict[str, str] = {}
    for logical, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            hit = normalised.get(text_key(alias))
            if hit is not None:
                mapped[logical] = hit
                break
    missing = REQUIRED - mapped.keys()
    if missing:
        raise ValueError("missing required columns: " + ", ".join(sorted(missing)))
    return mapped


def _field(source: dict, mapping: dict[str, str], logical: str) -> str:
    heading = mapping.get(logical)
    return clean_text(source.get(heading)) if heading else ""


def _source_row_id(row: dict) -> str:
    """Stable identity for one source appointment, independent of file path.

    Duplicate copies of the same scraped CSV can coexist during migration.
    The source URL/page/revision remain part of the identity, so genuinely
    independent source records are retained while byte-for-byte copies are
    collapsed.
    """
    parts = (
        str(row["season"]), text_key(row["club"]), identity_key(row["player"]),
        text_key(row["role"]), row.get("source_url", ""),
        row.get("player_url", ""), row.get("source_page", ""),
        str(row.get("source_revision") or ""), row.get("source_period", ""),
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def read_csvs(paths: list[str | Path]) -> list[dict]:
    """Read captain CSVs, split co-captains and collapse duplicate copies."""
    rows: list[dict] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = Path(raw_path)
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise ValueError(f"{path}: no header row")
            mapping = _header_map(reader.fieldnames)
            for line_no, source in enumerate(reader, start=2):
                season_text = _field(source, mapping, "season")
                match = re.search(r"(?:18|19|20)\d{2}", season_text)
                if not match:
                    raise ValueError(
                        f"{path}:{line_no}: invalid season {season_text!r}"
                    )
                season = int(match.group(0))
                club = _field(source, mapping, "club")
                raw_player = _field(source, mapping, "player")
                players = split_player_names(raw_player)
                if not club or not players:
                    raise ValueError(f"{path}:{line_no}: club/player cannot be blank")

                shared = {
                    "season": season,
                    "club": club,
                    "role": _field(source, mapping, "role") or "Captain",
                    "source_url": _field(source, mapping, "source_url"),
                    "player_url": _field(source, mapping, "player_url"),
                    "source_page": _field(source, mapping, "source_page"),
                    "source_revision": None,
                    "source_period": _field(source, mapping, "source_period"),
                    "source_notes": _field(source, mapping, "source_notes"),
                    "source_name": path.name,
                }
                revision = _field(source, mapping, "source_revision")
                if revision.isdigit():
                    shared["source_revision"] = int(revision)

                for player in players:
                    row = {"player": player, **shared}
                    row["source_row_id"] = _source_row_id(row)
                    if row["source_row_id"] in seen:
                        continue
                    seen.add(row["source_row_id"])
                    rows.append(row)
    return rows


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def load_reference_maps(con: sqlite3.Connection):
    if not {"player_id", "player"} <= table_columns(con, "players"):
        raise RuntimeError("players table lacks player_id/player")

    names: dict[str, set[int]] = defaultdict(set)
    player_names: dict[int, str] = {}
    spans: dict[int, tuple[int, int]] = {}
    for pid, name, debut, final in con.execute(
        "SELECT player_id, player, debut_season, final_season FROM players"
    ):
        pid = int(pid)
        names[identity_key(name)].add(pid)
        player_names[pid] = str(name)
        if debut is not None and final is not None:
            spans[pid] = (int(debut), int(final))

    active: dict[tuple[int, str], set[int]] = defaultdict(set)
    club_career: dict[str, set[int]] = defaultdict(set)
    for pid, season, current, historical in con.execute(
        "SELECT DISTINCT player_id, season, club_now, club_hist FROM games"
    ):
        for club in {current, historical}:
            if not club:
                continue
            key = text_key(club)
            active[(int(season), key)].add(int(pid))
            club_career[key].add(int(pid))
    return names, player_names, active, club_career, spans


CAPTAIN_COLUMNS_SQL = """
    captaincy_id INTEGER PRIMARY KEY,
    source_row_id TEXT NOT NULL UNIQUE,
    season INTEGER NOT NULL,
    club TEXT NOT NULL,
    player TEXT NOT NULL,
    role TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    player_url TEXT NOT NULL DEFAULT '',
    source_page TEXT NOT NULL DEFAULT '',
    source_revision INTEGER,
    source_period TEXT NOT NULL DEFAULT '',
    source_notes TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL,
    player_id INTEGER,
    match_status TEXT NOT NULL,
    candidate_count INTEGER NOT NULL,
    notes TEXT,
    imported_at TEXT NOT NULL
"""


def _dedupe_rows(rows: list[dict]) -> tuple[list[dict], int]:
    """Protect direct callers as well as CSV imports from duplicate IDs."""
    unique: list[dict] = []
    seen: dict[str, tuple] = {}
    duplicate_count = 0
    signature_fields = (
        "season", "club", "player", "role", "source_url", "player_url",
        "source_page", "source_revision", "source_period", "source_notes",
    )
    for original in rows:
        row = dict(original)
        row_id = row.get("source_row_id") or _source_row_id(row)
        signature = tuple(row.get(field) for field in signature_fields)
        if row_id in seen:
            if seen[row_id] == signature:
                duplicate_count += 1
                continue
            # A caller supplied the same opaque ID for different records.
            # Preserve both by replacing only the conflicting identifier.
            row_id = hashlib.sha256(
                (str(row_id) + "|" + repr(signature)).encode("utf-8")
            ).hexdigest()[:24]
            while row_id in seen:
                row_id = hashlib.sha256((row_id + "|").encode("utf-8")).hexdigest()[:24]
        row["source_row_id"] = row_id
        seen[row_id] = signature
        unique.append(row)
    return unique, duplicate_count


def _prepare_stage(con: sqlite3.Connection) -> None:
    con.execute("DROP TABLE IF EXISTS captaincies_import")
    con.execute(f"CREATE TABLE captaincies_import ({CAPTAIN_COLUMNS_SQL})")


def _publish_stage(con: sqlite3.Connection) -> None:
    con.execute("DROP TABLE IF EXISTS captaincies")
    con.execute("ALTER TABLE captaincies_import RENAME TO captaincies")
    con.execute(
        "CREATE INDEX idx_captaincies_player "
        "ON captaincies(player_id, match_status)"
    )
    con.execute(
        "CREATE INDEX idx_captaincies_club_season "
        "ON captaincies(club, season)"
    )
    con.execute(
        "CREATE INDEX idx_captaincies_status ON captaincies(match_status)"
    )


def _candidate_key(row: dict) -> tuple[str, str]:
    source_key = identity_key(row["player"])
    override = RECORD_OVERRIDES.get(
        (row["season"], text_key(row["club"]), source_key)
    )
    if override:
        return identity_key(override), f"reviewed record correction: {override}"
    alias = NAME_ALIASES.get(source_key)
    if alias:
        return identity_key(alias), f"reviewed name alias: {alias}"
    return source_key, "exact identity"


def _choose_candidates(row: dict, references):
    names, player_names, active, club_career, spans = references
    candidate_key, method = _candidate_key(row)
    by_name = names.get(candidate_key, set())
    club_key = text_key(row["club"])
    same_season = sorted(by_name & active.get((row["season"], club_key), set()))
    same_club = by_name & club_career.get(club_key, set())
    career = sorted(
        pid for pid in same_club
        if pid in spans and spans[pid][0] - 1 <= row["season"] <= spans[pid][1] + 1
    )

    if len(same_season) == 1:
        status = "unique" if method == "exact identity" else "resolved"
        return same_season[0], status, same_season, method + " + club-season"
    if len(same_season) > 1:
        return None, "ambiguous", same_season, method + ": multiple club-season matches"
    if len(career) == 1:
        return career[0], "resolved", career, method + " + club career window"
    if len(career) > 1:
        return None, "ambiguous", career, method + ": multiple club/career matches"

    if by_name:
        note = method + ": identity found, but club/career evidence does not match"
    else:
        note = method + ": identity not found in players"
    return None, "unmatched", [], note


def import_rows(con: sqlite3.Connection, rows: list[dict]) -> Counter:
    """Link and atomically replace captaincies only after every row succeeds."""
    references = load_reference_maps(con)
    player_names = references[1]
    imported_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    totals: Counter = Counter()
    rows, duplicate_count = _dedupe_rows(rows)

    try:
        con.execute("SAVEPOINT captaincy_import")
        _prepare_stage(con)
        for row in rows:
            if not is_captain_role(row["role"]):
                player_id, status, candidates = None, "unsupported_role", []
                notes = "role is not captain/co-captain/acting captain"
            else:
                player_id, status, candidates, notes = _choose_candidates(
                    row, references
                )
                if player_id is not None:
                    notes += f" -> {player_names[player_id]}"
                elif candidates:
                    notes += ": " + ", ".join(
                        player_names[pid] for pid in candidates[:8]
                    )

            con.execute("""
                INSERT INTO captaincies_import (
                    source_row_id, season, club, player, role, source_url,
                    player_url, source_page, source_revision, source_period,
                    source_notes, source_name, player_id, match_status,
                    candidate_count, notes, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row["source_row_id"], row["season"], row["club"], row["player"],
                row["role"], row["source_url"], row["player_url"],
                row["source_page"], row["source_revision"], row["source_period"],
                row["source_notes"], row["source_name"], player_id, status,
                len(candidates), notes, imported_at,
            ))
            totals[status] += 1
        _publish_stage(con)
        con.execute("RELEASE SAVEPOINT captaincy_import")
    except Exception:
        con.execute("ROLLBACK TO SAVEPOINT captaincy_import")
        con.execute("RELEASE SAVEPOINT captaincy_import")
        raise

    totals["duplicate_source_rows_ignored"] = duplicate_count
    return totals


def report(con: sqlite3.Connection, details: bool = False) -> None:
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='captaincies'"
    ).fetchone()
    if not exists:
        print("captaincies table does not exist")
        return

    print("Captaincy link report")
    for status, count in con.execute(
        "SELECT match_status, COUNT(*) FROM captaincies "
        "GROUP BY match_status ORDER BY COUNT(*) DESC"
    ):
        print(f"  {status:<18} {count:>6,}")
    trusted = con.execute(
        "SELECT COUNT(*) FROM captaincies WHERE player_id IS NOT NULL "
        "AND match_status IN ('unique','resolved')"
    ).fetchone()[0]
    print(f"  {'trusted':<18} {trusted:>6,}")

    if not details:
        return
    unresolved = con.execute("""
        SELECT season, club, player, match_status, notes
        FROM captaincies
        WHERE match_status NOT IN ('unique','resolved')
        ORDER BY club, season, player
    """).fetchall()
    if not unresolved:
        print("\nNo unresolved captaincy rows.")
        return
    print(f"\nUnresolved rows: {len(unresolved):,}")
    for season, club, player, status, notes in unresolved:
        print(f"  {season} | {club:<18} | {player:<28} | {status}: {notes}")


def suggest(con: sqlite3.Connection, limit: int = 5) -> None:
    """Show likely same-club names for unresolved rows without auto-linking."""
    player_rows = con.execute(
        "SELECT player, debut_season, final_season, clubs_hist, clubs_now FROM players"
    ).fetchall()
    unresolved = con.execute("""
        SELECT season, club, player FROM captaincies
        WHERE match_status='unmatched' ORDER BY club, season
    """).fetchall()
    for season, club, player in unresolved:
        club_key = text_key(club)
        candidates = []
        for name, debut, final, hist, current in player_rows:
            clubs = {text_key(item) for value in (hist, current)
                     for item in str(value or "").split("|") if item}
            if club_key not in clubs:
                continue
            score = difflib.SequenceMatcher(
                None, identity_key(player), identity_key(name)
            ).ratio()
            if score >= 0.55:
                candidates.append((score, name, debut, final))
        candidates.sort(reverse=True)
        formatted = ", ".join(
            f"{name} ({debut}-{final}, {score:.2f})"
            for score, name, debut, final in candidates[:limit]
        ) or "no close same-club names"
        print(f"{season} | {club} | {player} -> {formatted}")


def inspect(rows: list[dict]) -> None:
    print(f"Rows: {len(rows):,}")
    if not rows:
        return
    seasons = [row["season"] for row in rows]
    print(f"Seasons: {min(seasons)}-{max(seasons)}")
    print(f"Clubs: {len({row['club'] for row in rows}):,}")
    print(f"Players: {len({identity_key(row['player']) for row in rows}):,}")


def _default_sources() -> list[Path]:
    sources = captaincy_sources("afl")
    if not sources:
        raise FileNotFoundError(
            "no captaincy CSV found; run scrape_afl_captains.py or provide a CSV"
        )
    return sources


def refresh_default(db_path: str | None = None, verbose: bool = True):
    paths = _default_sources()
    rows = read_csvs(paths)
    con = sqlite3.connect(db_path or default_db("afl"))
    try:
        totals = import_rows(con, rows)
    finally:
        con.close()
    if verbose:
        print(f"Imported {sum(totals[s] for s in LINK_STATUSES):,} captaincy rows")
        for status in LINK_STATUSES:
            if totals[status]:
                print(f"  {status:<18} {totals[status]:>6,}")
        if totals["duplicate_source_rows_ignored"]:
            print(f"  {'duplicates ignored':<18} "
                  f"{totals['duplicate_source_rows_ignored']:>6,}")
    return totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", nargs="*", help="captaincy CSV files")
    parser.add_argument("--db", default=default_db("afl"))
    parser.add_argument("--inspect", action="store_true", help="validate CSV only")
    parser.add_argument("--report", action="store_true", help="show current link counts")
    parser.add_argument("--details", action="store_true",
                        help="include every unresolved row in the report")
    parser.add_argument("--suggest", action="store_true",
                        help="show close same-club names for unresolved rows")
    args = parser.parse_args(argv)

    try:
        if args.report or args.suggest:
            con = sqlite3.connect(args.db)
            try:
                if args.report:
                    report(con, details=args.details)
                if args.suggest:
                    suggest(con)
            finally:
                con.close()
            return 0

        paths = [Path(path) for path in args.csv] or _default_sources()
        rows = read_csvs(paths)
        if args.inspect:
            inspect(rows)
            return 0
        if not os.path.exists(args.db):
            raise FileNotFoundError(f"database not found: {args.db}")

        con = sqlite3.connect(args.db)
        try:
            totals = import_rows(con, rows)
        finally:
            con.close()
        print(f"Imported {sum(totals[s] for s in LINK_STATUSES):,} captaincy rows into {args.db}")
        for status in LINK_STATUSES:
            print(f"  {status:<18} {totals[status]:>6,}")
        if totals["duplicate_source_rows_ignored"]:
            print(f"  {'duplicates ignored':<18} "
                  f"{totals['duplicate_source_rows_ignored']:>6,}")
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
