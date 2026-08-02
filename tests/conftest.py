"""Make the repository root importable and current for every test."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# One-shot maintenance tooling lives in utils/ but is still test-covered
# (link_draft, link_people, repair_database). Keep the flat import names the
# tests already use rather than rewriting them to utils.<name>.
UTILS = ROOT / "utils"
if UTILS.is_dir() and str(UTILS) not in sys.path:
    sys.path.insert(0, str(UTILS))

os.chdir(ROOT)