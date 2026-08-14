"""The complete Gridley question vocabulary, held as a regression fixture.

tests/data/gridley_vocabulary.json is every distinct criterion wording the
official archive at gridleygame.com has used -- 788 wordings across 1,123
daily boards (2023-07-17 through 2026-08-12), captured with the board id,
how often each appeared, and Gridley's own description of the rule.

Two promises are tested over the whole vocabulary:

 1. Nothing is uninterpretable. Every wording either compiles to a
    constraint or declines with a *named* reason ("club lists aren't in
    the data"), because "couldn't interpret" on a real board means a
    square the app shrugs at rather than explains.

 2. Wordings that were once silently misparsed stay fixed. Each entry in
    EXACT_READINGS is a criterion the parser used to answer with a
    different question -- "PLAYED AT MCG 100+ Times" as mere attendance,
    "MULTI-PREMIERSHIP PLAYER" as any single flag -- pinned to the label
    of the constraint it must produce now.
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
import pathlib
import sqlite3

import pytest

from afl import parse_criteria as P

FIXTURE = pathlib.Path(__file__).parent / "data" / "gridley_vocabulary.json"
VOCABULARY = json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_fixture_is_the_whole_archive():
    assert len(VOCABULARY) >= 780
    appearances = sum(entry["count"] for entry in VOCABULARY)
    assert appearances >= 6700, "six squares per board over ~1,120 boards"


def test_every_wording_parses_or_declines_by_name():
    """Promise 1: no wording from a real board is ever a shrug."""
    unnamed = []
    for entry in VOCABULARY:
        constraint, label = P.parse(entry["label"])
        if constraint is None and str(label).startswith("couldn't interpret"):
            unnamed.append((entry["count"], entry["label"]))
    unnamed.sort(reverse=True)
    assert not unnamed, unnamed[:10]


def test_at_least_ninety_seven_percent_of_squares_are_answerable():
    """Coverage measured in board squares, weighting each wording by how
    often Gridley actually asked it. The remainder is data the loaded
    layers genuinely lack (22Under22, club lists, coaches), each declined
    by name rather than guessed at."""
    total = answered = 0
    for entry in VOCABULARY:
        total += entry["count"]
        constraint, _label = P.parse(entry["label"])
        if constraint is not None:
            answered += entry["count"]
    assert answered / total >= 0.97, f"{answered}/{total}"


#: Wording -> the parsed label it must produce. Every entry here was once
#: answered as a different question; see the module docstring.
EXACT_READINGS = {
    # The venue-tenure square, in Gridley's own two-line wording. This was
    # answered as played_at_venue -- confidently wrong by a hundred games.
    "PLAYED AT MCG 100+ Times": "100+ games at mcg",
    "PLAYED AT MCG 50+ Times": "50+ games at mcg",
    "100+ GAMES AT THE MCG": "100+ games at mcg",
    # Flag counts: "multi" and "Nx" both mean more than one.
    "MULTI-PREMIERSHIP PLAYER": "won 2+ premierships",
    "3x PREMIERSHIP PLAYER": "won 3+ premierships",
    "4x PREMIERSHIP PLAYER": "won 4+ premierships",
    # Inclusive physical caps: the 180cm player is 180cm or shorter.
    "180cm OR SHORTER": "180 cm or shorter",
    # Losing a Grand Final is not the same as playing one.
    "LOST A GRAND FINAL": "lost a grand final",
    # Counting finals wins is not the same as winning one.
    "5+ FINALS WINS": "5+ finals wins",
    "10+ FINALS WINS": "10+ finals wins",
    # Round-specific and club-spread finals questions.
    "PRELIM FINAL PLAYER": "played a preliminary final",
    "2+ PRELIM FINALS": "played 2+ preliminary finals",
    "GRAND FINAL FOR TWO CLUBS": "grand final for 2+ clubs",
    "FINALS PLAYER MULTIPLE CLUBS": "played finals for 2+ clubs",
    # Era-scoped honours must keep their era.
    "GRAND FINAL PLAYER DURING 2020s": "grand final 2020-2029",
    "PREMIERSHIP PLAYER 2010 TO 2019": "premiership 2010-2019",
    "ALL AUSTRALIAN DURING 2010s": "All-Australian 2010-2019",
    "DEBUT GAME 2010 TO 2019": "debuted 2010-2019",
    "DEBUT GAME 2020 ONWARDS": "debuted 2020 onwards",
    # Team-leading tallies.
    "MOST DISPOSALS TEAM": "led club in disposals in a season",
    "MOST BROWNLOW VOTES TEAM": "led club in brownlow in a season",
    "2x LEADING GOALKICKER TEAM": "2x club leading goalkicker",
    # A surname that contains an award name is still a teammate square.
    "KEIDEAN COLEMAN TEAMMATE": "Keidean Coleman teammate",
    "NICK LARKEY TEAMMATE": "Nick Larkey teammate",
    # The glued two-line display repeats the surname.
    "MAX GAWN GAWN TEAMMATE": "Max Gawn teammate",
    "ADAM SIMPSON TEAMMATE OF SIMPSON": "Adam Simpson teammate",
    # Loyalty, workload and streak counts.
    "200+ GAMES SAME CLUB": "200+ games at one club",
    "20+ GAMES IN 2023": "20+ games in 2023",
    "15 LOSSES SINGLE SEASON": "15+ losses in a season",
    "10 WINS IN A ROW": "10 wins in a row",
    "100+ TEAMMATES CAREER": "100+ career teammates",
    # Derby and marquee shapes beyond mere participation.
    "SHOWDOWN WINNER": "won a Showdown",
    "SHOWDOWN KICKED A GOAL": "1+ goals in a Showdown",
    "SYDNEY DERBY 5+ TACKLES": "5+ tackles in a Sydney Derby",
    "WESTERN DERBY PLAYED IN 10+": "10+ Western Derby games",
    "ANZAC DAY MATCH WINNER": "won an Anzac Day match",
    "ANZAC DAY MATCH PLAYED IN": "played an Anzac Day match",
    "BIG FREEZE MATCH PLAYED IN":
        "played a Big Freeze match (King's Birthday, 2015 on)",
    # Ground feats and the China games.
    "MARVEL STADIUM KICKED A GOAL": "1+ goals in a game at marvel stadium",
    "NINJA STADIUM KICKED A GOAL": "1+ goals in a game at ninja stadium",
    "PLAYED IN CHINA": "played at Jiangwan Stadium (China)",
    # Odd-shaped one-offs that are still real data questions.
    "MORE FREES FOR THAN AGAINST": "more career frees_for than frees_against",
    "WON BROWNLOW WITH 25+ VOTES": "won the Brownlow with 25+ votes",
    "DUSTIN MARTIN DEFEATED BY DUSTY IN A GF":
        "lost a grand final to Dustin Martin",
    "HALL OF FAME PLAYER": "Hall of Fame player",
    "TRADED 1+ TIMES": "traded at least once",
    "ROOKIE DRAFT PICK": "Rookie draft selection",
    "ALL AUSTRALIAN FORWARD": "All-Australian forward",
}


@pytest.mark.parametrize("wording,expected",
                         sorted(EXACT_READINGS.items()))
def test_a_once_misparsed_wording_reads_correctly(wording, expected):
    constraint, label = P.parse(wording)
    assert constraint is not None, f"{wording!r} declined: {label}"
    assert label == expected


def test_named_declines_stay_named():
    """The honest refusals: wordings whose data no loaded layer holds must
    decline with a reason a board can print, not parse to something else."""
    for wording, reason_fragment in {
        "22 UNDER 22 SELECTION": "22Under22",
        "2024 LISTED PLAYER": "club lists",
        "MARK OF THE YEAR": "Mark and Goal of the Year",
        "PREMIERSHIP COACH": "coaching records",
        "GAME WINNING KICK AFTER SIREN": "timing",
        "WORN #9 GUERNSEY": "jumper numbers",
    }.items():
        constraint, label = P.parse(wording)
        assert constraint is None, f"{wording!r} unexpectedly parsed"
        assert reason_fragment.lower() in str(label).lower(), (wording, label)


# ============================================================ live data

def _live():
    from pathlib import Path

    from data_paths import default_db
    db = default_db("afl")
    if not Path(db).exists():
        return None
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def test_live_every_parsed_wording_compiles_against_the_database():
    """EXPLAIN every constraint the whole vocabulary compiles to, so a
    builder whose SQL names a missing column cannot hide behind a passing
    parse. EXPLAIN, not COUNT: counting 763 constraints is minutes of
    scans and proves nothing more about the SQL's validity."""
    con = _live()
    if con is None:
        pytest.skip("no built database")
    from afl import constraints as C
    C.require_schema(con)
    bad = []
    for entry in VOCABULARY:
        parsed, label = P.parse(entry["label"])
        if parsed is None:
            continue
        sql, params = parsed
        try:
            con.execute(f"EXPLAIN SELECT 1 FROM players p "
                        f"WHERE p.player_id IN ({sql})", params)
        except sqlite3.Error as exc:
            bad.append((entry["label"], str(exc)))
    assert not bad, bad[:5]


def test_live_the_mcg_hundred_game_square_has_its_known_answers():
    """The square that motivated the venue-count fix, pinned to the live
    build: 100+ games at the MCG is a tenure question with ~150 answers,
    not the ~10,000 who ever appeared there."""
    con = _live()
    if con is None:
        pytest.skip("no built database")
    from afl import constraints as C
    C.require_schema(con)
    tenure, _ = P.parse("PLAYED AT MCG 100+ Times")
    played, _ = P.parse("MARVEL STADIUM PLAYED AT")
    n_tenure = C.count(con, [tenure])
    n_played = C.count(con, [played])
    assert 50 <= n_tenure <= 400, n_tenure
    assert n_played > 2000, "played-at stays the broad question"
