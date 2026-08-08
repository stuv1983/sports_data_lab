"""A criterion must reach the rule that owns it.

Every case here is a square that a *later* addition to parse_criteria stole
from an earlier rule. The parser is one long ordered chain, so a new rule
inserted at the wrong height does not fail loudly: it answers a different
question from the one the square asked, and the board still looks playable.
"""

import pytest

import afl.constraints as C
import afl.parse_criteria as P


def label(text):
    built, reason = P.parse(text)
    assert built is not None, f"{text!r} was declined: {reason}"
    return reason


def declined(text):
    built, reason = P.parse(text)
    assert built is None, f"{text!r} was answered as {reason!r}"
    return reason


# ------------------------------------------------------------- venues

@pytest.mark.parametrize("alias", sorted(C.VENUE_ALIASES))
def test_a_ground_is_never_read_as_a_players_name(alias):
    """The implicit-teammate rule ran before the venue block.

    Ten of the twenty-five grounds -- ADELAIDE OVAL, KARDINIA PARK,
    VICTORIA PARK, WINDY HILL among them -- are two plain words carrying no
    statistic, so they became teammate searches for players who do not
    exist. Those answer nobody and cost a full games sweep each.
    """
    built, reason = P.parse(alias.upper())
    if built is None:          # "M.C.G." has never parsed; not this test's job
        return
    assert "teammate" not in reason, (alias, reason)


def test_a_ground_still_resolves_to_the_ground():
    assert label("ADELAIDE OVAL") == "played at adelaide oval"
    assert label("KARDINIA PARK") == "played at kardinia park"
    assert label("MCG WON A FINAL") == "won a final at mcg"


# --------------------------------------------------- implicit game scope

def test_a_scopeless_stat_total_is_read_as_one_game():
    assert label("20+ KICKS") == "20+ kicks in a game"
    assert label("30+ MARKS") == "30+ marks in a game"


def test_the_scopeless_reading_does_not_outrank_the_rules_below_it():
    """Returning it inline claimed squares that later rules own."""
    assert label("30+ GOALS TWO DIFF CLUBS") == "30+ goals at 2 clubs"
    assert label("50+ GAMES TWO DIFF CLUBS") == "50+ games at 2 clubs"
    assert label("30+ DISPOSALS & 3+ GOALS GAME") == "30+ disposals & 3+ goals"
    assert label("100+ POINT WIN") == "won by 100+ points"


def test_the_scopeless_reading_needs_the_whole_stat_word():
    """STAT_WORDS matches by substring, which is too weak on its own.

    "GOALKICKER" contains "kick", and with no scope word to confirm the
    reading the square was answered as "10+ kicks in a game".
    """
    declined("TOP 10 GOALKICKER")


# --------------------------------------------------------------- draft

def test_top_n_is_a_draft_pick_when_it_stands_alone():
    """Gridley writes the bare form: grid 1117's third row is "TOP 10"."""
    assert label("TOP 10") == "top 10 draft pick"
    assert label("TOP 10 DRAFT PICK") == "top 10 draft pick"
    assert label("TOP 5 PICK") == "top 5 draft pick"


def test_top_n_qualified_by_something_else_is_not_a_draft_pick():
    assert label("TOP 10 BROWNLOW FINISH") == "top-10 Brownlow finish"
    declined("TOP 10 GOALKICKER")


# -------------------------------------------------------------- derbies

def test_a_derby_criterion_names_its_derby():
    """C.derby_winning_record comes from match_constraints and takes the
    derby. Calling it bare raised TypeError out of parse()."""
    assert label("WESTERN DERBY WINNING RECORD") == "Western Derby winning record"
    assert label("SHOWDOWN WINNING RECORD") == "Showdown winning record"
    assert label("QCLASH 10+ GAMES") == "10+ QClash games"
    assert label("SYDNEY DERBY") == "played in a Sydney Derby"


def test_an_unnamed_derby_declines_instead_of_raising():
    assert "which derby" in declined("DERBY WINNING RECORD")


def test_a_plain_winning_record_is_still_a_career_one():
    assert label("WINNING RECORD") == "winning record"


# ------------------------------------------------------------ physicals

def test_height_and_weight_squares():
    assert label("195+ CM") == "195+ cm tall"
    assert label("UNDER 180 CM") == "179 cm or shorter"
    assert label("100+ KG") == "100+ kg heavy"


# --------------------------------------------------- teammates and reasons

def test_a_bare_name_is_still_a_teammate_square():
    assert label("COLBY MCKERCHER") == "Colby McKercher teammate"
    assert label("NICK RIEWOLDT") == "Nick Riewoldt teammate"


def test_criterion_vocabulary_is_never_taken_for_a_name():
    """A teammate search is the most expensive thing this module asks for."""
    for text in ("LEFT FOOTED", "CLUB CAPTAIN", "WOODEN SPOON",
                 "ONE CLUB PLAYER", "PREMIERSHIP PLAYER"):
        built, reason = P.parse(text)
        assert built is None or "teammate" not in reason, (text, reason)


def test_a_decline_keeps_the_reason_it_was_given():
    """The fuzzy wrapper replaced every decline with "couldn't interpret",
    throwing away the UNSUPPORTED text historic_grids shows the player."""
    assert declined("JUMPER NUMBER 9") == "jumper numbers aren't stored"
    assert "which derby" in declined("DERBY WINNING RECORD")


def run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if hasattr(fn, "pytestmark"):
                continue
            fn()
    print("criterion precedence tests: passed")


if __name__ == "__main__":
    run()
