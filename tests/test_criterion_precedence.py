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


# ---------------------------------------------------------- rising star

def test_a_bare_rising_star_axis_is_the_nomination_not_the_award():
    """Gridley words the nomination square as just "RISING STAR" (board
    #1117) and the award as "RISING STAR WINNER"; the bare phrase must
    reach the nomination rule, and the winner wording must stay with the
    awards block above it."""
    assert label("RISING STAR") == "Rising Star nominee"
    assert label("RISING STAR NOMINATION") == "Rising Star nominee"
    assert label("RISING STAR WINNER") == "Rising Star winner"


# ------------------------------------------------- venue tenure counts

def test_a_games_count_at_a_ground_is_tenure_not_attendance():
    """"100+ GAMES AT THE MCG" used to fall through to the played-at rule
    and answer "ever appeared there" — wrong by a hundred games."""
    assert label("100+ GAMES AT THE MCG") == "100+ games at mcg"
    assert label("50+ VFL/AFL GAMES AT KARDINIA PARK") == \
        "50+ games at kardinia park"
    # The bare forms keep their old readings.
    assert label("MCG") == "played at mcg"
    assert label("MCG WON A FINAL") == "won a final at mcg"


def test_league_name_noise_is_stripped():
    assert label("150+ VFL/AFL GAMES") == "150+ games played"


# ----------------------------------------------------- finals stat scope

def test_plural_finals_is_the_career_total_singular_is_one_game():
    """"KICKED 30+ GOALS IN FINALS" is a finals-career total; reading the
    plural as a single game answered it with a bar nobody has cleared in
    one afternoon. "IN A FINAL" stays a single-game feat, and "KICKED" is
    the scoring verb, never the kicks statistic."""
    assert label("KICKED 30+ GOALS IN FINALS") == "30+ goals in finals (career)"
    assert label("30+ FINALS GOALS") == "30+ goals in finals (career)"
    assert label("5+ GOALS IN A FINAL") == "5+ goals in a final"
    assert label("20+ KICKS") == "20+ kicks in a game"


def test_a_stat_in_a_grand_final_is_a_feat_not_participation():
    """"3+ GOALS IN A GRAND FINAL" was swallowed by the bare participation
    rule; the participation and premiership readings must survive."""
    assert label("3+ GOALS IN A GRAND FINAL") == "3+ goals in a grand final"
    assert label("PLAYED A GRAND FINAL") == "played a grand final"
    assert label("WON A GRAND FINAL") == "premiership player"
    assert label("2+ GRAND FINALS") == "played in 2+ grand finals"


# ---------------------------------------------------------------- decades

def test_a_decade_reads_as_its_ten_seasons():
    assert label("PLAYED IN 2010s") == "played in the 2010s"
    assert label("PLAYED IN THE 1990s") == "played in the 1990s"


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


# ------------------------------------------- wording seen on real boards

def test_gridley_1119_wording_parses_as_the_board_states_it():
    """Boards past the captured library have to be typed in. These are the
    six axes of #1119 exactly as Gridley words them."""
    assert label("195cm OR TALLER") == "195+ cm tall"
    assert label("50+ GAMES TWO DIFF CLUBS") == "50+ games at 2 clubs"
    assert label("LUKE JACKSON TEAMMATE") == "Luke Jackson teammate"
    assert label("GRAND FINAL PLAYER") == "played a grand final"


def test_gridley_1118_wording_parses_as_the_board_states_it():
    """"3+ GRAND FINALS" must read 3, not 4 -- an off-by-one here silently
    answers a harder square than the board asked."""
    assert label("3+ GRAND FINALS") == "played in 3+ grand finals"
    assert label("TOP 10 BROWNLOW FINISH") == "top-10 Brownlow finish"
    assert label("HAWTHORN FIRST CAREER GAME") == "Hawthorn first career game"
    assert label("ALL-AUSTRALIAN SQUAD") == "All-Australian squad"


def test_a_grand_final_count_keeps_the_number_it_was_given():
    for n in (1, 2, 3, 4, 5):
        assert label(f"{n}+ GRAND FINALS") == f"played in {n}+ grand finals"


# ------------------------------ the minor premiership is not the premiership

def test_a_minor_premiership_is_never_answered_with_the_flag():
    """Finishing top of the home-and-away ladder and winning the grand final
    are different achievements, and in half of all seasons different clubs:
    they have coincided in 66 of 127. The bare "premiership" rule matched
    "MINOR PREMIERSHIP WINNER" first and answered every square about the
    ladder with the flag instead."""
    assert label("MINOR PREMIERSHIP WINNER") == "minor premiership"
    assert label("MINOR PREMIERSHIP") == "minor premiership"
    assert label("TOP OF THE LADDER") == "minor premiership"


def test_never_winning_a_minor_premiership_is_not_its_own_opposite():
    """This read as "premiership player" -- the precise inverse of the
    square, admitting only the players it was meant to exclude."""
    assert label("NEVER WON A MINOR PREMIERSHIP") == "no minor premierships"
    assert label("NO MINOR PREMIERSHIPS") == "no minor premierships"


def test_a_minor_premiership_count_is_counted():
    assert label("3+ MINOR PREMIERSHIPS") == "3+ minor premierships"


def test_the_flag_still_wins_a_plain_premiership_square():
    """The guard above must not swallow the criterion it was inserted in
    front of."""
    assert label("PREMIERSHIP PLAYER") == "premiership player"
    assert label("PREMIERSHIP WINNER") == "premiership player"


# ------------------------------------- winning and losing a grand final

def test_losing_grand_finals_is_not_playing_in_them():
    """"LOST 2+ GRAND FINALS" was answered by the count of grand finals
    *played*, which admits every dual premiership player -- the exact
    players the square is asking to keep out."""
    assert label("LOST 2+ GRAND FINALS") == "lost 2+ grand finals"
    assert label("LOST 3+ GRAND FINALS") == "lost 3+ grand finals"


def test_winning_grand_finals_is_not_playing_in_them_either():
    assert label("WON 2+ GRAND FINALS") == "won 2+ premierships"


def test_an_uncounted_grand_final_square_still_counts_appearances():
    """The wording with no verb keeps its old meaning."""
    assert label("3+ GRAND FINALS") == "played in 3+ grand finals"


def test_a_premiership_count_written_without_grand_finals_is_counted():
    """"2+ PREMIERSHIPS" fell through to the bare rule and answered with
    every player who had won one."""
    assert label("2+ PREMIERSHIPS") == "won 2+ premierships"
    assert label("4+ FLAGS") == "won 4+ premierships"


# --------------------------------------------- the club best-and-fairest

@pytest.mark.parametrize("text", ["BEST & FAIREST", "BEST AND FAIREST",
                                  "B&F", "B & F", "BEST 'N' FAIREST"])
def test_every_spelling_of_the_club_award_reaches_the_award_rule(text):
    """Gridley writes it with the ampersand, which nothing matched.

    The rule read "best and fairest" and "b&f" only, so the one spelling
    the site actually publishes fell past every rule in the chain and was
    declined as uninterpretable -- on a board whose other five criteria
    parsed, which is what made the whole grid unplayable in Authentic mode.
    """
    assert label(text) == "club best and fairest"


def test_a_repeat_winners_square_says_how_many_it_asked_for():
    """The count reached the builder but never the axis label."""
    assert label("2X BEST & FAIREST") == "2x club best and fairest"


def test_the_ampersand_spelling_still_yields_to_the_narrower_rules():
    """Broadening the wording must not cost the club and multi-club forms."""
    assert label("CARLTON BEST & FAIREST") == "Carlton best and fairest"
    assert label("BEST & FAIREST 2+ CLUBS") == "B&F at 2+ clubs"
    assert label("B&F AT 2 DIFFERENT CLUBS") == "B&F at 2+ clubs"
