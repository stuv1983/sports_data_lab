#!/usr/bin/env python3
"""Regression tests for the safe Advanced Search compiler."""

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

import core
import query_filters as Q
from afl import search_tokens


def _afl():
    """Fresh AFL token extensions, as sport.search_extensions() supplies.

    The captaincy and draft tokens are AFL extensions now, not built-in
    fields, so the tests that exercise them pass the extension list the
    same way the search page does.
    """
    return search_tokens.extensions()


def fixture():
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE players (
          player_id INTEGER, player TEXT, debut_season INTEGER,
          final_season INTEGER, career_games INTEGER, career_goals INTEGER,
          finals_played INTEGER, clubs_hist TEXT, obscurity REAL
        );
        CREATE TABLE games (
          player_id INTEGER, player TEXT, season INTEGER, round TEXT,
          opponent TEXT, club_now TEXT, club_hist TEXT, venue TEXT,
          career_game_no INTEGER, goals REAL, disposals REAL,
          is_final INTEGER, result TEXT
        );
        CREATE TABLE captaincies (
          player_id INTEGER, season INTEGER, club TEXT, match_status TEXT
        );
        INSERT INTO players VALUES
          (1,'Alpha One',1990,2000,200,300,10,'A|B',80),
          (2,'Beta Two',2001,2005,50,20,0,'B',40);
        INSERT INTO games VALUES
          (1,'Alpha One',1995,'1','B','A','A','MCG',1,3,31,0,'W'),
          (1,'Alpha One',1996,'2','C','B','B','MCG',2,2,20,1,'L'),
          (2,'Beta Two',2002,'1','A','B','B','SCG',1,1,10,0,'W');
        INSERT INTO captaincies VALUES (1,1996,'B','unique');
    """)
    schema = core.Schema(career_score="career_goals", career_postseason="finals_played", game_score="goals",

        stats=("goals", "disposals"), clubs=("A", "B"),
        required_games_cols=(), required_player_cols=(),
    )
    return con, schema


def test_base_compiler_end_to_end():
    """The end-to-end checks that used to hide inside a bare run()
    helper pytest never collected -- a green suite said nothing about
    them."""
    con, schema = fixture()
    sql, params, _ = Q.compile_query(
        schema,
        'club:A club:B game.disposals>=30 game.goals>=3 sort:obscurity',
        con=con,
    )
    rows = con.execute(sql, params).fetchall()
    assert [row[0] for row in rows] == ["Alpha One"]

    sql, params, _ = Q.compile_query(
        schema, 'captain:true captain_club:B captain_year:1995..1997',
        con=con, extensions=_afl(),
    )
    assert con.execute(sql, params).fetchone()[0] == "Alpha One"

    query = Q.query_from_params({
        "club": ["A", "B"], "games_min": ["100"],
        "game_disposals_min": ["30"],
    })
    assert query.count("club:") == 2
    assert "games>=100" in query
    assert "game.disposals>=30" in query

    with pytest.raises(Q.QuerySyntaxError):
        Q.compile_query(schema, "game.hacks>=1", con=con)


@pytest.mark.parametrize("query", ["limit:1.5", "limit:nan", "games>=inf"])
def test_non_integral_or_non_finite_numbers_are_rejected(query):
    con, schema = fixture()
    with pytest.raises(Q.QuerySyntaxError):
        Q.compile_query(schema, query, con=con)


# ------------------------------------------------------------- physicals
#
# Height and weight are not on `players` in every sport, and the table they
# are on differs. Compiling them against dg_people -- a name/URL index with
# no height_cm, weight_kg or player_id column at all -- raised "no such
# column: height_cm" on every AFL search that used them.

_PHYSICALS_SCHEMA = """
    CREATE TABLE games (
      player_id INTEGER, player TEXT, season INTEGER, round TEXT,
      opponent TEXT, club_now TEXT, club_hist TEXT, venue TEXT,
      career_game_no INTEGER, goals REAL, disposals REAL,
      is_final INTEGER, result TEXT
    );
"""


def _physicals_schema():
    return core.Schema(career_score="career_goals", career_postseason="finals_played", game_score="goals",

        stats=("goals", "disposals"), clubs=("A", "B"),
        required_games_cols=(), required_player_cols=(),
    )


def _register_fixture():
    """A sport that keeps physicals on club_player_register, as the AFL does.

    One row per club the player registered at, so player 1 appears twice.
    """
    con = sqlite3.connect(":memory:")
    con.executescript(_PHYSICALS_SCHEMA + """
        CREATE TABLE players (
          player_id INTEGER, player TEXT, debut_season INTEGER,
          final_season INTEGER, career_games INTEGER, career_goals INTEGER,
          finals_played INTEGER, clubs_hist TEXT, obscurity REAL
        );
        CREATE TABLE club_player_register (
          player_id INTEGER, club_id INTEGER,
          height_cm INTEGER, weight_kg INTEGER
        );
        INSERT INTO players VALUES
          (1,'Tall One',1990,2000,200,300,10,'A|B',80),
          (2,'Short Two',2001,2005,50,20,0,'B',40);
        INSERT INTO club_player_register VALUES
          (1, 1, 198, 101), (1, 2, 198, 101), (2, 2, 178, 76);
    """)
    return con, _physicals_schema()


def _players_column_fixture():
    """A sport that keeps physicals on `players`, as the NBA build does."""
    con = sqlite3.connect(":memory:")
    con.executescript(_PHYSICALS_SCHEMA + """
        CREATE TABLE players (
          player_id INTEGER, player TEXT, debut_season INTEGER,
          final_season INTEGER, career_games INTEGER, career_goals INTEGER,
          finals_played INTEGER, clubs_hist TEXT, obscurity REAL,
          height_cm INTEGER, weight_kg INTEGER
        );
        INSERT INTO players VALUES
          (1,'Tall One',1990,2000,200,300,10,'A',80,198,101),
          (2,'Short Two',2001,2005,50,20,0,'B',40,178,76);
    """)
    return con, _physicals_schema()


@pytest.mark.parametrize("build", [_register_fixture, _players_column_fixture])
@pytest.mark.parametrize("query,expected", [
    ("height>=195", ["Tall One"]),
    ("height<=180", ["Short Two"]),
    ("weight>=100", ["Tall One"]),
    ("weight<=80", ["Short Two"]),
    ("height>=195 weight>=100", ["Tall One"]),
])
def test_height_and_weight_compile_wherever_the_sport_keeps_them(
        build, query, expected):
    con, schema = build()
    sql, params, _ = Q.compile_query(schema, query, con=con)
    assert [row[0] for row in con.execute(sql, params)] == expected


def test_a_sport_without_physicals_says_so_instead_of_matching_nobody():
    """club_player_register carries the columns in every sport's schema but
    only the AFL import fills them, so existence alone is not enough."""
    con, schema = fixture()
    con.executescript("""
        CREATE TABLE club_player_register (
          player_id INTEGER, club_id INTEGER,
          height_cm INTEGER, weight_kg INTEGER
        );
        INSERT INTO club_player_register VALUES (1, 1, NULL, NULL);
    """)
    with pytest.raises(Q.QuerySyntaxError, match="not loaded"):
        Q.compile_query(schema, "height>=195", con=con)


# ------------------------------------------------------------ name search
#
# The name filter matches on letters alone -- case, accents and punctuation
# are interchangeable, the same rule the player picker applies -- and typed
# text never becomes a LIKE wildcard. Before that, "name:a_pha" matched
# Alpha One ("_" matched the "l"), "name:%" matched everybody, and
# "name:o'brien" died in shlex with "No closing quotation".

def test_a_name_search_still_finds_its_player():
    con, schema = fixture()
    sql, params, _ = Q.compile_query(schema, "name:alpha", con=con)
    assert [row[0] for row in con.execute(sql, params)] == ["Alpha One"]


@pytest.mark.parametrize("query", ["name:a_pha", "name:al%ne"])
def test_a_typed_wildcard_is_a_character_not_a_pattern(query):
    con, schema = fixture()
    sql, params, _ = Q.compile_query(schema, query, con=con)
    assert con.execute(sql, params).fetchall() == []


def test_a_search_of_nothing_but_punctuation_is_refused():
    """Folding strips punctuation, and an empty LIKE pattern would read as
    "match everybody" -- the exact over-match the folding exists to stop."""
    con, schema = fixture()
    with pytest.raises(Q.QuerySyntaxError):
        Q.compile_query(schema, "name:%", con=con)


def test_a_straight_apostrophe_is_a_letter_of_the_name_not_a_quote():
    """`name:o'brien` used to die in shlex with "No closing quotation":
    posix mode reads a single quote as opening a quotation. Double quotes
    are the only quoting the examples teach, so they are the only quoting
    the parser accepts -- and the apostrophe then matches the OBrien that
    AFL Tables strips it from."""
    con, schema = fixture()
    con.execute(
        "INSERT INTO players VALUES "
        "(3, 'Jim OBrien', 1990, 2000, 100, 50, 5, 'A', 60)")
    sql, params, _ = Q.compile_query(schema, "name:o'brien", con=con)
    assert [row[0] for row in con.execute(sql, params)] == ["Jim OBrien"]


def test_double_quotes_still_group_a_two_word_value():
    con, schema = fixture()
    sql, params, _ = Q.compile_query(schema, 'name:"alpha one"', con=con)
    assert [row[0] for row in con.execute(sql, params)] == ["Alpha One"]


def test_an_accented_name_is_found_by_its_plain_spelling():
    """SQLite's LOWER and LIKE fold ASCII only, so "acuna" could never
    reach "Acuña" in SQL -- 218 MLB names carry a diacritic."""
    con, schema = fixture()
    con.execute(
        "INSERT INTO players VALUES "
        "(3, 'Ronald Acuña', 2018, 2026, 900, 0, 20, 'A', 30)")
    for query in ("name:acuna", "name:acuña", "name:Acuña"):
        sql, params, _ = Q.compile_query(schema, query, con=con)
        assert [row[0] for row in con.execute(sql, params)] == \
            ["Ronald Acuña"], query


def test_a_clicked_suggestion_replaces_only_the_misspelled_name():
    """The did-you-mean buttons rewrite the query: the name token takes
    the clicked spelling, every other filter rides along untouched."""
    rewritten = Q.replace_name_term(
        'club:"St Kilda" name:"gary abblett" games>=100',
        "gary abblett", "Gary Ablett")
    assert Q.name_terms(rewritten) == ["Gary Ablett"]
    tokens = Q.tokenize(rewritten)
    assert "club:St Kilda" in tokens
    assert "games>=100" in tokens


def test_a_query_that_does_not_parse_is_returned_unchanged():
    assert Q.replace_name_term('name:"unclosed', "x", "y") == 'name:"unclosed'


# ---------------------------------------------------------- draft tokens

def _draft_fixture():
    con, schema = fixture()
    con.executescript("""
        CREATE TABLE draft (
          player TEXT, draft_year INTEGER, draft_type TEXT, draft_kind TEXT,
          pick INTEGER, club TEXT, original_club TEXT
        );
        CREATE TABLE draft_links (
          draft_rowid INTEGER, player_id INTEGER, match_status TEXT
        );
        INSERT INTO draft VALUES
          ('Alpha One',1990,'National','national',3,'A',
           'Glenelg / Sacred Heart College'),
          ('Beta Two',2001,'National','national',40,'B',
           'Greythorn / Oakleigh U18'),
          ('Beta Two',2004,'Rookie','rookie',3,'B',
           'Greythorn / Oakleigh U18');
        INSERT INTO draft_links VALUES (1,1,'unique'), (2,2,'unique'),
                                       (3,2,'ambiguous');
    """)
    return con, schema


def _found(query):
    con, schema = _draft_fixture()
    sql, params, _ = Q.compile_query(schema, query, con=con,
                                     extensions=_afl())
    return [row[0] for row in con.execute(sql, params)]


def test_recruited_from_matches_a_whole_step_of_the_path():
    """`original_club` is a path, so the term is matched against one step
    of it rather than as a substring of the whole thing."""
    assert _found("recruited_from:Glenelg") == ["Alpha One"]
    assert _found('recruited_from:"Oakleigh U18"') == ["Beta Two"]
    assert _found('recruited_from:"Sacred Heart College"') == ["Alpha One"]


def test_recruited_from_accepts_the_name_a_person_would_type():
    """"Oakleigh" is what somebody writes for a club whose step reads
    "Oakleigh U18"; "North Glenelg" is a different place entirely."""
    assert _found("recruited_from:Oakleigh") == ["Beta Two"]
    assert _found("recruited_from:Glen") == []


def test_a_pick_is_a_national_draft_pick():
    """Draftguru restarts pick numbering for the rookie draft, so an
    unqualified pick 3 would answer with two different players."""
    assert _found("pick:3") == ["Alpha One"]
    assert _found("pick:1..50") == ["Alpha One", "Beta Two"]


def test_draft_tokens_trust_only_a_resolved_link():
    """Beta Two's rookie row is ambiguous, so it cannot answer a search
    any more than it answers a grid square."""
    con, schema = _draft_fixture()
    con.execute("UPDATE draft_links SET match_status='ambiguous' "
                "WHERE draft_rowid=2")
    sql, params, _ = Q.compile_query(schema, "recruited_from:Oakleigh",
                                     con=con, extensions=_afl())
    assert con.execute(sql, params).fetchall() == []


def test_a_draft_year_is_a_year_or_a_range():
    assert _found("draft_year:1990") == ["Alpha One"]
    assert _found("draft_year:1990..2001") == ["Alpha One", "Beta Two"]


def test_a_draft_search_without_the_layer_says_so():
    con, schema = fixture()          # no draft tables
    for query in ("recruited_from:Glenelg", "pick:1", "draft_year:2001"):
        with pytest.raises(Q.QuerySyntaxError, match="not loaded"):
            Q.compile_query(schema, query, con=con, extensions=_afl())


def test_an_extension_token_without_its_extension_is_unknown():
    """A sport that registers no extensions rejects the token by name --
    the field genuinely does not exist for that sport."""
    con, schema = fixture()
    with pytest.raises(Q.QuerySyntaxError, match="Unknown search field"):
        Q.compile_query(schema, "captain:true", con=con)


def test_name_terms_reads_what_the_did_you_mean_needs():
    """The search page offers close spellings only for the name tokens,
    and a query that does not parse is already an error box."""
    assert Q.name_terms('club:A name:"gary ablet" games>=100') == \
        ["gary ablet"]
    assert Q.name_terms("player:smith name:jones") == ["smith", "jones"]
    assert Q.name_terms("games>=100") == []
    assert Q.name_terms('name:"unclosed') == []


# ------------------------------------------------------- NFL-shaped physicals
#
# The NFL keeps height/weight on `players` under the names the schema
# declares -- literally `height` and `weight`, in inches and pounds. The
# compiler used to hardcode height_cm/weight_kg for every sport, so
# `height>=72` answered "Height data is not loaded" on a database carrying
# height for every player.

def _nfl_shaped_fixture():
    con = sqlite3.connect(":memory:")
    con.executescript(_PHYSICALS_SCHEMA + """
        CREATE TABLE players (
          player_id INTEGER, player TEXT, debut_season INTEGER,
          final_season INTEGER, career_games INTEGER, career_goals INTEGER,
          finals_played INTEGER, clubs_hist TEXT, obscurity REAL,
          height INTEGER, weight INTEGER
        );
        INSERT INTO players VALUES
          (1,'Tall One',1999,2010,200,30,10,'A',80,78,320),
          (2,'Short Two',2001,2005,50,2,0,'B',40,68,180);
    """)
    schema = core.Schema(
        career_score="career_goals", career_postseason="finals_played",
        game_score="goals", stats=("goals", "disposals"), clubs=("A", "B"),
        height="height", weight="weight",
        required_games_cols=(), required_player_cols=(),
    )
    return con, schema


@pytest.mark.parametrize("query,expected", [
    ("height>=72", ["Tall One"]),
    ("weight>=200", ["Tall One"]),
    ("height<=70 weight<=190", ["Short Two"]),
])
def test_nfl_shaped_physicals_use_the_schema_declared_columns(
        query, expected):
    con, schema = _nfl_shaped_fixture()
    sql, params, _ = Q.compile_query(schema, query, con=con)
    assert [row[0] for row in con.execute(sql, params)] == expected


# ------------------------------------------------------ operator strictness
#
# Field-only tokens used to accept relational operators and silently treat
# them as ':' -- `limit>10` behaved as `limit:10`, `club>Hawthorn` as
# `club:Hawthorn` -- misleading semantics the parser now refuses.

@pytest.mark.parametrize("query", [
    "sort>games", "limit>10", "limit>=100", "name>Smith", "player>Smith",
    "club>A", "club_any>A", "played>1990", "season>1990",
    "postseason>true", "debut_year>1990",
])
def test_field_only_tokens_reject_relational_operators(query):
    con, schema = fixture()
    with pytest.raises(Q.QuerySyntaxError, match="field:value"):
        Q.compile_query(schema, query, con=con)


@pytest.mark.parametrize("query", [
    "captain_club>B", "captain_year>1995", "captain_season>1995",
    "pick>3", "draft_pick>3", "draft_year>1990", "drafted_year>1990",
])
def test_afl_extension_tokens_reject_relational_operators(query):
    con, schema = _draft_fixture()
    with pytest.raises(Q.QuerySyntaxError, match="field:value"):
        Q.compile_query(schema, query, con=con, extensions=_afl())


def test_debut_still_accepts_genuine_comparisons():
    """debut>=1990 is real relational syntax, not a field-only token."""
    con, schema = fixture()
    sql, params, _ = Q.compile_query(schema, "debut>=2000", con=con)
    assert [row[0] for row in con.execute(sql, params)] == ["Beta Two"]


# ------------------------------------------------------------ club identity

def test_known_clubs_and_aliases_compile_to_exact_in_lists():
    con, schema = fixture()
    sql, params, _ = Q.compile_query(schema, "club:a", con=con)
    assert "LOWER(" not in sql
    assert [row[0] for row in con.execute(sql, params)] == ["Alpha One"]


def test_unknown_clubs_are_rejected_not_scanned():
    """The forgiving fallback was LOWER(col)=LOWER(?) over the whole
    games table -- a full scan that almost always matched nothing. An
    unknown name now names itself in the error."""
    con, schema = fixture()
    with pytest.raises(Q.QuerySyntaxError, match="Unknown club"):
        Q.compile_query(schema, "club:Hogwarts", con=con)
    with pytest.raises(Q.QuerySyntaxError, match="Unknown club"):
        Q.compile_query(schema, "club_any:Hogwarts", con=con)


def test_lineage_aliases_still_resolve():
    """Era names from club_lineage expand exactly as the grid does."""
    con, schema = fixture()
    lineage_schema = core.Schema(
        career_score="career_goals", career_postseason="finals_played",
        game_score="goals", stats=("goals", "disposals"),
        clubs=("A", "B"), club_lineage={"A": ("Old A",)},
        required_games_cols=(), required_player_cols=(),
    )
    sql, params, _ = Q.compile_query(lineage_schema, 'club:"old a"',
                                     con=con)
    assert "LOWER(" not in sql
    assert params.count("Old A") >= 1


# ---------------------------------------------------------- family q= wins

def test_family_facade_gives_q_precedence_over_structured_params():
    """q= is the canonical whole-query parameter; appending the
    structured family parameters after it duplicated filters and grew
    the query on every canonicalisation pass."""
    import query_filters_family as QF

    assert QF.query_from_params(
        {"q": ["games>=100"], "family_relation": ["brother"]}
    ) == "games>=100"
    assert QF.query_from_params(
        {"family_relation": ["brother"], "related_to": ["Gary Ablett"]}
    ) == 'family_relation:brother related_to:"Gary Ablett"'


if __name__ == "__main__":
    test_base_compiler_end_to_end()
    print("query filter tests: passed")
