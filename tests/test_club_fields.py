#!/usr/bin/env python3
"""Regression tests for scraped club metadata presentation.

The Club Explorer showed no nickname for fourteen of the eighteen clubs and
truncated every other headline. Both were presentation faults over data
that was already loaded correctly, so these tests work on the raw strings
the scrape actually stores.
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

import pytest

from afl import club_fields as CF


# ------------------------------------------------------------- nicknames

def test_nickname_key_spelling_is_covered():
    """Fourteen clubs store it as 'nickname_s', which the old lookup missed."""
    assert "nickname_s" in CF.NICKNAME_KEYS
    assert CF.headline({"nickname_s": "Tigers , Tiges"}, "nickname").primary \
        == "Tigers"
    assert CF.headline({"nickname": "Cats"}, "nickname").primary == "Cats"
    assert CF.headline({"nicknames": "Blues Blue Baggers"},
                       "nickname").primary == "Blues"


def test_nickname_takes_the_first_and_keeps_the_rest():
    got = CF.nickname("Swans, Swannies, Bloods")
    assert got.primary == "Swans"
    assert got.extras == ["Swannies", "Bloods"]


def test_indigenous_round_name_is_not_the_primary_nickname():
    """The bug a general label pattern causes: Adelaide reading 'Kuwarna'.

    'Crows Indigenous rounds: Kuwarna' has no label before 'Crows', and a
    greedy pattern reads 'Crows Indigenous rounds' as one label, leaving
    the Indigenous-round name as the only segment.
    """
    got = CF.nickname("Crows Indigenous rounds: Kuwarna")
    assert got.primary == "Crows"
    assert got.extras == ["Kuwarna"]

    got = CF.nickname("Eagles Indigenous rounds: Waalitj Marawar")
    assert got.primary == "Eagles"


def test_competition_prefixed_nickname():
    got = CF.nickname("AFL: Demons, Dees Indigenous rounds: Narrm")
    assert got.primary == "Demons"
    assert got.extras == ["Dees", "Narrm"]


def test_nickname_without_a_colon_after_the_competition():
    """Port Adelaide writes 'AFL Power, Port SANFL: Magpies ...'."""
    got = CF.nickname("AFL Power, Port SANFL: Magpies "
                      "Indigenous rounds: Yartapuulti")
    assert got.primary == "Power"


def test_footnote_markers_are_removed():
    got = CF.nickname("Hawks , The Family Club [ 2 ] [ 3 ] [ 4 ]")
    assert got.primary == "Hawks"
    assert "[" not in got.detail


def test_run_together_nicknames_do_not_invent_a_list():
    """'Blues Blue Baggers Baggers Old Navy Blues' is not seven nicknames."""
    got = CF.nickname("Blues Blue Baggers Baggers Old Navy Blues")
    assert got.primary == "Blues"
    assert got.extras == []
    assert got.raw                      # the source string is still carried


# --------------------------------------------------------------- founded

def test_founded_drops_the_stale_years_ago_clause():
    got = CF.founded("1885 ; 141 years ago ( 1885 )")
    assert got.primary == "1885"
    assert "years ago" not in got.detail


def test_founded_keeps_a_full_date_as_the_extra():
    got = CF.founded("18 July 1859 ; 167 years ago ( 18 July 1859 )")
    assert got.primary == "1859"
    assert got.extras == ["18 July 1859"]


# ---------------------------------------------------------------- ground

def test_ground_takes_the_first_venue_without_its_capacity():
    got = CF.ground("AFL: Melbourne Cricket Ground (100,024) "
                    "Ninja Stadium (20,000) AFLW/VFL: Punt Road Oval (2,800)")
    assert got.primary == "Melbourne Cricket Ground"
    assert got.extras == ["Ninja Stadium", "Punt Road Oval"]


def test_capacity_label_does_not_truncate_the_venue_name():
    """'(capacity: 40,000)' contains a colon and must not read as a label."""
    got = CF.ground("GMHBA Stadium [ a ] (capacity: 40,000)")
    assert got.primary == "GMHBA Stadium"


def test_venue_year_range_is_not_part_of_the_name():
    got = CF.ground("AFL: Perth Stadium 2018-present (capacity: 61,266)")
    assert got.primary == "Perth Stadium"


def test_multi_competition_label_is_matched_whole():
    """'AFLW & VFL & VFLW:' must not leave 'AFLW/' style fragments."""
    got = CF.ground("AFL: Marvel Stadium (56,347) & Melbourne Cricket Ground "
                    "(100,024) AFLW & VFL & VFLW: Ikon Park (12,000)")
    assert got.primary == "Marvel Stadium"
    assert got.extras == ["Melbourne Cricket Ground", "Ikon Park"]


# ---------------------------------------------------------- premierships

def test_premierships_reduces_to_a_count_and_a_competition():
    got = CF.premierships("VFL/AFL (13) 1920 1921 1932 1934 1943 1967")
    assert got.primary == "13"
    assert got.qualifier == "VFL/AFL"


def test_premierships_keeps_other_competitions_as_extras():
    got = CF.premierships("AFL (0) NEAFL (1) 2016")
    assert got.primary == "0"
    assert got.qualifier == "AFL"
    assert got.extras == ["NEAFL 1"]


def test_a_bare_count_survives():
    assert CF.premierships("0").primary == "0"


# ------------------------------------------------------------- behaviour

def test_missing_field_returns_a_falsey_empty():
    got = CF.headline({}, "nickname")
    assert not got
    assert got.primary == ""


def test_unimprovable_value_is_returned_unchanged_not_mangled():
    got = CF.nickname("Something Unexpected")
    assert got.primary in ("Something", "Something Unexpected")
    assert got.raw == "Something Unexpected"


# ============================================================= live data

def test_live_every_club_gets_all_four_headlines():
    """The reported bug, as an assertion over the real database."""
    from pathlib import Path

    from data_paths import default_db
    db = default_db("afl")
    if not Path(db).exists():
        pytest.skip("no built database")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='club_wikipedia_fields'").fetchone():
        pytest.skip("club sources not loaded")

    missing = []
    for club_id, name in con.execute("SELECT club_id, name FROM clubs"):
        fields = dict(con.execute(
            "SELECT field_key, field_value FROM club_wikipedia_fields "
            "WHERE club_id=?", (club_id,)))
        for group in ("nickname", "founded", "ground", "premierships"):
            value = CF.headline(fields, group)
            if not value.primary:
                missing.append(f"{name}: {group}")
            # A headline that still needs truncating has not done its job.
            elif len(value.primary) > 32:
                missing.append(f"{name}: {group} too long ({value.primary!r})")
    assert not missing, missing


def test_live_known_nicknames_are_right():
    from pathlib import Path

    from data_paths import default_db
    db = default_db("afl")
    if not Path(db).exists():
        pytest.skip("no built database")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

    expected = {
        "adelaide": "Crows", "west_coast": "Eagles", "richmond": "Tigers",
        "melbourne": "Demons", "port_adelaide": "Power", "carlton": "Blues",
        "sydney": "Swans", "gws": "Giants", "north_melbourne": "Kangaroos",
        "western_bulldogs": "Bulldogs", "st_kilda": "Saints",
        "collingwood": "Magpies", "hawthorn": "Hawks", "geelong": "Cats",
    }
    for club_id, want in expected.items():
        fields = dict(con.execute(
            "SELECT field_key, field_value FROM club_wikipedia_fields "
            "WHERE club_id=?", (club_id,)))
        got = CF.headline(fields, "nickname").primary
        assert got == want, f"{club_id}: got {got!r}, want {want!r}"


def run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:
                if exc.__class__.__name__ == "Skipped":
                    continue
                raise
    print("club fields tests: passed")


if __name__ == "__main__":
    run()
