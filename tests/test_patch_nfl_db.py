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


# ----------------------------------------------------- NULL preservation

def test_all_null_touchdown_components_stay_null(tmp_path, monkeypatch):
    """A 1999 defensive lineman whose row records no touchdown column was
    not measured at zero -- games.touchdowns must stay NULL for him while
    still summing mixed-presence rows for everyone else. The project rule:
    unrecorded history is NULL, never 0."""
    db = tmp_path / "nfl.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE games (player_id TEXT, season INTEGER, "
                "season_type TEXT, passing_tds REAL, rushing_tds REAL, "
                "receiving_tds REAL)")
    con.execute("INSERT INTO games VALUES ('unmeasured', 1999, 'REG', "
                "NULL, NULL, NULL)")
    con.execute("INSERT INTO games VALUES ('partial', 2024, 'REG', "
                "2, NULL, 1)")
    con.commit()

    # Only the touchdown derivation is under test; the team/match joins
    # want a much larger nflverse-shaped fixture than this needs.
    monkeypatch.setattr(patch_nfl_db, "_patch_teams", lambda con, say: None)
    monkeypatch.setattr(patch_nfl_db, "_patch_from_matches",
                        lambda con, say: None)
    monkeypatch.setattr(patch_nfl_db, "_patch_career_game_no",
                        lambda con, say: None)
    patch_nfl_db.patch_games(con, lambda *_: None)

    rows = dict(con.execute("SELECT player_id, touchdowns FROM games"))
    con.close()
    assert rows["unmeasured"] is None, "an unmeasured row was stamped 0"
    assert rows["partial"] == 3, "mixed presence must still add what is there"


# ------------------------------------------------------------- indexes

def _games_for_indexes(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE games (player_id TEXT, season INTEGER, "
                "round TEXT, club_now TEXT, club_hist TEXT, "
                "opponent_team TEXT, venue TEXT, is_playoff INTEGER, "
                "career_game_no INTEGER)")
    con.commit()
    return con


def _index_columns(con, name):
    return [row[2] for row in sorted(con.execute(
        f"PRAGMA index_info({name})"))]


def test_an_existing_index_with_an_old_definition_is_rebuilt(tmp_path):
    """CREATE INDEX IF NOT EXISTS keeps whatever holds the name, so the
    widened (is_playoff, season) composite would silently never land on a
    database patched before the upgrade."""
    con = _games_for_indexes(tmp_path / "nfl.db")
    con.execute("CREATE INDEX ix_games_playoff ON games(is_playoff)")
    con.commit()

    patch_nfl_db.add_indexes(con, lambda *_: None)

    assert _index_columns(con, "ix_games_playoff") == ["is_playoff", "season"]
    con.close()


def test_indexes_come_from_the_sport_schema_not_hardcoded_strings(tmp_path):
    con = _games_for_indexes(tmp_path / "nfl.db")
    patch_nfl_db.add_indexes(con, lambda *_: None)

    names = {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"ix_games_opponent", "ix_games_player_season",
            "ix_games_playoff", "ix_games_season_round"} <= names
    assert _index_columns(con, "ix_games_opponent") == ["opponent_team"]
    con.close()


def test_a_current_index_is_left_alone_and_not_counted_as_created(tmp_path):
    con = _games_for_indexes(tmp_path / "nfl.db")
    said = []
    patch_nfl_db.add_indexes(con, said.append)
    patch_nfl_db.add_indexes(con, said.append)

    assert any("8 schema-derived indexes created" in line for line in said)
    assert any("0 schema-derived indexes created, 8 already current" in line
               for line in said)
    con.close()
