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


# --------------------------------------------------------------- fixtures
# Several suites here are dual-use: a plain `python tests/test_x.py` run drives
# them through their own main(), which passes these arguments explicitly. Under
# pytest the same functions are collected as tests, so the arguments have to
# exist as fixtures or every one of them errors at setup.

import tempfile

import pytest


@pytest.fixture
def tmp():
    """A scratch directory, matching what test_club_sources.main() passes."""
    with tempfile.TemporaryDirectory() as folder:
        yield Path(folder)


@pytest.fixture(scope="session")
def con():
    """Connection to the clean-build integration database.

    test_integration.py rebuilds `test_gridley.db` from the cached .rda, which
    takes minutes. That is a release gate, not something to run on every save,
    so it is opt-in: set SDL_INTEGRATION=1 to include it.
    """
    if os.environ.get("SDL_INTEGRATION") != "1":
        pytest.skip("set SDL_INTEGRATION=1 to run the clean-build suite")

    import test_integration

    connection = test_integration.build(keep=True)
    if connection is None:
        pytest.fail("build_db.py failed -- see captured output")
    yield connection
    connection.close()