#!/usr/bin/env python3
"""Install the club-source raw HTML parser hotfix.

Usage:
    python apply_club_data_parser_hotfix.py C:\\sports_data_lab

The script replaces the loader and its focused regression test, creates
``.bak_club_parser`` backups once, compiles both files, and runs the tests.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
FILES = ("load_club_sources.py", "test_club_sources.py")


def replace(root: Path, name: str) -> None:
    source = HERE / name
    target = root / "utils" / name
    if not source.exists():
        raise FileNotFoundError(f"hotfix bundle is missing {source}")
    if not target.exists():
        raise FileNotFoundError(f"project file is missing {target}")
    if source.read_bytes() == target.read_bytes():
        print(f"unchanged utils\\{name}")
        return
    backup = target.with_suffix(target.suffix + ".bak_club_parser")
    if not backup.exists():
        shutil.copy2(target, backup)
    shutil.copy2(source, target)
    print(f"patched  utils\\{name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".", help="project root")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if not (root / "app.py").exists() or not (root / "utils").is_dir():
        print(f"error: {root} does not look like the project root", file=sys.stderr)
        return 1
    try:
        for name in FILES:
            replace(root, name)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("\nCompiling parser files...")
    result = subprocess.run(
        [sys.executable, "-m", "py_compile",
         str(root / "utils" / "load_club_sources.py"),
         str(root / "utils" / "test_club_sources.py")],
        cwd=root,
        check=False,
    )
    if result.returncode:
        return result.returncode

    print("Running focused club-source tests...")
    result = subprocess.run(
        [sys.executable, str(root / "utils" / "test_club_sources.py")],
        cwd=root,
        check=False,
    )
    if result.returncode:
        return result.returncode

    print("\nClub-source parser hotfix installed. Rebuild the source layer with:")
    print(r"  python .\utils\load_club_sources.py --db .\data\afl\afl.db --report --details")
    print(r"  streamlit run .\app.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
