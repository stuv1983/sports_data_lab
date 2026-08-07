#!/usr/bin/env python3
"""Audit and extend ``matches`` from AFL Tables' chronological score list.

The player-stat build remains authoritative for player-game rows.  This layer
answers a different question: does the database contain every completed VFL/AFL
match that AFL Tables lists?  Score-only matches are appended transparently and
marked as such until the player-stat source catches up on the next rebuild.

Usage::

    python -m utils.afl.load_match_scores --source data/afl/raw/matches/afltables_bg3.txt
    python -m utils.afl.load_match_scores --source ... --audit-only
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3

from afl.build_db import CLUB_LINEAGE
from data_paths import default_db

SOURCE_URL = "https://afltables.com/afl/stats/biglists/bg3.txt"
FINALS = {"EF", "QF", "SF", "PF", "GF"}
LINE_RE = re.compile(
    r"^\s*(\d+)\.\s+(\d{1,2}-[A-Za-z]{3}-\d{4})\s+(\S+)\s+"
    r"(.+?)\s+(\d+\.\d+\.\d+)\s+(.+?)\s+"
    r"(\d+\.\d+\.\d+)\s+(.+?)\s*$"
)
SCORE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
TEAM_ALIASES = {"GW Sydney": "Greater Western Sydney"}


@dataclass(frozen=True)
class SourceMatch:
    source_number: int
    match_date: str
    season: int
    round: str
    home_team: str
    home_score_text: str
    home_score: int
    away_team: str
    away_score_text: str
    away_score: int
    venue: str


def _score(text: str) -> int:
    match = SCORE_RE.fullmatch(text)
    if not match:
        raise ValueError(f"invalid score {text!r}")
    goals, behinds, points = map(int, match.groups())
    if goals * 6 + behinds != points:
        raise ValueError(f"score arithmetic disagrees in {text!r}")
    return points


def _team(text: str) -> str:
    return TEAM_ALIASES.get(text.strip(), text.strip())


def _round(text: str) -> str:
    text = text.strip().upper()
    return text[1:] if re.fullmatch(r"R\d+", text) else text


def parse_biglist(text: str) -> list[SourceMatch]:
    """Parse ``bg3.txt`` and fail closed on any non-header data line."""
    rows: list[SourceMatch] = []
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not line or line.lower().startswith("all games"):
            continue
        match = LINE_RE.match(line)
        if not match:
            raise ValueError(f"unrecognised AFL Tables line {line_number}: {line!r}")
        number, date_text, rnd, home, home_raw, away, away_raw, venue = match.groups()
        date = datetime.strptime(date_text, "%d-%b-%Y").date().isoformat()
        rows.append(SourceMatch(
            int(number), date, int(date[:4]), _round(rnd), _team(home),
            home_raw, _score(home_raw), _team(away), away_raw,
            _score(away_raw), venue.strip()))
    if not rows:
        raise ValueError("no matches found in AFL Tables score list")
    expected = list(range(rows[0].source_number, rows[-1].source_number + 1))
    actual = [row.source_number for row in rows]
    if actual != expected:
        raise ValueError("AFL Tables match numbering is not contiguous")
    return rows


def read_source(path: Path) -> list[SourceMatch]:
    return parse_biglist(path.read_text(encoding="utf-8-sig"))


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS afltables_match_scores (
            source_number INTEGER PRIMARY KEY,
            match_date TEXT NOT NULL,
            season INTEGER NOT NULL,
            round TEXT NOT NULL,
            home_team TEXT NOT NULL,
            home_score_text TEXT NOT NULL,
            home_score INTEGER NOT NULL,
            away_team TEXT NOT NULL,
            away_score_text TEXT NOT NULL,
            away_score INTEGER NOT NULL,
            venue TEXT NOT NULL,
            source_url TEXT NOT NULL,
            db_match_id INTEGER,
            audit_status TEXT NOT NULL,
            audited_at TEXT NOT NULL
        )""")
    match_columns = _columns(con, "matches")
    for name in ("game_status", "data_status", "score_source_url"):
        if name not in match_columns:
            con.execute(f"ALTER TABLE matches ADD COLUMN {name} TEXT")


def _identity(row: sqlite3.Row) -> tuple:
    return (int(row["season"]), _round(row["round"]), str(row["match_date"])[:10],
            _team(row["home_team"]), int(row["home_score"]),
            _team(row["away_team"]), int(row["away_score"]))


def _source_identity(row: SourceMatch) -> tuple:
    return (row.season, row.round, row.match_date, row.home_team,
            row.home_score, row.away_team, row.away_score)


def _append_match(con: sqlite3.Connection, row: SourceMatch, match_id: int) -> None:
    key = (f"{row.season}|{row.round}|{row.match_date}|"
           f"{row.home_team}|{row.away_team}")
    winner = (row.home_team if row.home_score > row.away_score else
              row.away_team if row.away_score > row.home_score else None)
    con.execute("""
        INSERT INTO matches (
            match_id, match_key, season, round, match_date, venue,
            home_team, away_team, home_team_now, away_team_now,
            home_score, away_score, winner, margin, is_final,
            home_away_known, home_players, away_players, attendance,
            game_status, data_status, score_source_url
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        match_id, key, row.season, row.round, row.match_date, row.venue,
        row.home_team, row.away_team,
        CLUB_LINEAGE.get(row.home_team, row.home_team),
        CLUB_LINEAGE.get(row.away_team, row.away_team),
        row.home_score, row.away_score, winner,
        abs(row.home_score - row.away_score), int(row.round in FINALS),
        1, 0, 0, None, "played", "score_only", SOURCE_URL,
    ))


def _upsert_club_observations(con: sqlite3.Connection, row: SourceMatch,
                              match_id: int, now: str) -> None:
    """Make a score-only match visible to the existing Past Games queries."""
    if not con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='club_match_sources'").fetchone():
        return
    from utils.afl.club_sources import ALL_GAMES_BY_ID

    by_now = {club.db_club_now: club for club in ALL_GAMES_BY_ID.values()}
    home_now = CLUB_LINEAGE.get(row.home_team, row.home_team)
    away_now = CLUB_LINEAGE.get(row.away_team, row.away_team)
    if home_now not in by_now or away_now not in by_now:
        return
    key = f"bg3-{row.source_number}"
    source_round = f"R{row.round}" if row.round.isdigit() else row.round
    for mine, other, position, points_for, points_against in (
            (by_now[home_now], by_now[away_now], "H", row.home_score, row.away_score),
            (by_now[away_now], by_now[home_now], "A", row.away_score, row.home_score)):
        margin = points_for - points_against
        result = "W" if margin > 0 else "L" if margin < 0 else "D"
        con.execute("""
            INSERT OR REPLACE INTO club_match_sources (
                source_club_id, source_club_label, season, round, is_final,
                team_position, opponent_raw, points_for, points_against,
                result, margin, venue_raw, date_text, match_date,
                source_game_url, source_game_key, home_team_raw, away_team_raw,
                match_id, match_status, source_fetched_at, imported_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            mine.club_id, mine.name, row.season, source_round,
            int(row.round in FINALS), position, other.name, points_for,
            points_against, result, margin, row.venue, row.match_date,
            row.match_date, SOURCE_URL, key, row.home_team, row.away_team,
            match_id, "unique", now, now))


def load(con: sqlite3.Connection, rows: list[SourceMatch], *, append_missing: bool = True) -> dict[str, int]:
    """Write the audit snapshot and optionally append completed score-only games."""
    con.row_factory = sqlite3.Row
    _ensure_schema(con)
    existing_rows = list(con.execute("SELECT * FROM matches"))
    existing_ids = {int(row["match_id"]) for row in existing_rows}
    by_identity = {_identity(row): row for row in existing_rows}
    by_fixture: dict[tuple, list[sqlite3.Row]] = {}
    for candidate in existing_rows:
        fixture = (int(candidate["season"]), _round(candidate["round"]),
                   str(candidate["match_date"])[:10],
                   frozenset((_team(candidate["home_team"]),
                              _team(candidate["away_team"]))))
        by_fixture.setdefault(fixture, []).append(candidate)
    next_id = max(existing_ids, default=0) + 1
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    audit: list[tuple] = []
    counts = {"matched": 0, "partial_player_stats": 0, "score_only": 0,
              "missing_from_db": 0, "identity_mismatch": 0}
    for row in rows:
        candidate = by_identity.get(_source_identity(row))
        fixture = (row.season, row.round, row.match_date,
                   frozenset((row.home_team, row.away_team)))
        alternatives = by_fixture.get(fixture, [])
        if candidate is None and not alternatives:
            if append_missing:
                db_id = row.source_number if row.source_number not in existing_ids else next_id
                if db_id == next_id:
                    next_id += 1
                _append_match(con, row, db_id)
                _upsert_club_observations(con, row, db_id, now)
                existing_ids.add(db_id)
                status = "score_only"
            else:
                status, db_id = "missing_from_db", None
        elif candidate is not None:
            home_players = int(candidate["home_players"] or 0)
            away_players = int(candidate["away_players"] or 0)
            if not home_players and not away_players:
                status = "score_only"
            elif home_players < 12 or away_players < 12:
                status = "partial_player_stats"
            else:
                status = "matched"
            data_status = ("player_stats" if status == "matched" else status)
            db_id = int(candidate["match_id"])
            con.execute(
                "UPDATE matches SET game_status='played', "
                "data_status=?, "
                "score_source_url=? WHERE match_id=?",
                (data_status, SOURCE_URL, db_id))
            if status == "score_only":
                _upsert_club_observations(con, row, db_id, now)
        else:
            status, db_id = "identity_mismatch", int(alternatives[0]["match_id"])
        counts[status] += 1
        audit.append((
            row.source_number, row.match_date, row.season, row.round,
            row.home_team, row.home_score_text, row.home_score,
            row.away_team, row.away_score_text, row.away_score, row.venue,
            SOURCE_URL, db_id, status, now))
    con.execute("DELETE FROM afltables_match_scores")
    con.executemany(
        "INSERT INTO afltables_match_scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        audit)
    con.execute("CREATE INDEX IF NOT EXISTS ix_ams_season ON afltables_match_scores(season, round)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_ams_status ON afltables_match_scores(audit_status)")
    con.commit()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=Path(default_db("afl")))
    parser.add_argument("--audit-only", action="store_true")
    args = parser.parse_args()
    rows = read_source(args.source)
    with sqlite3.connect(args.db) as con:
        counts = load(con, rows, append_missing=not args.audit_only)
    print(f"AFL Tables score rows: {len(rows):,} ({rows[0].season}-{rows[-1].season})")
    for status, count in counts.items():
        if count:
            print(f"  {status:<22} {count:>7,}")


if __name__ == "__main__":
    main()
