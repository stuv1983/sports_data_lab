#!/usr/bin/env python3
"""Tkinter Wikipedia scraper for NBA, NFL and MLB reference data.

The user selects an output root. The application creates:

    <root>/nba/team.csv
    <root>/nfl/team.csv
    <root>/mlb/team.csv

It also writes Hall of Fame, league-section and per-team reference data,
including championships, records, leaders, awards, captains and retired
numbers when those sections exist on the relevant Wikipedia page.

Wikipedia is not a complete source for every official game/player statistic.
This program therefore preserves the structured reference material actually
present on the requested pages instead of pretending absent data was scraped.

Dependencies:
    python -m pip install requests beautifulsoup4 pandas lxml
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, unquote, urlparse

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText


APP_NAME = "Wikipedia Sports Scraper"
APP_VERSION = "2.0.0"
API_URL = "https://en.wikipedia.org/w/api.php"
BASE_WIKI_URL = "https://en.wikipedia.org/wiki/"
USER_AGENT = (
    f"WikiSportsScraper/{APP_VERSION} "
    "(personal research; low-volume cached MediaWiki API client)"
)

SPORTS: dict[str, dict[str, Any]] = {
    "nba": {
        "label": "NBA",
        "league_page": "National Basketball Association",
        "hall_page": "List of players in the Naismith Memorial Basketball Hall of Fame",
        "expected_teams": 30,
        "team_columns": ("team",),
    },
    "nfl": {
        "label": "NFL",
        "league_page": "National Football League",
        "hall_page": "List of Pro Football Hall of Fame inductees",
        "expected_teams": 32,
        "team_columns": ("team", "club"),
    },
    "mlb": {
        "label": "MLB",
        "league_page": "Major League Baseball",
        "hall_page": "List of members of the National Baseball Hall of Fame",
        "expected_teams": 30,
        "team_columns": ("team", "club"),
    },
}

# Headings vary between team pages. These phrases are deliberately broad,
# while year-prefixed history headings are rejected to avoid scraping every
# season narrative merely because it contains the word "championship".
COMMON_SECTION_PHRASES = (
    "championships",
    "championship",
    "franchise leaders",
    "all-time leaders",
    "team leaders",
    "statistical leaders",
    "statistics, records, and awards",
    "statistics and records",
    "records, retired numbers, and awards",
    "records and achievements",
    "notable records and achievements",
    "team records",
    "franchise records",
    "club records",
    "individual records",
    "individual awards",
    "team awards",
    "award recipients",
    "awards",
    "retired numbers",
    "retired number",
    "hall of fame",
    "hall of famers",
    "captains",
    "captaincy",
    "honours",
    "honors",
    "season-by-season record",
    "all-pro selections",
    "all-decade",
    "anniversary team",
)

SPORT_SECTION_PHRASES = {
    "nba": (
        "nba championships",
        "conference titles",
        "division titles",
        "fiba hall of fame",
    ),
    "nfl": (
        "super bowl championships",
        "afc championships",
        "nfc championships",
        "conference championships",
        "division championships",
        "pro football hall of famers",
        "first-team all-pro selections",
    ),
    "mlb": (
        "world series championships",
        "league pennants",
        "division titles",
        "baseball hall of famers",
        "world series titles",
    ),
}

LONG_RECORD_FIELDS = [
    "sport",
    "team",
    "team_slug",
    "section",
    "record_type",
    "table_index",
    "row_index",
    "label",
    "value",
    "data_json",
    "source_page",
    "source_url",
    "source_revision",
    "scraped_at_utc",
]

LOG_FIELDS = [
    "timestamp",
    "sport",
    "item",
    "status",
    "message",
    "output_path",
]


class ScrapeCancelled(RuntimeError):
    """Raised internally when the user presses Cancel."""


@dataclass(frozen=True)
class PageData:
    requested_title: str
    title: str
    revision: int | None
    html: str
    source_url: str
    fetched_from: str


@dataclass
class ScrapeOptions:
    output_root: Path
    sports: list[str]
    refresh: bool
    request_delay: float
    timeout: float


class MediaWikiClient:
    """Small cached MediaWiki API client with retries and cache fallback."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        refresh: bool,
        timeout: float,
        delay: float,
        cancel_event: threading.Event,
    ) -> None:
        self.cache_dir = cache_dir
        self.refresh = refresh
        self.timeout = timeout
        self.delay = max(0.0, delay)
        self.cancel_event = cancel_event
        self._last_request = 0.0
        self._session = None

    @property
    def session(self):
        if self._session is None:
            import requests

            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                }
            )
            self._session = session
        return self._session

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise ScrapeCancelled("Scrape cancelled by user")

    def _cache_path(self, title: str) -> Path:
        digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
        stem = safe_slug(title)[:90] or "page"
        return self.cache_dir / f"{stem}-{digest}.json"

    def _respect_delay(self) -> None:
        elapsed = time.monotonic() - self._last_request
        remaining = self.delay - elapsed
        while remaining > 0:
            self._check_cancelled()
            sleep_for = min(0.1, remaining)
            time.sleep(sleep_for)
            remaining -= sleep_for

    def fetch_page(self, title: str) -> PageData:
        self._check_cancelled()
        cache_path = self._cache_path(title)

        if cache_path.exists() and not self.refresh:
            try:
                return self._read_cache(cache_path, title, "cache")
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                # A corrupt cache should trigger a live refresh.
                pass

        params = {
            "action": "parse",
            "page": title,
            "prop": "text|revid|displaytitle",
            "redirects": "1",
            "disableeditsection": "1",
            "disabletoc": "1",
            "format": "json",
            "formatversion": "2",
            "maxlag": "5",
        }

        delay = 1.5
        last_error: Exception | None = None
        for attempt in range(1, 5):
            self._check_cancelled()
            self._respect_delay()
            try:
                response = self.session.get(
                    API_URL,
                    params=params,
                    timeout=self.timeout,
                )
                self._last_request = time.monotonic()
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else delay
                    self._interruptible_sleep(wait)
                    delay *= 2
                    continue
                response.raise_for_status()
                payload = response.json()
                if "error" in payload:
                    raise RuntimeError(f"MediaWiki API error: {payload['error']}")
                parsed = payload.get("parse") or {}
                html = parsed.get("text")
                if not isinstance(html, str) or not html.strip():
                    raise RuntimeError("MediaWiki response did not contain parse.text")
                resolved_title = clean_text(parsed.get("title") or title)
                revision = optional_int(parsed.get("revid"))
                data = {
                    "requested_title": title,
                    "title": resolved_title,
                    "revision": revision,
                    "html": html,
                    "source_url": page_url(resolved_title),
                    "cached_at_utc": utc_now(),
                }
                atomic_write_json(cache_path, data)
                return PageData(
                    requested_title=title,
                    title=resolved_title,
                    revision=revision,
                    html=html,
                    source_url=data["source_url"],
                    fetched_from="live",
                )
            except ScrapeCancelled:
                raise
            except Exception as exc:  # retries are intentionally broad here
                last_error = exc
                if attempt < 4:
                    self._interruptible_sleep(delay)
                    delay *= 2

        if cache_path.exists():
            try:
                return self._read_cache(cache_path, title, "cache-fallback")
            except Exception:
                pass
        raise RuntimeError(f"Could not fetch {title!r}: {last_error}")

    def _read_cache(self, path: Path, requested: str, source: str) -> PageData:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return PageData(
            requested_title=requested,
            title=clean_text(payload["title"]),
            revision=optional_int(payload.get("revision")),
            html=str(payload["html"]),
            source_url=str(payload.get("source_url") or page_url(payload["title"])),
            fetched_from=source,
        )

    def _interruptible_sleep(self, seconds: float) -> None:
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end:
            self._check_cancelled()
            time.sleep(min(0.1, max(0.0, end - time.monotonic())))


class RunReporter:
    """Collect run logs and forward UI events without touching Tk from a worker."""

    def __init__(
        self,
        emit: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self.emit = emit
        self.rows: list[dict[str, str]] = []
        self.counts = {"PASS": 0, "WARN": 0, "ERROR": 0, "SKIP": 0}

    def log(
        self,
        status: str,
        message: str,
        *,
        sport: str = "",
        item: str = "",
        output_path: str | Path = "",
    ) -> None:
        status = status.upper()
        timestamp = utc_now()
        row = {
            "timestamp": timestamp,
            "sport": sport.upper(),
            "item": item,
            "status": status,
            "message": message,
            "output_path": str(output_path),
        }
        self.rows.append(row)
        if status in self.counts:
            self.counts[status] += 1
        self.emit("log", row)
        self.emit("counts", dict(self.counts))

    def progress(self, current: int, maximum: int, text: str) -> None:
        self.emit(
            "progress",
            {"current": current, "maximum": maximum, "text": text},
        )


class SportsScraper:
    def __init__(
        self,
        options: ScrapeOptions,
        reporter: RunReporter,
        cancel_event: threading.Event,
    ) -> None:
        self.options = options
        self.reporter = reporter
        self.cancel_event = cancel_event
        self.scraped_at = utc_now()
        self.completed = 0
        self.maximum = sum(
            SPORTS[sport]["expected_teams"] + 3 for sport in options.sports
        )

    def run(self) -> dict[str, Any]:
        self.options.output_root.mkdir(parents=True, exist_ok=True)
        self.reporter.log(
            "PASS",
            f"Output root ready: {self.options.output_root}",
            item="output folder",
            output_path=self.options.output_root,
        )
        summaries: dict[str, Any] = {}

        for sport in self.options.sports:
            self._check_cancelled()
            summaries[sport] = self._scrape_sport(sport)

        log_path = self.options.output_root / "scrape_log.csv"
        write_rows_csv(log_path, self.reporter.rows, LOG_FIELDS)
        metadata = {
            "application": APP_NAME,
            "version": APP_VERSION,
            "generated_at_utc": utc_now(),
            "selected_sports": self.options.sports,
            "output_root": str(self.options.output_root),
            "counts": self.reporter.counts,
            "sports": summaries,
            "source": "English Wikipedia via MediaWiki API",
            "source_licence": "CC BY-SA; see each source page for attribution terms",
        }
        atomic_write_json(self.options.output_root / "scrape_metadata.json", metadata)
        return metadata

    def _scrape_sport(self, sport: str) -> dict[str, Any]:
        config = SPORTS[sport]
        label = config["label"]
        sport_dir = self.options.output_root / sport
        teams_dir = sport_dir / "teams"
        cache_dir = self.options.output_root / "_cache" / "wikipedia" / sport
        sport_dir.mkdir(parents=True, exist_ok=True)
        teams_dir.mkdir(parents=True, exist_ok=True)
        client = MediaWikiClient(
            cache_dir,
            refresh=self.options.refresh,
            timeout=self.options.timeout,
            delay=self.options.request_delay,
            cancel_event=self.cancel_event,
        )

        self.reporter.log("PASS", f"Starting {label}", sport=sport, item="sport")

        # 1. League page and current team catalogue.
        league_page = client.fetch_page(config["league_page"])
        team_rows = parse_team_catalog(league_page, sport)
        team_path = sport_dir / "team.csv"
        if not team_rows:
            raise RuntimeError(f"No {label} team rows were found")
        write_dynamic_csv(team_path, team_rows, preferred_team_fields(team_rows))
        self._step(f"{label}: team catalogue")

        expected = int(config["expected_teams"])
        if len(team_rows) < expected:
            self.reporter.log(
                "WARN",
                f"Saved only {len(team_rows)} teams; expected at least {expected}",
                sport=sport,
                item="team.csv",
                output_path=team_path,
            )
        else:
            self.reporter.log(
                "PASS",
                f"Saved {len(team_rows)} teams",
                sport=sport,
                item="team.csv",
                output_path=team_path,
            )

        # Adjust the progress maximum if a league expands beyond the baseline.
        if len(team_rows) > expected:
            self.maximum += len(team_rows) - expected

        # 2. League championships, trophies and awards sections.
        league_records = extract_relevant_sections(
            league_page,
            sport=sport,
            team="",
            include_league_sections=True,
        )
        league_path = sport_dir / "league_stats.csv"
        write_rows_csv(league_path, league_records, LONG_RECORD_FIELDS)
        self._step(f"{label}: league sections")
        self.reporter.log(
            "PASS" if league_records else "WARN",
            f"Saved {len(league_records)} league reference rows",
            sport=sport,
            item="league_stats.csv",
            output_path=league_path,
        )

        # 3. Hall of Fame page.
        hall_records: list[dict[str, Any]] = []
        try:
            hall_page = client.fetch_page(config["hall_page"])
            hall_records = extract_hall_of_fame(hall_page, sport)
            hall_path = sport_dir / "hall_of_fame.csv"
            write_rows_csv(hall_path, hall_records, LONG_RECORD_FIELDS)
            self.reporter.log(
                "PASS" if hall_records else "WARN",
                f"Saved {len(hall_records)} Hall of Fame rows",
                sport=sport,
                item="hall_of_fame.csv",
                output_path=hall_path,
            )
        except ScrapeCancelled:
            raise
        except Exception as exc:
            self.reporter.log(
                "ERROR",
                f"Hall of Fame scrape failed: {exc}",
                sport=sport,
                item=config["hall_page"],
            )
        self._step(f"{label}: Hall of Fame")

        # 4. Every team page. A failure is isolated to that team.
        all_team_records: list[dict[str, Any]] = []
        team_failures = 0
        for index, team_row in enumerate(team_rows, start=1):
            self._check_cancelled()
            team_name = clean_text(team_row.get("team") or team_row.get("club"))
            if not team_name:
                team_failures += 1
                self.reporter.log(
                    "ERROR",
                    "Team row has no team name",
                    sport=sport,
                    item=f"row {index}",
                )
                self._step(f"{label}: unnamed team")
                continue
            try:
                page_title = clean_text(team_row.get("team_page_title") or team_name)
                team_page = client.fetch_page(page_title)
                records = extract_relevant_sections(
                    team_page,
                    sport=sport,
                    team=team_name,
                    include_league_sections=False,
                )
                all_team_records.extend(records)
                per_team_path = teams_dir / f"{safe_slug(team_name)}.csv"
                write_rows_csv(per_team_path, records, LONG_RECORD_FIELDS)
                if records:
                    self.reporter.log(
                        "PASS",
                        f"{len(records)} reference rows",
                        sport=sport,
                        item=team_name,
                        output_path=per_team_path,
                    )
                else:
                    self.reporter.log(
                        "WARN",
                        "Page fetched, but no supported statistics/records sections were found",
                        sport=sport,
                        item=team_name,
                        output_path=per_team_path,
                    )
            except ScrapeCancelled:
                raise
            except Exception as exc:
                team_failures += 1
                self.reporter.log(
                    "ERROR",
                    f"Team scrape failed: {exc}",
                    sport=sport,
                    item=team_name,
                )
            self._step(f"{label}: {team_name} ({index}/{len(team_rows)})")

        team_stats_path = sport_dir / "team_stats.csv"
        write_rows_csv(team_stats_path, all_team_records, LONG_RECORD_FIELDS)
        self.reporter.log(
            "PASS" if all_team_records else "WARN",
            f"Saved {len(all_team_records)} consolidated team reference rows",
            sport=sport,
            item="team_stats.csv",
            output_path=team_stats_path,
        )

        sport_metadata = {
            "sport": sport,
            "label": label,
            "generated_at_utc": utc_now(),
            "teams": len(team_rows),
            "team_page_failures": team_failures,
            "team_reference_rows": len(all_team_records),
            "league_reference_rows": len(league_records),
            "hall_of_fame_rows": len(hall_records),
            "league_page": league_page.source_url,
            "league_revision": league_page.revision,
            "outputs": {
                "team": str(team_path),
                "team_stats": str(team_stats_path),
                "league_stats": str(league_path),
                "hall_of_fame": str(sport_dir / "hall_of_fame.csv"),
                "teams_directory": str(teams_dir),
            },
        }
        atomic_write_json(sport_dir / "metadata.json", sport_metadata)
        return sport_metadata

    def _step(self, text: str) -> None:
        self.completed += 1
        self.reporter.progress(self.completed, self.maximum, text)

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise ScrapeCancelled("Scrape cancelled by user")


def check_dependencies() -> list[str]:
    missing: list[str] = []
    for module, package in (
        ("requests", "requests"),
        ("bs4", "beautifulsoup4"),
        ("pandas", "pandas"),
        ("lxml", "lxml"),
    ):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    return missing


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if value != value:  # pandas/numpy NaN
            return ""
    except Exception:
        pass
    text = unicodedata.normalize("NFKC", str(value)).replace("\xa0", " ")
    text = re.sub(r"\[\s*(?:\d+|[a-z]|note\s*\d+)\s*\]", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        match = re.search(r"-?\d+", str(value))
        return int(match.group(0)) if match else None


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def safe_slug(value: str) -> str:
    text = unicodedata.normalize("NFKD", clean_text(value)).encode(
        "ascii", "ignore"
    ).decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return text or "item"


def page_url(title: str) -> str:
    return BASE_WIKI_URL + quote(clean_text(title).replace(" ", "_"), safe="_()'-")


def title_from_href(href: str) -> str:
    href = clean_text(href)
    if not href:
        return ""
    parsed = urlparse(href)
    path = parsed.path if parsed.scheme else href.split("#", 1)[0]
    marker = "/wiki/"
    if marker not in path:
        return ""
    title = unquote(path.split(marker, 1)[1]).replace("_", " ")
    if not title or ":" in title:
        return ""
    return clean_text(title)


def normalise_header(value: Any) -> str:
    text = clean_text(value).casefold()
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or "column"


def dedupe_names(names: Iterable[str]) -> list[str]:
    out: list[str] = []
    counts: dict[str, int] = {}
    for raw in names:
        name = normalise_header(raw)
        counts[name] = counts.get(name, 0) + 1
        out.append(name if counts[name] == 1 else f"{name}_{counts[name]}")
    return out


def flatten_columns(columns: Any) -> list[str]:
    names: list[str] = []
    for column in columns:
        if isinstance(column, tuple):
            parts = [
                clean_text(part)
                for part in column
                if clean_text(part) and not clean_text(part).startswith("Unnamed")
            ]
            unique: list[str] = []
            for part in parts:
                if part not in unique:
                    unique.append(part)
            names.append(" ".join(unique) if unique else "column")
        else:
            names.append(clean_text(column))
    return dedupe_names(names)


def dataframe_rows(df: Any) -> list[dict[str, str]]:
    df = df.copy()
    df.columns = flatten_columns(df.columns)
    rows: list[dict[str, str]] = []
    for _, series in df.iterrows():
        row = {str(column): clean_text(value) for column, value in series.items()}
        if any(value for value in row.values()):
            rows.append(row)
    return rows


def parse_team_catalog(page: PageData, sport: str) -> list[dict[str, Any]]:
    """Find the current-team table by column signature, not table position."""
    from bs4 import BeautifulSoup
    import pandas as pd

    soup = BeautifulSoup(page.html, "html.parser")
    config = SPORTS[sport]
    candidates: list[tuple[int, Any, list[dict[str, str]]]] = []

    for table in soup.find_all("table"):
        try:
            frames = pd.read_html(io.StringIO(str(table)))
        except (ValueError, ImportError):
            continue
        if not frames:
            continue
        rows = dataframe_rows(frames[0])
        if not rows:
            continue
        columns = set(rows[0])
        team_column = next(
            (name for name in config["team_columns"] if name in columns),
            None,
        )
        if not team_column:
            continue
        # Real league tables have many clubs and division/conference/league data.
        score = len(rows)
        if columns & {"conference", "division", "league"}:
            score += 100
        candidates.append((score, table, rows))

    if not candidates:
        return []
    _score, team_table, rows = max(candidates, key=lambda item: item[0])

    # Map displayed team names to their article titles from the selected table.
    link_map: dict[str, str] = {}
    for anchor in team_table.find_all("a", href=True):
        label = clean_text(anchor.get_text(" ", strip=True))
        title = title_from_href(anchor.get("href", ""))
        if label and title:
            link_map.setdefault(label.casefold(), title)

    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        team_column = next(
            (name for name in config["team_columns"] if clean_text(row.get(name))),
            None,
        )
        if not team_column:
            continue
        team_name = clean_text(row[team_column])
        team_name = re.sub(r"[\*†‡]+$", "", team_name).strip()
        if not team_name or team_name.casefold() in seen:
            continue
        seen.add(team_name.casefold())
        title = link_map.get(team_name.casefold(), team_name)
        clean_row = {key: clean_text(value) for key, value in row.items()}
        if team_column != "team":
            clean_row["team"] = team_name
        else:
            clean_row["team"] = team_name
        clean_row.update(
            {
                "team_page_title": title,
                "team_url": page_url(title),
                "source_page": page.title,
                "source_url": page.source_url,
                "source_revision": page.revision or "",
                "scraped_at_utc": utc_now(),
            }
        )
        output.append(clean_row)

    return output


def preferred_team_fields(rows: list[dict[str, Any]]) -> list[str]:
    preferred = [
        "conference",
        "league",
        "division",
        "team",
        "location",
        "city",
        "arena",
        "stadium",
        "capacity",
        "founded",
        "joined",
        "first_season",
        "head_coach",
        "team_page_title",
        "team_url",
        "source_page",
        "source_url",
        "source_revision",
        "scraped_at_utc",
    ]
    all_fields = {key for row in rows for key in row}
    return [field for field in preferred if field in all_fields] + sorted(
        all_fields - set(preferred)
    )


def heading_text(heading: Any) -> str:
    return clean_text(heading.get_text(" ", strip=True)).replace("[ edit ]", "").strip()


def heading_wrapper(heading: Any) -> Any:
    parent = heading.parent
    classes = set(parent.get("class", [])) if getattr(parent, "attrs", None) else set()
    return parent if parent.name == "div" and "mw-heading" in classes else heading


def next_heading_in_sibling(node: Any) -> Any | None:
    if getattr(node, "name", None) in {"h2", "h3", "h4", "h5"}:
        return node
    classes = set(node.get("class", [])) if getattr(node, "attrs", None) else set()
    if getattr(node, "name", None) == "div" and "mw-heading" in classes:
        return node.find(["h2", "h3", "h4", "h5"], recursive=False)
    return None


def direct_section_nodes(heading: Any) -> list[Any]:
    """Content after a heading up to the next heading of any level.

    Stopping at any heading avoids duplicate extraction from broad parent
    sections such as "Records, retired numbers, and awards" and their child
    sections.
    """
    nodes: list[Any] = []
    node = heading_wrapper(heading).find_next_sibling()
    while node is not None:
        if next_heading_in_sibling(node) is not None:
            break
        nodes.append(node)
        node = node.find_next_sibling()
    return nodes


def relevant_heading(text: str, sport: str, *, league: bool) -> bool:
    folded = clean_text(text).casefold()
    folded = re.sub(r"\s+", " ", folded)
    if not folded:
        return False
    # Avoid season-history headings such as "2023–24: 18th championship".
    if re.match(r"^[\"'“”‘’]?(?:18|19|20)\d{2}", folded):
        return False
    # Team-history chapter titles such as “The Idiots: 2004 World Series
    # Championship” are narrative, not reference/statistics sections.
    if ":" in folded and "championship" in folded:
        return False

    phrases = list(COMMON_SECTION_PHRASES) + list(SPORT_SECTION_PHRASES[sport])
    if league:
        phrases += [
            "teams",
            "trophies and awards",
            "team trophies",
            "player and coach awards",
            "achievements and records",
        ]
    return any(phrase in folded for phrase in phrases)


def extract_relevant_sections(
    page: PageData,
    *,
    sport: str,
    team: str,
    include_league_sections: bool,
) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page.html, "html.parser")
    records: list[dict[str, Any]] = []
    selected: list[tuple[Any, str]] = []
    for heading in soup.find_all(["h2", "h3", "h4"]):
        section = heading_text(heading)
        if relevant_heading(section, sport, league=include_league_sections):
            # Team catalogue is already written to team.csv.
            if include_league_sections and section.casefold() == "teams":
                continue
            selected.append((heading, section))

    for heading, section in selected:
        nodes = direct_section_nodes(heading)
        records.extend(
            records_from_nodes(
                nodes,
                sport=sport,
                team=team,
                section=section,
                page=page,
            )
        )
    return dedupe_long_records(records)


def extract_hall_of_fame(page: PageData, sport: str) -> list[dict[str, Any]]:
    """Extract Hall of Fame member/inductee tables and lists.

    If the page has no matching member heading, all wikitables are used as a
    conservative fallback because these dedicated pages are themselves lists.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page.html, "html.parser")
    records: list[dict[str, Any]] = []
    matched = False
    for heading in soup.find_all(["h2", "h3", "h4"]):
        section = heading_text(heading)
        folded = section.casefold()
        if any(word in folded for word in ("members", "inductees", "players")):
            matched = True
            records.extend(
                records_from_nodes(
                    direct_section_nodes(heading),
                    sport=sport,
                    team="",
                    section=section,
                    page=page,
                )
            )

    if not matched or not records:
        records = records_from_nodes(
            list(soup.find_all("table", class_=lambda value: value and "wikitable" in value)),
            sport=sport,
            team="",
            section="Hall of Fame",
            page=page,
        )
    return dedupe_long_records(records)


def records_from_nodes(
    nodes: list[Any],
    *,
    sport: str,
    team: str,
    section: str,
    page: PageData,
) -> list[dict[str, Any]]:
    import pandas as pd

    records: list[dict[str, Any]] = []
    team_slug = safe_slug(team) if team else ""
    table_index = 0
    seen_tables: set[int] = set()
    seen_lists: set[int] = set()
    seen_paragraphs: set[str] = set()

    for node in nodes:
        tables: list[Any] = []
        if getattr(node, "name", None) == "table":
            tables.append(node)
        if hasattr(node, "find_all"):
            tables.extend(node.find_all("table"))

        for table in tables:
            marker = id(table)
            if marker in seen_tables:
                continue
            seen_tables.add(marker)
            try:
                frames = pd.read_html(io.StringIO(str(table)))
            except (ValueError, ImportError):
                continue
            for frame in frames:
                table_index += 1
                rows = dataframe_rows(frame)
                for row_index, row in enumerate(rows, start=1):
                    first_value = next((value for value in row.values() if value), "")
                    records.append(
                        long_record(
                            sport=sport,
                            team=team,
                            team_slug=team_slug,
                            section=section,
                            record_type="table_row",
                            table_index=table_index,
                            row_index=row_index,
                            label=first_value,
                            value="",
                            data=row,
                            page=page,
                        )
                    )

        # Lists can hold franchise leaders and award recipients outside tables.
        if hasattr(node, "find_all"):
            list_items = node.find_all("li")
        else:
            list_items = []
        for item_index, item in enumerate(list_items, start=1):
            if item.find_parent("table") is not None:
                continue
            marker = id(item)
            if marker in seen_lists:
                continue
            seen_lists.add(marker)
            value = clean_text(item.get_text(" ", strip=True))
            if not value:
                continue
            links = [
                clean_text(anchor.get_text(" ", strip=True))
                for anchor in item.find_all("a")
                if clean_text(anchor.get_text(" ", strip=True))
            ]
            records.append(
                long_record(
                    sport=sport,
                    team=team,
                    team_slug=team_slug,
                    section=section,
                    record_type="list_item",
                    table_index="",
                    row_index=item_index,
                    label=links[0] if links else str(item_index),
                    value=value,
                    data={"text": value, "links": links},
                    page=page,
                )
            )

        # Preserve meaningful direct prose when a section is not structured.
        paragraphs: list[Any] = []
        if getattr(node, "name", None) == "p":
            paragraphs.append(node)
        elif hasattr(node, "find_all"):
            paragraphs.extend(node.find_all("p", recursive=False))
        for paragraph in paragraphs:
            text = clean_text(paragraph.get_text(" ", strip=True))
            if len(text) < 20 or text in seen_paragraphs:
                continue
            seen_paragraphs.add(text)
            records.append(
                long_record(
                    sport=sport,
                    team=team,
                    team_slug=team_slug,
                    section=section,
                    record_type="paragraph",
                    table_index="",
                    row_index="",
                    label="",
                    value=text[:4000],
                    data={"text": text[:4000]},
                    page=page,
                )
            )

    return records


def long_record(
    *,
    sport: str,
    team: str,
    team_slug: str,
    section: str,
    record_type: str,
    table_index: int | str,
    row_index: int | str,
    label: str,
    value: str,
    data: dict[str, Any],
    page: PageData,
) -> dict[str, Any]:
    return {
        "sport": sport,
        "team": team,
        "team_slug": team_slug,
        "section": section,
        "record_type": record_type,
        "table_index": table_index,
        "row_index": row_index,
        "label": clean_text(label),
        "value": clean_text(value),
        "data_json": json.dumps(data, ensure_ascii=False, sort_keys=True),
        "source_page": page.title,
        "source_url": page.source_url,
        "source_revision": page.revision or "",
        "scraped_at_utc": utc_now(),
    }


def dedupe_long_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for row in rows:
        key = (
            str(row.get("team", "")),
            str(row.get("section", "")),
            str(row.get("record_type", "")),
            str(row.get("label", "")),
            str(row.get("value", "")),
            str(row.get("data_json", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(row)
    return output


def write_rows_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    materialised = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in materialised:
            writer.writerow({field: row.get(field, "") for field in fields})


def write_dynamic_csv(
    path: Path,
    rows: list[dict[str, Any]],
    preferred_fields: list[str] | None = None,
) -> None:
    all_fields = {key for row in rows for key in row}
    preferred_fields = preferred_fields or []
    fields = [field for field in preferred_fields if field in all_fields]
    fields.extend(sorted(all_fields - set(fields)))
    write_rows_csv(path, rows, fields)


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


class ScraperApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1040x720")
        self.minsize(900, 620)

        self.events: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None

        self.output_var = tk.StringVar()
        self.refresh_var = tk.BooleanVar(value=False)
        self.delay_var = tk.DoubleVar(value=0.35)
        self.timeout_var = tk.DoubleVar(value=35.0)
        self.sport_vars = {
            key: tk.BooleanVar(value=True) for key in SPORTS
        }
        self.status_var = tk.StringVar(value="Ready")
        self.progress_text_var = tk.StringVar(value="Select an output folder and press Start.")
        self.count_vars = {
            status: tk.StringVar(value="0")
            for status in ("PASS", "WARN", "ERROR", "SKIP")
        }

        self._build_ui()
        self.after(100, self._process_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        title = ttk.Label(outer, text=APP_NAME, font=("Segoe UI", 18, "bold"))
        title.pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "Choose a root folder. The scraper creates nba/team.csv, "
                "nfl/team.csv and mlb/team.csv plus Hall of Fame and team reference files."
            ),
            wraplength=980,
        ).pack(anchor="w", pady=(2, 10))

        settings = ttk.LabelFrame(outer, text="Run settings", padding=10)
        settings.pack(fill="x")
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="Output root").grid(row=0, column=0, sticky="w")
        self.output_entry = ttk.Entry(settings, textvariable=self.output_var)
        self.output_entry.grid(row=0, column=1, sticky="ew", padx=8)
        self.browse_button = ttk.Button(settings, text="Browse…", command=self._browse)
        self.browse_button.grid(row=0, column=2)

        ttk.Label(settings, text="Sports").grid(row=1, column=0, sticky="w", pady=(10, 0))
        sport_frame = ttk.Frame(settings)
        sport_frame.grid(row=1, column=1, sticky="w", padx=8, pady=(10, 0))
        self.sport_checks: list[ttk.Checkbutton] = []
        for index, (key, config) in enumerate(SPORTS.items()):
            check = ttk.Checkbutton(
                sport_frame,
                text=config["label"],
                variable=self.sport_vars[key],
            )
            check.grid(row=0, column=index, padx=(0, 14))
            self.sport_checks.append(check)

        options_frame = ttk.Frame(settings)
        options_frame.grid(row=2, column=1, sticky="w", padx=8, pady=(10, 0))
        self.refresh_check = ttk.Checkbutton(
            options_frame,
            text="Refresh cached pages",
            variable=self.refresh_var,
        )
        self.refresh_check.grid(row=0, column=0, padx=(0, 18))
        ttk.Label(options_frame, text="Request delay (seconds)").grid(row=0, column=1)
        self.delay_spin = ttk.Spinbox(
            options_frame,
            from_=0.1,
            to=5.0,
            increment=0.05,
            width=7,
            textvariable=self.delay_var,
        )
        self.delay_spin.grid(row=0, column=2, padx=(6, 18))
        ttk.Label(options_frame, text="Timeout").grid(row=0, column=3)
        self.timeout_spin = ttk.Spinbox(
            options_frame,
            from_=10,
            to=120,
            increment=5,
            width=7,
            textvariable=self.timeout_var,
        )
        self.timeout_spin.grid(row=0, column=4, padx=(6, 0))

        buttons = ttk.Frame(settings)
        buttons.grid(row=0, column=3, rowspan=3, padx=(12, 0), sticky="ns")
        self.start_button = ttk.Button(buttons, text="Start scrape", command=self._start)
        self.start_button.pack(fill="x")
        self.cancel_button = ttk.Button(
            buttons,
            text="Cancel",
            command=self._cancel,
            state="disabled",
        )
        self.cancel_button.pack(fill="x", pady=(8, 0))

        progress_frame = ttk.LabelFrame(outer, text="Progress", padding=10)
        progress_frame.pack(fill="x", pady=(10, 0))
        self.progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=1)
        self.progress.pack(fill="x")
        ttk.Label(progress_frame, textvariable=self.progress_text_var).pack(
            anchor="w", pady=(5, 0)
        )

        counters = ttk.Frame(progress_frame)
        counters.pack(fill="x", pady=(8, 0))
        for index, status in enumerate(("PASS", "WARN", "ERROR", "SKIP")):
            ttk.Label(counters, text=f"{status}:", font=("Segoe UI", 9, "bold")).grid(
                row=0, column=index * 2, padx=(0 if index == 0 else 18, 4)
            )
            ttk.Label(counters, textvariable=self.count_vars[status]).grid(
                row=0, column=index * 2 + 1
            )

        log_frame = ttk.LabelFrame(outer, text="Passes, warnings and errors", padding=8)
        log_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.log_text = ScrolledText(
            log_frame,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("PASS", foreground="#187a2f")
        self.log_text.tag_configure("WARN", foreground="#9a6700")
        self.log_text.tag_configure("ERROR", foreground="#c62828")
        self.log_text.tag_configure("SKIP", foreground="#666666")
        self.log_text.tag_configure("INFO", foreground="#1f5f99")

        status_bar = ttk.Label(outer, textvariable=self.status_var, relief="sunken", anchor="w")
        status_bar.pack(fill="x", pady=(8, 0))

    def _browse(self) -> None:
        initial = self.output_var.get().strip() or str(Path.home())
        selected = filedialog.askdirectory(
            parent=self,
            title="Choose the output root folder",
            initialdir=initial,
        )
        if selected:
            self.output_var.set(selected)

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        missing = check_dependencies()
        if missing:
            command = "python -m pip install " + " ".join(missing)
            messagebox.showerror(
                "Missing Python packages",
                "Install the required packages, then run the program again:\n\n" + command,
                parent=self,
            )
            return

        output_text = self.output_var.get().strip()
        if not output_text:
            messagebox.showwarning("Output folder required", "Choose an output folder.", parent=self)
            return
        sports = [key for key, var in self.sport_vars.items() if var.get()]
        if not sports:
            messagebox.showwarning("Select a sport", "Select at least one sport.", parent=self)
            return

        try:
            output_root = Path(output_text).expanduser().resolve()
            output_root.mkdir(parents=True, exist_ok=True)
            delay = float(self.delay_var.get())
            timeout = float(self.timeout_var.get())
            if delay < 0.1:
                raise ValueError("Request delay must be at least 0.1 seconds")
            if timeout < 5:
                raise ValueError("Timeout must be at least 5 seconds")
        except (OSError, ValueError) as exc:
            messagebox.showerror("Invalid settings", str(exc), parent=self)
            return

        self._clear_log()
        for variable in self.count_vars.values():
            variable.set("0")
        self.cancel_event.clear()
        self._set_running(True)
        self.status_var.set("Running")
        self.progress_text_var.set("Starting…")
        self.progress["value"] = 0

        options = ScrapeOptions(
            output_root=output_root,
            sports=sports,
            refresh=self.refresh_var.get(),
            request_delay=delay,
            timeout=timeout,
        )
        self.worker = threading.Thread(
            target=self._worker_main,
            args=(options,),
            name="wiki-sports-scraper",
            daemon=True,
        )
        self.worker.start()

    def _worker_main(self, options: ScrapeOptions) -> None:
        reporter = RunReporter(self._emit)
        try:
            metadata = SportsScraper(options, reporter, self.cancel_event).run()
            self._emit("done", {"status": "complete", "metadata": metadata})
        except ScrapeCancelled as exc:
            reporter.log("WARN", str(exc), item="run")
            log_path = options.output_root / "scrape_log.csv"
            write_rows_csv(log_path, reporter.rows, LOG_FIELDS)
            self._emit("done", {"status": "cancelled"})
        except Exception as exc:
            reporter.log("ERROR", f"Fatal error: {exc}", item="run")
            reporter.log("ERROR", traceback.format_exc(), item="traceback")
            try:
                write_rows_csv(
                    options.output_root / "scrape_log.csv",
                    reporter.rows,
                    LOG_FIELDS,
                )
            except Exception:
                pass
            self._emit("done", {"status": "failed", "error": str(exc)})

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        self.events.put((event, payload))

    def _process_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "log":
                    status = payload.get("status", "INFO")
                    line = (
                        f"{payload.get('timestamp', '')} "
                        f"[{status:<5}] "
                        f"{payload.get('sport', ''):<4} "
                        f"{payload.get('item', '')}: {payload.get('message', '')}"
                    )
                    if payload.get("output_path"):
                        line += f" -> {payload['output_path']}"
                    self._append_log(line + "\n", status)
                elif event == "counts":
                    for status, value in payload.items():
                        if status in self.count_vars:
                            self.count_vars[status].set(str(value))
                elif event == "progress":
                    maximum = max(1, int(payload.get("maximum", 1)))
                    current = min(maximum, int(payload.get("current", 0)))
                    self.progress["maximum"] = maximum
                    self.progress["value"] = current
                    self.progress_text_var.set(
                        f"{current}/{maximum} — {payload.get('text', '')}"
                    )
                elif event == "done":
                    self._finish(payload)
        except queue.Empty:
            pass
        finally:
            self.after(100, self._process_events)

    def _finish(self, payload: dict[str, Any]) -> None:
        self._set_running(False)
        status = payload.get("status")
        if status == "complete":
            self.status_var.set("Complete")
            self.progress_text_var.set("Scrape complete. Review scrape_log.csv for the audit trail.")
            messagebox.showinfo(
                "Scrape complete",
                f"Files were written under:\n{self.output_var.get()}",
                parent=self,
            )
        elif status == "cancelled":
            self.status_var.set("Cancelled")
            self.progress_text_var.set("Cancelled. Completed files and scrape_log.csv were retained.")
        else:
            self.status_var.set("Failed")
            self.progress_text_var.set("The run failed. Review the error log below and scrape_log.csv.")
            messagebox.showerror(
                "Scrape failed",
                payload.get("error", "Unknown error"),
                parent=self,
            )

    def _cancel(self) -> None:
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status_var.set("Cancelling…")
            self.progress_text_var.set("Cancelling after the current request…")

    def _set_running(self, running: bool) -> None:
        normal = "disabled" if running else "normal"
        self.start_button.configure(state=normal)
        self.browse_button.configure(state=normal)
        self.output_entry.configure(state=normal)
        self.refresh_check.configure(state=normal)
        self.delay_spin.configure(state=normal)
        self.timeout_spin.configure(state=normal)
        for check in self.sport_checks:
            check.configure(state=normal)
        self.cancel_button.configure(state="normal" if running else "disabled")

    def _append_log(self, text: str, tag: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text, tag if tag in {"PASS", "WARN", "ERROR", "SKIP"} else "INFO")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            close = messagebox.askyesno(
                "Scrape is running",
                "Cancel the scrape and close the application?",
                parent=self,
            )
            if not close:
                return
            self.cancel_event.set()
        self.destroy()


def main() -> int:
    app = ScraperApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())