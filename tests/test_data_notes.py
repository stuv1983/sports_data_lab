"""The caveats a reader needs, shown where the number they doubt is shown.

The round-numbering note is the one that matters and the one that has
already caused a mistake: from 2024 the AFL's fixture opens with a Round 0
that this database does not count, so every round here is one higher than
the AFL's own. A season card, a round card and the home page each show a
different slice of these notes, and the slicing is what is tested -- a note
attached to the wrong seasons is worse than no note, because it is read as
applying to the season on screen.
"""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---

import pytest

import sports
from afl import data_notes as D


# ------------------------------------------------------ round numbering

@pytest.mark.parametrize("season, ours, theirs", [
    (2026, "23", "Round 22"),
    (2026, "R23", "Round 22"),
    (2025, "25", "Round 24"),
    (2024, "2", "Round 1"),
    # Our Round 1 is their Opening Round, which is worth naming: "Round 0"
    # is what the fixture calls it and not what anyone says.
    (2026, "1", "Opening Round"),
])
def test_a_round_is_translated_into_the_afls_own_number(season, ours, theirs):
    assert D.official_round(ours, season) == theirs


@pytest.mark.parametrize("season, round_value", [
    (2023, "23"),   # before the Opening Round existed, the two agree
    (2019, "1"),
    (2026, "GF"),   # a grand final is a grand final under either fixture
    (2026, "PF"),
    (2026, ""),
])
def test_a_round_the_two_fixtures_agree_on_is_not_translated(season,
                                                             round_value):
    assert D.official_round(round_value, season) is None


def test_the_round_note_starts_in_2024_and_does_not_apply_before_it():
    assert D.round_numbering_note(2024) is not None
    assert D.round_numbering_note(2026) is not None
    assert D.round_numbering_note(2023) is None
    assert D.round_numbering_note(1897) is None


def test_the_round_note_says_which_way_the_numbers_differ():
    """A note that only says 'they differ' leaves the reader where it found
    them; it has to say which is higher and by how many rounds."""
    text = D.ROUND_NUMBERING.text
    assert "Round 1" in text and "one higher" in text
    assert "25" in text and "24" in text


# ------------------------------------------------------ season slicing

@pytest.mark.parametrize("season, topics", [
    (1909, {"Naming", "Results"}),      # VFL era, and the forfeited match
    (1987, {"Rounds", "Naming"}),       # the three-week rounds
    (1992, {"Ladders"}),                # match-ratio ordering
    (1996, {"Results"}),                # the Waverley lights
    (2013, {"Ladders"}),                # Essendon relegated
    (2015, {"Ladders"}),                # Adelaide v Geelong cancelled
    (2026, {"Rounds"}),                 # the Opening Round offset
])
def test_a_season_gets_the_notes_that_are_about_it(season, topics):
    assert {note.topic for note in D.for_season(season)} == topics


def test_a_season_nothing_happened_in_gets_nothing_but_its_era(season=1975):
    assert [note.topic for note in D.for_season(season)] == ["Naming"]


def test_a_note_about_no_particular_season_is_never_attached_to_one():
    """'Some tables cut off at ten' is true always, so pinning it to a
    season card would push the notes that are about that season down."""
    scope = [note for note in D.NOTES if note.topic == "Scope"]
    assert scope, "the scope note has gone missing"
    for note in scope:
        assert not note.covers(2015)
        assert not note.covers(1900)
    # It is still in the full list, which is where it belongs.
    assert "Scope" in D.by_topic()


def test_the_vfl_era_ends_in_1989():
    naming = next(note for note in D.NOTES if note.topic == "Naming")
    assert naming.covers(1989) and not naming.covers(1990)
    assert naming.seasons == "Until 1989"


@pytest.mark.parametrize("season", [1979, 1982, 1985])
def test_the_seasons_with_matches_before_round_one_each_say_so(season):
    assert any("before Round 1" in note.text
               for note in D.for_season(season))


# ----------------------------------------------------------- labelling

@pytest.mark.parametrize("first, last, expected", [
    (2024, None, "2024 and beyond"),
    (None, 1989, "Until 1989"),
    (1991, 1994, "1991–1994"),
    (1987, 1987, "1987"),
    (None, None, ""),
])
def test_a_notes_seasons_read_as_a_person_would_write_them(first, last,
                                                           expected):
    assert D.Note("Rounds", "x", first, last).seasons == expected


def test_every_note_is_grouped_under_a_topic_the_full_list_shows():
    grouped = D.by_topic()
    assert sum(len(items) for items in grouped.values()) == len(D.NOTES)
    # Rounds first: the numbering note is the one people come looking for.
    assert list(grouped)[0] == "Rounds"


def test_a_bad_season_is_not_an_error():
    """Season arrives from a table cell and can be None or text."""
    for value in (None, "", "not a season", float("nan")):
        assert D.for_season(value) == []
        assert D.round_numbering_note(value) is None


# ------------------------------------------------------------ wiring up

def test_the_afl_declares_its_notes_and_the_others_have_none():
    """Only the AFL inherits these caveats, and asking is always safe."""
    assert sports.get("afl").notes() is D
    for key in ("nba", "nfl", "mlb"):
        assert sports.get(key).notes() is None
