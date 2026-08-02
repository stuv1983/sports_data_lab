#!/usr/bin/env python3
"""Clean generated and superseded Sports Data Lab files safely.

Dry-run is the default. Use ``--apply`` only after reviewing the list.
Database snapshots and parser-repair evidence require separate opt-in flags.
Stop Streamlit and any database refresh before applying cleanup.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path

WARNINGS: list[str] = []

# Exact files that are generated, superseded update-bundle material or
# temporary validation artefacts. Source code, fixtures, raw archives and the
# active database are deliberately absent.
SAFE_FILES = {
    "afldata(old).rda",
    "gitignore.txt",
    "test_draftguru.db",
    "test_gridley.db",
    "apply_update.py",
    "apply_cleanup_update.py",
    "apply_cleanup_hotfix.py",
    "apply_release_gate_hotfix.py",
    "apply_rising_star_update.py",
    "apply_speed_cleanup.py",
    "install_update.ps1",
    "restore_update.ps1",
    "INSTALL.md",
    "UPDATE_NOTES.md",
    "HOTFIX.md",
    "VALIDATION.md",
    "VALIDATION_HOTFIX.md",
    "VALIDATION_TEST_HOTFIX.md",
    "README_RISING_STAR_UPDATE.md",
    "FILES_TO_DELETE.md",
    "MANIFEST.md",
    "MANIFEST.sha256",
    "unmatched_report.py",
    "nearmiss.py",
}

DB_BACKUPS = {
    "gridley-before-awards.db",
    "gridley-before-matches.db",
    "gridley-before-rising-star.db",
}

REPAIR_FILES = {
    "fix_rising_star_parse_patch.py",
    "investigate_rising_star_mismatch.py",
    "parse_criteria.py.broken_rising_star",
    "parse_criteria.py.bak_rising_star",
}

BACKUP_GLOBS = (
    "*.bak_sdl",
    "*.bak_cleanup",
    "*.bak_hotfix",
    "*.bak_release_gate",
    "*.bak_speed_cleanup",
)


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def describe(action: str, source: Path, target: Path | None = None) -> str:
    if target is None:
        return f"{action:<8} {source}"
    return f"{action:<8} {source} -> {target}"


def _warn(message: str) -> None:
    WARNINGS.append(message)
    print(f"warning  {message}")


def _try_unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        _warn(f"could not delete {path}: {exc}")
        return False


def remove_path(path: Path, apply: bool) -> None:
    print(describe("delete", path))
    if not apply or not path.exists():
        return
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        _warn(f"could not delete {path}: {exc}")


def migrate_file(source: Path, target: Path, apply: bool) -> None:
    """Copy, verify and then best-effort delete a legacy duplicate."""
    if not source.exists():
        return
    if target.exists():
        try:
            identical = digest(source) == digest(target)
        except OSError as exc:
            _warn(f"could not compare {source} and {target}: {exc}")
            return
        if identical:
            print(describe("dedupe", source, target))
            if apply:
                _try_unlink(source)
        else:
            print(describe("keep", source, target))
            print("         destination differs; review both files manually")
        return

    print(describe("copy", source, target))
    if not apply:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if digest(source) != digest(target):
            raise OSError("copied file failed SHA-256 verification")
    except OSError as exc:
        _warn(f"could not copy {source} to {target}: {exc}")
        return

    if not _try_unlink(source):
        print("         canonical copy created; locked legacy source retained")


def migrate_cache(source: Path, target: Path, apply: bool) -> None:
    if not source.exists():
        return
    print(describe("merge", source, target))
    if not apply:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, dirs_exist_ok=True)
    except OSError as exc:
        _warn(f"could not copy cache {source} to {target}: {exc}")
        return
    try:
        shutil.rmtree(source)
    except OSError as exc:
        _warn(f"cache copied but legacy cache could not be deleted: {exc}")


def collect_safe_paths(
    root: Path,
    delete_db_backups: bool,
    delete_repair_files: bool,
) -> list[Path]:
    paths = [root / name for name in sorted(SAFE_FILES)]
    for pattern in BACKUP_GLOBS:
        paths.extend(sorted(root.glob(pattern)))
    paths.extend(sorted(root.rglob("__pycache__")))
    paths.extend(sorted(root.rglob("*.pyc")))

    if delete_db_backups:
        paths.extend(root / name for name in sorted(DB_BACKUPS))
    if delete_repair_files:
        paths.extend(root / name for name in sorted(REPAIR_FILES))

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if path.exists() and resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def report_retained(root: Path, names: set[str], heading: str) -> None:
    retained = [root / name for name in sorted(names) if (root / name).exists()]
    if not retained:
        return
    print(f"\n{heading}:")
    for path in retained:
        print(f"  {path.name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."),
                        help="project root (default: current directory)")
    parser.add_argument("--apply", action="store_true",
                        help="perform the displayed moves/deletions")
    parser.add_argument("--delete-db-backups", action="store_true",
                        help="delete accepted gridley-before-*.db snapshots")
    parser.add_argument("--delete-repair-files", action="store_true",
                        help="delete accepted Rising Star parser repair files")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if not (root / "app.py").exists():
        parser.error(f"{root} does not look like the Sports Data Lab root")

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"Sports Data Lab cleanup — {mode}")
    print(f"Root: {root}")
    print("Stop Streamlit and refresh/import jobs before using --apply.\n")

    raw = root / "data" / "afl" / "raw"
    cache = root / "data" / "afl" / "cache" / "captain_pages"
    migrate_file(root / "captaincies.csv",
                 raw / "wikipedia_captaincies.csv", args.apply)
    migrate_file(root / "captaincies.metadata.json",
                 raw / "wikipedia_captaincies.metadata.json", args.apply)
    migrate_cache(root / ".captain_pages_cache", cache, args.apply)

    for path in collect_safe_paths(
            root, args.delete_db_backups, args.delete_repair_files):
        remove_path(path, args.apply)

    if not args.delete_db_backups:
        report_retained(root, DB_BACKUPS, "Retained database snapshots")
        if any((root / name).exists() for name in DB_BACKUPS):
            print("Use --delete-db-backups only after release acceptance.")

    if not args.delete_repair_files:
        report_retained(root, REPAIR_FILES, "Retained parser repair evidence")
        if any((root / name).exists() for name in REPAIR_FILES):
            print("Use --delete-repair-files only after parser acceptance.")

    if not args.apply:
        print("\nNo changes made. Review the list, then rerun with --apply.")
    elif WARNINGS:
        print(f"\nCleanup completed with {len(WARNINGS)} warning"
              f"{'s' if len(WARNINGS) != 1 else ''}.")
    else:
        print("\nCleanup completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
