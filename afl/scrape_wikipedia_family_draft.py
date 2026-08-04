#!/usr/bin/env python3
"""Scrape AFL father-son and AFLW father-daughter draft selections.

Source:
    https://en.wikipedia.org/wiki/
    List_of_players_drafted_to_the_Australian_Football_League_under_the_father%E2%80%93son_rule

The script makes one MediaWiki API request, parses both tables, validates the
result, and writes:

* a combined CSV suitable for later player-linking/database import; and
* a metadata JSON file containing the Wikipedia revision and scrape details.

A cached API response is retained so ``--offline`` can rebuild the CSV without
another request. If a live request fails, the cache is used automatically when
available.

Dependencies:
    pip install requests beautifulsoup4

Examples:
    python -m afl.scrape_wikipedia_family_draft
    python -m afl.scrape_wikipedia_family_draft --refresh
    python -m afl.scrape_wikipedia_family_draft --offline
    python -m afl.scrape_wikipedia_family_draft --output father_son.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

API_URL = "https://en.wikipedia.org/w/api.php"
ARTICLE_URL = (
    "https://en.wikipedia.org/wiki/"
    "List_of_players_drafted_to_the_Australian_Football_League_"
    "under_the_father%E2%80%93son_rule"
)
PAGE_TITLE = (
    "List of players drafted to the Australian Football League "
    "under the father–son rule"
)
USER_AGENT = (
    "SportsDataLab-family-draft-scraper/1.0 "
    "(personal research; one page per run)"
)

CSV_COLUMNS = [
    "competition",
    "rule",
    "year",
    "drafted_player",
    "drafted_player_wikipedia_url",
    "club",
    "club_wikipedia_url",
    "father",
    "father_wikipedia_url",
    "selection_raw",
    "selection_pick",
    "selection_note",
    "games_played",
    "father_games_raw",
    "father_games_played",
    "father_games_note",
    "current_player",
    "changed_team",
    "status_marker",
    "source_url",
    "source_revision_id",
    "scraped_at_utc",
]

EXPECTED_HEADERS = {
    "year",
    "drafted player",
    "club",
    "father",
    "selection",
    "games played",
    "father's games played",
}


class ScrapeError(RuntimeError):
    """Raised when the source no longer matches the expected data shape."""


def _default_raw_dir() -> Path:
    """Use the project's central path helper when this script lives there."""
    try:
        from data_paths import raw_dir  # type: ignore
    except (ImportError, AttributeError):
        return Path("data") / "afl" / "raw"
    return Path(raw_dir("afl"))


def _default_cache_dir() -> Path:
    try:
        from data_paths import cache_dir  # type: ignore
    except (ImportError, AttributeError):
        return Path("data") / "afl" / "cache" / "wikipedia_family_draft"
    return Path(cache_dir("afl", "wikipedia_family_draft"))


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        tmp = Path(handle.name)
        handle.write(content)
        handle.flush()
    tmp.replace(path)


def _atomic_write_json(path: Path, payload: Any) -> None:
    content = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
    _atomic_write_bytes(path, (content + "\n").encode("utf-8"))


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def fetch_api_payload(
    cache_path: Path,
    *,
    offline: bool,
    timeout: float,
    no_cache: bool,
) -> tuple[dict[str, Any], str]:
    """Return MediaWiki API JSON and whether it came from live or cache."""
    if offline:
        if not cache_path.exists():
            raise ScrapeError(
                f"Offline mode requested but cache does not exist: {cache_path}"
            )
        return json.loads(cache_path.read_text(encoding="utf-8")), "cache"

    try:
        import requests
    except ImportError as exc:
        raise ScrapeError(
            "Missing dependency: requests. Run: pip install requests beautifulsoup4"
        ) from exc

    params = {
        "action": "parse",
        "page": PAGE_TITLE,
        "prop": "text|revid|displaytitle",
        "redirects": "1",
        "format": "json",
        "formatversion": "2",
    }
    try:
        response = requests.get(
            API_URL,
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise ScrapeError(f"MediaWiki API error: {payload['error']}")
        if "parse" not in payload or "text" not in payload["parse"]:
            raise ScrapeError("MediaWiki API response is missing parse.text")
    except (requests.RequestException, ValueError, ScrapeError) as exc:
        if cache_path.exists():
            print(
                f"warning: live fetch failed ({exc}); using cached response",
                file=sys.stderr,
            )
            return json.loads(cache_path.read_text(encoding="utf-8")), "cache-fallback"
        if isinstance(exc, ScrapeError):
            raise
        raise ScrapeError(f"Could not fetch Wikipedia: {exc}") from exc

    if not no_cache:
        _atomic_write_json(cache_path, payload)
    return payload, "live"


def _normalise_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _cell_text(cell: Any, *, keep_status_markers: bool = True) -> str:
    """Extract visible cell text while removing citation footnotes."""
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise ScrapeError(
            "Missing dependency: beautifulsoup4. Run: pip install beautifulsoup4"
        ) from exc

    clone = BeautifulSoup(str(cell), "html.parser")
    for node in clone.select(
        "sup.reference, span.reference, .mw-editsection, style, script, noscript"
    ):
        node.decompose()
    text = _normalise_space(clone.get_text(" ", strip=True))
    text = re.sub(r"\[(?:\d+|[a-z]|note\s*\d+)\]", "", text, flags=re.I)
    if not keep_status_markers:
        text = re.sub(r"\s*[\^*]\s*$", "", text)
    return _normalise_space(text)


def _normalise_header(value: str) -> str:
    value = value.casefold().replace("’", "'")
    value = re.sub(r"[^a-z0-9']+", " ", value)
    return _normalise_space(value)


def _first_wiki_url(cell: Any) -> str:
    anchor = cell.find("a", href=True)
    if not anchor:
        return ""
    href = str(anchor.get("href", "")).strip()
    if not href or href.startswith("#"):
        return ""
    return urljoin(ARTICLE_URL, href)


def _player_status(cell: Any) -> tuple[str, bool, bool, str]:
    text = _cell_text(cell, keep_status_markers=True)
    marker = ""

    # Current-player symbols are usually superscripts but may become plain
    # trailing text after a Wikipedia parser change.
    for sup in cell.find_all("sup"):
        classes = set(sup.get("class", []))
        if "reference" in classes:
            continue
        candidate = _normalise_space(sup.get_text(" ", strip=True))
        if candidate in {"^", "*"}:
            marker = candidate
            break
    if not marker:
        match = re.search(r"([\^*])\s*$", text)
        if match:
            marker = match.group(1)

    name = re.sub(r"\s*[\^*]\s*$", "", text).strip()
    changed_team = marker == "*"
    current_player = marker in {"^", "*"}
    return name, current_player, changed_team, marker


def _parse_optional_int(value: str) -> int | None:
    match = re.search(r"\d[\d,]*", value)
    if not match:
        return None
    return int(match.group(0).replace(",", ""))


def _split_number_note(value: str) -> tuple[int | None, str]:
    number = _parse_optional_int(value)
    note = value
    if number is not None:
        note = re.sub(r"^\s*\d[\d,]*\s*", "", note, count=1)
    else:
        note = re.sub(r"^\s*(?:N/?A|Unknown)\s*", "", note, flags=re.I)
    note = note.strip()
    if note.startswith("(") and note.endswith(")"):
        note = note[1:-1].strip()
    return number, note


def _table_competition(table: Any, table_index: int) -> tuple[str, str]:
    heading = table.find_previous(["h2", "h3"])
    heading_text = _normalise_space(heading.get_text(" ", strip=True)) if heading else ""
    folded = heading_text.casefold()
    if "women" in folded or "aflw" in folded:
        return "AFLW", "father-daughter"
    if folded == "afl" or "australian football league" in folded:
        return "AFL", "father-son"
    # Conservative fallback for the known page order only.
    return ("AFL", "father-son") if table_index == 0 else ("AFLW", "father-daughter")


def parse_rows(payload: dict[str, Any], scraped_at: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise ScrapeError(
            "Missing dependency: beautifulsoup4. Run: pip install beautifulsoup4"
        ) from exc

    parsed = payload.get("parse", {})
    html = parsed.get("text")
    if not isinstance(html, str) or not html.strip():
        raise ScrapeError("Cached/API payload contains no parse.text HTML")

    revision_id = parsed.get("revid")
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, Any]] = []
    matched_tables = 0

    for table_index, table in enumerate(soup.select("table.wikitable")):
        tr_list = table.find_all("tr")
        if not tr_list:
            continue
        header_cells = tr_list[0].find_all(["th", "td"], recursive=False)
        headers = [_normalise_header(_cell_text(cell)) for cell in header_cells]
        if not EXPECTED_HEADERS.issubset(set(headers)):
            continue

        matched_tables += 1
        competition, rule = _table_competition(table, matched_tables - 1)
        positions = {name: headers.index(name) for name in EXPECTED_HEADERS}

        for tr in tr_list[1:]:
            cells = tr.find_all(["th", "td"], recursive=False)
            if len(cells) < len(headers):
                continue

            year_text = _cell_text(cells[positions["year"]])
            if not re.fullmatch(r"\d{4}", year_text):
                continue

            player_cell = cells[positions["drafted player"]]
            club_cell = cells[positions["club"]]
            father_cell = cells[positions["father"]]
            selection_raw = _cell_text(cells[positions["selection"]])
            father_games_raw = _cell_text(cells[positions["father's games played"]])

            player, current, changed, marker = _player_status(player_cell)
            selection_pick, selection_note = _split_number_note(selection_raw)
            father_games, father_games_note = _split_number_note(father_games_raw)

            row = {
                "competition": competition,
                "rule": rule,
                "year": int(year_text),
                "drafted_player": player,
                "drafted_player_wikipedia_url": _first_wiki_url(player_cell),
                "club": _cell_text(club_cell, keep_status_markers=False),
                "club_wikipedia_url": _first_wiki_url(club_cell),
                "father": _cell_text(father_cell, keep_status_markers=False),
                "father_wikipedia_url": _first_wiki_url(father_cell),
                "selection_raw": selection_raw,
                "selection_pick": selection_pick,
                "selection_note": selection_note,
                "games_played": _parse_optional_int(
                    _cell_text(cells[positions["games played"]])
                ),
                "father_games_raw": father_games_raw,
                "father_games_played": father_games,
                "father_games_note": father_games_note,
                "current_player": int(current),
                "changed_team": int(changed),
                "status_marker": marker,
                "source_url": ARTICLE_URL,
                "source_revision_id": revision_id if revision_id is not None else "",
                "scraped_at_utc": scraped_at,
            }
            rows.append(row)

    if matched_tables != 2:
        raise ScrapeError(
            f"Expected two family-draft tables but matched {matched_tables}. "
            "Wikipedia may have changed the page structure."
        )

    rows.sort(key=lambda r: (r["competition"], r["year"], r["drafted_player"]))
    info = {
        "page_title": parsed.get("title", PAGE_TITLE),
        "display_title": parsed.get("displaytitle", ""),
        "revision_id": revision_id,
        "matched_tables": matched_tables,
    }
    return rows, info


def validate_rows(rows: Iterable[dict[str, Any]], min_afl: int, min_aflw: int) -> dict[str, int]:
    materialised = list(rows)
    counts = {
        "AFL": sum(1 for row in materialised if row["competition"] == "AFL"),
        "AFLW": sum(1 for row in materialised if row["competition"] == "AFLW"),
    }
    if counts["AFL"] < min_afl:
        raise ScrapeError(
            f"Validation failed: only {counts['AFL']} AFL rows; expected at least {min_afl}"
        )
    if counts["AFLW"] < min_aflw:
        raise ScrapeError(
            f"Validation failed: only {counts['AFLW']} AFLW rows; expected at least {min_aflw}"
        )

    required = ("year", "drafted_player", "club", "father")
    for number, row in enumerate(materialised, start=1):
        missing = [key for key in required if row.get(key) in (None, "")]
        if missing:
            raise ScrapeError(f"Validation failed at row {number}: missing {missing}")

    keys = [
        (row["competition"], row["year"], row["drafted_player"])
        for row in materialised
    ]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ScrapeError(f"Validation failed: duplicate player/year rows: {duplicates[:5]}")
    return counts


def render_csv(rows: list[dict[str, Any]]) -> bytes:
    import io

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def run(args: argparse.Namespace) -> int:
    output = Path(args.output)
    metadata_path = Path(args.metadata) if args.metadata else output.with_suffix(
        ".metadata.json"
    )
    cache_path = Path(args.cache)
    scraped_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    payload, fetch_mode = fetch_api_payload(
        cache_path,
        offline=args.offline,
        timeout=args.timeout,
        no_cache=args.no_cache,
    )
    rows, source_info = parse_rows(payload, scraped_at)
    counts = validate_rows(rows, args.min_afl, args.min_aflw)

    csv_bytes = render_csv(rows)
    _atomic_write_bytes(output, csv_bytes)

    years = {
        competition: {
            "from": min(row["year"] for row in rows if row["competition"] == competition),
            "to": max(row["year"] for row in rows if row["competition"] == competition),
        }
        for competition in ("AFL", "AFLW")
    }
    metadata = {
        "source": {
            "article_url": ARTICLE_URL,
            "api_url": API_URL,
            "page_title": source_info["page_title"],
            "display_title": source_info["display_title"],
            "revision_id": source_info["revision_id"],
            "licence": "CC BY-SA 4.0; see the source page for attribution terms",
        },
        "scrape": {
            "scraped_at_utc": scraped_at,
            "fetch_mode": fetch_mode,
            "user_agent": USER_AGENT,
            "matched_tables": source_info["matched_tables"],
        },
        "output": {
            "csv": str(output),
            "row_count": len(rows),
            "counts": counts,
            "years": years,
            "sha256": _sha256(csv_bytes),
            "columns": CSV_COLUMNS,
        },
    }
    _atomic_write_json(metadata_path, metadata)

    if not args.quiet:
        print(f"Source revision: {source_info['revision_id'] or 'unknown'} ({fetch_mode})")
        print(
            f"Parsed {len(rows):,} rows: AFL {counts['AFL']:,}, "
            f"AFLW {counts['AFLW']:,}"
        )
        print(f"CSV:      {output}")
        print(f"Metadata: {metadata_path}")
        if not args.no_cache:
            print(f"Cache:    {cache_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    raw = _default_raw_dir()
    cache = _default_cache_dir()
    parser = argparse.ArgumentParser(
        description="Scrape Wikipedia AFL father-son and AFLW father-daughter tables."
    )
    parser.add_argument(
        "--output",
        default=str(raw / "wikipedia_family_draft.csv"),
        help="combined CSV path",
    )
    parser.add_argument(
        "--metadata",
        default=None,
        help="metadata JSON path (default: OUTPUT with .metadata.json)",
    )
    parser.add_argument(
        "--cache",
        default=str(cache / "page_api.json"),
        help="cached MediaWiki API response",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="do not access the network; require the cached API response",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="do not update the local API-response cache",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--min-afl", type=int, default=100)
    parser.add_argument("--min-aflw", type=int, default=10)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (OSError, json.JSONDecodeError, ScrapeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
