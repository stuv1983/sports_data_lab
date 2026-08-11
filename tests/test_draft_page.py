#!/usr/bin/env python3
"""The draft board, and the rules that make it readable.

The database has held every Draftguru selection since 1981 all along;
nothing displayed one. What is tested here is the two things that make a
board mean something: the filters that narrow 6,810 records to the ones
asked for, and the dated rules that explain what is on screen -- a 1986
draft with no West Australians in it, a 1997 club holding two picks in the
first five.
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

import pandas as pd
import pytest

import sports
from afl import data_notes as N
from afl import draft_page as D
from afl import recruitment as R


# -------------------------------------------------------------- the rules

def test_the_rules_of_a_draft_are_dated_to_the_drafts_they_governed():
    """Priority picks were automatic from 1993 and discretionary from
    2012, so a 1990 board must not claim either."""
    assert N.for_draft_year(1990) == []
    text_1997 = " ".join(n.text for n in N.for_draft_year(1997))
    assert "automatically" in text_1997
    text_2020 = " ".join(n.text for n in N.for_draft_year(2020))
    assert "discretion" in text_2020
    assert "automatically" not in text_2020


def test_the_first_modern_draft_explains_its_own_missing_players():
    """1986 has no West Australians in it, which reads as broken data
    until the exclusion is stated."""
    text = " ".join(n.text for n in N.for_draft_year(1986))
    assert "West Australian" in text
    assert "Brisbane Bears" in text


def test_the_age_rule_starts_the_year_after_the_17_year_old():
    assert not any("31 December" in n.text for n in N.for_draft_year(2008))
    assert any("31 December" in n.text for n in N.for_draft_year(2009))


def test_the_standing_rules_belong_to_no_single_year():
    """The father-son rule predates the draft by thirty years, so pinning
    it to a draft year would be a lie; it is background instead."""
    background = N.draft_background()
    assert background
    for note in background:
        assert note.first is None and note.last is None
        assert not note.covers(2020)
    assert any("Father-son" in n.text for n in background)
    assert any("academies" in n.text.lower() for n in background)


def test_draft_rules_never_leak_into_the_season_cards():
    """`for_season` feeds the season and round cards. A draft rule shown
    there would read as a caveat about that season's results."""
    for season in (1986, 1993, 2009, 2012, 2024):
        for note in N.for_season(season):
            assert note not in N.DRAFT_NOTES, (season, note.text)


# ------------------------------------------------------------ the filters

def _fixture():
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE draft (
          player TEXT, name_key TEXT, draft_year INTEGER, draft_type TEXT,
          pick INTEGER, pick_note TEXT, club TEXT, signing TEXT,
          signing_kind TEXT, original_club TEXT, draft_age INTEGER,
          height_cm INTEGER, grade TEXT, games INTEGER, goals INTEGER,
          awards_text TEXT, dg_person_id INTEGER
        );
        CREATE TABLE person_links (
          dg_person_id INTEGER, player_id INTEGER, match_status TEXT
        );
        CREATE TABLE players (
          player_id INTEGER, player TEXT, career_games INTEGER,
          career_goals INTEGER
        );
        INSERT INTO draft VALUES
          ('Harley Reid','harley reid',2023,'National',1,NULL,'West Coast',
           NULL,NULL,'Tongala / Bendigo U18',18,187,'B',58,37,NULL,1),
          ('Dan OBrien','dan obrien',2023,'Rookie',4,NULL,'Carlton',
           NULL,NULL,'Northern',19,180,'C',0,0,NULL,2),
          ('Tom Hawkins','tom hawkins',2006,'National',41,NULL,'Geelong',
           'Father-Son ( Jack Hawkins )','Father-Son',
           'Finley / Geelong College / Geelong U18',18,196,'A',
           359,800,NULL,3),
          -- The same person, drafted a second time. 1,473 people in the
          -- real table were.
          ('Tom Hawkins','tom hawkins',2010,'Trade',NULL,NULL,'Geelong',
           NULL,NULL,'Finley / Geelong College / Geelong U18',22,196,'A',
           359,800,NULL,3);
        INSERT INTO person_links VALUES (1,10,'from_draft'), (3,30,'unique'),
                                        (2,20,'implausible');
        INSERT INTO players VALUES (10,'Harley Reid',60,38),
                                   (30,'Tom Hawkins',360,801);
    """)
    return con


def _rows(con, **kwargs):
    args = {"year": D.ANY, "draft_type": D.ANY, "club": D.ANY,
            "signing": D.ANY, "name": "", "source": D.ANY}
    args.update(kwargs)
    where, params = D._where(args["year"], args["draft_type"], args["club"],
                             args["signing"], args["name"], con,
                             source=args["source"])
    return D._board.__wrapped__(where, params, None, con)


def test_every_filter_narrows_to_what_it_names():
    con = _fixture()
    assert len(_rows(con)) == 4
    assert _rows(con, year=2023)["Player"].tolist() == ["Harley Reid",
                                                        "Dan OBrien"]
    assert _rows(con, draft_type="National")["Player"].tolist() == \
        ["Harley Reid", "Tom Hawkins"]
    assert _rows(con, club="Geelong")["Player"].tolist() == \
        ["Tom Hawkins", "Tom Hawkins"]
    assert _rows(con, signing="Father-Son")["Player"].tolist() == \
        ["Tom Hawkins"]


def test_the_name_box_matches_on_letters_alone():
    """The site-wide rule: "o'brien" finds the OBrien that AFL Tables
    strips the apostrophe from, and a typed wildcard is a character."""
    con = _fixture()
    assert _rows(con, name="o'brien")["Player"].tolist() == ["Dan OBrien"]
    assert _rows(con, name="HAWKINS")["Player"].tolist() == \
        ["Tom Hawkins", "Tom Hawkins"]     # the board lists selections
    assert _rows(con, name="%").empty
    assert _rows(con, name="h_wkins").empty


def test_career_games_prefer_our_own_table_over_the_scrape():
    """Draftguru's copy is a snapshot taken at import; `players` is
    current to the last round loaded, so a linked row uses ours."""
    con = _fixture()
    board = _rows(con, name="reid")
    assert board["Games"].iat[0] == 60        # players, not the draft's 58
    assert board["Goals"].iat[0] == 38


def test_a_row_whose_link_was_never_trusted_falls_back_to_the_scrape():
    """`implausible` is not a resolved link, so the row must not borrow
    that player's career -- it shows Draftguru's own count and opens no
    card."""
    con = _fixture()
    board = _rows(con, name="obrien")
    assert board["Games"].iat[0] == 0
    assert pd.isna(board["player_id"].iat[0])


def test_a_missing_pick_number_does_not_turn_the_rest_into_decimals():
    """One free-agency row with no pick used to draw pick 3 as "3.0"."""
    con = _fixture()
    con.execute("INSERT INTO draft VALUES ('Free Agent','free agent',2025,"
                "'Free Agency',NULL,NULL,'St Kilda',NULL,NULL,NULL,26,191,"
                "'B',20,5,NULL,9)")
    board = _rows(con)
    assert str(board["Pick"].dtype) == "Int64"
    assert sorted(board["Pick"].dropna()) == [1, 4, 41]
    # The free agent and the traded player each have no pick number.
    assert board["Pick"].isna().sum() == 2


def test_the_summary_counts_selections_but_measures_careers_by_person():
    """A player drafted twice is two selections and one career. Counting
    his games once per selection inflated the real table's total by 177,000
    games and credited his junior club with a career it produced once."""
    con = _fixture()
    where, params = D._where(D.ANY, D.ANY, D.ANY, D.ANY, "", con)
    records, clubs, played, games = D._summary.__wrapped__(
        where, params, None, con)
    assert records == 4                      # Hawkins twice
    assert clubs == 3
    assert played == 2                       # OBrien never played one
    assert games == 60 + 360                 # not 60 + 360 + 360


def test_a_twice_drafted_player_appears_once_in_the_career_list():
    con = _fixture()
    where, params = D._where(D.ANY, D.ANY, D.ANY, D.ANY, "", con)
    best = D._best.__wrapped__(where, params, None, con)
    assert best["Player"].tolist() == ["Tom Hawkins", "Harley Reid"]


def test_the_career_list_shows_a_selection_that_actually_happened():
    """Grouping by person with both a MIN(year) and a MAX(games) let
    SQLite take the year from one selection and the club and type from
    another, inventing a row nobody was ever picked in."""
    con = _fixture()
    where, params = D._where(D.ANY, D.ANY, D.ANY, D.ANY, "", con)
    row = D._best.__wrapped__(where, params, None, con).iloc[0]
    assert (row["Year"], row["Draft"], row["Pick"]) == (2006, "National", 41)
    assert row["Games"] == 360               # the career, across both


def test_the_year_table_keeps_selections_as_records():
    """Filtering the board to a year must produce the row count this
    table promised for it, so Selections counts records even though the
    career columns count people."""
    con = _fixture()
    where, params = D._where(D.ANY, D.ANY, D.ANY, D.ANY, "", con)
    classes = D._classes.__wrapped__(where, params, None, con)
    by_year = dict(zip(classes["Year"], classes["Selections"]))
    assert by_year[2023] == 2 and by_year[2006] == 1 and by_year[2010] == 1


# ------------------------------------------------------ recruitment paths

def test_a_path_reads_junior_end_first():
    assert R.path("Greythorn / Xavier College / Oakleigh U18") == \
        ["Greythorn", "Xavier College", "Oakleigh U18"]
    assert R.drafted_from("Greythorn / Xavier College / Oakleigh U18") == \
        "Oakleigh U18"
    assert R.junior_club("Greythorn / Xavier College / Oakleigh U18") == \
        "Greythorn"


def test_a_single_step_path_is_both_ends_of_itself():
    """A West Australian listed only as "Claremont" was drafted from
    Claremont, and started there too as far as the source says."""
    assert R.path("Claremont") == ["Claremont"]
    assert R.drafted_from("Claremont") == "Claremont"
    assert R.junior_club("Claremont") == "Claremont"


def test_a_record_with_no_path_yields_nothing_rather_than_a_blank():
    for empty in (None, "", "   "):
        assert R.path(empty) == []
        assert R.drafted_from(empty) is None


def test_segments_are_sorted_only_where_the_data_is_unambiguous():
    assert R.kind("Sandringham U18") == R.TALENT_LEAGUE
    assert R.kind("Xavier College") == R.SCHOOL
    assert R.kind("Hale School") == R.SCHOOL
    assert R.kind("Melbourne Grammar") == R.SCHOOL
    # Everything else is a club, which is the honest answer for a name
    # that says nothing about what sort of place it is.
    assert R.kind("Claremont") == R.CLUB
    assert R.kind("Whitford JFC") == R.CLUB
    assert R.kind("Port Adelaide (SANFL)") == R.CLUB
    # A talent-league club named for a school is still the pathway.
    assert R.kind("Geelong College U18") == R.TALENT_LEAGUE


def test_a_source_matches_whole_segments_not_substrings():
    """"Geelong" is inside "Geelong U18" and "Geelong College", which are
    three different places -- substring matching conflated all of them."""
    con = _fixture()
    assert _rows(con, source="Geelong U18")["Player"].tolist() == \
        ["Tom Hawkins", "Tom Hawkins"]
    assert _rows(con, source="Geelong College")["Player"].tolist() == \
        ["Tom Hawkins", "Tom Hawkins"]
    assert _rows(con, source="Geelong").empty
    assert _rows(con, source="Bendigo U18")["Player"].tolist() == \
        ["Harley Reid"]


def test_a_source_is_credited_for_every_player_who_passed_through_it():
    """Crediting only the last step would answer "which talent-league
    clubs exist"; the junior club had a hand in the career too."""
    con = _fixture()
    sources = D._sources.__wrapped__(None, con)
    by_name = sources.set_index("Source")
    assert set(by_name.index) >= {"Tongala", "Bendigo U18", "Finley",
                                  "Geelong College", "Geelong U18"}
    assert by_name.loc["Tongala", "Games"] == 60
    # Hawkins twice through Geelong U18: two selections, one career.
    assert by_name.loc["Geelong U18", "Selections"] == 2
    assert by_name.loc["Geelong U18", "Played"] == 1
    assert by_name.loc["Geelong U18", "Games"] == 360
    assert by_name.loc["Geelong U18", "Kind"] == R.TALENT_LEAGUE


def test_a_column_nothing_filled_is_dropped_rather_than_shown_blank():
    con = _fixture()
    board = _rows(con, year=2023)
    assert "Awards" not in D._drop_empty(board).columns


# ------------------------------------ recruitment as a square and a search

def test_a_recruitment_square_is_offered_real_places_to_pick_from():
    """The grid maker used to ask for this as free text, which meant
    guessing that the Oakleigh Chargers are written "Oakleigh U18"."""
    con = _fixture()
    from afl import constraints as C

    ordered = C.recruit_sources(con)
    sources = dict(ordered)
    assert sources["Geelong U18"] == 2          # Hawkins, drafted twice
    assert sources["Tongala"] == 1
    # Ordered by how many selections came through, so the squares with
    # answers are the ones offered first. Ties fall back to the name, so
    # the list is stable between runs.
    counts = [count for _, count in ordered]
    assert counts == sorted(counts, reverse=True)
    assert ordered[0][1] == 2


def test_the_solver_reads_a_recruitment_criterion():
    """Gridley writes these as prose, and the brand name is not what
    Draftguru stores: the Sandringham Dragons are "Sandringham U18"."""
    from afl import parse_criteria as P

    for text, expected in (
        ("RECRUITED FROM GLENELG", "glenelg"),
        ("FROM OAKLEIGH CHARGERS", "oakleigh"),
        ("VIA NORWOOD", "norwood"),
        ("RECRUITED FROM SANDRINGHAM DRAGONS", "sandringham"),
    ):
        constraint, label = P.parse(text)
        assert constraint is not None, text
        assert expected in label


def test_a_criterion_that_only_looks_like_one_is_left_alone():
    """"30+ GOALS" and a bare club name are answered by their own rules;
    a recruitment rule that swallowed them would break real squares."""
    from afl import parse_criteria as P

    assert P.parse("30+ GOALS")[1] == "30+ career goals"
    assert P.parse("ST KILDA")[1] == "St Kilda"


# ============================================================ live data

def _live():
    if not sports.AFL.exists():
        pytest.skip("no built AFL database")
    return sqlite3.connect(f"file:{sports.AFL.db}?mode=ro", uri=True)


def test_live_the_board_opens_on_the_real_database():
    con = _live()
    try:
        options = D._options.__wrapped__(D._revision(sports.AFL.db), con)
        assert 1981 in options["years"] and 2025 in options["years"]
        assert "National" in options["types"]
        assert "Father-Son" in options["signings"]

        where, params = D._where(2023, "National", D.ANY, D.ANY, "", con)
        board = D._board.__wrapped__(where, params, None, con)
        first = board.iloc[0]
        assert (first["Pick"], first["Player"], first["Club"]) == \
            (1, "Harley Reid", "West Coast")
    finally:
        con.close()


def test_live_the_longest_father_son_career_is_the_one_wikipedia_names():
    """Dustin Fletcher, 400 games -- the most of any father-son selection."""
    con = _live()
    try:
        where, params = D._where(D.ANY, D.ANY, D.ANY, "Father-Son", "", con)
        best = D._best.__wrapped__(where, params, None, con)
        assert best["Player"].iat[0] == "Dustin Fletcher"
        assert best["Games"].iat[0] >= 400
    finally:
        con.close()


def run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:
                if exc.__class__.__name__ == "Skipped":
                    continue
                raise
    print("draft page tests: passed")


if __name__ == "__main__":
    run()
