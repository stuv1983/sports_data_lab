#!/usr/bin/env python3
"""Fetch and parse FootyWire AFL Rising Star nomination pages.

The importer saves one CSV per season plus one combined CSV.  It can then
load and link those rows into the AFL SQLite database through
``afl/load_rising_star.py``.

FootyWire's published terms prohibit automated page copying without prior
written permission.  Live HTTP fetching is therefore disabled unless the
operator explicitly supplies ``--permission-confirmed``.  The parser can
always work from pages saved manually with a browser via ``--html-dir``.

Examples
--------
Parse manually saved pages and load the database::

    python -m afl.fetch_footywire_rising_star \
        --html-dir saved_footywire_pages --load-db

After receiving written permission from FootyWire, save the HTML first::

    python -m afl.fetch_footywire_rising_star \
        --permission-confirmed --from 1993 --to 2026 --delay 3 \
        --save-html-only

Then parse the cached pages and load the database::

    python -m afl.fetch_footywire_rising_star \
        --html-dir data/afl/raw/footywire/rising_star/html --load-db

Expected local page names are ``1993.html`` or
``rising_star_nominees_1993.html``.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html as html_lib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = "https://www.footywire.com/afl/footy/rising_star_nominations?year={year}"
TERMS_URL = "https://www.footywire.com/afl/footy/info?if=t"
DEFAULT_START = 1993
DEFAULT_END = dt.date.today().year
USER_AGENT = (
    "SportsDataLab-RisingStar/1.0 (personal non-commercial research; "
    "permission-confirmed by operator)"
)

STAT_HEADERS = {
    "K": "kicks",
    "H": "handballs",
    "D": "disposals",
    "M": "marks",
    "G": "goals",
    "B": "behinds",
    "T": "tackles",
    "HO": "hitouts",
    "FF": "frees_for",
    "FA": "frees_against",
    "SC": "supercoach",
    "AF": "afl_fantasy",
}

CSV_FIELDS = [
    "source_key", "season", "nomination_round", "round_number",
    "player", "player_display", "name_key", "club", "team_display",
    "team_slug", "opponent", "opponent_display", "opponent_slug",
    *STAT_HEADERS.values(), "unavailable_stats", "is_season_winner", "winner_name",
    "winner_team", "player_url", "source_url", "scraped_at",
]

# FootyWire uses mascot-oriented URL slugs.  Keep the raw slug in the CSV,
# but map it to the same club identities used by the local AFL database.
TEAM_SLUGS = {
    "adelaide-crows": "Adelaide",
    "brisbane-bears": "Brisbane Bears",
    "brisbane-lions": "Brisbane Lions",
    "carlton-blues": "Carlton",
    "collingwood-magpies": "Collingwood",
    "essendon-bombers": "Essendon",
    "fitzroy-lions": "Fitzroy",
    "fremantle-dockers": "Fremantle",
    "geelong-cats": "Geelong",
    "gold-coast-suns": "Gold Coast",
    "greater-western-sydney-giants": "GWS",
    "gws-giants": "GWS",
    "hawthorn-hawks": "Hawthorn",
    "melbourne-demons": "Melbourne",
    "north-melbourne-kangaroos": "North Melbourne",
    "kangaroos": "North Melbourne",
    "port-adelaide-power": "Port Adelaide",
    "richmond-tigers": "Richmond",
    "st-kilda-saints": "St Kilda",
    "sydney-swans": "Sydney",
    "west-coast-eagles": "West Coast",
    "western-bulldogs": "Western Bulldogs",
    "footscray-bulldogs": "Western Bulldogs",
}


def clean(value: object) -> str:
    """Collapse HTML/Unicode whitespace into one ordinary space."""
    return re.sub(r"\s+", " ", html_lib.unescape(str(value or ""))).strip()


def normalise_name(value: object) -> str:
    """Use the repository normaliser when available, with a safe fallback."""
    try:
        from names import normalise_name as project_normalise
    except ImportError:
        import unicodedata

        text = unicodedata.normalize("NFKD", clean(value)).casefold()
        text = "".join(c for c in text if not unicodedata.combining(c))
        text = re.sub(r"[^a-z0-9]+", " ", text)
        return clean(text)
    return project_normalise(clean(value))


def _node_text(node) -> str:
    return clean(" ".join(node.itertext()))


def _href(node) -> str:
    links = node.xpath(".//a[@href]")
    return clean(links[0].get("href")) if links else ""


def _url_slug(url: str, prefixes: tuple[str, ...]) -> str:
    tail = urllib.parse.urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    for prefix in prefixes:
        if tail.startswith(prefix):
            return tail[len(prefix):]
    return tail


def player_name_from_url(url: str, display: str) -> str:
    """Recover the full player name from FootyWire's ``--name`` URL slug."""
    tail = urllib.parse.urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    if "--" not in tail:
        return clean(display)
    slug = tail.split("--", 1)[1]
    words = [word for word in slug.split("-") if word]
    return " ".join(word.capitalize() for word in words) or clean(display)


def canonical_team(url: str, display: str, season: int | None = None) -> tuple[str, str]:
    """Map FootyWire's display/URL identity to the local club identity.

    FootyWire sometimes points historical Brisbane Bears rows at the modern
    Brisbane Lions URL.  Historical display names therefore take precedence
    over URL slugs where the display is unambiguous.
    """
    slug = _url_slug(url, ("ty-", "th-", "tt-")) if url else ""
    display = clean(display)

    if display == "Bears":
        return "Brisbane Bears", slug
    if display == "Lions" and season is not None and season <= 1996:
        return "Fitzroy", slug

    if slug in TEAM_SLUGS:
        return TEAM_SLUGS[slug], slug

    # Display-name fallback.  "Lions" remains season-sensitive above.
    simple = {
        "Blues": "Carlton", "Bombers": "Essendon",
        "Bulldogs": "Western Bulldogs", "Cats": "Geelong",
        "Crows": "Adelaide", "Demons": "Melbourne",
        "Dockers": "Fremantle", "Eagles": "West Coast",
        "Giants": "GWS", "Hawks": "Hawthorn",
        "Kangaroos": "North Melbourne", "Magpies": "Collingwood",
        "Power": "Port Adelaide", "Saints": "St Kilda",
        "Suns": "Gold Coast", "Swans": "Sydney", "Tigers": "Richmond",
        "Bears": "Brisbane Bears",
    }
    return simple.get(display, display), slug


def _int_or_none(value: object) -> int | None:
    text = clean(value)
    if not text or text in {"-", "–", "—", "nan", "None"}:
        return None
    m = re.search(r"-?\d+", text.replace(",", ""))
    return int(m.group()) if m else None


def _winner(page_text: str, year: int) -> tuple[str, str]:
    match = re.search(
        rf"\b{year}\s+Rising Star winner is\s+(.+?)\s+of the\s+(.+?)\s*\.",
        page_text,
        flags=re.I,
    )
    if not match:
        return "", ""
    return clean(match.group(1)), clean(match.group(2))


def _name_signature(value: object) -> tuple[str, str] | None:
    """Return a conservative first-initial/surname signature."""
    tokens = re.findall(r"[a-z0-9]+", normalise_name(value))
    if len(tokens) < 2 or not tokens[0] or not tokens[1:]:
        return None
    return tokens[0][0], "".join(tokens[1:])


def _warn_template_leaks(rows: list[dict], year: int) -> list[str]:
    """Report unrendered source template placeholders.

    FootyWire's 1997 page emitted a literal ``$nomination.team.mascot`` for
    one nominee, corrupting every team-derived field for that row including
    the player URL.  The club was therefore unrecoverable from the row and
    the nomination silently became a ``club_mismatch`` days later.  Warning
    at parse time makes the next occurrence visible immediately.

    This warns rather than raises: one corrupt cell must not discard an
    otherwise complete season.  The row is still linkable through a reviewed
    override in ``rising_star_name_overrides.csv``.
    """
    leaked = []
    for row in rows:
        for field in ("club", "team_display", "team_slug", "opponent",
                      "opponent_display", "opponent_slug", "player_url"):
            value = str(row.get(field) or "")
            if any(token in value for token in ("${", "$nomination", "{{")):
                leaked.append(
                    f"{year} round {row.get('nomination_round')}: "
                    f"{row.get('player')!r} field {field!r} = {value!r}"
                )
    for message in leaked:
        print(f"WARNING: unrendered source template -- {message}", file=sys.stderr)
    if leaked:
        print("WARNING: affected rows cannot resolve their club automatically; "
              "add a reviewed correction to "
              "data/afl/reference/rising_star_name_overrides.csv",
              file=sys.stderr)
    return leaked


def _apply_page_quality_rules(rows: list[dict], winner_name: str, year: int) -> None:
    """Validate invariants and mark source-wide unavailable statistics."""
    _warn_template_leaks(rows, year)
    for row in rows:
        k, h, d = row.get("kicks"), row.get("handballs"), row.get("disposals")
        if None not in (k, h, d) and k + h != d:
            raise ValueError(
                f"{year} round {row['nomination_round']}: disposals {d} "
                f"do not equal kicks {k} + handballs {h}"
            )

    rounds = [row.get("round_number") for row in rows]
    if len(rounds) != len(set(rounds)):
        raise ValueError(f"{year}: duplicate nomination rounds found")

    player_keys = [row.get("name_key") for row in rows]
    if len(player_keys) != len(set(player_keys)):
        raise ValueError(f"{year}: a player appears more than once in the nomination table")

    optional = ("frees_for", "frees_against", "supercoach", "afl_fantasy")
    unavailable = []
    for field in optional:
        values = [row.get(field) for row in rows]
        if not any(value not in (None, 0) for value in values):
            unavailable.append(field)
            for row in rows:
                row[field] = None
    unavailable_text = "|".join(unavailable)
    for row in rows:
        row["unavailable_stats"] = unavailable_text
        row["is_season_winner"] = 0

    if winner_name:
        winner_key = normalise_name(winner_name)
        matches = [row for row in rows if row.get("name_key") == winner_key]
        if not matches:
            signature = _name_signature(winner_name)
            matches = [row for row in rows if signature and _name_signature(row.get("player")) == signature]
        if len(matches) == 1:
            matches[0]["is_season_winner"] = 1


def _find_table(root):
    """Return the nominations table and its header-row position."""
    for table in root.xpath("//table"):
        rows = table.xpath(".//tr")
        for index, row in enumerate(rows):
            cells = row.xpath("./th|./td")
            headings = [_node_text(cell).upper() for cell in cells]
            if len(headings) >= 4 and headings[:4] == ["RD", "PLAYER", "TEAM", "OPP"]:
                return table, index, headings
    raise ValueError("Could not locate the Rising Star nominations table")


def parse_page(html_text: str, year: int, source_url: str | None = None,
               scraped_at: str | None = None) -> list[dict]:
    """Parse one FootyWire year page into normalised row dictionaries."""
    try:
        from lxml import html as lxml_html
    except ImportError as exc:
        raise RuntimeError("Missing dependency. Run: pip install lxml") from exc

    root = lxml_html.fromstring(html_text)
    page_text = clean(root.text_content())
    if "AFL Rising Star Nominations" not in page_text:
        raise ValueError("Page does not look like a Rising Star nominations page")

    table, header_index, headings = _find_table(root)
    header_map = {name: idx for idx, name in enumerate(headings)}
    required = {"RD", "PLAYER", "TEAM", "OPP"}
    missing = required - set(header_map)
    if missing:
        raise ValueError(f"Nominations table is missing columns: {sorted(missing)}")

    winner_name, winner_team = _winner(page_text, year)
    source_url = source_url or BASE_URL.format(year=year)
    scraped_at = scraped_at or dt.datetime.now(dt.timezone.utc).isoformat()
    rows_out: list[dict] = []

    rows = table.xpath(".//tr")
    for tr in rows[header_index + 1:]:
        cells = tr.xpath("./th|./td")
        if len(cells) < 4:
            continue

        round_text = _node_text(cells[header_map["RD"]])
        round_number = _int_or_none(round_text)
        if not round_text or round_number is None:
            continue

        player_cell = cells[header_map["PLAYER"]]
        team_cell = cells[header_map["TEAM"]]
        opp_cell = cells[header_map["OPP"]]
        player_display = _node_text(player_cell)
        player_href = _href(player_cell)
        if not player_href or "pp-" not in player_href:
            raise ValueError(
                f"{year} round {round_text}: player link is missing or changed"
            )
        player_url = urllib.parse.urljoin(source_url, player_href)
        player = player_name_from_url(player_url, player_display)

        team_display = _node_text(team_cell)
        team_href = urllib.parse.urljoin(source_url, _href(team_cell))
        club, team_slug = canonical_team(team_href, team_display, year)

        opponent_display = _node_text(opp_cell)
        opponent_href = urllib.parse.urljoin(source_url, _href(opp_cell))
        opponent, opponent_slug = canonical_team(opponent_href, opponent_display, year)

        row = {
            "season": year,
            "nomination_round": round_text,
            "round_number": round_number,
            "player": player,
            "player_display": player_display,
            "name_key": normalise_name(player),
            "club": club,
            "team_display": team_display,
            "team_slug": team_slug,
            "opponent": opponent,
            "opponent_display": opponent_display,
            "opponent_slug": opponent_slug,
            "is_season_winner": 0,
            "winner_name": winner_name,
            "winner_team": winner_team,
            "player_url": player_url,
            "source_url": source_url,
            "scraped_at": scraped_at,
        }
        for source_header, field in STAT_HEADERS.items():
            idx = header_map.get(source_header)
            row[field] = _int_or_none(_node_text(cells[idx])) if idx is not None and idx < len(cells) else None

        identity = f"{year}|{round_text}|{player_url}|{team_slug}"
        row["source_key"] = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        rows_out.append(row)

    if not rows_out:
        raise ValueError(f"{year}: nominations table contained no data rows")

    _apply_page_quality_rules(rows_out, winner_name, year)

    duplicates = len(rows_out) - len({row["source_key"] for row in rows_out})
    if duplicates:
        raise ValueError(f"{year}: parsed {duplicates} duplicate source rows")
    return rows_out


def fetch_html(year: int, timeout: float, retries: int) -> str:
    url = BASE_URL.format(year=year)
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    })
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504} or attempt == retries:
                break
            retry_after = exc.headers.get("Retry-After")
            sleep_for = float(retry_after) if retry_after and retry_after.isdigit() else attempt * 5.0
            time.sleep(sleep_for)
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == retries:
                break
            time.sleep(attempt * 5.0)
    raise RuntimeError(f"{year}: fetch failed after {retries} attempts: {last_error}")


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def find_local_html(folder: Path, year: int) -> Path | None:
    candidates = [
        folder / f"{year}.html",
        folder / f"rising_star_nominees_{year}.html",
        folder / "html" / f"{year}.html",
    ]
    return next((path for path in candidates if path.exists()), None)


def default_db() -> str:
    try:
        from data_paths import sport_db
    except ImportError:      # data_paths sits beside this file; near-dead path
        from pathlib import Path
        return str(Path(__file__).resolve().parent / "gridley.db")
    return sport_db("afl", "gridley.db")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="start", type=int, default=DEFAULT_START)
    parser.add_argument("--to", dest="end", type=int, default=DEFAULT_END)
    parser.add_argument("--output-dir", type=Path,
                        default=Path("data/afl/raw/footywire/rising_star"))
    parser.add_argument("--html-dir", type=Path,
                        help="parse browser-saved pages instead of fetching")
    parser.add_argument("--permission-confirmed", action="store_true",
                        help="confirm written FootyWire permission for automation")
    parser.add_argument("--delay", type=float, default=3.0,
                        help="seconds between live requests (default 3)")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--refresh", action="store_true",
                        help="re-fetch pages even when cached HTML exists")
    parser.add_argument("--save-html-only", action="store_true",
                        help="save/copy canonical yearly HTML files without parsing CSVs")
    parser.add_argument("--keep-going", action="store_true",
                        help="continue with later seasons after a fetch or parse failure")
    parser.add_argument("--load-db", action="store_true",
                        help="load/link the combined CSV after parsing")
    parser.add_argument("--db", default=default_db())
    args = parser.parse_args(argv)

    if args.start < DEFAULT_START or args.end < args.start:
        parser.error(f"season range must be {DEFAULT_START} or later and ordered")
    if args.delay < 2.0 and not args.html_dir:
        parser.error("live request delay must be at least 2 seconds")
    if args.save_html_only and args.load_db:
        parser.error("--save-html-only cannot be combined with --load-db")
    if not args.html_dir and not args.permission_confirmed:
        print("Live fetch blocked: FootyWire's terms prohibit automated copying "
              "without prior written permission.", file=sys.stderr)
        print(f"Terms: {TERMS_URL}", file=sys.stderr)
        print("Use --html-dir for manually saved pages, or obtain permission and "
              "then pass --permission-confirmed.", file=sys.stderr)
        return 2

    html_cache = args.output_dir / "html"
    csv_dir = args.output_dir / "csv"
    all_rows: list[dict] = []
    years = list(range(args.start, args.end + 1))
    completed_years: list[int] = []
    failures: list[tuple[int, str]] = []

    for index, year in enumerate(years):
        cache_path = html_cache / f"{year}.html"
        source = ""
        try:
            if args.html_dir:
                local = find_local_html(args.html_dir, year)
                if local is None:
                    raise FileNotFoundError(f"no saved HTML found for {year}")
                html_text = local.read_text(encoding="utf-8", errors="replace")
                source = str(local)
                # Normalise any browser-saved input into the canonical cache.
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                if local.resolve() != cache_path.resolve():
                    cache_path.write_text(html_text, encoding="utf-8")
            elif cache_path.exists() and not args.refresh:
                html_text = cache_path.read_text(encoding="utf-8", errors="replace")
                source = "cache"
            else:
                html_text = fetch_html(year, args.timeout, args.retries)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(html_text, encoding="utf-8")
                source = "live"

            if args.save_html_only:
                completed_years.append(year)
                print(f"{year}: saved {cache_path} ({source})")
            else:
                rows = parse_page(html_text, year)
                write_csv(csv_dir / f"rising_star_nominees_{year}.csv", rows)
                all_rows.extend(rows)
                completed_years.append(year)
                unavailable = str(rows[0].get("unavailable_stats") or "")
                coverage = f"; unavailable: {unavailable.replace('|', ', ')}" if unavailable else ""
                print(f"{year}: {len(rows):>2} nominees ({source}){coverage}")
        except Exception as exc:
            failures.append((year, f"{type(exc).__name__}: {exc}"))
            print(f"{year}: FAILED — {failures[-1][1]}", file=sys.stderr)
            if not args.keep_going:
                return 1

        # Delay only after a real live request. Cache/local processing should
        # not be artificially slowed, and the final season needs no delay.
        if source == "live" and index < len(years) - 1:
            time.sleep(args.delay)

    if args.save_html_only:
        print(f"\nSaved {len(completed_years):,} HTML pages to {html_cache}")
        if failures:
            print("Failed seasons: " + ", ".join(str(year) for year, _ in failures),
                  file=sys.stderr)
            return 1
        return 0

    if not all_rows:
        print("No nomination rows were parsed.", file=sys.stderr)
        return 1

    all_rows.sort(key=lambda row: (
        int(row["season"]), int(row.get("round_number") or 999), row["player"]
    ))
    combined = args.output_dir / "rising_star_nominees.csv"
    write_csv(combined, all_rows)
    print(f"\nSaved {len(all_rows):,} rows across {len(completed_years)} seasons")
    print(f"Combined CSV: {combined}")

    if failures:
        print("Failed seasons: " + ", ".join(str(year) for year, _ in failures),
              file=sys.stderr)
        return 1

    if args.load_db:
        from . import load_rising_star
        result = load_rising_star.load_sources(args.db, [combined], verbose=True)
        if result.get("trusted", 0) == 0:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
