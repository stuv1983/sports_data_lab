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
import time
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
    "ineligible", "ineligible_reason",
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


def fetch_html(season: int, timeout: float = 30.0, retries: int = 4) -> str:
    """Return the rendered article HTML from the MediaWiki parse API.

    Retries on the responses that mean "later, not never". Wikipedia rate
    limits anonymous clients, and a backfill that walks thirty seasons will
    meet a 429 -- without a retry the first one cascades, because every
    following season fails immediately against a limiter that is still
    counting. `Retry-After` is honoured where the server sends one.
    """
    title = article_title(season)
    url = (f"{API}?action=parse&page={urllib.parse.quote(title)}"
           f"&prop=text&format=json&formatversion=2")
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json"})

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read())
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise PageNotFound(title) from exc
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504} or attempt == retries:
                raise
            retry_after = (exc.headers or {}).get("Retry-After")
            wait = (float(retry_after)
                    if str(retry_after or "").isdigit() else 5.0 * 2 ** (attempt - 1))
            print(f"  {title}: HTTP {exc.code}; waiting {wait:.0f}s "
                  f"(attempt {attempt} of {retries})", file=sys.stderr)
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == retries:
                raise
            time.sleep(5.0 * attempt)
    else:  # pragma: no cover - the loop either breaks or raises
        raise RuntimeError(f"{title}: fetch failed: {last_error}")

    if "error" in payload:
        code = str(payload["error"].get("code", ""))
        if code in {"missingtitle", "nosuchpageid", "invalidtitle"}:
            raise PageNotFound(title)
        raise RuntimeError(f"{title}: MediaWiki error {payload['error']}")
    return payload["parse"]["text"]


#: Symbols the article puts beside a name to point at its legend.
#:
#: These are data, not noise. `*` marks a nominee who is ineligible to win
#: because they were suspended -- a nomination that can never become a win,
#: which nothing else in the database records -- and `^` marks the season
#: winner far more reliably than reading it out of the article's prose.
MARKERS = "*^†‡§"
INELIGIBLE_MARKER = "*"
WINNER_MARKER = "^"


def _cell_text(cell) -> str:
    """Cell text with citation markers and sort keys removed.

    Legend markers are deliberately left in place; use _split_markers to
    separate them from the name they annotate.
    """
    for junk in cell.xpath(".//sup[contains(@class,'reference')]"
                           "|.//style|.//span[@class='sortkey']"):
        junk.getparent().remove(junk)
    return clean(" ".join(cell.itertext()))


def _split_markers(text: str) -> tuple[str, set[str]]:
    """Separate trailing legend markers from the text they annotate."""
    found = set(re.findall(f"[{re.escape(MARKERS)}]", text))
    return clean(re.sub(f"[{re.escape(MARKERS)}]+", "", text)), found


def _legend(root) -> dict[str, str]:
    """The article's own marker key, as ``marker -> meaning``.

    Read from the page rather than hardcoded so the stored reason is the
    source's own words -- including "NAB Rising Star" for the seasons the
    award carried a sponsor's name.

    Two renderings, because the article has used both: recent seasons put
    the key in a two-cell table row, while 2010 and its neighbours write it
    as a sentence under the table. Missing the second left nine correctly
    flagged nominations with no stated reason.
    """
    legend: dict[str, str] = {}
    for row in root.xpath("//table//tr"):
        cells = row.xpath("./th|./td")
        if len(cells) != 2:
            continue
        marker, meaning = _cell_text(cells[0]), _cell_text(cells[1])
        if len(marker) == 1 and marker in MARKERS and meaning:
            legend.setdefault(marker, meaning)

    for match in PROSE_LEGEND.finditer(clean(" ".join(root.itertext()))):
        marker, meaning = match.group(1), clean(match.group(2))
        if meaning:
            legend.setdefault(marker, meaning[0].upper() + meaning[1:])
    return legend


#: Prose form of the key: a marker, then its explanation, then a full stop.
#:
#: Both bounds are load-bearing. The explanation must begin within a few
#: characters of the marker, and the whole match must stay inside one
#: sentence. Without the first bound this matched the asterisk beside a
#: nominee's name in the table and ran on to the note far below it --
#: table text contains no full stop to stop at -- storing the entire
#: nominations table as the reason a player was ineligible.
PROSE_LEGEND = re.compile(
    rf"([{re.escape(MARKERS)}])\s*([^.]{{0,30}}?ineligible[^.]{{0,200}}\.)",
    re.I,
)


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


#: Heading wording accepted for each column we need. Seasons up to 2008
#: head the club column "Team" and later ones "Club"; both mean the
#: nominee's side.
COLUMN_HEADINGS = {
    "round": {"round", "rd"},
    "player": {"player", "nominee"},
    "club": {"club", "team"},
}


def _find_nominations_table(root):
    """Return the nominations table, its header row, and its column map.

    Columns are located by heading, never by position. The article orders
    them Round/Player/Club in most seasons but Player/Round/Club in 2016
    and 2017, and a parser that trusted position read the round number out
    of the player column and dropped both seasons.

    The table itself is found by its headings for the same reason: the
    article also carries eligibility and voting tables whose order relative
    to this one is not stable across seasons.
    """
    for table in root.xpath("//table[contains(@class,'wikitable')]"):
        for index, row in enumerate(table.xpath(".//tr")):
            headings = [_cell_text(cell).casefold()
                        for cell in row.xpath("./th|./td")]
            columns = {
                name: next((position for position, heading
                            in enumerate(headings) if heading in accepted), None)
                for name, accepted in COLUMN_HEADINGS.items()
            }
            if all(position is not None for position in columns.values()):
                return table, index, columns
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
    table, header_index, columns = _find_nominations_table(root)
    source_url = source_url or ARTICLE_URL.format(title=article_title(season))
    scraped_at = scraped_at or dt.datetime.now(dt.timezone.utc).isoformat()
    legend = _legend(root)
    unknown: set[str] = set()

    rows: list[dict] = []
    for tr in table.xpath(".//tr")[header_index + 1:]:
        cells = tr.xpath("./th|./td")
        if len(cells) <= max(columns.values()):
            continue
        round_cell = cells[columns["round"]]
        player_cell = cells[columns["player"]]
        club_cell = cells[columns["club"]]

        round_text, _ = _split_markers(_cell_text(round_cell))
        match = re.search(r"-?\d+", round_text)
        if match is None:
            continue
        round_number = int(match.group())
        player, markers = _split_markers(_cell_text(player_cell))
        if not player:
            continue
        unknown |= markers - {INELIGIBLE_MARKER, WINNER_MARKER}
        club = canonical_club(club_cell)
        player_slug = _link_target(player_cell)
        name_key = normalise_name(player)
        ineligible = INELIGIBLE_MARKER in markers

        rows.append({
            "ineligible": int(ineligible),
            "ineligible_reason": (legend.get(INELIGIBLE_MARKER, "") if ineligible
                                  else ""),
            "won_marker": WINNER_MARKER in markers,
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
            "team_display": _cell_text(club_cell),
            "team_slug": _link_target(club_cell),
            "opponent": "", "opponent_display": "", "opponent_slug": "",
            "unavailable_stats": UNAVAILABLE,
            "is_season_winner": 0,
            "winner_name": "",
            "winner_team": "",
            "player_url": (ARTICLE_URL.format(title=player_slug)
                           if player_slug else ""),
            "source_url": source_url,
            "scraped_at": scraped_at,
            "source": SOURCE_NAME,
        })

    if not rows:
        raise ValueError(f"{season}: the nominations table held no data rows")

    _apply_winner(rows, root, season)
    for row in rows:
        row.pop("won_marker", None)

    if unknown:
        # Warn rather than raise: one unrecognised marker must not discard
        # an otherwise complete season, but a new legend symbol that nobody
        # notices would quietly become data the database does not hold.
        print(f"WARNING: {season}: unrecognised legend marker(s) "
              f"{''.join(sorted(unknown))} beside a nominee's name; "
              f"legend reads {legend}", file=sys.stderr)

    seen_rounds = [row["round_number"] for row in rows]
    if len(seen_rounds) != len(set(seen_rounds)):
        raise ValueError(f"{season}: duplicate nomination rounds found")
    keys = [row["source_key"] for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{season}: a player appears more than once")
    return rows


def _apply_winner(rows: list[dict], root, season: int) -> None:
    """Mark the season winner, preferring the table's own marker.

    The article marks the winner with `^` and a gold row. That beats
    reading the winner out of the article's prose: the sentence wording
    varies by season and a regex over it will eventually match the wrong
    name, whereas the marker is structural.

    Prose remains the fallback for a season whose table carries no marker,
    and a season with no winner yet -- every season in progress -- ends
    with no row marked, which is correct rather than a failure.
    """
    marked = [row for row in rows if row.get("won_marker")]
    if len(marked) == 1:
        marked[0]["is_season_winner"] = 1
        winner_name = marked[0]["player"]
        winner_team = marked[0]["club"]
    else:
        winner_name = _winner(root, season)
        winner_team = ""
        if winner_name:
            key = normalise_name(winner_name)
            matches = [row for row in rows if row["name_key"] == key]
            if len(matches) == 1:
                matches[0]["is_season_winner"] = 1
                winner_team = matches[0]["club"]
    for row in rows:
        row["winner_name"] = winner_name
        row["winner_team"] = winner_team


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
    """Which nominations exist: round and nominee, nothing else."""
    return {f"{row.get('round_number')}|{row.get('name_key')}" for row in rows}


def _content_keys(rows) -> set[str]:
    """What the table says, including facts added long after the nomination.

    Ineligibility and the winner marker are edited into a season's table
    days or months later. Keyed on identity alone those edits would read as
    "no change" and never reach the database, so they belong here -- but
    not in _round_keys, or a nominee gaining an asterisk would be announced
    as a brand new nomination.
    """
    return {
        "|".join((
            str(row.get("round_number")), str(row.get("name_key")),
            str(row.get("club")), str(row.get("ineligible") or 0),
            str(row.get("ineligible_reason") or ""),
            str(row.get("is_season_winner") or 0),
        )) for row in rows
    }


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
    edited = _content_keys(rows) != _content_keys(before)
    if edited:
        write_csv(path, rows)
    ineligible = [row for row in rows if row.get("ineligible")]
    return {
        "season": season,
        "path": str(path),
        "rows": len(rows),
        "previous_rows": len(before),
        "added": len(added),
        "removed": len(removed),
        "changed": edited,
        "latest_round": max((row["round_number"] for row in rows), default=None),
        "ineligible": len(ineligible),
        "winner": next((row["player"] for row in rows
                        if row.get("is_season_winner")), None),
        "new_nominations": [
            {"round": row["round_number"], "player": row["player"],
             "club": row["club"], "ineligible": bool(row.get("ineligible"))}
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


def _report(result: dict) -> None:
    summary = (f"{result['season']}: {result['rows']:>2} nominations "
               f"(latest round {result['latest_round']})")
    if result.get("ineligible"):
        summary += f", {result['ineligible']} ineligible"
    if result.get("winner"):
        summary += f", won by {result['winner']}"
    print(summary)
    for nomination in result["new_nominations"]:
        flag = "  [ineligible]" if nomination.get("ineligible") else ""
        print(f"  + round {nomination['round']:>2}  {nomination['player']} "
              f"({nomination['club']}){flag}")
    if result["removed"]:
        print(f"  {result['removed']} row(s) no longer listed")
    if not result["changed"]:
        print("  no change since the last fetch")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    this_year = dt.date.today().year
    parser.add_argument("--season", type=int, default=None,
                        help="one season to refresh (default: this year)")
    parser.add_argument("--from", dest="start", type=int, default=None,
                        help=f"first season of a range (from {FIRST_SEASON})")
    parser.add_argument("--to", dest="end", type=int, default=None,
                        help="last season of a range (default: this year)")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--delay", type=float, default=2.0,
                        help="seconds between requests in a range (default 2)")
    parser.add_argument("--load-db", action="store_true",
                        help="reload the Rising Star table after fetching")
    parser.add_argument("--db", default=None)
    args = parser.parse_args(argv)

    if args.season is not None and (args.start or args.end):
        parser.error("use --season for one year or --from/--to for a range")
    if args.season is not None:
        seasons = [args.season]
    elif args.start or args.end:
        seasons = list(range(args.start or FIRST_SEASON, (args.end or this_year) + 1))
    else:
        seasons = [this_year]
    if seasons[0] < FIRST_SEASON:
        parser.error(f"the award starts in {FIRST_SEASON}")
    if not seasons or seasons[-1] < seasons[0]:
        parser.error("season range must be ordered")
    if args.delay < 0.5 and len(seasons) > 1:
        parser.error("a multi-season fetch needs at least 0.5s between requests")

    changed, missing, failed = 0, [], []
    for index, season in enumerate(seasons):
        try:
            result = refresh_season(season, args.output_dir, args.timeout)
        except PageNotFound:
            missing.append(season)
            print(f"{season}: no Wikipedia article; skipped")
        except (RuntimeError, ValueError, OSError) as exc:
            # One malformed season must not abandon the rest of a backfill.
            failed.append(season)
            print(f"{season}: FAILED -- {type(exc).__name__}: {exc}",
                  file=sys.stderr)
        else:
            _report(result)
            changed += bool(result["changed"])
        if index < len(seasons) - 1:
            time.sleep(args.delay)

    if len(seasons) > 1:
        print(f"\n{len(seasons)} seasons checked, {changed} written"
              + (f", {len(missing)} with no article" if missing else "")
              + (f", {len(failed)} failed" if failed else ""))

    if args.load_db:
        from utils.afl import load_rising_star
        loaded = load_rising_star.refresh_default(
            db_path=args.db or default_db(), verbose=True)
        if not loaded or not loaded.get("trusted"):
            return 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
