#!/usr/bin/env python3
"""Fetch the All Games page for Brisbane Bears, Fitzroy and University.

These three exist only in ``club_sources.HISTORICAL_SOURCES`` -- they are not
part of the 18-club catalogue, so ``fetch_club_sources.py --club`` rejects
them. This reuses the same fetch/validate/cache machinery for just these three
so match history from before Brisbane Lions (1997), and University's brief
1908-1914 stint, is not missing from the All Games layer.

Same permission gate as the main fetcher.

Usage:
    python -m utils.afl.fetch_historical_all_games --afltables-permission-confirmed --report
    python -m utils.afl.fetch_historical_all_games --print-urls
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import webbrowser

sys.path.insert(0, str(Path(__file__).resolve().parent))
from club_sources import HISTORICAL_SOURCES, source_path  # noqa: E402
import fetch_club_sources as base  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw-dir", type=Path, default=base.DEFAULT_RAW_DIR)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--afltables-permission-confirmed", action="store_true")
    ap.add_argument("--open-afltables", action="store_true")
    ap.add_argument("--print-urls", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--delay", type=float, default=2.0)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--user-agent", default=base.DEFAULT_USER_AGENT)
    args = ap.parse_args(argv)

    if args.print_urls or args.open_afltables:
        for club in HISTORICAL_SOURCES:
            url = club.afltables_all_games_url
            print(f"{club.club_id:20} {url}")
            if args.open_afltables:
                webbrowser.open_new_tab(url)
        if args.open_afltables:
            print(f"\nSave each as: {args.raw_dir}\\{{club_id}}\\afltables_all_games.html")
        if args.print_urls and not args.afltables_permission_confirmed:
            return 0

    if not args.afltables_permission_confirmed:
        print("Permission not confirmed. Use --print-urls / --open-afltables "
              "for manual saving, or --afltables-permission-confirmed to fetch.")
        return 0

    fetched = errors = 0
    for index, club in enumerate(HISTORICAL_SOURCES):
        path = source_path(args.raw_dir, club, "afltables_all_games")
        try:
            status = base.fetch_one(
                club.afltables_all_games_url, path,
                source_type="afltables_all_games", club_name=club.name,
                user_agent=args.user_agent, timeout=args.timeout,
                refresh=args.refresh)
            print(f"[{club.club_id}] {status:8} {path}")
            fetched += status == "fetched"
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"[{club.club_id}] ERROR    {exc}")
            errors += 1
        if index + 1 < len(HISTORICAL_SOURCES):
            import time
            time.sleep(args.delay)

    if args.report:
        print("\nHistorical source cache")
        for club in HISTORICAL_SOURCES:
            path = source_path(args.raw_dir, club, "afltables_all_games")
            ok = path.exists() and path.stat().st_size > 0
            print(f"{club.name:20} afl:all_games: {'yes' if ok else 'NO'}")

    print(f"\nNew downloads: {fetched}; errors: {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
