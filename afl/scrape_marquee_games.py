#!/usr/bin/env python3
"""Tag AFL marquee fixtures (Anzac Day, Dreamtime, King's Birthday) in the games table.

    python -m afl.scrape_marquee_games            # fetch (cached), tag database
    python -m afl.scrape_marquee_games --refresh   # re-fetch even if cached

Adds a ``match_event`` column to ``games`` and tags every player-row of a
matching fixture with the event name, by cross-referencing each Wikipedia
results table against the games table already built by afl/build_db.py.

Fetched through the MediaWiki API (not the article URL) with a declared
User-Agent -- Wikipedia's edge blocks pandas' default urllib UA with a 403.
Pages are cached under data/afl/cache/marquee_games/ so a rerun doesn't
re-request them; pass --refresh to bypass the cache.

None of these three pages record a Round column that matches every year --
Dreamtime does (as 'Rd'), Anzac Day and King's Birthday don't, because
those two are fixed-calendar-date fixtures rather than a fixed round. Where
Round is available it's used; otherwise the fixture is identified by season
+ teams + the calendar month the event is always played in. That month
filter is only as reliable as the fixture's real-world scheduling -- e.g.
the 2020 Anzac Day match was played in December because of COVID -- so a
handful of pandemic-affected fixtures may go untagged rather than risk
mismatching the teams' other meeting that season.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sqlite3
import urllib.parse
import urllib.request

import pandas as pd

from data_paths import cache_dir, sport_db

API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "SportsDataLab/1.0 (personal research; contact via repository)"

MARQUEE_CONFIGS = [
    {
        "event_name": "Anzac Day",
        "page": "Anzac_Day_match",
        "team_a": "Collingwood",
        "team_b": "Essendon",
        "round_column": None,      # not recorded on this page
        "month": "04",              # always played in April
    },
    {
        "event_name": "Dreamtime at the 'G",
        "page": "Dreamtime_at_the_'G",
        "team_a": "Essendon",
        "team_b": "Richmond",
        "round_column": "Rd",      # this page records the round directly
        "month": None,
    },
    {
        "event_name": "King's Birthday",
        "page": "King's_Birthday_match_(AFL)",
        "team_a": "Melbourne",
        "team_b": "Collingwood",
        "round_column": None,      # not recorded on this page
        "month": "06",              # always played in June
    },
]


def _fetch_html(page: str, refresh: bool = False) -> str:
    folder = cache_dir("afl", "marquee_games")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{page}.html"
    if refresh or not path.exists():
        url = (f"{API}?action=parse&page={urllib.parse.quote(page)}"
               f"&prop=text&format=json&formatversion=2")
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
        path.write_text(payload["parse"]["text"], encoding="utf-8")
    return path.read_text(encoding="utf-8")


def _results_table(html: str) -> pd.DataFrame | None:
    """The Year/Home Team/Away Team results table on a marquee-fixture page.

    Wikipedia renders the header row with a rowspan'd blank corner cell,
    which defeats pandas' header inference -- the table comes back with
    integer column names and the real header sitting in row 0. Promote it
    by hand, then pick the table by column signature rather than position,
    since a new section on the page would silently shift any fixed index.
    """
    for df in pd.read_html(io.StringIO(html)):
        if list(df.columns) == list(range(df.shape[1])) and df.shape[0] > 1:
            df = df.set_axis(df.iloc[0], axis=1).iloc[1:].reset_index(drop=True)
        cols = {str(c).strip() for c in df.columns}
        if "Year" in cols and cols & {"Home Team", "Home team"} and df.shape[0] > 5:
            return df
    return None


def clean_year(year_str) -> int | None:
    """Extracts just the 4-digit year from messy Wikipedia text."""
    match = re.search(r"\d{4}", str(year_str))
    return int(match.group()) if match else None


def clean_round(round_str) -> str:
    """Strips citation markers like '[2]' from a round cell."""
    return re.sub(r"\[\w+\]", "", str(round_str)).strip()


def scrape_and_update(conn: sqlite3.Connection, refresh: bool = False) -> int:
    cursor = conn.cursor()

    try:
        cursor.execute("ALTER TABLE games ADD COLUMN match_event TEXT;")
        print("Added 'match_event' column to games table.")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            print("'match_event' column already exists.")
        else:
            raise

    total_updated = 0

    for config in MARQUEE_CONFIGS:
        print(f"\nScraping {config['event_name']}...")
        try:
            html = _fetch_html(config["page"], refresh=refresh)
            df = _results_table(html)
            if df is None:
                print(f"  [!] Couldn't find the results table for "
                      f"{config['event_name']}. Skipping.")
                continue

            round_col = config["round_column"]
            updates = 0
            for _, row in df.iterrows():
                year = clean_year(row["Year"])
                if not year:
                    continue

                params = [config["event_name"], year,
                          config["team_a"], config["team_b"],
                          config["team_b"], config["team_a"]]
                query = """
                    UPDATE games
                    SET match_event = ?
                    WHERE season = ?
                      AND ((club_now = ? AND opponent = ?) OR (club_now = ? AND opponent = ?))
                """
                if round_col:
                    rnd = clean_round(row[round_col])
                    if not rnd:
                        continue
                    query += " AND round = ?"
                    params.append(rnd)
                else:
                    query += " AND strftime('%m', date) = ?"
                    params.append(config["month"])

                cursor.execute(query, params)
                updates += cursor.rowcount

            print(f"  -> Tagged {updates} player-rows as {config['event_name']}.")
            total_updated += updates

        except Exception as e:
            print(f"  [!] Failed to scrape {config['event_name']}: {e}")

    conn.commit()
    print(f"\nFinished! Successfully tagged {total_updated} total rows "
          f"across AFL marquee games.")
    return total_updated


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true",
                         help="re-fetch Wikipedia pages even if cached")
    args = parser.parse_args()

    conn = sqlite3.connect(sport_db("afl", "gridley.db"))
    scrape_and_update(conn, refresh=args.refresh)
    conn.close()
