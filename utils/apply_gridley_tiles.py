#!/usr/bin/env python3
"""Restyle the board squares as Gridley-style gradient tiles.

The board stays dark. Each filled square becomes a gradient tile coloured by
how obscure its best answer is, the way gridleygame.com colours a solved cell
by how rare the pick was: deep indigo for the rarest, magenta for the rest.

Dry run by default.

    python apply_gridley_tiles.py
    python apply_gridley_tiles.py --apply

Both edited files are backed up as *.bak_gridley_tiles.
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

RARE_AT = 75.0   # obscurity at or above this renders as the indigo tile


def read_raw(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def write_raw(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


THEME_EDITS = [
    # 1. Two new palette keys, so Custom mode gets pickers for them.
    (
        'KEYS = ["board", "panel", "line", "chalk", "muted", "accent", "hover"]',
        'KEYS = ["board", "panel", "line", "chalk", "muted", "accent", "hover",\n'
        '        "tile_rare", "tile_common"]',
    ),
    (
        '    "hover": "Hover",\n}',
        '    "hover": "Hover",\n'
        '    "tile_rare": "Rare tile",\n'
        '    "tile_common": "Common tile",\n}',
    ),
    # 2. Tile colours default per sport rather than being repeated in every
    #    palette, so an added mode can never miss them.
    (
        'MODES = ["Dark", "Light", "Custom"]',
        '#: Gridley colours a solved cell by how rare the answer was. The same\n'
        '#: idea drives the board here, keyed to our own obscurity rating.\n'
        'TILE_DEFAULTS = {\n'
        '    "afl": {"tile_rare": "#4C2FB0", "tile_common": "#C42794"},\n'
        '    "nba": {"tile_rare": "#3E2A93", "tile_common": "#C4451F"},\n'
        '}\n'
        '\n'
        'MODES = ["Dark", "Light", "Custom"]',
    ),
    # 3. Fill the tile keys for whichever palette was resolved.
    (
        '    base = PALETTES.get((sport_key, mode if mode != "Custom" else "Dark"),\n'
        '                        PALETTES[("afl", "Dark")])\n'
        '    if mode != "Custom":\n'
        '        return dict(base)\n'
        '    merged = dict(base)',
        '    base = PALETTES.get((sport_key, mode if mode != "Custom" else "Dark"),\n'
        '                        PALETTES[("afl", "Dark")])\n'
        '    tiles = TILE_DEFAULTS.get(sport_key, TILE_DEFAULTS["afl"])\n'
        '    base = {**tiles, **base}\n'
        '    if mode != "Custom":\n'
        '        return dict(base)\n'
        '    merged = dict(base)',
    ),
    # 4. Expose the tiles to the stylesheet.
    (
        "  --hover:  {p['hover']};\n}}",
        "  --hover:  {p['hover']};\n"
        "  --tile-rare:   {p['tile_rare']};\n"
        "  --tile-common: {p['tile_common']};\n}}",
    ),
    # 5. The square itself.
    (
        """/* Square face: a painted panel carrying the prefilled answer. */
.square {{
  background: var(--panel);
  border: 1px solid var(--line);""",
        """/* Square face: a Gridley-style gradient tile. The hue carries meaning --
   indigo for a rare best answer, magenta for a common one -- so the board
   reads at a glance without anyone parsing the star row. */
.square {{
  background:
    radial-gradient(circle at 22% 18%, rgba(255,255,255,.16) 0 1px, transparent 1px),
    radial-gradient(circle at 68% 34%, rgba(255,255,255,.11) 0 1px, transparent 1px),
    radial-gradient(circle at 41% 77%, rgba(255,255,255,.13) 0 1px, transparent 1px),
    radial-gradient(circle at 84% 66%, rgba(255,255,255,.09) 0 1px, transparent 1px),
    linear-gradient(150deg,
      color-mix(in srgb, var(--tile-common) 82%, black) 0%,
      var(--tile-common) 55%,
      color-mix(in srgb, var(--tile-common) 72%, #ff7ad9) 100%);
  border: 1px solid color-mix(in srgb, var(--tile-common) 60%, black);""",
    ),
    (
        """.square.is-open {{ border-color: var(--amber); }}
.square.is-empty {{ opacity: .55; }}""",
        """.square.tile-rare {{
  background:
    radial-gradient(circle at 22% 18%, rgba(255,255,255,.18) 0 1px, transparent 1px),
    radial-gradient(circle at 68% 34%, rgba(255,255,255,.12) 0 1px, transparent 1px),
    radial-gradient(circle at 41% 77%, rgba(255,255,255,.14) 0 1px, transparent 1px),
    radial-gradient(circle at 84% 66%, rgba(255,255,255,.10) 0 1px, transparent 1px),
    linear-gradient(150deg,
      color-mix(in srgb, var(--tile-rare) 74%, black) 0%,
      var(--tile-rare) 58%,
      color-mix(in srgb, var(--tile-rare) 70%, #7f6bff) 100%);
  border-color: color-mix(in srgb, var(--tile-rare) 55%, black);
}}
.square.is-open {{
  border-color: var(--amber);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--amber) 55%, transparent);
}}
.square.is-empty {{
  background: var(--panel);
  border: 1px solid var(--line);
  opacity: .55;
}}""",
    ),
    # 6. Tile text needs its own contrast: the chalk colour is tuned for the
    #    board, not for a saturated tile.
    (
        """.square-name {{
  font-family: 'Oswald', sans-serif;
  text-transform: uppercase;
  letter-spacing: .03em;
  font-size: .95rem;
  line-height: 1.1;
  color: var(--chalk);
}}
.square-meta {{
  font-family: 'IBM Plex Mono', monospace;
  font-size: .66rem;
  color: var(--muted);""",
        """.square-name {{
  font-family: 'Oswald', sans-serif;
  text-transform: uppercase;
  letter-spacing: .03em;
  font-size: .95rem;
  line-height: 1.1;
  color: #FFFFFF;
  text-shadow: 0 1px 2px rgba(0,0,0,.45);
}}
.square.is-empty .square-name {{ color: var(--chalk); text-shadow: none; }}
.square-meta {{
  font-family: 'IBM Plex Mono', monospace;
  font-size: .66rem;
  color: rgba(255,255,255,.78);""",
    ),
    (
        """.square-meta {{
  font-family: 'IBM Plex Mono', monospace;
  font-size: .66rem;
  color: rgba(255,255,255,.78);
  letter-spacing: .04em;
  text-transform: uppercase;
}}""",
        """.square-meta {{
  font-family: 'IBM Plex Mono', monospace;
  font-size: .66rem;
  color: rgba(255,255,255,.78);
  letter-spacing: .04em;
  text-transform: uppercase;
}}
.square.is-empty .square-meta {{ color: var(--muted); }}
.square .stars-back {{ color: rgba(255,255,255,.30); }}
.square .stars-fore {{ color: #FFD87A; }}
.square .stars-num  {{ color: rgba(255,255,255,.78); }}""",
    ),
]

APP_EDITS = [
    (
        """            face = (
                f"<div class='square{' is-open' if open_here else ''}'>"
                f"<div class='square-name'>{sq.best_name}</div>\"""",
        """            # Gridley tints a solved cell by how rare the answer was.
            # Our obscurity rating is the same idea, so it picks the tile.
            tier = " tile-rare" if sq.obscurity >= 75 else ""
            face = (
                f"<div class='square{tier}{' is-open' if open_here else ''}'>"
                f"<div class='square-name'>{sq.best_name}</div>\"""",
    ),
]


def apply_edits(path: Path, edits, apply: bool) -> bool:
    if not path.exists():
        print(f"  missing  {path}")
        return False
    text = read_raw(path)
    nl = "\r\n" if "\r\n" in text else "\n"
    flat = text.replace("\r\n", "\n")

    missing = []
    staged = flat
    for old, new in edits:
        if old in staged:
            staged = staged.replace(old, new, 1)
        else:
            missing.append(old)
    if missing:
        print(f"  SKIP     {path.name}: {len(missing)} anchor(s) not found")
        for old in missing:
            print(f"           first line: {old.splitlines()[0][:70]!r}")
        print("           the file has already been edited, or differs from "
              "the version this patch was written against")
        return False

    flat = staged

    print(f"  patch    {path.name}  ({len(edits)} edits)")
    if not apply:
        return True
    shutil.copy2(path, path.with_suffix(path.suffix + ".bak_gridley_tiles"))
    write_raw(path, flat.replace("\n", nl))
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    root = args.root.resolve()
    if not (root / "app.py").exists():
        ap.error(f"{root} does not look like the Sports Data Lab root")

    print(f"Gridley tiles — {'APPLY' if args.apply else 'DRY RUN'}")
    ok = apply_edits(root / "theme.py", THEME_EDITS, args.apply)
    ok = apply_edits(root / "app.py", APP_EDITS, args.apply) and ok

    if not ok:
        print("\nNo file was changed. Resolve the skipped anchors first.")
        return 1
    if not args.apply:
        print("\nNothing written. Rerun with --apply.")
    else:
        print("\nDone. Restart Streamlit to see the board.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
