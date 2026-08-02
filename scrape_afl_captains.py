#!/usr/bin/env python3
"""Scrape official-season VFL/AFL club captains from Wikipedia.

The MediaWiki API supplies one cached page response per club. Only tables under
an AFL or VFL/AFL heading are accepted; AFLW/VFLW and modern second-tier VFL
sections are excluded. Output grain is one row per season, club and captain.

Examples:
    python scrape_afl_captains.py --inspect
    python scrape_afl_captains.py
    python scrape_afl_captains.py --load --db gridley.db
    python scrape_afl_captains.py --refresh --through 2026
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html as html_lib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from data_paths import cache_dir, default_db, raw_dir

API = "https://en.wikipedia.org/w/api.php"
CATEGORY = "Category:Lists of Australian Football League captains"
CATEGORY_URL = (
    "https://en.wikipedia.org/wiki/"
    "Category:Lists_of_Australian_Football_League_captains"
)
DEFAULT_OUTPUT = raw_dir("afl") / "wikipedia_captaincies.csv"
DEFAULT_CACHE = cache_dir("afl") / "captain_pages"
DEFAULT_UA = (
    "SportsDataLab/1.2 (personal AFL research; cached low-volume requests)"
)

CLUB_BY_PAGE = {
    "List of Adelaide Football Club captains": "Adelaide",
    "List of Brisbane Bears captains": "Brisbane Bears",
    "List of Brisbane Lions captains": "Brisbane Lions",
    "List of Carlton Football Club captains": "Carlton",
    "List of Collingwood Football Club captains": "Collingwood",
    "List of Essendon Football Club captains": "Essendon",
    "List of Fitzroy Football Club captains": "Fitzroy",
    "List of Fremantle Football Club captains": "Fremantle",
    "List of Geelong Football Club captains": "Geelong",
    "List of Gold Coast Suns captains": "Gold Coast",
    "List of Greater Western Sydney Giants captains": "GWS",
    "List of Hawthorn Football Club captains": "Hawthorn",
    "List of Melbourne Football Club captains": "Melbourne",
    "List of North Melbourne Football Club captains": "North Melbourne",
    "List of Port Adelaide Football Club captains": "Port Adelaide",
    "List of Richmond Football Club captains": "Richmond",
    "List of St Kilda Football Club captains": "St Kilda",
    "List of Sydney Swans captains": "Sydney",
    "List of Tasmania Football Club captains": "Tasmania",
    "List of University Football Club captains": "University",
    "List of West Coast Eagles captains": "West Coast",
    "List of Western Bulldogs captains": "Western Bulldogs",
}

OUTPUT_FIELDS = [
    "season", "club", "player", "role", "source_url", "player_url",
    "source_page", "source_revision", "source_period", "source_notes",
]


@dataclass(frozen=True)
class PagePayload:
    title: str
    revid: int
    html: str


@dataclass(frozen=True)
class CaptainRow:
    season: int
    club: str
    player: str
    role: str
    source_url: str
    player_url: str
    source_page: str
    source_revision: int
    source_period: str
    source_notes: str


class FetchError(RuntimeError):
    """Raised when Wikipedia cannot be fetched safely or completely."""


def clean_text(value: object) -> str:
    text = html_lib.unescape(str(value or "")).replace("\xa0", " ")
    text = re.sub(r"\[[a-z0-9]+\]", "", text, flags=re.I)
    return " ".join(text.split()).strip()


def page_url(title: str) -> str:
    encoded = urllib.parse.quote(title.replace(" ", "_"), safe="_()'-")
    return "https://en.wikipedia.org/wiki/" + encoded


def club_for_title(title: str) -> str:
    try:
        return CLUB_BY_PAGE[title]
    except KeyError as exc:
        raise ValueError(
            f"unmapped captain page {title!r}; add it to CLUB_BY_PAGE"
        ) from exc


def _short_end(start: int, token: str) -> int:
    end = int(token)
    if len(token) == 4:
        return end
    end += start // 100 * 100
    return end + 100 if end < start else end


def expand_period(period: str, through: int,
                  minimum: int = 1897) -> list[int]:
    """Expand values such as ``1914–1915; 1917`` and ``2023–``."""
    text = clean_text(period)
    text = (text.replace("—", "–").replace("−", "–")
            .replace("present", str(through)).replace("Present", str(through))
            .replace("current", str(through)).replace("Current", str(through)))
    text = re.sub(r"\([^)]*\)", " ", text)
    years: set[int] = set()
    pattern = re.compile(
        r"(?<!\d)((?:18|19|20)\d{2})"
        r"(?:\s*[–-]\s*((?:\d{4}|\d{2})?))?"
    )
    for match in pattern.finditer(text):
        start = int(match.group(1))
        token = match.group(2)
        is_range = "–" in match.group(0) or "-" in match.group(0)
        end = (through if token in (None, "") else _short_end(start, token)) \
            if is_range else start
        if end < start:
            start, end = end, start
        years.update(range(max(start, minimum), min(end, through) + 1))
    return sorted(years)


def _api_get(params: dict[str, object], user_agent: str,
             retries: int = 4) -> dict:
    query = dict(params)
    query.setdefault("format", "json")
    query.setdefault("formatversion", 2)
    query.setdefault("maxlag", 5)
    url = API + "?" + urllib.parse.urlencode(query)
    delay = 2.0

    for attempt in range(retries):
        request = urllib.request.Request(
            url, headers={"User-Agent": user_agent, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if "error" in payload:
                raise FetchError(str(payload["error"]))
            return payload
        except urllib.error.HTTPError as exc:
            retryable = exc.code in {429, 500, 502, 503, 504}
            if not retryable or attempt == retries - 1:
                raise FetchError(f"Wikipedia HTTP {exc.code}: {url}") from exc
            retry_after = exc.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after else delay)
        except (urllib.error.URLError, TimeoutError,
                json.JSONDecodeError) as exc:
            if attempt == retries - 1:
                raise FetchError(f"Wikipedia request failed: {url}: {exc}") \
                    from exc
            time.sleep(delay)
        delay *= 2
    raise FetchError(f"Wikipedia request failed: {url}")


def category_pages(user_agent: str) -> list[str]:
    payload = _api_get({
        "action": "query",
        "list": "categorymembers",
        "cmtitle": CATEGORY,
        "cmnamespace": 0,
        "cmlimit": 500,
    }, user_agent)
    pages = [item["title"] for item in payload["query"]["categorymembers"]]
    if len(pages) < 18:
        raise FetchError(
            f"category returned only {len(pages)} pages; refusing a partial scrape"
        )
    return pages


def _cache_path(directory: Path, title: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("_")
    return directory / f"{safe}.json"


def fetch_page(title: str, directory: Path, refresh: bool,
               user_agent: str) -> PagePayload:
    path = _cache_path(directory, title)
    if path.exists() and not refresh:
        data = json.loads(path.read_text(encoding="utf-8"))
        return PagePayload(data["title"], int(data["revid"]), data["html"])

    payload = _api_get({
        "action": "parse",
        "page": title,
        "prop": "text|revid",
        "disableeditsection": 1,
        "disabletoc": 1,
    }, user_agent)
    parsed = payload["parse"]
    result = PagePayload(parsed["title"], int(parsed["revid"]), parsed["text"])
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(result), ensure_ascii=False),
                    encoding="utf-8")
    return result


def _normal_heading(node) -> str:
    return clean_text(node.text_content()).casefold() if node is not None else ""


def _valid_competition_table(table) -> bool:
    headings = table.xpath("preceding::h2[1]")
    heading = _normal_heading(headings[0]) if headings else ""
    if any(word in heading for word in ("women", "aflw", "vflw")):
        return False
    return bool(re.search(r"\b(?:vfl\s*/\s*afl|afl\s*/\s*vfl|afl)\b",
                          heading))


def _headers(table) -> tuple[int, int, int | None]:
    for row in table.xpath(".//tr"):
        cells = row.xpath("./th|./td")
        labels = [clean_text(cell.text_content()).casefold() for cell in cells]
        captain = next((i for i, label in enumerate(labels)
                        if "captain" in label), None)
        period = next((i for i, label in enumerate(labels)
                       if any(key in label for key in
                              ("year", "season", "date"))), None)
        notes = next((i for i, label in enumerate(labels)
                      if "note" in label), None)
        if captain is not None and period is not None:
            return period, captain, notes
    raise ValueError("captain table has no recognisable headers")


def _plain_names(text: str) -> list[str]:
    """Split unlinked plain text, preserving suffix commas such as `Jr.`."""
    primary = [part.strip() for part in re.split(
        r"\s*(?:;|/|\band\b|\s&\s)\s*", text, flags=re.I
    ) if part.strip()]
    names: list[str] = []
    for part in primary:
        pieces = [piece.strip() for piece in part.split(",") if piece.strip()]
        suffix = len(pieces) == 2 and re.fullmatch(
            r"(?:jr|junior|sr|senior|ii|iii|iv)\.?", pieces[1], flags=re.I
        )
        names.extend([part] if len(pieces) <= 1 or suffix else pieces)
    return names


def _player_links(cell) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in cell.xpath(".//a[not(ancestor::sup)]"):
        name = clean_text(anchor.text_content())
        if not name or not re.search(r"[A-Za-z]", name):
            continue
        key = name.casefold()
        if key in seen:
            continue
        href = anchor.get("href", "")
        if href.startswith("//"):
            url = "https:" + href
        elif href.startswith("/"):
            url = "https://en.wikipedia.org" + href
        elif href.startswith("http"):
            url = href
        else:
            url = ""
        seen.add(key)
        found.append((name, url))
    if found:
        return found

    for br in cell.xpath(".//br"):
        br.tail = "\n" + (br.tail or "")
    for item in cell.xpath(".//li"):
        item.tail = "\n" + (item.tail or "")

    for raw in cell.text_content().splitlines():
        value = clean_text(raw)
        value = re.sub(
            r"\b(?:co[- ]captain|acting captain|captain)\b", "", value,
            flags=re.I,
        ).strip(" -–—,;")
        for name in _plain_names(value):
            if not name or not re.search(r"[A-Za-z]", name):
                continue
            key = name.casefold()
            if key not in seen:
                seen.add(key)
                found.append((name, ""))
    return found


def parse_page(payload: PagePayload, through: int,
               minimum: int = 1897) -> tuple[list[CaptainRow], list[str]]:
    try:
        from lxml import html
    except ImportError as exc:
        raise RuntimeError("Missing dependency. Run: pip install lxml") from exc

    club = club_for_title(payload.title)
    document = html.fromstring(payload.html)
    rows: list[CaptainRow] = []
    issues: list[str] = []
    accepted_tables = 0

    for table in document.xpath("//table"):
        try:
            period_index, captain_index, notes_index = _headers(table)
        except ValueError:
            continue
        if not _valid_competition_table(table):
            continue
        accepted_tables += 1
        for html_row in table.xpath(".//tr"):
            cells = html_row.xpath("./th|./td")
            highest = max(period_index, captain_index, notes_index or 0)
            if highest >= len(cells):
                continue
            period = clean_text(cells[period_index].text_content())
            seasons = expand_period(period, through, minimum)
            if not seasons:
                continue
            players = _player_links(cells[captain_index])
            if not players:
                issues.append(f"{payload.title}: no player parsed for {period!r}")
                continue
            notes = (clean_text(cells[notes_index].text_content())
                     if notes_index is not None and notes_index < len(cells)
                     else "")
            for season in seasons:
                for player, player_link in players:
                    rows.append(CaptainRow(
                        season, club, player, "Captain", page_url(payload.title),
                        player_link, payload.title, payload.revid, period, notes,
                    ))

    if accepted_tables == 0:
        issues.append(
            f"{payload.title}: no AFL or VFL/AFL captain table found; skipped"
        )

    unique = {(row.season, row.club, row.player.casefold()): row for row in rows}
    return sorted(unique.values(),
                  key=lambda row: (row.season, row.club, row.player)), issues


def scrape(*, through: int, minimum: int, directory: Path, refresh: bool,
           delay: float, user_agent: str
           ) -> tuple[list[CaptainRow], list[str], list[PagePayload]]:
    pages = category_pages(user_agent)
    all_rows: list[CaptainRow] = []
    issues: list[str] = []
    payloads: list[PagePayload] = []

    for index, title in enumerate(pages, start=1):
        payload = fetch_page(title, directory, refresh, user_agent)
        payloads.append(payload)
        parsed, page_issues = parse_page(payload, through, minimum)
        all_rows.extend(parsed)
        issues.extend(page_issues)
        print(f"  {index:>2}/{len(pages)}  {title}: "
              f"{len(parsed):,} season-player rows")
        next_cached = (index < len(pages)
                       and _cache_path(directory, pages[index]).exists())
        if index < len(pages) and (refresh or not next_cached):
            time.sleep(max(delay, 0.0))

    unique = {(row.season, row.club, row.player.casefold()): row
              for row in all_rows}
    result = sorted(unique.values(),
                    key=lambda row: (row.season, row.club, row.player))
    return result, issues, payloads


def write_csv(path: Path, rows: Iterable[CaptainRow]) -> int:
    materialised = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for row in materialised:
            writer.writerow(asdict(row))
    return len(materialised)


def write_metadata(path: Path, payloads: list[PagePayload], row_count: int,
                   through: int, minimum: int) -> Path:
    metadata_path = path.with_suffix(".metadata.json")
    data = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "source_category": CATEGORY_URL,
        "source_name": "Wikipedia",
        "source_licence": "CC BY-SA 4.0",
        "minimum_season": minimum,
        "through_season": through,
        "rows": row_count,
        "pages": [
            {"title": item.title, "url": page_url(item.title),
             "revision": item.revid}
            for item in payloads
        ],
    }
    metadata_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return metadata_path


def inspect(rows: list[CaptainRow], issues: list[str]) -> None:
    if rows:
        print(f"Rows: {len(rows):,}")
        print(f"Seasons: {min(row.season for row in rows)}-"
              f"{max(row.season for row in rows)}")
        print(f"Clubs: {len({row.club for row in rows})}")
        print(f"Players: {len({row.player.casefold() for row in rows}):,}")
    if issues:
        print("\nReview notes:")
        for issue in issues:
            print(f"  - {issue}")


def load_into_db(csv_path: Path, db_path: str) -> None:
    import sqlite3
    import load_captains

    if not Path(db_path).exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    rows = load_captains.read_csvs([csv_path])
    connection = sqlite3.connect(db_path)
    try:
        totals = load_captains.import_rows(connection, rows)
    finally:
        connection.close()
    print(f"Loaded {sum(totals.values()):,} captaincy rows into {db_path}")
    for status in ("unique", "resolved", "ambiguous", "unmatched",
                   "unsupported_role"):
        if totals[status]:
            print(f"  {status:<18} {totals[status]:>6,}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--through", type=int, default=dt.date.today().year,
                        help="expand open-ended ranges through this season")
    parser.add_argument("--from-season", type=int, default=1897)
    parser.add_argument("--delay", type=float, default=0.75,
                        help="delay between uncached page requests")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore cached MediaWiki responses")
    parser.add_argument("--inspect", action="store_true",
                        help="validate without writing CSV")
    parser.add_argument("--load", action="store_true",
                        help="write CSV and import it into the AFL database")
    parser.add_argument("--db", default=default_db("afl"))
    parser.add_argument("--user-agent", default=DEFAULT_UA)
    args = parser.parse_args(argv)

    if args.from_season > args.through:
        parser.error("--from-season cannot be later than --through")

    try:
        print(f"Wikipedia AFL captains {args.from_season}-{args.through}")
        rows, issues, payloads = scrape(
            through=args.through,
            minimum=args.from_season,
            directory=args.cache_dir,
            refresh=args.refresh,
            delay=args.delay,
            user_agent=args.user_agent,
        )
        inspect(rows, issues)
        if not rows:
            raise RuntimeError("no captain rows parsed")
        if args.inspect:
            return 0

        count = write_csv(args.output, rows)
        metadata = write_metadata(
            args.output, payloads, count, args.through, args.from_season
        )
        print(f"\nSaved {args.output} ({count:,} rows)")
        print(f"Saved {metadata}")
        print("Source: Wikipedia, CC BY-SA 4.0; revisions are in metadata.")
        if args.load:
            load_into_db(args.output, args.db)
        else:
            print(f"Next: python load_captains.py {args.output}")
        return 0
    except (FetchError, OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
