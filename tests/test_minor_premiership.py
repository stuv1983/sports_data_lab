"""The minor premiership: top of the home-and-away ladder.

A different achievement from the premiership, and in half of all seasons a
different club -- the two have gone together in 66 of 127 grand finals. The
constraint therefore cannot be derived from grand-final results, and these
tests pin it to the ladder instead.
"""

import sqlite3

import pytest

import afl.constraints as C
import sports


@pytest.fixture(scope="module")
def con():
    if not sports.AFL.exists():
        pytest.skip("no built AFL database")
    connection = sqlite3.connect(sports.AFL.db)
    yield connection
    connection.close()


def ids(con, built):
    sql, args = built
    return {row[0] for row in con.execute(sql, args)}


def test_the_ladder_leader_is_the_club_the_real_ladder_names(con):
    """`ladder_rank` is a mean of two ranks rather than the AFL's own rule of
    points and then percentage, so it is worth pinning that the two agree --
    including 2020, decided on percentage with both clubs on 56 points."""
    known = {
        2000: "Essendon", 2016: "Sydney", 2017: "Adelaide",
        2018: "Richmond", 2019: "Geelong", 2020: "Port Adelaide",
        2021: "Melbourne", 2022: "Geelong", 2023: "Collingwood",
    }
    for season, club in known.items():
        row = con.execute(
            "SELECT club_now FROM team_seasons "
            "WHERE season = ? AND ladder_rank = 1", (season,)).fetchone()
        assert row and row[0] == club, f"{season}: got {row}"


def test_exactly_one_club_tops_the_ladder_in_every_season(con):
    """A tie would make two clubs minor premiers, and the rank is built from
    a mean of two ranks, which can tie where the real rule does not."""
    ties = con.execute(
        "SELECT season, COUNT(*) FROM team_seasons WHERE ladder_rank = 1 "
        "GROUP BY season HAVING COUNT(*) <> 1").fetchall()
    assert not ties, f"seasons without a single ladder leader: {ties}"


def test_the_ladder_is_read_from_home_and_away_games_only(con):
    """A minor premiership is decided before the finals start. If finals
    results reached the ladder, a club could win it in September."""
    finals = con.execute(
        "SELECT COUNT(*) FROM games WHERE is_final = 1").fetchone()[0]
    assert finals, "no finals in the database to distinguish"
    played = con.execute(
        "SELECT MAX(played) FROM team_seasons").fetchone()[0]
    longest = con.execute(
        "SELECT MAX(c) FROM (SELECT COUNT(DISTINCT date) c FROM games "
        "WHERE is_final = 0 GROUP BY season, club_now)").fetchone()[0]
    assert played == longest


def test_every_player_either_has_a_minor_premiership_or_has_not(con):
    """The two constraints must partition the whole table. A player whose
    entire career was finals football has still never won one, and an
    inner-join phrasing would have left them in neither."""
    have = ids(con, C.minor_premiership_player())
    have_not = ids(con, C.no_minor_premierships())
    everyone = {r[0] for r in con.execute("SELECT player_id FROM players")}

    assert not (have & have_not)
    assert have | have_not == everyone


def test_a_minor_premiership_is_not_the_same_set_as_a_premiership(con):
    """If these ever coincided, one of them would be reading the wrong
    column -- which is the bug this whole constraint exists to fix."""
    minor = ids(con, C.minor_premiership_player())
    flag = ids(con, C.premiership_player())
    assert minor - flag, "no minor premier ever missed the flag"
    assert flag - minor, "no premiership was ever won from off the top"


def test_a_ladder_leading_season_counts_for_everyone_who_played_it(con):
    """Geelong topped the ladder in 2019 and did not reach the grand final,
    so their players are minor premiers on that season alone."""
    minor = ids(con, C.minor_premiership_player())
    squad = {r[0] for r in con.execute(
        "SELECT DISTINCT player_id FROM games "
        "WHERE season = 2019 AND club_now = 'Geelong'")}
    assert squad and squad <= minor


def test_a_count_is_by_season_not_by_game(con):
    """Playing twenty-two games for a ladder-leading club is one minor
    premiership, not twenty-two."""
    once = ids(con, C.minor_premierships_min(1))
    assert once == ids(con, C.minor_premiership_player())

    for count in (2, 3, 5):
        assert ids(con, C.minor_premierships_min(count)) <= once
    assert (ids(con, C.minor_premierships_min(5))
            < ids(con, C.minor_premierships_min(2)))


def test_the_count_matches_a_hand_counted_career(con):
    """Careers counted straight from the ladder, against the builder: the
    threshold a player clears must be exactly the number of ladder-leading
    seasons they played in, and one more must shut them out."""
    careers = con.execute("""
        SELECT g.player_id, COUNT(DISTINCT g.season) FROM games g
        JOIN team_seasons t
          ON t.season = g.season AND t.club_now = g.club_now
        WHERE t.ladder_rank = 1
        GROUP BY g.player_id ORDER BY 2 DESC LIMIT 20""").fetchall()
    assert careers and careers[0][1] >= 3

    eligible = {}
    for pid, expected in careers:
        for count in (expected, expected + 1):
            if count not in eligible:
                eligible[count] = ids(con, C.minor_premierships_min(count))
        assert pid in eligible[expected]
        assert pid not in eligible[expected + 1]


# ------------------------------------- the invariant the speed-up rests on

@pytest.mark.parametrize(
    "sport", [sports.AFL, sports.NBA, sports.MLB, sports.NFL],
    ids=lambda s: s.key)
def test_round_and_result_codes_are_stored_normalised(sport):
    """`core._code` normalises the parameter instead of the column, so the
    index on (round, player_id) can be used -- 1.6 seconds a square down to
    nothing. That is only correct while the stored codes are already upper
    case and trimmed, so a build that starts writing ' gf ' or 'Final' has
    to fail here rather than quietly answer a grand-final square with
    nobody."""
    if not sport.exists():
        pytest.skip(f"no built {sport.key} database")
    connection = sqlite3.connect(sport.db)
    try:
        sc = sport.schema
        for column in (sc.round, sc.result):
            dirty = connection.execute(
                f"SELECT DISTINCT {column} FROM {sc.games} "
                f"WHERE {column} IS NOT NULL "
                f"AND {column} <> UPPER(TRIM({column})) LIMIT 5").fetchall()
            assert not dirty, (
                f"{sport.key}.{column} stores un-normalised codes: {dirty}")
    finally:
        connection.close()


def test_a_grand_final_criterion_still_finds_the_same_players(con):
    """The speed-up must not change any answer. Compared against the query
    it replaced, wrapped column and all."""
    for code, outcome in (("GF", "W"), ("GF", "L")):
        for times in (1, 2, 3):
            sql, args = sports.AFL.C._G.round_outcome_min(code, outcome, times)
            fast = {r[0] for r in con.execute(sql, args)}
            slow = {r[0] for r in con.execute(
                """SELECT player_id FROM games
                   WHERE UPPER(TRIM(round)) = UPPER(?)
                     AND UPPER(TRIM(result)) = UPPER(?)
                   GROUP BY player_id
                   HAVING COUNT(DISTINCT season) >= ?""",
                [code, outcome, times])}
            assert fast == slow, f"{code}/{outcome}/{times} changed"
