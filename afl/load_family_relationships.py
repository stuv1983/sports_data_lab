#!/usr/bin/env python3
"""Import and conservatively link Wikipedia family members to AFL players.

Input files are produced by ``afl/scrape_wikipedia_families.py``.  Every member is
linked independently against the local ``players``/``games`` tables.  Exact
normalised names are trusted only when club evidence does not contradict the
candidate; same-name players require one clear club match.  Ambiguous and
unmatched members remain in the database for audit and are excluded from all
search/grid constraints.

Tables written:

* ``family_members`` -- source person rows plus player-link status;
* ``family_relationships`` -- explicit source relationships, joined through
  ``family_members`` when queried.

Examples:
    python -m afl.load_family_relationships --inspect
    python -m afl.load_family_relationships --db gridley.db
    python -m afl.load_family_relationships --db gridley.db --report --details
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from data_paths import (
        default_db,
        family_member_sources,
        family_relationship_sources,
    )
except ImportError:  # standalone bundle validation
    def default_db(sport_key: str) -> str:
        root = Path(__file__).resolve().parent
        modern = root / "data" / sport_key / f"{sport_key}.db"
        return str(modern if modern.exists() else root / "gridley.db")

    def family_member_sources(sport_key: str = "afl") -> list[Path]:
        path = Path("data") / sport_key / "raw" / "wikipedia_family_members.csv"
        return [path] if path.exists() else []

    def family_relationship_sources(sport_key: str = "afl") -> list[Path]:
        path = (
            Path("data") / sport_key / "raw" /
            "wikipedia_family_relationships.csv"
        )
        return [path] if path.exists() else []

TRUSTED_STATUSES = {"unique", "resolved"}
MEMBER_REQUIRED = {
    "source_member_id",
    "family_key",
    "family_name",
    "member_name",
    "clubs_raw",
    "source_url",
}
RELATION_REQUIRED = {
    "source_relationship_id",
    "family_key",
    "person_a_source_member_id",
    "person_b_source_member_id",
    "relationship_type",
    "relationship_label",
    "confidence",
}

_SUFFIX_RE = re.compile(r"\b(?:jr|jnr|junior|sr|snr|senior|ii|iii|iv)\.?\s*$", re.I)

# Historical/source spelling differences that otherwise break strong club
# disambiguation.  Both source and database values are converted through this.
CLUB_ALIASES = {
    "brisbane": "brisbane lions",
    "brisbane bears": "brisbane bears",
    "brisbane lions": "brisbane lions",
    "foot scray": "western bulldogs",
    "footscray": "western bulldogs",
    "kangaroos": "north melbourne",
    "north melbourne kangaroos": "north melbourne",
    "south melbourne": "sydney",
    "sydney swans": "sydney",
    "greater western sydney": "gws",
    "greater western sydney giants": "gws",
    "western bulldogs": "western bulldogs",
}


def clean_text(value: object) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip()


def _ascii_words(value: object) -> list[str]:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("’", "'").replace("`", "'")
    text = _SUFFIX_RE.sub(" ", text)
    return re.findall(r"[A-Za-z0-9]+", text.lower())


def identity_key(value: object) -> str:
    return "".join(_ascii_words(value))


def relaxed_identity_key(value: object) -> str:
    words = _ascii_words(value)
    if len(words) > 2:
        words = [
            word
            for index, word in enumerate(words)
            if not (0 < index < len(words) - 1 and len(word) == 1)
        ]
    return "".join(words)


def text_key(value: object) -> str:
    return " ".join(_ascii_words(value))


def club_key(value: object) -> str:
    key = text_key(value)
    return CLUB_ALIASES.get(key, key)


def optional_int(value: object) -> int | None:
    text = clean_text(value)
    if not text:
        return None
    match = re.search(r"-?\d+", text)
    return int(match.group(0)) if match else None


def read_csvs(paths: list[str | Path], required: set[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    id_field = (
        "source_member_id" if "source_member_id" in required
        else "source_relationship_id"
    )
    for raw_path in paths:
        path = Path(raw_path)
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise ValueError(f"{path}: no header row")
            missing = required - set(reader.fieldnames)
            if missing:
                raise ValueError(
                    f"{path}: missing required columns: {', '.join(sorted(missing))}"
                )
            for line_no, source in enumerate(reader, start=2):
                row = {key: clean_text(value) for key, value in source.items()}
                source_id = row.get(id_field, "")
                if not source_id:
                    raise ValueError(f"{path}:{line_no}: {id_field} cannot be blank")
                if source_id in seen:
                    continue
                seen.add(source_id)
                row["source_name"] = path.name
                rows.append(row)
    return rows


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def load_reference_maps(con: sqlite3.Connection):
    required = {
        "player_id", "player", "debut_season", "final_season",
        "career_games", "clubs_hist", "clubs_now",
    }
    missing = required - table_columns(con, "players")
    if missing:
        raise RuntimeError(f"players table lacks {sorted(missing)}")

    exact: dict[str, set[int]] = defaultdict(set)
    relaxed: dict[str, set[int]] = defaultdict(set)
    player_names: dict[int, str] = {}
    player_clubs: dict[int, set[str]] = defaultdict(set)
    spans: dict[int, tuple[int, int]] = {}
    career_games: dict[int, int] = {}

    for pid, name, debut, final, games, hist, current in con.execute("""
        SELECT player_id, player, debut_season, final_season, career_games,
               clubs_hist, clubs_now
        FROM players
    """):
        pid = int(pid)
        exact[identity_key(name)].add(pid)
        relaxed[relaxed_identity_key(name)].add(pid)
        player_names[pid] = str(name)
        for value in (hist, current):
            for club in str(value or "").split("|"):
                if club:
                    player_clubs[pid].add(club_key(club))
        if debut is not None and final is not None:
            spans[pid] = (int(debut), int(final))
        if games is not None:
            career_games[pid] = int(games)

    if {"player_id", "club_now", "club_hist"} <= table_columns(con, "games"):
        for pid, current, historical in con.execute(
            "SELECT player_id, club_now, club_hist FROM games"
        ):
            pid = int(pid)
            for club in (current, historical):
                if club:
                    player_clubs[pid].add(club_key(club))

    known_clubs = {
        club for clubs in player_clubs.values() for club in clubs if club
    }
    return (
        exact, relaxed, player_names, player_clubs, spans, career_games,
        known_clubs,
    )


def source_clubs(value: object) -> set[str]:
    text = clean_text(value)
    if not text:
        return set()
    # Wikipedia uses commas and slashes between clubs.  Semicolons/pipes are
    # also accepted so manually corrected rows remain loadable.
    parts = re.split(r"\s*(?:,|/|;|\|)\s*", text)
    out = set()
    for part in parts:
        part = re.sub(r"\b(?:rookie list|umpire|coach)\b.*$", "", part, flags=re.I)
        part = re.sub(r"\b(?:18|19|20)\d{2}(?:[–-](?:18|19|20)?\d{2})?\b", "", part)
        key = club_key(part)
        if key:
            out.add(key)
    return out


def _candidate_set(name: str, refs):
    exact, relaxed = refs[0], refs[1]
    direct = set(exact.get(identity_key(name), set()))
    if direct:
        return direct, "exact normalised identity"
    loose = set(relaxed.get(relaxed_identity_key(name), set()))
    if loose:
        return loose, "identity with middle initials ignored"
    return set(), "identity not found"


def resolve_member(row: dict[str, str], refs):
    candidates, method = _candidate_set(row["member_name"], refs)
    player_names, player_clubs, known_clubs = refs[2], refs[3], refs[6]
    listed_clubs = source_clubs(row.get("clubs_raw", ""))
    relevant_source_clubs = listed_clubs & known_clubs

    if not candidates:
        if listed_clubs and not relevant_source_clubs:
            return (
                None,
                "out_of_scope",
                [],
                method + ": listed clubs are outside the local AFL/VFL database",
            )
        return None, "unmatched", [], method

    if listed_clubs and not relevant_source_clubs:
        return (
            None,
            "out_of_scope",
            sorted(candidates),
            method + ": listed clubs are outside the local AFL/VFL database",
        )

    overlap_scores = {
        pid: len(relevant_source_clubs & player_clubs.get(pid, set()))
        for pid in candidates
    }
    best_overlap = max(overlap_scores.values(), default=0)
    club_matches = {
        pid for pid, score in overlap_scores.items() if score == best_overlap and score > 0
    }
    if len(club_matches) == 1:
        pid = next(iter(club_matches))
        status = "unique" if len(candidates) == 1 and method.startswith("exact") else "resolved"
        return (
            pid,
            status,
            sorted(club_matches),
            method + f" + strongest listed-club overlap ({best_overlap})",
        )
    if len(club_matches) > 1:
        return None, "ambiguous", sorted(club_matches), method + ": multiple equal listed-club matches"

    if len(candidates) == 1:
        pid = next(iter(candidates))
        if relevant_source_clubs:
            # The source names at least one club represented in the local AFL
            # database, but the candidate never played for it.  Do not let a
            # unique name override contradictory career evidence.
            return (
                None,
                "unmatched",
                [pid],
                method + ": unique name conflicts with listed AFL/VFL club",
            )
        return pid, "unique" if method.startswith("exact") else "resolved", [pid], method

    names = ", ".join(player_names[pid] for pid in sorted(candidates)[:8])
    return None, "ambiguous", sorted(candidates), method + f": {names}"


def normalise_member_row(row: dict[str, str]) -> dict[str, Any]:
    return {
        "source_member_id": row["source_member_id"],
        "family_key": row["family_key"],
        "family_name": row["family_name"],
        "member_name": row["member_name"],
        "member_wikipedia_url": row.get("member_wikipedia_url", ""),
        "clubs_raw": row.get("clubs_raw", ""),
        "list_depth": optional_int(row.get("list_depth")) or 0,
        "member_order": optional_int(row.get("member_order")) or 0,
        "parent_source_member_id": row.get("parent_source_member_id", ""),
        "explicit_relation_label": row.get("explicit_relation_label", ""),
        "family_notes": row.get("family_notes", ""),
        "source_url": row.get("source_url", ""),
        "source_revision_id": optional_int(row.get("source_revision_id")),
        "scraped_at_utc": row.get("scraped_at_utc", ""),
        "source_name": row.get("source_name", ""),
    }


def normalise_relationship_row(row: dict[str, str]) -> dict[str, Any]:
    return {
        "source_relationship_id": row["source_relationship_id"],
        "family_key": row["family_key"],
        "family_name": row.get("family_name", ""),
        "person_a_source_member_id": row["person_a_source_member_id"],
        "person_a_name": row.get("person_a_name", ""),
        "person_a_role": row.get("person_a_role", ""),
        "person_b_source_member_id": row["person_b_source_member_id"],
        "person_b_name": row.get("person_b_name", ""),
        "person_b_role": row.get("person_b_role", ""),
        "relationship_type": row["relationship_type"],
        "relationship_label": row["relationship_label"],
        "evidence": row.get("evidence", ""),
        "extraction_method": row.get("extraction_method", ""),
        "confidence": row.get("confidence", ""),
        "source_url": row.get("source_url", ""),
        "source_revision_id": optional_int(row.get("source_revision_id")),
        "scraped_at_utc": row.get("scraped_at_utc", ""),
        "source_name": row.get("source_name", ""),
    }


def build_linked_members(rows: list[dict[str, str]], refs) -> list[dict[str, Any]]:
    linked: list[dict[str, Any]] = []
    for source in rows:
        row = normalise_member_row(source)
        pid, status, candidates, notes = resolve_member(source, refs)
        row.update(
            {
                "player_id": pid,
                "match_status": status,
                "candidate_count": len(candidates),
                "candidate_player_ids": "|".join(str(pid) for pid in candidates),
                "match_notes": notes,
            }
        )
        linked.append(row)
    return linked


def validate_relationships(
    rows: list[dict[str, str]], members: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    known = {row["source_member_id"]: row for row in members}
    output: list[dict[str, Any]] = []
    for source in rows:
        row = normalise_relationship_row(source)
        a_id = row["person_a_source_member_id"]
        b_id = row["person_b_source_member_id"]
        if a_id not in known or b_id not in known:
            raise ValueError(
                f"relationship {row['source_relationship_id']} references unknown member"
            )
        if a_id == b_id:
            raise ValueError(
                f"relationship {row['source_relationship_id']} is a self-link"
            )
        if known[a_id]["family_key"] != known[b_id]["family_key"]:
            raise ValueError(
                f"relationship {row['source_relationship_id']} crosses families"
            )
        if row["family_key"] != known[a_id]["family_key"]:
            raise ValueError(
                f"relationship {row['source_relationship_id']} family_key mismatch"
            )
        output.append(row)
    return output


def create_tables(
    con: sqlite3.Connection,
    members: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
) -> None:
    imported_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    con.execute("DROP TABLE IF EXISTS family_relationships")
    con.execute("DROP TABLE IF EXISTS family_members")
    con.execute("""
        CREATE TABLE family_members (
            family_member_id INTEGER PRIMARY KEY,
            source_member_id TEXT NOT NULL UNIQUE,
            family_key TEXT NOT NULL,
            family_name TEXT NOT NULL,
            member_name TEXT NOT NULL,
            member_wikipedia_url TEXT,
            clubs_raw TEXT,
            list_depth INTEGER,
            member_order INTEGER,
            parent_source_member_id TEXT,
            explicit_relation_label TEXT,
            family_notes TEXT,
            source_url TEXT,
            source_revision_id INTEGER,
            scraped_at_utc TEXT,
            source_name TEXT,
            player_id INTEGER,
            match_status TEXT NOT NULL,
            candidate_count INTEGER NOT NULL,
            candidate_player_ids TEXT,
            match_notes TEXT,
            imported_at TEXT NOT NULL
        )
    """)
    member_fields = [
        "source_member_id", "family_key", "family_name", "member_name",
        "member_wikipedia_url", "clubs_raw", "list_depth", "member_order",
        "parent_source_member_id", "explicit_relation_label", "family_notes",
        "source_url", "source_revision_id", "scraped_at_utc", "source_name",
        "player_id", "match_status", "candidate_count",
        "candidate_player_ids", "match_notes",
    ]
    con.executemany(
        f"INSERT INTO family_members ({', '.join(member_fields)}, imported_at) "
        f"VALUES ({', '.join('?' for _ in member_fields)}, ?)",
        [tuple(row[field] for field in member_fields) + (imported_at,) for row in members],
    )

    con.execute("""
        CREATE TABLE family_relationships (
            relationship_id INTEGER PRIMARY KEY,
            source_relationship_id TEXT NOT NULL UNIQUE,
            family_key TEXT NOT NULL,
            family_name TEXT,
            person_a_source_member_id TEXT NOT NULL,
            person_a_name TEXT,
            person_a_role TEXT,
            person_b_source_member_id TEXT NOT NULL,
            person_b_name TEXT,
            person_b_role TEXT,
            relationship_type TEXT NOT NULL,
            relationship_label TEXT NOT NULL,
            evidence TEXT,
            extraction_method TEXT,
            confidence TEXT,
            source_url TEXT,
            source_revision_id INTEGER,
            scraped_at_utc TEXT,
            source_name TEXT,
            imported_at TEXT NOT NULL,
            FOREIGN KEY (person_a_source_member_id)
                REFERENCES family_members(source_member_id),
            FOREIGN KEY (person_b_source_member_id)
                REFERENCES family_members(source_member_id)
        )
    """)
    relationship_fields = [
        "source_relationship_id", "family_key", "family_name",
        "person_a_source_member_id", "person_a_name", "person_a_role",
        "person_b_source_member_id", "person_b_name", "person_b_role",
        "relationship_type", "relationship_label", "evidence",
        "extraction_method", "confidence", "source_url",
        "source_revision_id", "scraped_at_utc", "source_name",
    ]
    con.executemany(
        f"INSERT INTO family_relationships ({', '.join(relationship_fields)}, imported_at) "
        f"VALUES ({', '.join('?' for _ in relationship_fields)}, ?)",
        [
            tuple(row[field] for field in relationship_fields) + (imported_at,)
            for row in relationships
        ],
    )

    for statement in [
        "CREATE INDEX ix_family_members_family ON family_members(family_key)",
        "CREATE INDEX ix_family_members_player ON family_members(player_id, match_status)",
        "CREATE INDEX ix_family_members_name ON family_members(member_name)",
        "CREATE INDEX ix_family_rel_family ON family_relationships(family_key)",
        "CREATE INDEX ix_family_rel_a ON family_relationships(person_a_source_member_id)",
        "CREATE INDEX ix_family_rel_b ON family_relationships(person_b_source_member_id)",
        "CREATE INDEX ix_family_rel_type ON family_relationships(relationship_type)",
    ]:
        con.execute(statement)
    if "meta" in {
        row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }:
        con.execute("DELETE FROM meta WHERE key='family_relationships_imported'")
        con.execute(
            "INSERT INTO meta VALUES ('family_relationships_imported', ?)",
            (imported_at,),
        )
    con.commit()


def report(
    members: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    *,
    details: bool,
) -> None:
    status = Counter(row["match_status"] for row in members)
    print(f"  members:              {len(members):>6,}")
    print(f"  families:             {len({row['family_key'] for row in members}):>6,}")
    for name in ("unique", "resolved", "ambiguous", "unmatched", "out_of_scope"):
        print(f"  {name:<20} {status[name]:>6,}")
    linked_ids = {
        row["player_id"] for row in members
        if row["match_status"] in TRUSTED_STATUSES and row["player_id"] is not None
    }
    print(f"  linked AFL players:   {len(linked_ids):>6,}")
    print(f"  relationships:        {len(relationships):>6,}")

    by_member_id = {row["source_member_id"]: row for row in members}
    trusted_relationships = 0
    by_type: Counter[str] = Counter()
    for relationship in relationships:
        a = by_member_id[relationship["person_a_source_member_id"]]
        b = by_member_id[relationship["person_b_source_member_id"]]
        if (
            a["match_status"] in TRUSTED_STATUSES
            and b["match_status"] in TRUSTED_STATUSES
            and a["player_id"] is not None
            and b["player_id"] is not None
        ):
            trusted_relationships += 1
            by_type[relationship["relationship_type"]] += 1
    print(f"  trusted relationships:{trusted_relationships:>6,}")
    for relation_type, count in sorted(by_type.items()):
        print(f"    {relation_type:<26} {count:>5,}")

    if details:
        for label in ("ambiguous", "unmatched"):
            rows = [row for row in members if row["match_status"] == label]
            if not rows:
                continue
            print(f"\n{label.upper()} ({len(rows):,})")
            for row in rows[:100]:
                print(
                    f"  {row['family_name']} — {row['member_name']} "
                    f"[{row['clubs_raw'] or 'no clubs listed'}]: {row['match_notes']}"
                )
            if len(rows) > 100:
                print(f"  ... {len(rows) - 100:,} more")


def load(
    db_path: str | Path,
    member_paths: list[str | Path],
    relationship_paths: list[str | Path],
    *,
    show_report: bool = False,
    details: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    member_source = read_csvs(member_paths, MEMBER_REQUIRED)
    relationship_source = read_csvs(relationship_paths, RELATION_REQUIRED)
    con = sqlite3.connect(str(db_path))
    try:
        refs = load_reference_maps(con)
        members = build_linked_members(member_source, refs)
        relationships = validate_relationships(relationship_source, members)
        create_tables(con, members, relationships)
    finally:
        con.close()
    print(
        f"Imported {len(members):,} family-member rows and "
        f"{len(relationships):,} relationships into {db_path}"
    )
    if show_report:
        report(members, relationships, details=details)
    return members, relationships


def refresh_default(db_path: str | None = None, *, verbose: bool = True) -> bool:
    member_paths = family_member_sources("afl")
    relationship_paths = family_relationship_sources("afl")
    if not member_paths or not relationship_paths:
        if verbose:
            print("family relationship refresh skipped: source CSVs not found")
        return False
    db = db_path or default_db("afl")
    if not Path(db).exists():
        if verbose:
            print(f"family relationship refresh skipped: database not found: {db}")
        return False
    load(db, member_paths, relationship_paths, show_report=verbose)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=default_db("afl"))
    parser.add_argument("--members", type=Path, action="append")
    parser.add_argument("--relationships", type=Path, action="append")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--details", action="store_true")
    args = parser.parse_args(argv)

    member_paths = args.members or family_member_sources("afl")
    relationship_paths = args.relationships or family_relationship_sources("afl")
    if not member_paths:
        print(
            "error: family-member CSV not found; run afl/scrape_wikipedia_families.py",
            file=sys.stderr,
        )
        return 1
    if not relationship_paths:
        print(
            "error: family-relationship CSV not found; run afl/scrape_wikipedia_families.py",
            file=sys.stderr,
        )
        return 1

    try:
        member_source = read_csvs(member_paths, MEMBER_REQUIRED)
        relationship_source = read_csvs(relationship_paths, RELATION_REQUIRED)
        if args.inspect:
            print(
                f"Read {len(member_source):,} members and "
                f"{len(relationship_source):,} relationships"
            )
            print(
                f"Families: {len({row['family_key'] for row in member_source}):,}"
            )
            return 0
        if not Path(args.db).exists():
            print(f"error: database not found: {args.db}", file=sys.stderr)
            return 1
        load(
            args.db,
            member_paths,
            relationship_paths,
            show_report=args.report or args.details,
            details=args.details,
        )
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
