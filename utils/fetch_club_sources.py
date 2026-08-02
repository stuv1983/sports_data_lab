#!/usr/bin/env python3
"""Fetch and cache the 18 current AFL clubs' source pages.

Wikipedia is fetched through the MediaWiki API. AFL Tables fetching is
permission-gated because the project has historically avoided automated
requests to its statistics paths. Without explicit permission, the command
still prints or opens every required AFL Tables URL and imports manually saved
HTML through ``load_club_sources.py``.

Examples from the project root:
    python utils/fetch_club_sources.py --report
    python utils/fetch_club_sources.py --club adelaide --refresh
    python utils/fetch_club_sources.py --open-afltables
    python utils/fetch_club_sources.py --afltables-permission-confirmed
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from club_sources import (DEFAULT_RAW_DIR, SOURCE_FILES, selected_clubs,
                              source_path)
else:
    from .club_sources import (DEFAULT_RAW_DIR, SOURCE_FILES, selected_clubs,
                               source_path)

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
DEFAULT_USER_AGENT = (
    "SportsDataLab-club-sources/1.0 "
    "(+https://github.com/stuv1983/sports_data_lab)"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def request_bytes(url: str, *, user_agent: str, timeout: float,
                  attempts: int = 4) -> tuple[bytes, dict[str, str]]:
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.5",
        "Accept-Encoding": "identity",
    }
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = response.read()
                meta = {
                    "status": str(getattr(response, "status", 200)),
                    "content_type": response.headers.get("Content-Type", ""),
                    "etag": response.headers.get("ETag", ""),
                    "last_modified": response.headers.get("Last-Modified", ""),
                    "final_url": response.geturl(),
                }
                return data, meta
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in {429, 500, 502, 503, 504}:
                raise
            retry_after = exc.headers.get("Retry-After")
            sleep_for = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            sleep_for = 2 ** attempt
        if attempt < attempts:
            time.sleep(sleep_for + random.random())
    assert last is not None
    raise last


def wikipedia_api_url(title: str) -> str:
    query = urllib.parse.urlencode({
        "action": "parse",
        "page": title,
        "prop": "text|revid|displaytitle|properties",
        "redirects": "1",
        "format": "json",
        "formatversion": "2",
        "origin": "*",
    })
    return f"{WIKIPEDIA_API}?{query}"


def validate_wikipedia(data: bytes, expected_title: str) -> dict:
    payload = json.loads(data.decode("utf-8"))
    if "error" in payload:
        raise ValueError(f"MediaWiki error: {payload['error']}")
    parsed = payload.get("parse") or {}
    if not parsed.get("text") or not parsed.get("revid"):
        raise ValueError(f"Wikipedia response for {expected_title!r} is incomplete")
    return payload


def validate_afltables(data: bytes, club_name: str, source_type: str) -> None:
    if len(data) < 1000:
        raise ValueError(f"{source_type}: response is too small ({len(data)} bytes)")
    probe = data[:100000].decode("windows-1252", errors="replace").casefold()
    required = {
        "afltables_player_totals": "player totals",
        "afltables_records": "most ",
        "afltables_all_time": "all time player list",
    }[source_type]
    if required not in probe:
        raise ValueError(f"{source_type}: expected marker {required!r} not found")

    # AFL Tables retains historical labels in two current-club record pages:
    # North Melbourne is titled "Kangaroos" and Western Bulldogs is titled
    # "Footscray". Accept those source labels without changing the canonical
    # 18-club model used by the database or UI.
    marker_aliases = {
        "north melbourne": ("north melbourne", "kangaroos"),
        "western bulldogs": ("western bulldogs", "footscray"),
    }
    club_key = club_name.casefold()
    markers = marker_aliases.get(club_key, (club_key.split()[0],))
    if not any(marker in probe for marker in markers):
        expected = " or ".join(repr(marker) for marker in markers)
        raise ValueError(
            f"{source_type}: club marker for {club_name!r} not found "
            f"(expected {expected})"
        )


def record_metadata(path: Path, record: dict) -> None:
    meta_path = path.parent / "sources.metadata.json"
    existing: dict = {}
    if meta_path.exists():
        try:
            existing = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
    existing[record["source_type"]] = record
    atomic_write(
        meta_path,
        (json.dumps(existing, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def fetch_one(url: str, path: Path, *, source_type: str, club_name: str,
              user_agent: str, timeout: float, refresh: bool) -> str:
    if path.exists() and not refresh:
        return "cached"
    data, headers = request_bytes(url, user_agent=user_agent, timeout=timeout)
    revision_id = None
    if source_type == "wikipedia":
        payload = validate_wikipedia(data, club_name)
        revision_id = (payload.get("parse") or {}).get("revid")
        data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    else:
        validate_afltables(data, club_name, source_type)
    atomic_write(path, data)
    record_metadata(path, {
        "source_type": source_type,
        "source_url": url,
        "saved_path": str(path),
        "fetched_at": utc_now(),
        "sha256": sha256_bytes(data),
        "bytes": len(data),
        "revision_id": revision_id,
        **headers,
    })
    return "fetched"


def source_urls(club) -> list[tuple[str, str]]:
    return [
        ("afltables_player_totals", club.afltables_player_totals_url),
        ("afltables_records", club.afltables_records_url),
        ("afltables_all_time", club.afltables_all_time_url),
    ]


def print_report(raw_dir: Path, clubs) -> None:
    print("\nClub source cache")
    print("=" * 72)
    totals = {key: 0 for key in SOURCE_FILES}
    for club in clubs:
        found = []
        for source_type in SOURCE_FILES:
            path = source_path(raw_dir, club, source_type)
            ok = path.exists() and path.stat().st_size > 0
            totals[source_type] += int(ok)
            found.append(f"{source_type.replace('afltables_', 'afl:')}: {'yes' if ok else 'NO'}")
        print(f"{club.name:24} " + " | ".join(found))
    print("-" * 72)
    for source_type, count in totals.items():
        print(f"{source_type:28} {count:2}/{len(clubs)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--club", action="append", dest="clubs",
                    help="club id; repeat to select multiple clubs")
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--wikipedia-only", action="store_true")
    ap.add_argument("--afltables-only", action="store_true")
    ap.add_argument("--afltables-permission-confirmed", action="store_true",
                    help="confirm permission to automate AFL Tables stats requests")
    ap.add_argument("--open-afltables", action="store_true",
                    help="open the selected AFL Tables pages in the default browser")
    ap.add_argument("--print-urls", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--delay", type=float, default=2.0,
                    help="seconds between requests (default: 2.0)")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    args = ap.parse_args(argv)

    if args.wikipedia_only and args.afltables_only:
        ap.error("--wikipedia-only and --afltables-only cannot be combined")
    try:
        clubs = selected_clubs(args.clubs)
    except ValueError as exc:
        ap.error(str(exc))

    if args.print_urls or args.open_afltables:
        for club in clubs:
            print(f"\n{club.name}")
            print(f"  Wikipedia: {club.wikipedia_url}")
            for source_type, url in source_urls(club):
                print(f"  {source_type}: {url}")
                if args.open_afltables:
                    webbrowser.open_new_tab(url)
        if args.open_afltables:
            print("\nOpened AFL Tables pages. Save them under each club directory")
            print(f"inside: {args.raw_dir}")
        if args.print_urls and not args.report:
            return 0

    do_wikipedia = not args.afltables_only
    do_afltables = not args.wikipedia_only
    if do_afltables and not args.afltables_permission_confirmed:
        print(
            "AFL Tables automatic fetching is disabled until permission is "
            "explicitly confirmed. Wikipedia will still be fetched.\n"
            "Use --open-afltables for browser-assisted saving, or run with "
            "--afltables-permission-confirmed only when authorised."
        )
        do_afltables = False

    errors = 0
    fetched = 0
    for club in clubs:
        print(f"\n[{club.club_id}] {club.name}")
        jobs: list[tuple[str, str, Path]] = []
        if do_wikipedia:
            jobs.append((
                "wikipedia", wikipedia_api_url(club.wikipedia_title),
                source_path(args.raw_dir, club, "wikipedia"),
            ))
        if do_afltables:
            jobs.extend((source_type, url,
                         source_path(args.raw_dir, club, source_type))
                        for source_type, url in source_urls(club))
        for index, (source_type, url, path) in enumerate(jobs):
            try:
                status = fetch_one(
                    url, path, source_type=source_type, club_name=club.name,
                    user_agent=args.user_agent, timeout=args.timeout,
                    refresh=args.refresh,
                )
                print(f"  {status:7} {source_type:28} {path}")
                fetched += status == "fetched"
            except Exception as exc:
                errors += 1
                print(f"  ERROR   {source_type}: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
            if index + 1 < len(jobs) and args.delay > 0:
                time.sleep(args.delay)

    if args.report or True:
        print_report(args.raw_dir, clubs)
    print(f"\nNew downloads: {fetched}; errors: {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
