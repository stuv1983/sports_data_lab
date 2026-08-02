#!/usr/bin/env python3
"""Retire the stale BROTHER PLAYED "unsupported" expectation.

Grid #1109 was captured when BROTHER PLAYED had no data source. The broad
Wikipedia family layer now supplies one, so two places still claim otherwise:

  1. historic_grids.py -- the #1109 record declares
     ``unsupported=('BROTHER PLAYED',)``. This single tuple drives both
     remaining integration failures: it is what ``grid_fixtures`` exposes as
     the criterion that MUST decline, and what the practice-mode check counts
     replacements against.

  2. tests/test_integration.py -- ``genuinely_unsupported`` names the
     criterion directly. The assertion is inverted rather than deleted, so
     BROTHER PLAYED stays pinned, to the opposite outcome.

Dry run by default.

    python fix_brother_played_expectation.py
    python fix_brother_played_expectation.py --apply
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

BACKUP_SUFFIX = ".bak_brother_expectation"


class PatchError(RuntimeError):
    pass


# --------------------------------------------------------------------------

GRID_OLD = """        rows=("LEADING GOALKICKER TEAM", "JAKE WATERMAN TEAMMATE",
              "BROTHER PLAYED"),
        unsupported=('BROTHER PLAYED',),
"""

GRID_NEW = """        rows=("LEADING GOALKICKER TEAM", "JAKE WATERMAN TEAMMATE",
              "BROTHER PLAYED"),
        # BROTHER PLAYED was unsupported when this grid was captured. The
        # broad Wikipedia family layer now answers it, so the criterion is
        # no longer declined and the board is fully playable.
        unsupported=(),
"""

TEST_OLD = '''    # VALIDATION_OPTIONAL_RISING_STAR_V1 — Rising Star nominations moved from
    # "unsupported" to a supported optional layer, exactly as CLUB CAPTAIN did.
    # Only criteria with no data source at all belong in this list.
    genuinely_unsupported = ("BROTHER PLAYED",)
'''

TEST_NEW = '''    # VALIDATION_FAMILY_LAYER_V1 — BROTHER PLAYED moved from "unsupported" to
    # the broad Wikipedia family layer, exactly as CLUB CAPTAIN and Rising
    # Star nominations did before it. Only criteria with no data source at
    # all belong in this list, and none of the captured grids now carry one.
    genuinely_unsupported = ()
'''

TEST_ANCHOR = '''    captain, captain_label = P.parse("CLUB CAPTAIN")
'''

TEST_EXTRA = '''    brother, brother_label = P.parse("BROTHER PLAYED")
    check("BROTHER PLAYED parses as the optional family constraint",
          brother is not None and brother_label == "brother also played",
          brother_label or "declined")

'''


def patch(path: Path, edits: list[tuple[str, str]], marker: str,
          apply: bool) -> None:
    if not path.is_file():
        raise PatchError(f"{path} not found")
    raw = path.read_text(encoding="utf-8")
    crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")

    if marker in text:
        print(f"  unchanged  {path.name} (already updated)")
        return

    for old, new in edits:
        if old not in text:
            raise PatchError(
                f"{path.name}: expected block not found; patch by hand"
            )
        text = text.replace(old, new, 1)

    if crlf:
        text = text.replace("\n", "\r\n")

    print(f"  patch      {path.name}")
    if apply:
        backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
        if not backup.exists():
            shutil.copy2(path, backup)
        path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    if not (root / "app.py").exists():
        print(f"error: {root} is not the Sports Data Lab root", file=sys.stderr)
        return 1

    mode = "APPLYING" if args.apply else "DRY RUN -- nothing will be written"
    print(f"Retire the BROTHER PLAYED decline: {root}")
    print(f"Mode: {mode}\n")

    try:
        patch(
            root / "historic_grids.py",
            [(GRID_OLD, GRID_NEW)],
            "The\n        # broad Wikipedia family layer now answers it",
            args.apply,
        )
        patch(
            root / "tests" / "test_integration.py",
            [(TEST_OLD, TEST_NEW), (TEST_ANCHOR, TEST_EXTRA + TEST_ANCHOR)],
            "VALIDATION_FAMILY_LAYER_V1",
            args.apply,
        )
    except PatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        "\nNote: no captured grid now declares an unsupported criterion, so\n"
        "the practice-mode block in test_integration.py (four checks, guarded\n"
        "by `if not report.grid.unsupported: continue`) will be skipped\n"
        "entirely. Practice mode still works; it is simply no longer covered\n"
        "by a real fixture. Restoring that coverage needs a grid carrying a\n"
        "still-unsupported criterion -- birthplace, jumper number or coaching\n"
        "wording all qualify."
    )

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the changes.")
    else:
        print(
            "\nDone. Verify with:\n"
            "  python -m compileall -q .\n"
            "  python tests\\test_integration.py"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
