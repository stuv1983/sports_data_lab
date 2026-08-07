#!/usr/bin/env python3
"""Cache and load AFL Tables' complete venue catalogue and record pages."""
from __future__ import annotations

import argparse
from datetime import datetime
from io import BytesIO
from pathlib import Path
import sqlite3
import time
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
import pandas as pd

from data_paths import default_db

OVERALL_URL = "https://afltables.com/afl/venues/overall.html"
USER_AGENT = "sports-data-lab/1.0 (low-rate AFL venue archive)"


def _text(value):
    if value is None or pd.isna(value):
        return None
    text = str(value).replace("\xa0", " ").strip()
    return text or None


def _integer(value):
    number = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(number) else int(number)


def _number(value):
    number = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(number) else float(number)


def _date(value):
    text = _text(value)
    if not text:
        return None
    return datetime.strptime(text, "%d-%b-%Y").date().isoformat()


def venue_catalogue(path: Path) -> list[dict]:
    """Parse the authoritative overall table and its profile links."""
    html = path.read_text(encoding="windows-1252", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    links = {}
    for anchor in soup.find_all("a", href=True):
        url = urljoin(OVERALL_URL, anchor["href"])
        parsed = urlparse(url)
        if (parsed.path.startswith("/afl/venues/")
                and parsed.path.endswith(".html")
                and parsed.path != "/afl/venues/overall.html"):
            links.setdefault(anchor.get_text(" ", strip=True), url)
    table = pd.read_html(path)[0]
    if isinstance(table.columns, pd.MultiIndex):
        table.columns = table.columns.get_level_values(-1)
    output = []
    for row in table.itertuples(index=False):
        venue = _text(row[0])
        url = links.get(venue)
        if not venue or not url:
            raise ValueError(f"venue profile link missing for {venue!r}")
        output.append({
            "venue": venue, "in_use": _text(row[1]),
            "games": _integer(row[2]), "goals": _integer(row[3]),
            "behinds": _integer(row[4]), "points": _integer(row[5]),
            "average_score": _number(row[6]), "scores_100": _integer(row[7]),
            "profile_url": url, "source_url": OVERALL_URL,
        })
    if len(output) < 50:
        raise ValueError(f"expected the full venue catalogue, found {len(output)}")
    return output


def _profile_path(raw_dir: Path, url: str) -> Path:
    return raw_dir / Path(urlparse(url).path).name


def fetch_profiles(raw_dir: Path, catalogue: list[dict], *, delay=2.0,
                   refresh=False) -> None:
    """Fetch venue pages sequentially and validate each before saving."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    fetched = 0
    for venue in catalogue:
        target = _profile_path(raw_dir, venue["profile_url"])
        if target.is_file() and target.stat().st_size and not refresh:
            continue
        if fetched:
            time.sleep(max(0.0, delay))
        request = Request(venue["profile_url"], headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=30) as response:  # noqa: S310
            payload = response.read()
        title = BeautifulSoup(payload, "html.parser").find("title")
        if title is None or "AFL Tables" not in title.get_text(" ", strip=True):
            raise ValueError(f"unexpected venue page for {venue['venue']}")
        if len(pd.read_html(BytesIO(payload))) < 5:
            raise ValueError(f"venue tables missing for {venue['venue']}")
        target.write_bytes(payload)
        fetched += 1
        print(f"{venue['venue']}: {len(payload):,} bytes")


def parse_profile(path: Path, venue: str, profile_url: str) -> dict[str, list]:
    """Parse team, match, career and single-game records for one venue."""
    tables = pd.read_html(path)
    if len(tables) < 6:
        raise ValueError(f"expected six venue tables in {path}")

    team = tables[0]
    if isinstance(team.columns, pd.MultiIndex):
        team.columns = team.columns.get_level_values(-1)
    teams = []
    for rank, row in enumerate(team.itertuples(index=False), start=1):
        teams.append((
            venue, rank, _text(row[0]), _integer(row[1]), _integer(row[2]) or 0,
            _integer(row[3]) or 0, _integer(row[4]) or 0, _text(row[5]),
            _integer(row[6]), _text(row[7]), _integer(row[8]), _number(row[9]),
            _number(row[10]), _integer(row[11]) or 0, _integer(row[12]) or 0,
            profile_url,
        ))

    matches = []
    for category, table in zip(
            ("biggest_win", "highest_score", "lowest_score"), tables[1:4]):
        for rank, row in enumerate(table.itertuples(index=False), start=1):
            values = list(row)
            matches.append((
                venue, category, rank, _integer(values[0]), _text(values[1]),
                _text(values[2]), _integer(values[3]), _text(values[4]),
                _text(values[5]), _integer(values[6]), _date(values[7]),
                profile_url,
            ))

    careers = []
    for rank, row in enumerate(tables[4].itertuples(index=False), start=1):
        values = list(row)
        for category, offset in (("most_games", 0), ("most_goals", 3)):
            if _text(values[offset + 1]):
                careers.append((
                    venue, category, rank, _integer(values[offset]),
                    _text(values[offset + 1]), _text(values[offset + 2]),
                    profile_url,
                ))

    single_games = []
    for rank, row in enumerate(tables[5].itertuples(index=False), start=1):
        values = list(row)
        for category, offset in (("most_goals_game", 0),
                                 ("most_disposals_game", 3)):
            if _text(values[offset + 1]):
                single_games.append((
                    venue, category, rank, _integer(values[offset]),
                    _text(values[offset + 1]), _text(values[offset + 2]),
                    profile_url,
                ))
    return {"teams": teams, "matches": matches, "careers": careers,
            "single_games": single_games}


def load(con: sqlite3.Connection, raw_dir: Path) -> dict[str, int]:
    catalogue = venue_catalogue(raw_dir / "overall.html")
    parsed = {"teams": [], "matches": [], "careers": [], "single_games": []}
    missing = []
    for item in catalogue:
        path = _profile_path(raw_dir, item["profile_url"])
        if not path.is_file():
            missing.append(item["venue"])
            continue
        profile = parse_profile(path, item["venue"], item["profile_url"])
        for key in parsed:
            parsed[key].extend(profile[key])
    if missing:
        raise FileNotFoundError("missing venue profiles: " + ", ".join(missing))

    con.executescript("""
        DROP TABLE IF EXISTS venue_summary;
        DROP TABLE IF EXISTS venue_team_records;
        DROP TABLE IF EXISTS venue_match_records;
        DROP TABLE IF EXISTS venue_player_records;
        DROP TABLE IF EXISTS venue_player_game_records;
        CREATE TABLE venue_summary (
          venue TEXT PRIMARY KEY, in_use TEXT, games INTEGER, goals INTEGER,
          behinds INTEGER, points INTEGER, average_score REAL,
          scores_100 INTEGER, profile_url TEXT NOT NULL, source_url TEXT NOT NULL);
        CREATE TABLE venue_team_records (
          venue TEXT, rank INTEGER, team TEXT, played INTEGER, wins INTEGER,
          draws INTEGER, losses INTEGER, scoring_for TEXT, points_for INTEGER,
          scoring_against TEXT, points_against INTEGER, percentage REAL,
          win_percentage REAL, scores_100_for INTEGER, scores_100_against INTEGER,
          source_url TEXT, PRIMARY KEY (venue, team));
        CREATE TABLE venue_match_records (
          venue TEXT, category TEXT, rank INTEGER, record_value INTEGER,
          team TEXT, team_progress TEXT, team_score INTEGER, opponent TEXT,
          opponent_progress TEXT, opponent_score INTEGER, match_date TEXT,
          source_url TEXT, PRIMARY KEY (venue, category, rank));
        CREATE TABLE venue_player_records (
          venue TEXT, category TEXT, rank INTEGER, record_value INTEGER,
          player TEXT, clubs TEXT, source_url TEXT,
          PRIMARY KEY (venue, category, rank));
        CREATE TABLE venue_player_game_records (
          venue TEXT, category TEXT, rank INTEGER, record_value INTEGER,
          player TEXT, match_description TEXT, source_url TEXT,
          PRIMARY KEY (venue, category, rank));
    """)
    con.executemany(
        "INSERT INTO venue_summary VALUES (?,?,?,?,?,?,?,?,?,?)",
        [tuple(item.values()) for item in catalogue])
    con.executemany("INSERT INTO venue_team_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    parsed["teams"])
    con.executemany("INSERT INTO venue_match_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    parsed["matches"])
    con.executemany("INSERT INTO venue_player_records VALUES (?,?,?,?,?,?,?)",
                    parsed["careers"])
    con.executemany("INSERT INTO venue_player_game_records VALUES (?,?,?,?,?,?,?)",
                    parsed["single_games"])
    con.execute("CREATE INDEX ix_venue_team ON venue_team_records(venue, rank)")
    con.execute("CREATE INDEX ix_venue_match ON venue_match_records(venue, category, rank)")
    con.commit()
    return {"venues": len(catalogue), **{key: len(value) for key, value in parsed.items()}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path,
                        default=Path("data/afl/raw/venues"))
    parser.add_argument("--db", type=Path, default=Path(default_db("afl")))
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--delay", type=float, default=2.0)
    args = parser.parse_args()
    catalogue = venue_catalogue(args.raw_dir / "overall.html")
    if args.fetch:
        fetch_profiles(args.raw_dir, catalogue, delay=args.delay,
                       refresh=args.refresh)
    with sqlite3.connect(args.db) as con:
        counts = load(con, args.raw_dir)
    print(", ".join(f"{count:,} {label}" for label, count in counts.items()))


if __name__ == "__main__":
    main()
