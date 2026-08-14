#!/usr/bin/env python3
"""Regenerate docs/afl_grid_criteria.md from the live builder registry.

The doc is a rendering of afl/constraints.py's BUILDER_GROUPS and BUILDERS
-- the same registry the Grid Solver's Type picker reads -- so the app and
its catalogue cannot drift apart quietly. Run after adding or regrouping a
builder:

    python utils/afl/make_grid_criteria_doc.py
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "docs" / "afl_grid_criteria.md"


def build() -> str:
    from afl import constraints as C

    lines = [
        "# AFL grid questions — the organised catalogue",
        "",
        "Every axis the Grid Solver can build, grouped by the kind of",
        "question a square asks. Generated from `afl/constraints.py`'s",
        "`BUILDER_GROUPS` and `BUILDERS` — the same registry the Type",
        "picker reads — so this list and the app cannot drift apart",
        "quietly. Regenerate after adding a builder:",
        "",
        "    python utils/afl/make_grid_criteria_doc.py",
        "",
        "Optional-layer builders (draft, awards, Brownlow, Rising Star,",
        "captaincy, family, match context) appear in the app only when",
        "their data is loaded.",
        "",
    ]
    placed: set[str] = set()
    for group, names in C.BUILDER_GROUPS.items():
        offered = [name for name in names if name in C.BUILDERS]
        if not offered:
            continue
        lines += [f"## {group}", "",
                  "| Question | Arguments | What it answers |",
                  "|---|---|---|"]
        for name in offered:
            placed.add(name)
            fn, argnames = C.BUILDERS[name]
            doc = inspect.getdoc(fn) or ""
            first = doc.splitlines()[0].strip() if doc else ""
            arglist = ", ".join(argnames) if argnames else "—"
            lines.append(f"| {name} | {arglist} | {first} |")
        lines.append("")
    leftovers = [name for name in C.BUILDERS if name not in placed]
    if leftovers:
        lines += ["## Ungrouped", ""]
        lines += [f"- {name}" for name in leftovers]
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    OUT.write_text(build(), encoding="utf-8")
    from afl import constraints as C
    placed = {name for names in C.BUILDER_GROUPS.values() for name in names}
    print(f"wrote {OUT}")
    print(f"{len(C.BUILDERS)} builders, "
          f"{len([n for n in C.BUILDERS if n in placed])} grouped, "
          f"{len([n for n in C.BUILDERS if n not in placed])} ungrouped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
