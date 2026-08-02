#!/usr/bin/env python3
"""Reorganise Sports Data Lab into a git-ready layout.

Dry run by default.  Nothing is written until ``--apply`` is supplied.

    python reorganise_project.py                 # show the plan
    python reorganise_project.py --apply         # do it
    python reorganise_project.py --apply --delete-db-backups

What it does
------------
1. Tests   -> tests/ (plus tests/fixtures/draftguru), with a bootstrap header
              so each test still runs standalone from anywhere.
2. Data    -> data/afl/{raw,cache,reference} for everything AFL, including the
              Draftguru tree and the afldata.rda source cache.
3. Deletes -> __pycache__, *.pyc, *.bak_*, *.broken_*, duplicate captaincy CSV,
              discover/ diagnostics.  Database snapshots need --delete-db-backups.
4. Patches -> the few hard-coded paths that the moves would otherwise break.

Stop Streamlit and any import/refresh job before applying.
"""

from __future__ import annotations

# Run standalone from anywhere: the project root is one level up.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import shutil
import sys
from pathlib import Path

WARNINGS: list[str] = []

# Backups that clean_project.py's narrower glob list leaves behind.
DELETE_EXTRA = (
    "SHA256SUMS.txt",
)

# Generated / superseded material that is safe to delete outright.
DELETE_GLOBS = (
    "*.bak_*",
    "*.broken_*",
    "*.pyc",
)
DELETE_DIRS = (
    "discover",          # fetch_grid.py --discover diagnostics, regenerable
)
DB_BACKUPS = (
    "gridley-before-awards.db",
    "gridley-before-matches.db",
    "gridley-before-rising-star.db",
)

BOOTSTRAP = """# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---
"""

CONFTEST = '''"""Make the repository root importable and current for every test."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
'''

TESTS_README = """# Tests

Run from the repository root:

    python -m pytest tests                  # everything pytest can collect
    python tests/test_core_regressions.py   # or any file directly

Each file adds the repository root to `sys.path` and changes the working
directory to it, so relative paths such as `sql/`, `gridley.db` and
`tests/fixtures/draftguru` resolve the same way in both styles of run.

`fixtures/draftguru` is the tiny hand-made Draftguru tree used by
`test_draftguru.py`. It is source-controlled; the real scraped tree lives in
`data/afl/raw/draftguru` and is ignored by git.
"""


# ---------------------------------------------------------------- utilities

def warn(message: str) -> None:
    WARNINGS.append(message)
    print(f"  warning  {message}")


def show(action: str, source, target=None) -> None:
    if target is None:
        print(f"  {action:<8} {source}")
    else:
        print(f"  {action:<8} {source} -> {target}")


def move(source: Path, target: Path, apply: bool) -> None:
    if not source.exists():
        return
    if target.exists():
        show("skip", source, target)
        warn(f"{target} already exists; {source} left in place")
        return
    show("move", source, target)
    if not apply:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
    except OSError as exc:
        warn(f"could not move {source}: {exc}")


def delete(path: Path, apply: bool) -> None:
    if not path.exists():
        return
    show("delete", path)
    if not apply:
        return
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        warn(f"could not delete {path}: {exc}")


def read_raw(path: Path) -> str:
    """Read without newline translation, so CRLF files stay CRLF."""
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def write_raw(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def newline_of(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def patch(path: Path, replacements: list[tuple[str, str]], apply: bool) -> None:
    """Apply literal replacements, preserving the file's line endings."""
    if not path.exists():
        return
    text = read_raw(path)
    nl = newline_of(text)
    flat = text.replace("\r\n", "\n")
    changed = False
    for old, new in replacements:
        if old in flat:
            flat = flat.replace(old, new)
            changed = True
    if not changed:
        return
    show("patch", path)
    if not apply:
        return
    write_raw(path, flat.replace("\n", nl))


def insert_bootstrap(path: Path, apply: bool) -> None:
    """Insert the sys.path/chdir header after the shebang, docstring and
    any __future__ imports, so the file stays importable and runnable."""
    if not path.exists():
        return
    text = read_raw(path)
    if "test bootstrap" in text:
        return
    nl = newline_of(text)
    lines = text.replace("\r\n", "\n").split("\n")

    i = 0
    if i < len(lines) and lines[i].startswith("#!"):
        i += 1
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
        i += 1
    if i < len(lines):
        stripped = lines[i].lstrip()
        for quote in ('"""', "'''"):
            if stripped.startswith(quote):
                rest = stripped[3:]
                if quote in rest:            # single-line docstring
                    i += 1
                else:
                    i += 1
                    while i < len(lines) and quote not in lines[i]:
                        i += 1
                    i += 1
                break
    # keep __future__ imports above the bootstrap
    j = i
    while j < len(lines):
        stripped = lines[j].strip()
        if not stripped:
            j += 1
            continue
        if stripped.startswith("from __future__"):
            j += 1
            i = j
            continue
        break

    show("header", path)
    if not apply:
        return
    block = [""] + BOOTSTRAP.rstrip("\n").split("\n") + [""]
    lines[i:i] = block
    write_raw(path, nl.join(lines))


def write(path: Path, content: str, apply: bool) -> None:
    if path.exists():
        return
    show("create", path)
    if not apply:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------- stages

def find_tests(root: Path) -> list[str]:
    """Every root-level test module, however recently it was added."""
    return sorted(p.name for p in root.glob("test_*.py"))


def stage_tests(root: Path, apply: bool) -> None:
    print("\nTests")
    tests = root / "tests"
    names = find_tests(root)
    if not names:
        print("  (no root-level test_*.py found; already moved?)")
    for name in names:
        move(root / name, tests / name, apply)
    move(root / "fixture_draftguru", tests / "fixtures" / "draftguru", apply)
    write(tests / "conftest.py", CONFTEST, apply)
    write(tests / "README.md", TESTS_README, apply)
    for name in sorted(set(names) | {p.name for p in tests.glob("test_*.py")}):
        insert_bootstrap(tests / name, apply)
    patch(tests / "test_draftguru.py",
          [('ROOT = "fixture_draftguru"',
            'ROOT = "tests/fixtures/draftguru"')], apply)


DOCS = (
    "CLEANUP.md",
    "DRAFTGURU_IMPORT.md",
    "README_FAMILY_DRAFT_UPDATE.md",
    "SPEED_CLEANUP_NOTES.md",
    "AFL_DATA_GAPS.csv",
)


def stage_docs(root: Path, apply: bool) -> None:
    """Keep README.md and ACKNOWLEDGEMENTS.md at the root; file the rest."""
    print("\nDocumentation")
    for name in DOCS:
        move(root / name, root / "docs" / name, apply)


def stage_data(root: Path, apply: bool) -> None:
    print("\nData")
    raw = root / "data" / "afl" / "raw"

    # AFL source cache alongside the rest of the AFL raw data.
    move(root / "afldata.rda", raw / "afldata.rda", apply)

    # Draftguru is AFL data, so it belongs under data/afl/raw.
    move(root / "data" / "draftguru", raw / "draftguru", apply)

    # captaincies.csv is a byte-for-byte copy of the scraper's output.
    dup = raw / "captaincies.csv"
    canonical = raw / "wikipedia_captaincies.csv"
    if dup.exists() and canonical.exists():
        if dup.stat().st_size == canonical.stat().st_size:
            delete(dup, apply)
        else:
            warn(f"{dup} differs from {canonical}; review both by hand")


def stage_patches(root: Path, apply: bool) -> None:
    print("\nPath fixes required by the moves")

    # data_paths must accept the name the scraper actually writes.
    patch(root / "data_paths.py", [(
        '    single = base / "captaincies.csv"\n'
        '    if single.exists():\n'
        '        sources.append(single)\n',
        '    for name in ("captaincies.csv", "wikipedia_captaincies.csv"):\n'
        '        single = base / name\n'
        '        if single.exists():\n'
        '            sources.append(single)\n'
        '            break\n',
    )], apply)

    # Draftguru tree moved under data/afl/raw.
    for name in ("load_draftguru.py", "test_draftguru.py", "DRAFTGURU_IMPORT.md",
                 "README.md", "app.py", "health.py", "constraints.py"):
        target = root / name
        if name.startswith("test_"):
            target = root / "tests" / name
        patch(target, [("data/draftguru", "data/afl/raw/draftguru")], apply)

    # afldata.rda moved; keep a fallback to the legacy root copy.
    patch(root / "build_db.py", [(
        'CACHE = "afldata.rda"',
        'CACHE = os.path.join("data", "afl", "raw", "afldata.rda")\n'
        'if not os.path.exists(CACHE) and os.path.exists("afldata.rda"):\n'
        '    CACHE = "afldata.rda"          # legacy pre-reorganisation location',
    ), (
        "    urllib.request.urlretrieve(DATA_URL, CACHE)",
        '    os.makedirs(os.path.dirname(CACHE) or ".", exist_ok=True)\n'
        "    urllib.request.urlretrieve(DATA_URL, CACHE)",
    )], apply)


def stage_deletes(root: Path, apply: bool, db_backups: bool) -> None:
    print("\nGenerated and superseded files")
    for path in sorted(root.rglob("__pycache__")):
        delete(path, apply)
    for pattern in DELETE_GLOBS:
        for path in sorted(root.rglob(pattern)):
            delete(path, apply)
    for name in DELETE_DIRS:
        delete(root / name, apply)
    for name in DELETE_EXTRA:
        delete(root / name, apply)

    if db_backups:
        for name in DB_BACKUPS:
            delete(root / name, apply)
    else:
        retained = [n for n in DB_BACKUPS if (root / n).exists()]
        if retained:
            print("\n  Retained rollback databases (git-ignored):")
            for name in retained:
                mb = (root / name).stat().st_size / 1e6
                print(f"    {name}  ({mb:.0f} MB)")
            print("  Delete with --delete-db-backups once the release is tagged.")


def stage_gitignore(root: Path, apply: bool) -> None:
    print("\n.gitignore")
    path = root / ".gitignore"
    if not path.exists():
        warn("no .gitignore found")
        return
    text = read_raw(path)
    nl = newline_of(text)
    additions = [
        line for line in ("discover/", "*.rda", ".coverage", "htmlcov/")
        if line not in text
    ]
    if not additions:
        return
    show("append", path)
    if not apply:
        for line in additions:
            print(f"           + {line}")
        return
    block = nl + "# Local diagnostics and coverage output" + nl + nl.join(additions) + nl
    write_raw(path, text.rstrip() + nl + block)


# ---------------------------------------------------------------- entry

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--delete-db-backups", action="store_true")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if not (root / "app.py").exists():
        parser.error(f"{root} does not look like the Sports Data Lab root")

    print(f"Sports Data Lab reorganisation — {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"Root: {root}")

    stage_tests(root, args.apply)
    stage_docs(root, args.apply)
    stage_data(root, args.apply)
    stage_patches(root, args.apply)
    stage_deletes(root, args.apply, args.delete_db_backups)
    stage_gitignore(root, args.apply)

    if not args.apply:
        print("\nNothing changed. Review the plan, then rerun with --apply.")
    elif WARNINGS:
        print(f"\nFinished with {len(WARNINGS)} warning(s) — see above.")
    else:
        print("\nFinished. Now run:  python -m compileall -q .  then the tests.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
