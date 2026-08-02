#!/usr/bin/env python3
"""Sports Data Lab -- repo cleanup and reorganisation.
 
Dry run by default. Nothing is written until ``--apply`` is supplied.
 
    python cleanup_repo.py                             # show the plan
    python cleanup_repo.py --apply                     # backups, caches, bundles
    python cleanup_repo.py --apply --all               # everything below
    python cleanup_repo.py --apply --delete-db-backups # stale .db snapshots
    python cleanup_repo.py --apply --move-db           # gridley.db -> data/afl/afl.db
    python cleanup_repo.py --apply --move-tests        # root test_*.py -> tests/
    python cleanup_repo.py --apply --move-utils        # one-shot tooling -> utils/
 
Stop Streamlit and any import or refresh job before applying.
 
Why the database moves to ``data/afl/afl.db`` rather than ``database/``
----------------------------------------------------------------------
``data_paths.sport_db()`` already prefers ``data/<sport>/<sport>.db`` and falls
back to the legacy ``gridley.db`` only when that file is absent. Fifteen
modules -- including ``app.py`` via ``sports.py`` -- resolve the database
through it, so moving the file there needs no code change at all. A new
``database/`` folder would mean patching every one of those call sites plus
four hard-coded ``--db`` defaults, for the same result. Use ``--db-dir`` if you
want the other layout anyway; the four hard-coded defaults are patched either
way.
"""
 
from __future__ import annotations
 
import argparse
import ast
import re
import shutil
import sys
from pathlib import Path
 
WARNINGS: list[str] = []
PLAN: list[str] = []
 
 
# --------------------------------------------------------------------------
# What goes where
# --------------------------------------------------------------------------
 
# Generated or superseded material, deleted by default.
DELETE_GLOBS = ("*.bak_*", "*.broken_*", "*.pyc")
DELETE_DIR_NAMES = ("__pycache__",)
 
# One-shot installers and their notes. The changes they made are already in
# the working tree; the bundles themselves are not source.
DELETE_FILES = (
    "apply_family_relationships_update.py",
    "apply_family_hotfix.py",
    "README_FAMILY_HOTFIX_1.md",
    "README_FAMILY_RELATIONSHIPS_UPDATE.md",
    "README_FAMILY_RELATIONSHIPS_LOCAL_UPDATE.md",
    "test_family_parsing.py.orig",
)
DELETE_DOCS = ("docs/README_FAMILY_DRAFT_UPDATE.md",)
 
# Superseded database snapshots. Opt-in: this is ~700 MB and irreversible.
DB_BACKUPS = (
    "gridley-before-awards.db",
    "gridley-before-matches.db",
    "gridley-before-rising-star.db",
    "test_gridley.db",
    "test_draftguru.db",
)
 
# Loose regression scripts at the root that belong with the rest of the suite.
MOVE_TESTS = (
    "test_family_parsing.py",
    "test_family_relationships.py",
    "test_family_search.py",
)
 
# One-shot maintenance tooling. Every entry is imported by nothing, so moving
# it cannot break another module; each gets a sys.path bootstrap so it still
# runs standalone. Loaders, scrapers and app entry points stay at the root.
MOVE_UTILS = (
    "apply_gridley_tiles.py",
    "clean_project.py",
    "diagnose_answer.py",
    "grid_fixtures.py",
    "link_draft.py",
    "link_people.py",
    "make_sql.py",
    "optimise_database.py",
    "reorganise_project.py",
    "repair_database.py",
)
 
# Files with a hard-coded legacy default that the database move would strand.
HARDCODED_DB_DEFAULTS = (
    "build_db.py",
    "link_draft.py",
    "link_people.py",
    "load_draftguru.py",
)
 
BOOTSTRAP = """
# Run standalone from anywhere: the project root is one level up.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
"""
 
 
# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
 
def note(line: str) -> None:
    PLAN.append(line)
    print(line)
 
 
def human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.1f} {unit}" if unit != "B" else f"{size:,} B"
        size /= 1024.0
    return str(size)
 
 
ROOT = Path(".")
 
 
def rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()
 
 
def rm_file(path: Path, apply: bool) -> int:
    if not path.exists():
        return 0
    size = path.stat().st_size
    note(f"  delete  {rel(path):<52} {human(size)}")
    if apply:
        path.unlink()
    return size
 
 
def rm_tree(path: Path, apply: bool) -> int:
    if not path.exists():
        return 0
    size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    note(f"  delete  {rel(path) + '/':<52} {human(size)}")
    if apply:
        shutil.rmtree(path)
    return size
 
 
def move(src: Path, dst: Path, apply: bool) -> None:
    note(f"  move    {rel(src)}  ->  {rel(dst)}")
    if apply:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
 
 
def bootstrap_insert_line(text: str) -> int:
    """Line index after the docstring and any __future__ imports."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return 0
    line = 0
    for node in tree.body:
        is_doc = (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
        is_future = (
            isinstance(node, ast.ImportFrom) and node.module == "__future__"
        )
        if is_doc or is_future:
            line = node.end_lineno or line
        else:
            break
    return line
 
 
def add_bootstrap(path: Path, apply: bool) -> None:
    text = path.read_text(encoding="utf-8")
    if "_Path(__file__).resolve().parent.parent" in text:
        return
    lines = text.splitlines(keepends=True)
    at = bootstrap_insert_line(text)
    new = "".join(lines[:at]) + BOOTSTRAP + "".join(lines[at:])
    note(f"  patch   {rel(path)} (sys.path bootstrap)")
    if apply:
        path.write_text(new, encoding="utf-8")
 
 
# --------------------------------------------------------------------------
# Phases
# --------------------------------------------------------------------------
 
def phase_delete(root: Path, apply: bool) -> int:
    note("\n[1] Backups, caches and superseded bundles")
    freed = 0
    for name in DELETE_DIR_NAMES:
        for path in sorted(root.rglob(name)):
            if path.is_dir():
                freed += rm_tree(path, apply)
    for pattern in DELETE_GLOBS:
        for path in sorted(root.rglob(pattern)):
            if path.is_file() and "__pycache__" not in path.parts:
                freed += rm_file(path, apply)
    for name in DELETE_FILES:
        path = root / name
        if path.is_file():
            freed += rm_file(path, apply)
    for name in DELETE_DOCS:
        path = root / name
        if path.is_file():
            freed += rm_file(path, apply)
    if PLAN[-1].lstrip().startswith("["):
        note("  (nothing to do)")
    return freed
 
 
def phase_db_backups(root: Path, apply: bool) -> int:
    note("\n[2] Superseded database snapshots")
    freed = 0
    for name in DB_BACKUPS:
        path = root / name
        if path.is_file():
            freed += rm_file(path, apply)
    if freed == 0:
        note("  (nothing to do)")
    return freed
 
 
def phase_move_db(root: Path, apply: bool, db_dir: str | None) -> None:
    note("\n[3] Active database")
    src = root / "gridley.db"
    if not src.is_file():
        note("  gridley.db not at the root -- skipping")
        return
 
    if db_dir:
        dst = root / db_dir / "gridley.db"
        note(f"  custom location: {db_dir}/ -- data_paths.sport_db() will NOT")
        note("  find this automatically; every module now needs an explicit")
        note("  --db argument. data/afl/afl.db is the zero-change option.")
        WARNINGS.append(
            f"database moved to {db_dir}/; modules using data_paths will not "
            "resolve it without --db"
        )
    else:
        dst = root / "data" / "afl" / "afl.db"
 
    if dst.exists():
        note(f"  {rel(dst)} already exists -- skipping move")
        return
    move(src, dst, apply)
 
    rel = dst.relative_to(root).as_posix()
    for name in HARDCODED_DB_DEFAULTS:
        path = root / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        pattern = r'(ap\.add_argument\(\s*"--db",\s*default=)"gridley\.db"'
        if not re.search(pattern, text):
            continue
        new = re.sub(pattern, rf'\1"{rel}"', text)
        note(f"  patch   {name} (--db default -> {rel})")
        if apply:
            path.write_text(new, encoding="utf-8")
 
 
def phase_move_tests(root: Path, apply: bool) -> None:
    note("\n[4] Loose regression scripts")
    tests = root / "tests"
    moved = False
    for name in MOVE_TESTS:
        src = root / name
        if not src.is_file():
            continue
        dst = tests / name
        if dst.exists():
            note(f"  {rel(dst)} already exists -- skipping")
            continue
        move(src, dst, apply)
        if apply:
            add_bootstrap(dst, apply)
        else:
            note(f"  patch   tests/{name} (sys.path bootstrap)")
        moved = True
    if not moved:
        note("  (nothing to do)")
 
 
def phase_move_utils(root: Path, apply: bool) -> None:
    note("\n[5] One-shot maintenance tooling")
    utils = root / "utils"
    moved = False
    for name in MOVE_UTILS:
        src = root / name
        if not src.is_file():
            continue
        dst = utils / name
        if dst.exists():
            note(f"  {rel(dst)} already exists -- skipping")
            continue
        move(src, dst, apply)
        if apply:
            add_bootstrap(dst, apply)
        else:
            note(f"  patch   utils/{name} (sys.path bootstrap)")
        moved = True
    if moved and apply:
        init = utils / "__init__.py"
        if not init.exists():
            init.write_text("", encoding="utf-8")
    if not moved:
        note("  (nothing to do)")
 
 
def phase_gitignore(root: Path, apply: bool) -> None:
    note("\n[6] .gitignore")
    path = root / ".gitignore"
    if not path.is_file():
        note("  .gitignore not found -- skipping")
        return
    text = path.read_text(encoding="utf-8")
    wanted = ["*.bak_*", "*.broken_*", "__pycache__/", "*.pyc", "*.db"]
    missing = [rule for rule in wanted if rule not in text]
    if not missing:
        note("  already covers backups, caches and databases")
        return
    block = "\n# added by cleanup_repo.py\n" + "\n".join(missing) + "\n"
    note(f"  append  {', '.join(missing)}")
    if apply:
        path.write_text(text.rstrip("\n") + "\n" + block, encoding="utf-8")
 
 
# --------------------------------------------------------------------------
 
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default is a dry run)")
    ap.add_argument("--all", action="store_true",
                    help="enable every optional phase")
    ap.add_argument("--delete-db-backups", action="store_true")
    ap.add_argument("--move-db", action="store_true")
    ap.add_argument("--db-dir", default=None,
                    help="custom database folder instead of data/afl")
    ap.add_argument("--move-tests", action="store_true")
    ap.add_argument("--move-utils", action="store_true")
    args = ap.parse_args(argv)
 
    global ROOT
    root = Path(args.root).resolve()
    ROOT = root
    if not (root / "app.py").exists() or not (root / "constraints.py").exists():
        print(f"error: {root} is not the Sports Data Lab root", file=sys.stderr)
        return 1
 
    mode = "APPLYING" if args.apply else "DRY RUN -- nothing will be written"
    print(f"Sports Data Lab cleanup: {root}")
    print(f"Mode: {mode}\n")
 
    freed = phase_delete(root, args.apply)
    if args.all or args.delete_db_backups:
        freed += phase_db_backups(root, args.apply)
    if args.all or args.move_db or args.db_dir:
        phase_move_db(root, args.apply, args.db_dir)
    if args.all or args.move_tests:
        phase_move_tests(root, args.apply)
    if args.all or args.move_utils:
        phase_move_utils(root, args.apply)
    phase_gitignore(root, args.apply)
 
    print(f"\nSpace reclaimed: {human(freed)}")
    for warning in WARNINGS:
        print(f"warning: {warning}")
    if not args.apply:
        print("\nDry run. Re-run with --apply to make these changes.")
    else:
        print(
            "\nDone. Verify with:\n"
            "  python -m compileall -q .\n"
            "  python -m pytest tests -q\n"
            "  python health.py\n"
            "  streamlit run .\\app.py"
        )
    return 0
 
 
if __name__ == "__main__":
    raise SystemExit(main())