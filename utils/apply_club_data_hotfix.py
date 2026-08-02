#!/usr/bin/env python3
"""Apply the first AFL club-source hotfix in place.

Fixes:
* AFL Tables record-page validation accepts the source's historical titles
  "Kangaroos" (North Melbourne) and "Footscray" (Western Bulldogs).
* apply_club_data_update.py treats its two companion Markdown documents as
  optional, so a code-only extraction can still install successfully.

Run from the Sports Data Lab project root:
    python utils/apply_club_data_hotfix.py
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys

BACKUP_SUFFIX = ".bak_club_data_hotfix"


class HotfixError(RuntimeError):
    pass


def backup(path: Path) -> None:
    target = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    if path.exists() and not target.exists():
        shutil.copy2(path, target)


def write_changed(path: Path, text: str) -> bool:
    old = path.read_text(encoding="utf-8")
    if old == text:
        print(f"current  {path.relative_to(path.parents[1])}")
        return False
    backup(path)
    path.write_text(text, encoding="utf-8")
    print(f"patched  {path.relative_to(path.parents[1])}")
    return True


def patch_fetcher(path: Path) -> bool:
    if not path.exists():
        raise HotfixError(f"missing {path}")
    text = path.read_text(encoding="utf-8")
    marker = 'marker_aliases = {\n        "north melbourne"'
    if marker in text:
        print("current  utils/fetch_club_sources.py")
        return False

    old = '''def validate_afltables(data: bytes, club_name: str, source_type: str) -> None:
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
    if club_name.casefold().split()[0] not in probe:
        raise ValueError(f"{source_type}: club marker for {club_name!r} not found")
'''
    new = '''def validate_afltables(data: bytes, club_name: str, source_type: str) -> None:
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
'''
    if old not in text:
        raise HotfixError(
            "utils/fetch_club_sources.py does not match the expected version"
        )
    return write_changed(path, text.replace(old, new, 1))


def patch_installer(path: Path) -> bool:
    if not path.exists():
        raise HotfixError(f"missing {path}")
    text = path.read_text(encoding="utf-8")
    marker = "optional bundle document missing"
    if marker in text:
        print("current  apply_club_data_update.py")
        return False

    old = '''    if not source.exists():
        raise PatchError(f"bundle is missing {relative}")
'''
    new = '''    if not source.exists():
        if relative in {
            "README_CLUB_DATA_UPDATE.md",
            "VALIDATION_CLUB_DATA_UPDATE.md",
        }:
            print(f"skipped  {relative} (optional bundle document missing)")
            return
        raise PatchError(f"bundle is missing {relative}")
'''
    if old not in text:
        raise HotfixError(
            "apply_club_data_update.py does not match the expected version"
        )
    return write_changed(path, text.replace(old, new, 1))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1],
        help="Sports Data Lab project root",
    )
    args = ap.parse_args(argv)
    root = args.root.resolve()
    if not (root / "app.py").exists() or not (root / "utils").is_dir():
        print(f"error: {root} does not look like the project root", file=sys.stderr)
        return 1

    try:
        patch_fetcher(root / "utils" / "fetch_club_sources.py")
        patch_installer(root / "apply_club_data_update.py")
    except (OSError, HotfixError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("\nCompiling patched files...")
    result = subprocess.run(
        [
            sys.executable, "-m", "compileall", "-q",
            str(root / "utils" / "fetch_club_sources.py"),
            str(root / "apply_club_data_update.py"),
        ],
        check=False,
    )
    if result.returncode:
        print("error: compileall failed", file=sys.stderr)
        return result.returncode

    print("\nHotfix installed. Next run:")
    print(r"  python .\apply_club_data_update.py .")
    print(r"  python .\utils\fetch_club_sources.py --club north_melbourne --club western_bulldogs --afltables-only --afltables-permission-confirmed --report")
    print(r"  python .\utils\load_club_sources.py --db .\data\afl\afl.db --report --details")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
