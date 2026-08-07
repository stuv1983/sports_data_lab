"""Load the AFL's official All-Australian history into an AFL database."""

from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import urllib.request
from collections import defaultdict
from pathlib import Path

from data_paths import default_db, raw_dir
from names import normalise_name

HISTORY_URL = "https://www.afl.com.au/all-australian/history"
FALLBACK_DATASET_URL = "https://datawrapper.dwcdn.net/Dtbc3/13/dataset.csv"
DEFAULT_SOURCE = raw_dir("afl") / "official" / "all_australian_history.csv"
USER_AGENT = "SportsDataLab/1.0 (+database update; public AFL history data)"

DDL = """
CREATE TABLE all_australian_history (
    season INTEGER NOT NULL,
    player_source TEXT NOT NULL,
    club_source TEXT,
    name_key TEXT NOT NULL,
    player_id INTEGER,
    match_status TEXT NOT NULL,
    match_method TEXT NOT NULL,
    candidate_count INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    UNIQUE(season, player_source, club_source)
)
"""

ALIASES = {
    "cam guthrie": "Cameron Guthrie",
    "daniel hannebery": "Dan Hannebery",
    "grant shannon": "Shannon Grant",
    "greg whittlesea": "Gregory Whittlesea",
    "hannebery daniel": "Dan Hannebery",
    "issac heeney": "Isaac Heeney",
    "malcolm brown": "Mal Brown",
    "matt taberner": "Matthew Taberner",
    "menegola sam": "Sam Menegola",
    "riewoldt jack": "Jack Riewoldt",
    "rod ashman": "Rodney Ashman",
    "ryder paddy": "Paddy Ryder",
    "sheppard brad": "Brad Sheppard",
    "steve malaxos": "Stephen Malaxos",
    "steve odwyer": "Steven O'Dwyer",
    "zack merrett": "Zach Merrett",
}

CLUB_ALIASES = {
    "Adelaide Crows": "Adelaide",
    "Footscray": "Western Bulldogs",
    "Geelong Cats": "Geelong",
    "Gold Coast Suns": "Gold Coast",
    "Greater Western Sydney": "GWS",
    "GWS GIANTS": "GWS",
    "GWS Giants": "GWS",
    "Port Adelaide Power": "Port Adelaide",
    "South Melbourne": "Sydney",
    "Sydney Swans": "Sydney",
    "West Coast Eagles": "West Coast",
}


def _request(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _dataset_url() -> str:
    try:
        page = _request(HISTORY_URL).decode("utf-8", errors="replace")
    except OSError:
        return FALLBACK_DATASET_URL
    matches = re.findall(
        r"https://datawrapper\.dwcdn\.net/([A-Za-z0-9]+)/([0-9]+)/", page)
    if not matches:
        return FALLBACK_DATASET_URL
    chart, version = matches[-1]
    return f"https://datawrapper.dwcdn.net/{chart}/{version}/dataset.csv"


def fetch(destination: str | Path = DEFAULT_SOURCE) -> Path:
    destination = Path(destination)
    data = _request(_dataset_url())
    first_line = data.splitlines()[0].decode("utf-8-sig", errors="replace")
    if first_line.strip() != "Year,Player,Club":
        raise RuntimeError(
            f"unexpected AFL All-Australian CSV header: {first_line!r}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, destination)
    return destination


def _source_name(value: str) -> str:
    value = value.strip().rstrip("*").strip()
    if "," in value:
        family, given = value.split(",", 1)
        value = f"{given.strip()} {family.strip()}"
    return ALIASES.get(normalise_name(value), value)


def _loose_name(value: str) -> str:
    words = re.sub(r"[^a-z0-9 ]", "", normalise_name(value)).split()
    words = [word for word in words
             if word not in {"jr", "jnr", "sr", "snr", "ii", "iii", "iv"}]
    while len(words) > 2 and len(words[0]) == len(words[1]) == 1:
        words[:2] = [words[0] + words[1]]
    return " ".join(words)


def _players(con: sqlite3.Connection) -> dict[str, list[tuple]]:
    found: dict[str, list[tuple]] = defaultdict(list)
    for row in con.execute(
            "SELECT player_id, player, debut_season, final_season, "
            "clubs_hist, clubs_now FROM players"):
        clubs = {club for text in row[4:6]
                 for club in str(text or "").split("|") if club}
        found[_loose_name(row[1])].append((*row[:4], clubs))
    return found


def _resolve(row: dict, players: dict[str, list[tuple]]) -> tuple:
    name = _source_name(row["Player"])
    season = int(row["Year"])
    candidates = players.get(_loose_name(name), [])
    active = [candidate for candidate in candidates
              if candidate[2] is not None and candidate[3] is not None
              and candidate[2] - 1 <= season <= candidate[3] + 1]
    source_club = CLUB_ALIASES.get(row.get("Club", "").strip(),
                                   row.get("Club", "").strip())
    club_matches = [candidate for candidate in candidates
                    if source_club and source_club in candidate[4]]

    if len(active) == 1:
        chosen, status, method = active[0], "resolved", "career_season"
    elif len(club_matches) == 1:
        chosen, status, method = club_matches[0], "resolved", "club"
    elif len(candidates) == 1:
        chosen, status, method = candidates[0], "unique", "unique_name"
    else:
        chosen = None
        status = "ambiguous" if candidates else "unmatched"
        method = "multiple_candidates" if candidates else "no_name_match"
    return name, (chosen[0] if chosen else None), status, method, len(candidates)


def load(db_path: str | Path, source: str | Path = DEFAULT_SOURCE,
         verbose: bool = True) -> dict[str, int]:
    source = Path(source)
    if not source.exists():
        raise FileNotFoundError(f"No official All-Australian CSV at {source}")
    with source.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or not {"Year", "Player", "Club"} <= set(rows[0]):
        raise ValueError("official All-Australian CSV has an unexpected schema")

    con = sqlite3.connect(db_path)
    try:
        players = _players(con)
        records = []
        counts: dict[str, int] = defaultdict(int)
        for row in rows:
            name, player_id, status, method, candidate_count = _resolve(
                row, players)
            counts[status] += 1
            records.append((
                int(row["Year"]), row["Player"].strip(),
                row.get("Club", "").strip() or None, normalise_name(name),
                player_id, status, method, candidate_count, HISTORY_URL,
            ))
        con.execute("DROP TABLE IF EXISTS all_australian_history")
        con.execute(DDL)
        con.executemany(
            "INSERT OR IGNORE INTO all_australian_history VALUES "
            "(?,?,?,?,?,?,?,?,?)", records)
        con.execute(
            "CREATE INDEX ix_aa_history_player "
            "ON all_australian_history(player_id, season)")
        con.commit()
    finally:
        con.close()
    if verbose:
        print(f"All-Australian history: loaded {len(records):,} rows")
        for status, count in sorted(counts.items()):
            print(f"  {status:<10} {count:>5,}")
    return dict(counts)


def refresh_default(db_path: str | Path, refresh: bool = True,
                    source: str | Path = DEFAULT_SOURCE,
                    verbose: bool = True) -> dict[str, int]:
    source = Path(source)
    if refresh:
        try:
            fetch(source)
        except (OSError, RuntimeError) as exc:
            if not source.exists():
                raise
            if verbose:
                print(f"Official AFL history refresh failed; using cache: {exc}")
    return load(db_path, source, verbose=verbose)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=default_db("afl"))
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    refresh_default(args.db, refresh=args.refresh, source=args.source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
