#!/usr/bin/env python3
"""Install the Sports Data Lab UI refresh without changing database data.

Replaces theme.py and sports.py, then moves the Appearance control in app.py
below Database status. Existing files receive one .bak_ui_refresh backup.
"""

from __future__ import annotations

import argparse
import py_compile
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPLACEMENTS = ("theme.py", "sports.py")
BACKUP_SUFFIX = ".bak_ui_refresh"


class UpdateError(RuntimeError):
    pass


def validate_bundle() -> None:
    missing = [name for name in REPLACEMENTS if not (HERE / name).exists()]
    if missing:
        raise UpdateError("bundle is incomplete: " + ", ".join(missing))
    for name in (*REPLACEMENTS, Path(__file__).name):
        path = HERE / name
        if path.suffix == ".py":
            py_compile.compile(str(path), doraise=True)


def backup(path: Path) -> None:
    target = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    if path.exists() and not target.exists():
        shutil.copy2(path, target)
        print(f"backup    {target.name}")


def install_file(root: Path, name: str) -> None:
    source, target = HERE / name, root / name
    if target.exists() and target.read_bytes() == source.read_bytes():
        print(f"unchanged {name}")
        return
    if target.exists():
        backup(target)
    shutil.copy2(source, target)
    print(f"installed {name}")


def _remove_ready_caption_calls(text: str) -> str:
    """Hide duplicate ready captions now rendered by SPORT.status().

    The not-loaded instructions remain visible. Replacing the call with pass
    keeps surrounding if/else blocks syntactically valid across update versions.
    """
    targets = (
        "Club data: ready",
        "Family links: ready",
        "Rising Star nominations: ready",
    )
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if "st.caption" not in line:
            output.append(line)
            i += 1
            continue

        block = [line]
        balance = line.count("(") - line.count(")")
        j = i + 1
        while balance > 0 and j < len(lines):
            block.append(lines[j])
            balance += lines[j].count("(") - lines[j].count(")")
            j += 1
        joined = "".join(block)
        if any(target in joined for target in targets):
            indent = re.match(r"\s*", line).group(0)
            output.append(
                indent + "pass  # ready state is shown in Database status\n"
            )
        else:
            output.extend(block)
        i = j
    return "".join(output)


def patch_app(path: Path) -> None:
    if not path.exists():
        raise UpdateError("app.py was not found")
    original = path.read_text(encoding="utf-8")
    text = original

    appearance = (
        "# ---------------------------------------------------------- appearance\n"
        "PALETTE = theme.controls(st, SPORT.key)\n"
        "st.markdown(theme.css(PALETTE), unsafe_allow_html=True)\n"
    )

    # Remove the old early call so Appearance is not the first sidebar control.
    call_pattern = re.compile(
        r"\n?PALETTE\s*=\s*theme\.controls\(st,\s*SPORT\.key(?:,.*?)?\)\s*\n"
        r"st\.markdown\(theme\.css\(PALETTE\),\s*unsafe_allow_html=True\)\s*\n?",
        re.S,
    )
    text = call_pattern.sub("\n", text)

    # Remove an earlier installer marker if this refresh is being reapplied.
    text = text.replace(appearance + "\n", "")
    text = text.replace(appearance, "")

    marker = "# ------------------------------------------------------- axis definition"
    if marker not in text:
        raise UpdateError("app.py axis-definition marker was not found")
    text = text.replace(marker, appearance + "\n\n" + marker, 1)
    text = _remove_ready_caption_calls(text)

    if text == original:
        print("unchanged app.py")
        return
    backup(path)
    path.write_text(text, encoding="utf-8")
    py_compile.compile(str(path), doraise=True)
    print("patched   app.py")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "root", nargs="?", type=Path, default=Path("."),
        help="Sports Data Lab project root",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()

    if not (root / "app.py").exists() or not (root / "core.py").exists():
        print(f"error: {root} does not look like the project root", file=sys.stderr)
        return 1

    try:
        validate_bundle()
        for name in REPLACEMENTS:
            install_file(root, name)
        patch_app(root / "app.py")
    except (OSError, UpdateError, py_compile.PyCompileError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("\nCompiling project Python files...")
    result = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", str(root)], check=False
    )
    if result.returncode:
        print("error: compileall failed", file=sys.stderr)
        return result.returncode

    print("\nUI refresh installed. No database rows were changed.")
    print("Start the app with:")
    print("  streamlit run .\\app.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
