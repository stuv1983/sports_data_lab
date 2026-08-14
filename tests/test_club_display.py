#!/usr/bin/env python3
"""One club, one name: era names collapse and era logos resolve.

North Melbourne played 1999-2007 as the Kangaroos, Sydney was South
Melbourne until 1981, the Western Bulldogs were Footscray until 1996.
`games.club_hist` rightly records the name of the time, but a career
list or a club menu reading "Kangaroos, North Melbourne" is one club
shown as two. The rule under test: within one club, a single era name is
kept as written -- the team of the time -- and a span across the rename
is named as the club is now.

The Brisbane Bears are the counter-example throughout: a genuinely
separate club (the database keeps them statistically distinct from the
Lions), so they must never collapse into anyone -- and they have their
own badge.
"""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---


import types

import pytest

import registry
import sports


RENAMES = {
    "Kangaroos": "North Melbourne",
    "South Melbourne": "Sydney",
    "Footscray": "Western Bulldogs",
}


# ------------------------------------------------------------- collapsing

def test_a_rename_mid_career_is_one_club_named_as_it_is_now():
    """Brent Harvey: Kangaroos then North Melbourne, one club throughout."""
    assert registry.collapse_clubs(
        ["Kangaroos", "North Melbourne"], RENAMES) == ["North Melbourne"]


def test_a_lone_era_name_stays_the_team_of_the_time():
    """A South Melbourne career that ended in 1979 was never 'Sydney'."""
    assert registry.collapse_clubs(["South Melbourne"], RENAMES) == \
        ["South Melbourne"]


def test_a_two_club_career_keeps_both_with_each_era_name():
    """Barry Round: Footscray, then South Melbourne into Sydney. His
    Footscray years never crossed that club's rename, so they keep the
    name; his Swans years crossed one, so they take the current name."""
    assert registry.collapse_clubs(
        ["Footscray", "South Melbourne", "Sydney"], RENAMES) == \
        ["Footscray", "Sydney"]


def test_the_answer_does_not_depend_on_the_order_the_names_arrive():
    """GROUP_CONCAT makes no ordering promise; a career path does. Both
    must land on the same answer."""
    assert registry.collapse_clubs(
        ["North Melbourne", "Kangaroos"], RENAMES) == ["North Melbourne"]


def test_the_bears_never_collapse_into_the_lions():
    """Renames are club_hist <> club_now only. The Bears' club_now is the
    Bears, so they are absent from the map and must survive intact."""
    assert registry.collapse_clubs(
        ["Brisbane Bears", "Brisbane Lions"], RENAMES) == \
        ["Brisbane Bears", "Brisbane Lions"]


def test_collapse_club_path_keeps_the_separator_style():
    sport = types.SimpleNamespace(
        club_renames=lambda: RENAMES,
        collapse_clubs=lambda parts: registry.collapse_clubs(parts, RENAMES),
        collapse_club_path=registry.Sport.collapse_club_path,
    )
    collapse = lambda text: registry.Sport.collapse_club_path(sport, text)
    assert collapse("Kangaroos|North Melbourne") == "North Melbourne"
    assert collapse("Footscray, Melbourne") == "Footscray, Melbourne"
    assert collapse("") == ""


# ------------------------------------------------------- club cell menus

def test_a_club_cell_spanning_a_rename_becomes_one_button_not_a_menu():
    import components

    sport = types.SimpleNamespace(
        collapse_clubs=lambda parts: registry.collapse_clubs(parts, RENAMES))
    assert components._club_actions(
        "South Melbourne, Sydney", sport) == "Sydney"
    assert components._club_actions(
        "Kangaroos|North Melbourne", sport) == "North Melbourne"
    # Two real clubs still make a menu.
    assert components._club_actions(
        "Fitzroy | Sydney", sport) == ["Fitzroy", "Sydney"]
    # Without a sport, the cell renders as it always did.
    assert components._club_actions("South Melbourne, Sydney") == \
        ["South Melbourne", "Sydney"]


# ------------------------------------------------------------- era logos

def _touch(folder, *names):
    for name in names:
        (folder / name).write_text("logo")


def test_an_era_name_finds_its_own_badge(tmp_path):
    from afl import club_logos as CL

    _touch(tmp_path, "Footscray_Football_Club_colours.svg",
           "Brisbane_Bears.png", "Western_Bulldogs_logo.svg")
    found = CL.era_logo_files(
        ["Footscray", "Brisbane Bears"], tmp_path,
        exclude=[tmp_path / "Western_Bulldogs_logo.svg"])
    assert found["Footscray"].name == "Footscray_Football_Club_colours.svg"
    assert found["Brisbane Bears"].name == "Brisbane_Bears.png"


def test_an_era_name_cannot_steal_a_current_clubs_file(tmp_path):
    """'Melbourne' is inside 'North_Melbourne_FC_logo', but that file is
    North's: era matching only sees the files no current club claimed."""
    from afl import club_logos as CL

    _touch(tmp_path, "North_Melbourne_FC_logo.svg")
    found = CL.era_logo_files(
        ["Melbourne", "Kangaroos"], tmp_path,
        exclude=[tmp_path / "North_Melbourne_FC_logo.svg"])
    assert found == {}


# ============================================================ live data

def _live_afl():
    if not sports.AFL.exists():
        pytest.skip("no built AFL database")
    return sports.AFL


def test_live_the_renames_come_from_the_games_table():
    sport = _live_afl()
    renames = sport.club_renames()
    for era, now in RENAMES.items():
        assert renames.get(era) == now, (era, renames.get(era))
    assert "Brisbane Bears" not in renames
    assert "Fitzroy" not in renames


def test_live_harvey_and_round_read_as_their_clubs():
    sport = _live_afl()
    assert sport.collapse_club_path("Kangaroos|North Melbourne") == \
        "North Melbourne"
    assert sport.collapse_club_path("Footscray|South Melbourne|Sydney") == \
        "Footscray|Sydney"


def test_live_era_names_resolve_to_era_appropriate_logos():
    """A 1954 card says Footscray and shows Footscray's colours; a Bears
    card shows the Bears; a Kangaroos card shows North's badge, because
    no Kangaroos-era file exists and North is who they are."""
    import sqlite3

    import overlays

    sport = _live_afl()
    con = sqlite3.connect(f"file:{sport.db}?mode=ro", uri=True)
    try:
        footscray = overlays.logo_for(sport, con, "Footscray")
        bears = overlays.logo_for(sport, con, "Brisbane Bears")
        kangaroos = overlays.logo_for(sport, con, "Kangaroos")
        north = overlays.logo_for(sport, con, "North Melbourne")
        south = overlays.logo_for(sport, con, "South Melbourne")
        sydney = overlays.logo_for(sport, con, "Sydney")
    finally:
        con.close()

    assert footscray and "Footscray" in footscray
    assert bears and "Bears" in bears
    assert kangaroos == north and north
    assert south == sydney and sydney


def run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:
                if exc.__class__.__name__ == "Skipped":
                    continue
                raise
    print("club display tests: passed")


if __name__ == "__main__":
    run()
