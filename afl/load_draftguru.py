#!/usr/bin/env python3
"""Compatibility entry point for the AFL Draftguru loader."""

import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from utils.afl.load_draftguru import main


if __name__ == "__main__":
    main()
