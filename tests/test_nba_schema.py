#!/usr/bin/env python3
"""The NBA schema declaration, and the contracts it has to satisfy.

Four of these pin bugs that were already in sports.py before the NBA build
existed, and would have surfaced only once the sport became selectable:

  * `career_postseason` was never overridden, so the schema still named the
    AFL's `finals_played` while the build writes `playoffs_played`.
    core.require_schema would have rejected every NBA database.
  * `clubs` was `[]`. app.axis_widget does `clubs[0]`.
  * `solve_cols` was `()`, so core's six-column default applied -- but
    app.py and afl/fetch_grid.py read that tuple by position.
  * data_paths.LEGACY listed a root `nba.db` that has never existed, so
    sport_db("nba") resolved outside data/nba/ for exactly as long as the
    database was missing.

The solve-column test runs over every registered sport, not just the NBA.
Obscurity-last is an unenforced contract read in three places
(core.square's row[width], app.py's best[-1], afl/fetch_grid.py's g[7]) and
getting it wrong rates every square by the wrong column, silently.
"""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---


import json

import pytest

import data_paths
from nba import nba_reference
import sports


# ------------------------------------------------------- the four bugs

def test_career_postseason_is_the_column_the_build_writes():
    assert sports.NBA_SCHEMA.career_postseason == "playoffs_played"
    assert sports.NBA_SCHEMA.career_postseason != sports.AFL_SCHEMA.career_postseason


def test_clubs_is_never_empty():
    """axis_widget does clubs[0] with no guard."""
    clubs = sports.NBA_SCHEMA.clubs
    assert clubs, "an empty club list is an IndexError in axis_widget"
    assert all(isinstance(c, str) and c.strip() for c in clubs)
    assert len(clubs) >= 30


def test_stats_lead_with_points():
    """axis_widget offers stats[0] as the default statistic."""
    assert sports.NBA_SCHEMA.stats[0] == "points"


def test_nba_db_never_resolves_outside_its_data_directory(tmp_path,
                                                          monkeypatch):
    """Even with a stray root nba.db present, which LEGACY used to prefer."""
    monkeypatch.setattr(data_paths, "ROOT", tmp_path)
    (tmp_path / "nba.db").write_bytes(b"")          # the stray legacy file
    resolved = data_paths.sport_db("nba")
    assert resolved.endswith(_os.path.join("data", "nba", "nba.db")), resolved
    assert "nba" not in data_paths.LEGACY


# ------------------------------------------- the positional solve contract

@pytest.mark.parametrize("sport", list(sports.SPORTS.values()),
                         ids=list(sports.SPORTS))
def test_obscurity_is_the_last_solve_column(sport):
    columns = sport.schema.solve_columns()
    expression, header = columns[-1]
    assert expression == f"p.{sport.schema.obscurity}", (
        f"{sport.key}: core.square() reads the final column as the rating")
    assert header == sport.schema.obscurity_header()


@pytest.mark.parametrize("sport", list(sports.SPORTS.values()),
                         ids=list(sports.SPORTS))
def test_solve_columns_are_eight_wide(sport):
    """app.py and afl/fetch_grid.py index this tuple positionally."""
    assert len(sport.schema.solve_columns()) == 8, sport.key


@pytest.mark.parametrize("sport", list(sports.SPORTS.values()),
                         ids=list(sports.SPORTS))
def test_solve_column_headers_are_unique(sport):
    headers = [h for _, h in sport.schema.solve_columns()]
    assert len(headers) == len(set(headers)), sport.key


def test_the_two_sports_name_their_club_column_differently():
    """Which is why app.py must not test for the literal "Clubs"."""
    assert sports.AFL_SCHEMA.clubs_hist_header() == "Clubs"
    assert sports.NBA_SCHEMA.clubs_hist_header() == "Teams"


# ------------------------------------------------------ franchise lineage

def test_franchise_lineage_expands_one_way_only():
    """A Seattle square is a question about Seattle."""
    s = sports.NBA_SCHEMA
    thunder = s.club_identities("Oklahoma City Thunder")
    assert "Seattle SuperSonics" in thunder
    assert "Oklahoma City Thunder" in thunder
    assert s.club_identities("Seattle SuperSonics") == ["Seattle SuperSonics"]


def test_every_lineage_lists_its_own_current_name_first():
    for current, identities in sports.NBA_SCHEMA.club_lineage.items():
        assert identities[0] == current, current
        assert len(identities) == len(set(identities)), current


def test_every_lineage_key_is_a_current_team():
    teams = set(sports.NBA_SCHEMA.clubs)
    for current in sports.NBA_SCHEMA.club_lineage:
        assert current in teams, f"{current} is not in the team list"


# --------------------------------------------------- the reference bootstrap

def test_a_missing_reference_file_falls_back_rather_than_raising(monkeypatch,
                                                                 tmp_path):
    monkeypatch.setattr(nba_reference, "PATH", tmp_path / "absent.json")
    assert nba_reference.load() == {}
    assert nba_reference.is_measured() is False
    assert list(nba_reference.teams()) == list(nba_reference.FALLBACK_TEAMS)


def test_a_corrupt_reference_file_falls_back_rather_than_raising(monkeypatch,
                                                                 tmp_path):
    """A truncated file must not be able to stop `import sports`."""
    broken = tmp_path / "nba_reference.json"
    broken.write_text('{"teams": ["Boston Celtics"', encoding="utf-8")
    monkeypatch.setattr(nba_reference, "PATH", broken)
    assert nba_reference.load() == {}
    assert len(nba_reference.teams()) == len(nba_reference.FALLBACK_TEAMS)
    assert nba_reference.club_lineage() == dict(nba_reference.FALLBACK_LINEAGE)


def test_a_measured_reference_file_supersedes_the_fallback(monkeypatch,
                                                           tmp_path):
    path = tmp_path / "nba_reference.json"
    path.write_text(json.dumps({
        "teams": ["Boston Celtics", "Chicago Bulls"],
        "club_lineage": {"Chicago Bulls": ["Chicago Bulls"]},
        "stat_eras": {"steals": 1973},
    }), encoding="utf-8")
    monkeypatch.setattr(nba_reference, "PATH", path)
    assert nba_reference.is_measured() is True
    assert nba_reference.teams() == ["Boston Celtics", "Chicago Bulls"]
    assert nba_reference.stat_eras() == {"steals": 1973}


def test_a_reference_file_that_is_not_an_object_falls_back(monkeypatch,
                                                           tmp_path):
    path = tmp_path / "nba_reference.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setattr(nba_reference, "PATH", path)
    assert nba_reference.load() == {}


# ------------------------------------------------------------- registry

def test_nba_is_not_offered_until_a_database_exists():
    """enabled=True is safe because selectable() also requires exists()."""
    assert sports.NBA.enabled is True
    if not sports.NBA.exists():
        assert sports.NBA not in sports.selectable()


def test_nba_declares_its_own_obscurity_model():
    from afl import obscurity_model as afl_obscurity
    from nba import obscurity_model as nba_obscurity
    assert sports.NBA.obscurity_model is nba_obscurity.MODEL
    assert sports.AFL.obscurity_model is afl_obscurity.MODEL
    assert nba_obscurity.MODEL is not afl_obscurity.MODEL


def test_the_star_disclaimer_matches_the_sport():
    """Telling NBA users their rating comes from Brownlow votes is a lie."""
    assert "Brownlow" in sports.AFL.star_disclaimer
    assert "Brownlow" not in sports.NBA.star_disclaimer
    assert "points" in sports.NBA.star_disclaimer


def test_required_player_cols_names_the_obscurity_inputs():
    """career_minutes is an NBA obscurity term; require_schema must demand it."""
    assert "career_minutes" in sports.NBA_SCHEMA.required_player_cols
    assert "name_key" in sports.NBA_SCHEMA.required_player_cols


# ------------------------------------------------- staging is not the app db

def test_staging_output_is_never_the_database_the_app_opens():
    """The fifth bug, and the one that broke the NBA sport outright.

    The Basketball-Reference ingestion scripts wrote leaderboard and award
    staging tables straight into data/nba/nba.db -- the path sport_db("nba")
    resolves. The result was a file with a `players` table of the wrong
    shape, so Sport.exists() said yes and every query then failed on a
    missing column.
    """
    staging = data_paths.staging_db("nba", "nba_bbr.db")
    assert str(staging) != data_paths.sport_db("nba")
    assert staging.parent.name == "staging"


@pytest.mark.parametrize("module", ["nba.link_nba_bbr_players",
                                    "utils.nba.load_nba_bbr_staging",
                                    "utils.nba.patch_nba_db",
                                    "nba.seed_nba_test_players",
                                    "utils.nba.load_and_link_nba_sample"])
def test_no_bbr_script_points_at_the_app_database(module):
    import importlib

    imported = importlib.import_module(module)
    assert str(imported.DB_PATH) != data_paths.sport_db("nba")
    assert "staging" in str(imported.DB_PATH)


def main():
    import subprocess
    return subprocess.call([_sys.executable, "-m", "pytest", __file__, "-q"])


if __name__ == "__main__":
    _sys.exit(main())
