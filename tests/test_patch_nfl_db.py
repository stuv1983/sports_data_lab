"""Regression tests for utils/nfl/patch_nfl_db.py's reference file handling.

The bug: write_reference() used to write straight to nfl_reference.PATH, a
fixed constant, no matter what --db pointed at. database_updates.py patches
a staging file that sits beside the live database (same directory,
nfl.db.update-building next to nfl.db) and is not promoted until later
checks pass -- so every staging patch was already overwriting the live
data/nfl/reference/nfl_reference.json before the outer job even validated
the build it came from. mlb/build_mlb_db.py had the identical bug, fixed
already; see tests/test_mlb_build.py::test_a_fixture_build_writes_its_reference_beside_itself.
"""

import json
import sqlite3

from nfl import nfl_reference
from utils.nfl import patch_nfl_db


def _minimal_db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE games (club_now TEXT, season INTEGER, "
                "touchdowns INTEGER)")
    con.execute("INSERT INTO games VALUES ('Kansas City Chiefs', 2024, 3)")
    con.execute("INSERT INTO games VALUES ('Buffalo Bills', 2024, 1)")
    con.commit()
    return con


def test_the_reference_file_is_written_beside_its_own_database(tmp_path):
    db = tmp_path / "nfl.db"
    con = _minimal_db(db)
    try:
        patch_nfl_db.write_reference(
            con, lambda *_: None, patch_nfl_db._reference_path(db))
    finally:
        con.close()

    beside = tmp_path / "reference" / "nfl_reference.json"
    assert beside.exists()
    assert beside != nfl_reference.PATH
    payload = json.loads(beside.read_text(encoding="utf-8"))
    assert "Kansas City Chiefs" in payload["teams"]


def test_reference_only_does_not_require_the_full_patch_step(tmp_path):
    """--reference-only is what database_updates.py runs against the live
    database after promotion -- it must work on a plain, already-patched
    database without redoing patch_games/patch_players."""
    db = tmp_path / "nfl.db"
    con = _minimal_db(db)
    con.close()

    assert patch_nfl_db.main(["--db", str(db), "--reference-only"]) == 0
    assert (tmp_path / "reference" / "nfl_reference.json").exists()


def test_no_reference_leaves_the_reference_file_absent(tmp_path, monkeypatch):
    """The staging step in database_updates.py passes --no-reference so a
    patch that is never promoted cannot leave its view of the teams behind
    for the live database to read. patch_games/patch_players are stubbed
    out here because this test is only about the write_reference_file
    wiring, not the full column-derivation pipeline, which wants a much
    larger nflverse-shaped fixture than this test needs."""
    db = tmp_path / "nfl.db"
    con = _minimal_db(db)
    con.execute("CREATE TABLE players (player_id INTEGER)")
    con.commit()
    con.close()

    monkeypatch.setattr(patch_nfl_db, "patch_games", lambda con, say: None)
    monkeypatch.setattr(patch_nfl_db, "patch_players", lambda con, say: None)
    monkeypatch.setattr(patch_nfl_db, "add_indexes", lambda con, say: None)

    patch_nfl_db.patch(str(db), verbose=False, write_reference_file=False)
    assert not (tmp_path / "reference" / "nfl_reference.json").exists()
