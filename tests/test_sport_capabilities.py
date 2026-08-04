#!/usr/bin/env python3
"""No page may branch on `sport.key`.

sports.py's docstring has stated this rule since the registry was written:

    Nothing in app.py or explore.py should ever branch on `sport.key`; if it
    needs to, the difference belongs in this file as a vocabulary or schema
    field.

Nothing enforced it, and by the time the NBA build existed there were nine
violations across five files. They are not hypothetical: `LIBRARY = ... if
SPORT.key == "afl" else []` reads as "the AFL has a grid library", but what
it means is "no other sport ever will", and the third sport inherits the
bug rather than the capability.

The last test in this file is the enforcement. It is blunt -- a substring
scan over the repository root -- and that is the point: a capability flag
someone can route around is not a capability flag.
"""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---


import sqlite3
from pathlib import Path

import pytest

import sports

CAPABILITIES = ("criterion_parser", "grid_library", "game_lab_module",
                "has_club_explorer", "has_awards_page", "has_past_games",
                "loader_hints", "club_data_tables", "club_data_hint",
                "family_hint", "search_examples", "grid_defaults",
                "venue_display")

#: Files allowed to name a sport. sports.py and data_paths.py are the
#: registries; theme.py names a *palette*, which happens to be called "AFL",
#: and that is presentation rather than capability.
ALLOWED = {"sports.py", "data_paths.py", "theme.py"}


@pytest.mark.parametrize("sport", list(sports.SPORTS.values()),
                         ids=list(sports.SPORTS))
def test_every_sport_declares_every_capability(sport):
    for field in CAPABILITIES:
        assert hasattr(sport, field), f"{sport.key} is missing {field}"


@pytest.mark.parametrize("sport", list(sports.SPORTS.values()),
                         ids=list(sports.SPORTS))
def test_grid_defaults_are_three_columns_and_three_rows(sport):
    columns, rows = sport.grid_defaults
    assert len(columns) == 3 and len(rows) == 3, sport.key


@pytest.mark.parametrize("sport", list(sports.SPORTS.values()),
                         ids=list(sports.SPORTS))
def test_grid_defaults_name_builders_the_sport_actually_has(sport):
    """Otherwise the board opens on a KeyError."""
    builders = sport.C.BUILDERS
    for axis in sport.grid_defaults:
        for name, _args in axis:
            assert name in builders, f"{sport.key}: {name!r} is not a builder"


@pytest.mark.parametrize("sport", list(sports.SPORTS.values()),
                         ids=list(sports.SPORTS))
def test_grid_default_clubs_are_clubs_the_sport_has(sport):
    """An NBA board opening on "St Kilda" is silently coerced to clubs[0]."""
    clubs = set(sport.schema.clubs)
    for axis in sport.grid_defaults:
        for _name, args in axis:
            if "club" in args:
                assert args["club"] in clubs, f"{sport.key}: {args['club']}"


@pytest.mark.parametrize("sport", list(sports.SPORTS.values()),
                         ids=list(sports.SPORTS))
def test_grid_default_stats_are_stats_the_sport_has(sport):
    stats = set(sport.schema.stats)
    for axis in sport.grid_defaults:
        for _name, args in axis:
            for key in ("stat", "stat_a", "stat_b"):
                if key in args:
                    assert args[key] in stats, f"{sport.key}: {args[key]}"


@pytest.mark.parametrize("sport", list(sports.SPORTS.values()),
                         ids=list(sports.SPORTS))
def test_search_examples_are_present_and_non_empty(sport):
    assert sport.search_examples
    assert all(isinstance(e, str) and e.strip() for e in sport.search_examples)


@pytest.mark.parametrize("sport", list(sports.SPORTS.values()),
                         ids=list(sports.SPORTS))
def test_every_search_example_actually_compiles(sport, request):
    """An example that does not run teaches a query that does not work.

    Compiled against the sport's own database, because several tokens are
    only valid when their layer is loaded -- `award:` declines with "Award
    data is not loaded" rather than a syntax error, and an example is only
    worth showing if it runs where it is shown.
    """
    import query_filters_family as Q

    if sport.key == "nba":
        request.getfixturevalue("nba_db")
    if not sport.exists():
        pytest.skip(f"no {sport.key} database built")

    con = sport.connect()
    failures = []
    try:
        for example in sport.search_examples:
            try:
                sql, params, *_ = Q.compile_query(sport.schema, example, con)
                con.execute(sql, params).fetchall()
            except Q.QuerySyntaxError as exc:
                # "not loaded" is an honest decline, not a broken example.
                if "not loaded" not in str(exc):
                    failures.append(f"{example!r}: {exc}")
            except Exception as exc:                    # noqa: BLE001
                failures.append(f"{example!r}: {type(exc).__name__}: {exc}")
    finally:
        con.close()
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("sport", list(sports.SPORTS.values()),
                         ids=list(sports.SPORTS))
def test_every_loader_hint_names_a_declared_layer(sport):
    """A hint for a layer nobody reports on is never shown."""
    probes = set(sport.optional_layers.values())
    assert set(sport.loader_hints) <= probes, sport.key


# --------------------------------------------------------------- the AFL

def test_afl_declares_the_pages_it_actually_has():
    assert sports.AFL.grid_library is True
    assert sports.AFL.criterion_parser == "afl.parse_criteria"
    assert sports.AFL.game_lab_module == "afl.game_lab"
    assert sports.AFL.has_club_explorer is True
    assert sports.AFL.has_awards_page is True
    assert sports.AFL.has_past_games is True


def test_afl_club_data_tables_are_the_six_app_py_used_to_hardcode():
    assert sports.AFL.club_data_tables == frozenset({
        "clubs", "club_source_snapshots", "club_wikipedia_fields",
        "club_player_totals", "club_player_register", "club_player_records"})


def test_afl_has_a_hint_for_every_layer_it_declares():
    assert set(sports.AFL.loader_hints) == set(
        sports.AFL.optional_layers.values())


def test_afl_criterion_parser_and_game_lab_modules_import():
    import importlib
    for name in (sports.AFL.criterion_parser, sports.AFL.game_lab_module):
        assert importlib.import_module(name)


# --------------------------------------------------------------- the NBA

def test_nba_declares_none_of_the_afl_only_pages():
    assert sports.NBA.grid_library is False
    assert sports.NBA.criterion_parser == ""
    assert sports.NBA.game_lab_module == ""
    assert sports.NBA.has_club_explorer is False
    assert sports.NBA.has_awards_page is False
    assert sports.NBA.has_past_games is False


def test_nba_has_no_loader_hints_because_it_has_no_loaders():
    """Naming a script that does not exist is worse than saying nothing."""
    assert sports.NBA.loader_hints == {}
    assert sports.NBA.club_data_tables == frozenset()


def test_an_empty_club_data_table_set_is_trivially_satisfied():
    """app.py computes CLUB_DATA_OK for every sport; it must not be False
    for a sport that simply has no Club Explorer."""
    assert sports.NBA.club_data_tables <= {"anything"}


def test_missing_layer_hints_yields_nothing_for_a_sport_without_hints(nba_db):
    con = sqlite3.connect(f"file:{nba_db}?mode=ro", uri=True)
    try:
        assert list(sports.NBA.missing_layer_hints(con)) == []
    finally:
        con.close()


def test_missing_layer_hints_yields_the_afl_hints_when_layers_are_absent():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE players (player_id INTEGER)")
    found = dict(sports.AFL.missing_layer_hints(con))
    assert "Draft data" in found
    assert "afl/load_draftguru.py" in found["Draft data"]
    con.close()


def test_the_nba_status_panel_reports_its_layers_as_not_loaded(nba_db):
    con = sqlite3.connect(f"file:{nba_db}?mode=ro", uri=True)
    try:
        rows = dict(sports.NBA.status(con))
    finally:
        con.close()
    assert rows["Draft data"] == "not loaded"
    assert rows["Award data"] == "not loaded"
    assert "Players" in rows


# ---------------------------------------------------------- the rule itself

def test_no_module_branches_on_the_sport_key():
    """The rule sports.py has claimed since it was written."""
    offenders = []
    for path in sorted(Path(_ROOT).glob("*.py")):
        if path.name in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if 'key == "afl"' in line or 'key != "afl"' in line:
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, (
        "branch on a capability field, not on the sport's name:\n"
        + "\n".join(offenders))


def main():
    import subprocess
    return subprocess.call([_sys.executable, "-m", "pytest", __file__, "-q"])


if __name__ == "__main__":
    _sys.exit(main())
