#!/usr/bin/env python3
"""Validate cached AFL Tables season/game record pages."""
from __future__ import annotations

from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from load_club_sources import parse_records  # noqa: E402


def main() -> int:
    raw = HERE.parent / "data" / "afl" / "raw" / "clubs"
    pages = sorted(raw.glob("*/afltables_records.html"))
    if not pages:
        print(f"No cached record pages found under {raw}")
        return 0
    failures: list[str] = []
    total = 0
    for page in pages:
        try:
            rows = parse_records(page)
            seasons = sum(row["scope"] == "season" for row in rows)
            games = sum(row["scope"] == "game" for row in rows)
            print(f"{page.parent.name:22} {len(rows):4} rows ({seasons} season, {games} game)")
            total += len(rows)
            if not seasons or not games:
                failures.append(f"{page.parent.name}: missing one scope")
        except Exception as exc:
            failures.append(f"{page.parent.name}: {type(exc).__name__}: {exc}")
    if failures:
        print("\nRecord validation failures:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(f"\nValidated {len(pages)} club record pages and {total:,} rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
