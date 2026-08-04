"""
afl/grid_fixtures.py -- Real Gridley grids, kept as permanent regression
fixtures.

The grids themselves now live in afl/historic_grids.py, as HistoricGrid
records the app can load and Game Lab can mine for clue wording. This
module is the test-facing view of that list, and keeps the exact dict
shape the existing suite indexes, so nothing in test_integration.py or
test_awards_integration.py had to change to follow the move.

`unsupported` names the criteria that are genuinely absent from the
dataset and MUST be declined rather than guessed at. A criterion listed
there is never substituted: see the module docstring in afl/historic_grids.py
for why Practice mode is a separate, loudly-labelled thing.
"""

# Run standalone from anywhere: this file lives at the project root.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

from .historic_grids import GRIDS as _GRIDS

#: Complete captures, in the historical dict shape: every criterion here
#: sits on a known axis, so all nine intersections are checkable.
GRIDS = {
    g.key: {
        "cols": list(g.cols),
        "rows": list(g.rows),
        "unsupported": list(g.unsupported),
    }
    for g in _GRIDS if g.complete
}

#: Criteria from partial captures -- known wording, unknown axis. They
#: cannot be intersected, but each one must still parse or decline
#: correctly, so the suite exercises them individually.
LOOSE_CRITERIA = {
    g.key: {
        "criteria": list(g.partial_criteria),
        "unsupported": list(g.unsupported),
        "note": g.note,
    }
    for g in _GRIDS if not g.complete
}

# #1106 was played using this tool's answers. Kept as an outcome record:
# the three top-row picks were the tool's top three, and the grid scored
# 1.8 total rarity with three squares at 0.0%.
KNOWN_GOOD_ANSWERS = {
    ("#1106 (2026-07-26)", "30+ GOALS TWO DIFF CLUBS", "St Kilda"):
        "Percy Outram",
    ("#1106 (2026-07-26)", "30+ GOALS TWO DIFF CLUBS", "North Melbourne"):
        "Sel Murray",
    ("#1106 (2026-07-26)", "30+ GOALS TWO DIFF CLUBS", "150+ GAMES PLAYED"):
        "Billy Dick",
}
