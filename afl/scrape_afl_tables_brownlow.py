"""Download AFL Tables Brownlow voting tables as one CSV per season.

The Brownlow index is used as the source of truth for available seasons and
medal winners.  AFL Tables did not award the medal from 1942 through 1945, so
those seasons are (correctly) absent from the output.
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag


INDEX_URL = "https://afltables.com/afl/brownlow/brownlow_idx.html"
USER_AGENT = "sports-data-lab/1.0 (Brownlow data importer)"
SEASON_RE = re.compile(r"/brownlow/brownlow(\d{4})\.html$")


def _text(tag: Tag) -> str:
    return tag.get_text(" ", strip=True).replace("\xa0", "").strip()


def _read_local_or_fetch(source: str, session: requests.Session) -> bytes:
    path = Path(source)
    if path.is_file():
        return path.read_bytes()
    response = session.get(source, timeout=30)
    response.raise_for_status()
    return response.content


def parse_index(html: bytes) -> tuple[dict[int, str], dict[int, set[str]]]:
    """Return season URLs and winner player URLs from the Brownlow index."""
    soup = BeautifulSoup(html, "html.parser")
    season_urls: dict[int, str] = {}
    winners: dict[int, set[str]] = {}

    for link in soup.select("a[href]"):
        href = urljoin(INDEX_URL, link.get("href", ""))
        match = SEASON_RE.search(urlparse(href).path)
        if match:
            season_urls[int(match.group(1))] = href

    # The first table is the winners-by-year table. Tied winners have a row
    # each, which is why the value is a set rather than a single URL.
    tables = soup.find_all("table")
    if tables:
        current_year: int | None = None
        for row in tables[0].find_all("tr"):
            cells = row.find_all("td", recursive=False)
            if len(cells) < 2:
                continue
            year_text = _text(cells[0])
            if year_text.isdigit():
                current_year = int(year_text)
                player_cell = cells[1]
            elif current_year is not None and cells[0].find(
                "a", href=re.compile(r"/stats/players/")
            ):
                # Tied winners follow the first row under an HTML rowspan, so
                # their row starts directly with the player cell.
                player_cell = cells[0]
            else:
                continue
            player_link = player_cell.find("a", href=True)
            if player_link:
                player_url = urljoin(INDEX_URL, player_link["href"])
                winners.setdefault(current_year, set()).add(player_url)

    return season_urls, winners


def _find_player_table(soup: BeautifulSoup) -> tuple[Tag, list[str]]:
    for table in soup.find_all("table"):
        first_row = table.find("tr")
        if first_row is None:
            continue
        # A handful of pages (notably 1984) omit the closing </th> after TM,
        # causing every later header to be nested beneath it. Descendant order
        # still matches the visual table and repairs that historical typo.
        headers = [_text(cell) for cell in first_row.find_all("th")]
        if len(headers) > 1 and headers[1].startswith("TM V "):
            headers[1] = "TM"
        detailed = headers[:3] == ["Player", "TM", "V"] and "GM" in headers
        historical = headers[:4] == ["Player", "Teams", "Votes", "Games"]
        if detailed or historical:
            return table, headers
    raise ValueError("No AFL Tables Brownlow player table found")


def _integer_text(value: str, *, zero_for_dash: bool = False) -> str:
    if zero_for_dash and value == "-":
        return "0"
    return value.rstrip("*").strip()


def parse_season(
    html: bytes,
    season: int,
    source_url: str,
    winner_urls: set[str],
) -> tuple[list[str], list[dict[str, object]]]:
    """Parse one season page, preserving blank (did not play) round cells."""
    soup = BeautifulSoup(html, "html.parser")
    table, headers = _find_player_table(soup)
    detailed = headers[:3] == ["Player", "TM", "V"]
    games_index = headers.index("GM" if detailed else "Games")
    round_headers = headers[3:games_index] if detailed else []
    if not all(header.isdigit() for header in round_headers):
        raise ValueError(f"Unexpected round columns for {season}: {round_headers}")

    max_round = max(map(int, round_headers), default=0)
    fieldnames = ["season", "player", "team", "votes", "ineligible"]
    fieldnames.extend(f"round_{round_number}" for round_number in range(1, max_round + 1))
    fieldnames.extend(
        [
            "games",
            "three_vote_games",
            "two_vote_games",
            "one_vote_games",
            "polling_games",
            "winner",
            "player_id",
            "player_url",
            "team_url",
            "source_url",
        ]
    )

    records: list[dict[str, object]] = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td", recursive=False)
        if len(cells) < len(headers):
            continue

        player_link = cells[0].find("a", href=True)
        player_url = urljoin(source_url, player_link["href"]) if player_link else ""
        team_links = [
            urljoin(source_url, link["href"])
            for link in cells[1].find_all("a", href=True)
            if Path(urlparse(link["href"]).path).stem != "_totals"
        ]
        player_id = Path(urlparse(player_url).path).stem if player_url else ""

        votes_text = _text(cells[2])
        record: dict[str, object] = {
            "season": season,
            "player": _text(cells[0]),
            "team": _text(cells[1]),
            "votes": _integer_text(votes_text),
            # AFL Tables marks players who could not win because of a
            # suspension with an asterisk (for example, Corey McKernan in
            # 1996). Keep votes numeric and expose that meaning explicitly.
            "ineligible": "*" in votes_text,
        }
        for offset, round_header in enumerate(round_headers, start=3):
            record[f"round_{int(round_header)}"] = _integer_text(
                _text(cells[offset]), zero_for_dash=True
            )

        trailing = cells[games_index : games_index + 5]
        trailing_values = [_integer_text(_text(cell)) for cell in trailing]
        trailing_values.extend([""] * (5 - len(trailing_values)))
        record.update(
            {
                "games": trailing_values[0],
                "three_vote_games": trailing_values[1],
                "two_vote_games": trailing_values[2],
                "one_vote_games": trailing_values[3],
                "polling_games": trailing_values[4],
                "winner": player_url in winner_urls,
                "player_id": player_id,
                "player_url": player_url,
                "team_url": "|".join(dict.fromkeys(team_links)),
                "source_url": source_url,
            }
        )
        records.append(record)

    if not records:
        raise ValueError(f"No Brownlow player rows found for {season}")
    return fieldnames, records


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build(args: argparse.Namespace) -> None:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    index_html = _read_local_or_fetch(args.index, session)
    season_urls, winners = parse_index(index_html)

    selected = [
        season
        for season in sorted(season_urls)
        if args.start_year <= season <= args.end_year
    ]
    if not selected:
        raise SystemExit("No Brownlow seasons matched the requested year range")

    output_dir = Path(args.output_dir)
    for position, season in enumerate(selected):
        url = season_urls[season]
        html = _read_local_or_fetch(url, session)
        fieldnames, rows = parse_season(html, season, url, winners.get(season, set()))
        destination = output_dir / f"{season}.csv"
        write_csv(destination, fieldnames, rows)
        print(f"{season}: {len(rows):>3} players -> {destination}")
        if args.delay and position < len(selected) - 1:
            time.sleep(args.delay)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", default=INDEX_URL, help="Index URL or saved HTML file")
    parser.add_argument("--output-dir", default="data/afl/bm")
    parser.add_argument("--start-year", type=int, default=1924)
    parser.add_argument("--end-year", type=int, default=9999)
    parser.add_argument("--delay", type=float, default=0.15, help="Seconds between requests")
    return parser


if __name__ == "__main__":
    build(make_parser().parse_args())
