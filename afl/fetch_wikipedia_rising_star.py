#!/usr/bin/env python3
"""Fetch the current season's Rising Star nominations from Wikipedia.

    python -m afl.fetch_wikipedia_rising_star                 # this season
    python -m afl.fetch_wikipedia_rising_star --season 2025   # one season
    python -m afl.fetch_wikipedia_rising_star --load-db       # and load it

Writes ``data/afl/raw/wikipedia/rising_star/csv/rising_star_nominees_<year>.csv``
in the same shape as the FootyWire importer, so
``utils/afl/load_rising_star.py`` reads both without knowing the difference.

Why a second source at all
--------------------------
FootyWire is the richer source -- it carries the nominee's match statistics
-- but its published terms prohibit automated copying without written
permission, so ``afl/fetch_footywire_rising_star.py`` refuses to fetch on a
timer and its CSVs only advance when an operator saves pages by hand. That
left the award, which gains one nomination every week of the season, sitting
however many weeks behind the last manual run.

Wikipedia's Rising Star article is updated within a day of each nomination,
is licensed for reuse, and is reached here through the same MediaWiki API
the other scrapers in this package use. It carries only round, player and
club -- no statistics -- which is exactly the part the app actually
constrains on (see ``afl/rising_star.py``: every builder filters on
player_id, season and club, and none reads a stat column).

So the two sources are not rivals. FootyWire wins wherever it has the round;
Wikipedia fills the tail of the current season until it does. That
precedence lives in the loader, keyed on ``source`` -- see
``utils/afl/load_rising_star.py``.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "SportsDataLab/1.0 (personal research; contact via repository)"
PAGE_TITLE = "{year}_AFL_Rising_Star"
ARTICLE_URL = "https://en.wikipedia.org/wiki/{title}"
#: The award began in 1993 as the Norwich Rising Star.
FIRST_SEASON = 1993
SOURCE_NAME = "wikipedia"

#: Columns written here, in the FootyWire importer's order. The statistic
#: columns are written empty on purpose rather than omitted: the loader
#: reads one row shape from every source, and a blank cell is honest about
#: what Wikipedia publishes where a zero would not be.
CSV_FIELDS = [
    "source_key", "season", "nomination_round", "round_number",
    "player", "player_display", "name_key", "club", "team_display",
    "team_slug", "opponent", "opponent_display", "opponent_slug",
    "kicks", "handballs", "disposals", "marks", "goals", "behinds",
    "tackles", "hitouts", "frees_for", "frees_against", "supercoach",
    "afl_fantasy", "unavailable_stats", "is_season_winner", "winner_name",
    "winner_team", "player_url", "source_url", "scraped_at", "source",
]

#: Every statistic FootyWire supplies and Wikipedia does not. Recorded in
#: the row's `unavailable_stats` so the gap is visible in the database
#: rather than looking like a nominee who registered nothing.
UNAVAILABLE = "|".join([
    "kicks", "handballs", "disposals", "marks", "goals", "behinds",
    "tackles", "hitouts", "frees_for", "frees_against", "supercoach",
    "afl_fantasy",
])

#: Club article slug -> the club identity used by the local database.
#:
#: Keyed on the wiki link target rather than the cell's display text
#: because the display text is editorial and drifts ("Greater Western
#: Sydney" and "GWS Giants" have both been used in this table), while the
#: article title is a redirect target that stays put.
CLUB_ARTICLES = {
    "Adelaide_Football_Club": "Adelaide",
    "Brisbane_Bears": "Brisbane Bears",
    "Brisbane_Lions": "Brisbane Lions",
    "Carlton_Football_Club": "Carlton",
    "Collingwood_Football_Club": "Collingwood",
    "Essendon_Football_Club": "Essendon",
    "Fitzroy_Football_Club": "Fitzroy",
    "Fremantle_Football_Club": "Fremantle",
    "Geelong_Football_Club": "Geelong",
    "Gold_Coast_Football_Club": "Gold Coast",
    "Gold_Coast_Suns": "Gold Coast",
    "Greater_Western_Sydney_Giants": "GWS",
    "Greater_Western_Sydney_Football_Club": "GWS",
    "Hawthorn_Football_Club": "Hawthorn",
    "Melbourne_Football_Club": "Melbourne",
    "North_Melbourne_Football_Club": "North Melbourne",
    "Port_Adelaide_Football_Club": "Port Adelaide",
    "Richmond_Football_Club": "Richmond",
    "St_Kilda_Football_Club": "St Kilda",
    "Sydney_Swans": "Sydney",
    "West_Coast_Eagles": "West Coast",
    "Western_Bulldogs": "Western Bulldogs",
    "Footscray_Football_Club": "Western Bulldogs",
}

#: Display-text fallback for a cell whose link is missing or unrecognised.
CLUB_NAMES = {
    "adelaide": "Adelaide",
    "adelaide crows": "Adelaide",
    "brisbane": "Brisbane Lions",
    "brisbane bears": "Brisbane Bears",
    "brisbane lions": "Brisbane Lions",
    "carlton": "Carlton",
    "collingwood": "Collingwood",
    "essendon": "Essendon",
    "fitzroy": "Fitzroy",
    "footscray": "Western Bulldogs",
    "fremantle": "Fremantle",
    "geelong": "Geelong",
    "gold coast": "Gold Coast",
    "gold coast suns": "Gold Coast",
    "greater western sydney": "GWS",
    "gws": "GWS",
    "gws giants": "GWS",
    "hawthorn": "Hawthorn",
    "kangaroos": "North Melbourne",
    "melbourne": "Melbourne",
    "north melbourne": "North Melbourne",
    "port adelaide": "Port Adelaide",
    "richmond": "Richmond",
    "st kilda": "St Kilda",
    "sydney": "Sydney",
    "sydney swans": "Sydney",
    "west coast": "West Coast",
    "western bulldogs": "Western Bulldogs",
}


class PageNotFound(LookupError):
    """The season's article does not exist yet.

    Not an error worth failing a scheduled run over: in February the next
    season's article has usually not been created, and the correct response
    is to do nothing until it is.
    """


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalise_name(value: object) -> str:
    try:
        from names import normalise_name as project_normalise
    except ImportError:
        text = unicodedata.normalize("NFKD", clean(value)).casefold()
        text = "".join(c for c in text if not unicodedata.combining(c))
        return clean(re.sub(r"[^a-z0-9]+", " ", text))
    return project_normalise(clean(value))


def article_title(season: int) -> str:
    return PAGE_TITLE.format(year=int(season))


def fetch_html(season: int, timeout: float = 30.0) -> str:
    """Return the rendered article HTML from the MediaWiki parse API."""
    title = article_title(season)
    url = (f"{API}?action=parse&page={urllib.parse.quote(title)}"
           f"&prop=text&format=json&formatversion=2")
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise PageNotFound(title) from exc
        raise
    if "error" in payload:
        code = str(payload["error"].get("code", ""))
        if code in {"missingtitle", "nosuchpageid", "invalidtitle"}:
            raise PageNotFound(title)
        raise RuntimeError(f"{title}: MediaWiki error {payload['error']}")
    return payload["parse"]["text"]


def _cell_text(cell) -> str:
    """Cell text with reference markers and sort keys removed."""
    for junk in cell.xpath(".//sup[contains(@class,'reference')]"
                           "|.//style|.//span[@class='sortkey']"):
        junk.getparent().remove(junk)
    text = clean(" ".join(cell.itertext()))
    # A dagger or asterisk beside a name is an eligibility footnote, not
    # part of the name. The 2026 table marks a suspended nominee this way.
    return clean(re.sub(r"[*†‡]+", "", text))


def _link_target(cell) -> str:
    """The article slug a cell links to, if it links to an article."""
    for anchor in cell.xpath(".//a[@href]"):
        href = anchor.get("href") or ""
        if href.startswith("/wiki/") and ":" not in href[6:]:
            return urllib.parse.unquote(href[6:].split("#", 1)[0])
    return ""


def canonical_club(cell) -> str:
    """Map a club cell to the club identity the local database uses."""
    slug = _link_target(cell)
    if slug in CLUB_ARTICLES:
        return CLUB_ARTICLES[slug]
    text = _cell_text(cell)
    return CLUB_NAMES.get(text.casefold(), text)


def _find_nominations_table(root):
    """Return the wikitable whose header is Round / Player / Club.

    Located by its header rather than by position, because the article also
    carries eligibility and (in past seasons) voting tables, and their order
    is not stable across seasons.
    """
    for table in root.xpath("//table[contains(@class,'wikitable')]"):
        rows = table.xpath(".//tr")
        for index, row in enumerate(rows):
            headings = [_cell_text(cell).casefold()
                        for cell in row.xpath("./th|./td")]
            if len(headings) >= 3 and headings[:3] == ["round", "player", "club"]:
                return table, index
    raise ValueError("Could not locate the Round/Player/Club nominations table")


def _winner(root, season: int) -> str:
    """The season's winner, where the article states one unambiguously.

    Only a sentence that names the award and the season counts. The article
    mentions many players, and a looser read would happily crown a nominee
    from the eligibility prose.
    """
    text = clean(" ".join(root.itertext()))
    for pattern in (
        rf"{season}\s+(?:AFL\s+)?Rising Star(?:\s+award)?\s+was\s+won\s+by\s+"
        rf"([A-Z][\w'’.-]+(?:\s+[A-Z][\w'’.-]+){{1,3}})",
        rf"([A-Z][\w'’.-]+(?:\s+[A-Z][\w'’.-]+){{1,3}})\s+won\s+the\s+{season}\s+"
        rf"(?:AFL\s+)?Rising Star",
    ):
        match = re.search(pattern, text)
        if match:
            return clean(match.group(1))
    return ""


def parse_page(html_text: str, season: int, source_url: str | None = None,
               scraped_at: str | None = None) -> list[dict]:
    """Parse one season article into loader-shaped rows."""
    try:
        from lxml import html as lxml_html
    except ImportError as exc:
        raise RuntimeError("Missing dependency. Run: pip install lxml") from exc

    root = lxml_html.fromstring(html_text)
    table, header_index = _find_nominations_table(root)
    source_url = source_url or ARTICLE_URL.format(title=article_title(season))
    scraped_at = scraped_at or dt.datetime.now(dt.timezone.utc).isoformat()
    winner_name = _winner(root, season)
    winner_key = normalise_name(winner_name) if winner_name else ""

    rows: list[dict] = []
    for tr in table.xpath(".//tr")[header_index + 1:]:
        cells = tr.xpath("./th|./td")
        if len(cells) < 3:
            continue
        round_text = _cell_text(cells[0])
        match = re.search(r"-?\d+", round_text)
        if match is None:
            continue
        round_number = int(match.group())
        player = _cell_text(cells[1])
        if not player:
            continue
        club = canonical_club(cells[2])
        player_slug = _link_target(cells[1])
        name_key = normalise_name(player)

        rows.append({
            "source_key": hashlib.sha256(
                f"wikipedia|{season}|{round_number}|{name_key}".encode("utf-8")
            ).hexdigest()[:24],
            "season": season,
            "nomination_round": round_text,
            "round_number": round_number,
            "player": player,
            "player_display": player,
            "name_key": name_key,
            "club": club,
            "team_display": _cell_text(cells[2]),
            "team_slug": _link_target(cells[2]),
            "opponent": "", "opponent_display": "", "opponent_slug": "",
            "unavailable_stats": UNAVAILABLE,
            "is_season_winner": int(bool(winner_key) and name_key == winner_key),
            "winner_name": winner_name,
            "winner_team": "",
            "player_url": (ARTICLE_URL.format(title=player_slug)
                           if player_slug else ""),
            "source_url": source_url,
            "scraped_at": scraped_at,
            "source": SOURCE_NAME,
        })

    if not rows:
        raise ValueError(f"{season}: the nominations table held no data rows")

    seen_rounds = [row["round_number"] for row in rows]
    if len(seen_rounds) != len(set(seen_rounds)):
        raise ValueError(f"{season}: duplicate nomination rounds found")
    keys = [row["source_key"] for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{season}: a player appears more than once")
    return rows


def output_dir() -> Path:
    try:
        from data_paths import rising_star_wikipedia_dir
    except ImportError:
        return Path("data/afl/raw/wikipedia/rising_star")
    return rising_star_wikipedia_dir("afl")


def csv_path(season: int, folder: Path | None = None) -> Path:
    base = folder or output_dir()
    return base / "csv" / f"rising_star_nominees_{int(season)}.csv"


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_existing(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _round_keys(rows) -> set[str]:
    return {f"{row.get('round_number')}|{row.get('name_key')}" for row in rows}


def refresh_season(season: int, folder: Path | None = None,
                   timeout: float = 30.0,
                   html_text: str | None = None) -> dict:
    """Refresh one season's CSV and report what changed.

    Returns the counts a caller needs to decide whether the database is
    worth rebuilding: rewriting a CSV that is byte-identical is not a
    reason to promote a new database.
    """
    path = csv_path(season, folder)
    before = read_existing(path)
    if html_text is None:
        html_text = fetch_html(season, timeout=timeout)
    rows = parse_page(html_text, season)

    previous, current = _round_keys(before), _round_keys(rows)
    added = current - previous
    removed = previous - current
    if added or removed:
        write_csv(path, rows)
    return {
        "season": season,
        "path": str(path),
        "rows": len(rows),
        "previous_rows": len(before),
        "added": len(added),
        "removed": len(removed),
        "changed": bool(added or removed),
        "latest_round": max((row["round_number"] for row in rows), default=None),
        "new_nominations": [
            {"round": row["round_number"], "player": row["player"],
             "club": row["club"]}
            for row in rows
            if f"{row['round_number']}|{row['name_key']}" in added
        ],
    }


def default_db() -> str:
    try:
        from data_paths import sport_db
    except ImportError:
        return str(Path(__file__).resolve().parents[1] / "gridley.db")
    return sport_db("afl", "gridley.db")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--season", type=int,
                        default=dt.date.today().year,
                        help="season to refresh (default: this year)")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--load-db", action="store_true",
                        help="reload the Rising Star table after fetching")
    parser.add_argument("--db", default=None)
    args = parser.parse_args(argv)

    if args.season < FIRST_SEASON:
        parser.error(f"the award starts in {FIRST_SEASON}")

    try:
        result = refresh_season(args.season, args.output_dir, args.timeout)
    except PageNotFound:
        print(f"{args.season}: no Wikipedia article yet; nothing to do")
        return 0

    print(f"{result['season']}: {result['rows']} nominations "
          f"(latest round {result['latest_round']})")
    for nomination in result["new_nominations"]:
        print(f"  + round {nomination['round']:>2}  {nomination['player']} "
              f"({nomination['club']})")
    if result["removed"]:
        print(f"  {result['removed']} row(s) no longer listed")
    if not result["changed"]:
        print("  no change since the last fetch")
    else:
        print(f"  wrote {result['path']}")

    if args.load_db:
        from utils.afl import load_rising_star
        loaded = load_rising_star.refresh_default(
            db_path=args.db or default_db(), verbose=True)
        if not loaded or not loaded.get("trusted"):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
