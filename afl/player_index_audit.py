#!/usr/bin/env python3
"""Low-rate AFL Tables player-index fetch and identity audit.

Only the 26 surname index pages are fetched. Individual profiles are not
crawled: the cached fitzRoy data identifies each player with the same AFL
Tables profile URL, which safely separates namesakes.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from data_paths import default_db

BASE = "https://afltables.com/afl/stats/"
INDEX_URL = BASE + "players{letter}_idx.html"
PROFILE_RE = re.compile(r"/afl/stats/players/[A-Z]/[^/]+\.html$", re.I)
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
PROFILE_ALIASES = {
    "/afl/stats/players/l/lyle_anderson.html": "/afl/stats/players/l/lyall_anderson.html",
    "/afl/stats/players/s/steven_icke.html": "/afl/stats/players/s/stephen_icke.html",
    "/afl/stats/players/n/norman_paternoster.html": "/afl/stats/players/h/henry_paternoster.html",
    "/afl/stats/players/j/jonathon_ross.html": "/afl/stats/players/j/jonathan_ross.html",
    "/afl/stats/players/g/glenn_scanlon.html": "/afl/stats/players/g/glen_scanlon.html",
}


@dataclass(frozen=True)
class IndexPlayer:
    letter: str
    source_name: str
    profile_url: str
    source_url: str

    @property
    def profile_key(self) -> str:
        return urlparse(self.profile_url).path.lower()


def parse_index(html: bytes | str, letter: str) -> list[IndexPlayer]:
    if isinstance(html, bytes):
        html = html.decode("windows-1252", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    source_url = INDEX_URL.format(letter=letter.upper())
    rows: list[IndexPlayer] = []
    for anchor in soup.find_all("a", href=True):
        url = urljoin(source_url, anchor["href"])
        if not PROFILE_RE.search(urlparse(url).path):
            continue
        rows.append(IndexPlayer(letter.upper(), anchor.get_text(" ", strip=True),
                                url, source_url))
    if not rows:
        raise ValueError(f"no player-profile links found on index {letter!r}")
    keys = [row.profile_key for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError(f"duplicate profile URL on index {letter!r}")
    return rows


def fetch_indexes(raw_dir: Path, *, delay: float = 2.0,
                  refresh: bool = False) -> None:
    """Fetch at most one small index page per delay interval."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    fetched = 0
    for letter in LETTERS:
        target = raw_dir / f"players{letter}_idx.html"
        if target.is_file() and target.stat().st_size and not refresh:
            continue
        if fetched:
            time.sleep(max(0.0, delay))
        url = INDEX_URL.format(letter=letter)
        request = Request(url, headers={
            "User-Agent": "sports-data-lab/1.0 (low-rate player index audit)"
        })
        with urlopen(request, timeout=30) as response:  # noqa: S310
            payload = response.read()
        parse_index(payload, letter)
        target.write_bytes(payload)
        fetched += 1
        print(f"{letter}: {len(payload):,} bytes")


def read_indexes(raw_dir: Path) -> list[IndexPlayer]:
    rows: list[IndexPlayer] = []
    missing = []
    for letter in LETTERS:
        path = raw_dir / f"players{letter}_idx.html"
        if not path.is_file():
            missing.append(letter)
        else:
            rows.extend(parse_index(path.read_bytes(), letter))
    if missing:
        raise FileNotFoundError(f"missing player indexes: {', '.join(missing)}")
    return rows


def _name_key(value: object) -> str:
    text = str(value or "").strip()
    if "," in text:
        surname, given = [part.strip() for part in text.split(",", 1)]
        text = f"{given} {surname}"
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _profile_key(url: object) -> str:
    return urlparse(str(url or "").strip()).path.lower()


def audit(con: sqlite3.Connection, index_rows: list[IndexPlayer],
          stat_rows: list[tuple[int, str, str]]) -> dict[str, int]:
    """Reconcile index URLs -> fitzRoy source IDs -> database player IDs."""
    db = {int(pid): name for pid, name in
          con.execute("SELECT player_id, player FROM players")}
    stats = {}
    for pid, player, url in stat_rows:
        key = _profile_key(url)
        if key:
            stats[key] = (int(pid), str(player), str(url))
    indexed = {row.profile_key: row for row in index_rows}
    consumed, alias_keys = set(), set()
    unindexed_stats = {key: value for key, value in stats.items()
                       if key not in indexed}
    for key, source in indexed.items():
        if key in stats:
            continue
        old_key = PROFILE_ALIASES.get(key)
        candidates = [old_key] if old_key in unindexed_stats else []
        if not candidates:
            candidates = [candidate for candidate, value in unindexed_stats.items()
                          if candidate not in consumed
                          and _name_key(value[1]) == _name_key(source.source_name)]
        if len(candidates) == 1:
            old_key = candidates[0]
            stats[key] = unindexed_stats[old_key]
            consumed.add(old_key)
            alias_keys.add(key)
    indexed_player_ids = {stats[key][0] for key in indexed if key in stats}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    output, counts = [], {}
    for key in sorted(indexed.keys() | stats.keys()):
        if key in consumed:
            continue
        source, stat = indexed.get(key), stats.get(key)
        if source is None and stat is not None and stat[0] in indexed_player_ids:
            continue
        pid = stat[0] if stat else None
        db_name = db.get(pid) if pid is not None else None
        if source is None:
            status = "missing_from_index"
        elif stat is None:
            status = "missing_from_player_stats"
        elif db_name is None:
            status = "missing_from_db"
        elif key in alias_keys:
            status = "matched_profile_alias"
        elif _name_key(source.source_name) != _name_key(db_name):
            status = "matched_name_variant"
        else:
            status = "matched"
        counts[status] = counts.get(status, 0) + 1
        output.append((key, source.letter if source else None,
                       source.source_name if source else None,
                       source.profile_url if source else stat[2],
                       source.source_url if source else None, pid, db_name,
                       status, now))
    con.execute("""
        CREATE TABLE IF NOT EXISTS afltables_player_index (
            profile_key TEXT PRIMARY KEY, surname_letter TEXT,
            source_name TEXT, profile_url TEXT NOT NULL, source_url TEXT,
            player_id INTEGER, db_player TEXT, audit_status TEXT NOT NULL,
            audited_at TEXT NOT NULL)""")
    con.execute("DELETE FROM afltables_player_index")
    con.executemany(
        "INSERT INTO afltables_player_index VALUES (?,?,?,?,?,?,?,?,?)", output)
    con.execute("CREATE INDEX IF NOT EXISTS ix_api_status ON afltables_player_index(audit_status)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_api_player ON afltables_player_index(player_id)")
    con.commit()
    return counts


def rda_players(path: Path) -> list[tuple[int, str, str]]:
    import pyreadr
    from .build_db import repair_missing_player_ids
    frame = repair_missing_player_ids(pyreadr.read_r(path)["afldata"])
    unique = (frame[["ID", "Player", "url"]]
              .dropna(subset=["ID", "url"])
              .drop_duplicates(["ID", "url"]))
    return [(int(row.ID), str(row.Player), str(row.url))
            for row in unique.itertuples(index=False)]


def player_index_available(con: sqlite3.Connection) -> bool:
    if not con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='afltables_player_index'").fetchone():
        return False
    total, problems = con.execute("""
        SELECT COUNT(*), SUM(audit_status IN
          ('missing_from_index','missing_from_player_stats','missing_from_db'))
        FROM afltables_player_index""").fetchone()
    return bool(total and not problems)


def player_index_count(con: sqlite3.Connection) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM afltables_player_index "
        "WHERE audit_status IN "
        "('matched','matched_name_variant','matched_profile_alias')").fetchone()[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path,
                        default=Path("data/afl/raw/player_index"))
    parser.add_argument("--rda", type=Path,
                        default=Path("data/afl/raw/afldata.rda"))
    parser.add_argument("--db", type=Path, default=Path(default_db("afl")))
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--delay", type=float, default=2.0)
    args = parser.parse_args()
    if args.fetch:
        fetch_indexes(args.raw_dir, delay=args.delay, refresh=args.refresh)
    rows = read_indexes(args.raw_dir)
    with sqlite3.connect(args.db) as con:
        counts = audit(con, rows, rda_players(args.rda))
    print(f"Indexed profiles: {len(rows):,}")
    for status, count in sorted(counts.items()):
        print(f"  {status:<26} {count:>7,}")


if __name__ == "__main__":
    main()
