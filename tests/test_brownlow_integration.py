"""Brownlow CSV linking, ranking, constraints and parser regression tests."""

import csv
import sqlite3
from pathlib import Path

from afl import brownlow
from afl import awards_page
from afl import parse_criteria
from utils.afl.load_brownlow import load_sources
from names import normalise_name


FIELDS = [
    "season", "player", "team", "votes", "ineligible", "round_1", "round_2",
    "games", "three_vote_games", "two_vote_games", "one_vote_games",
    "polling_games", "winner", "player_id", "player_url", "team_url", "source_url",
]


def _database(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE players (player_id INTEGER, player TEXT, name_key TEXT, "
        "debut_season INTEGER, final_season INTEGER)"
    )
    con.execute(
        "CREATE TABLE games (player_id INTEGER, season INTEGER, club_hist TEXT)"
    )
    con.execute(
        "CREATE TABLE awards (award_slug TEXT, award_name TEXT, "
        "award_category TEXT, season INTEGER)"
    )
    con.execute(
        "INSERT INTO awards VALUES ('brownlow-medal','Brownlow Medal',"
        "'award',1980)"
    )
    players = [
        (1, "Patrick Cripps", 2012, 2025, "Carlton"),
        (2, "Lachie Neale", 2012, 2025, "Brisbane Lions"),
        (3, "Marcus Bontempelli", 2014, 2025, "Western Bulldogs"),
        (4, "Stephen Icke", 1975, 1987, "Melbourne"),
        (5, "Jack Paterson", 1930, 1935, "North Melbourne"),
    ]
    con.executemany(
        "INSERT INTO players VALUES (?,?,?,?,?)",
        [(pid, name, normalise_name(name), lo, hi)
         for pid, name, lo, hi, _club in players],
    )
    con.executemany(
        "INSERT INTO games VALUES (?,?,?)",
        [(pid, lo if pid in {4, 5} else 2024, club)
         for pid, _name, lo, _hi, club in players],
    )
    # Put the historical spelling variants in seasons that exercise both
    # the initial/surname and one-edit-surname fallbacks.
    con.execute("UPDATE games SET season=1984 WHERE player_id=4")
    con.execute("UPDATE games SET season=1932 WHERE player_id=5")
    con.commit()
    con.close()


def _write(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _row(season, player, team, votes, slug, club_slug, **extra):
    return {
        "season": season, "player": player, "team": team, "votes": votes,
        "ineligible": extra.get("ineligible", False),
        "round_1": extra.get("round_1", ""), "round_2": extra.get("round_2", ""),
        "games": extra.get("games", 20),
        "three_vote_games": extra.get("three_vote_games", ""),
        "two_vote_games": extra.get("two_vote_games", ""),
        "one_vote_games": extra.get("one_vote_games", ""),
        "polling_games": extra.get("polling_games", ""),
        "winner": extra.get("winner", False), "player_id": slug,
        "player_url": f"https://afltables.com/afl/stats/players/X/{slug}.html",
        "team_url": f"https://afltables.com/afl/brownlow/{club_slug}_totals.html",
        "source_url": f"https://afltables.com/afl/brownlow/brownlow{season}.html",
    }


def test_load_rank_link_and_query(tmp_path):
    db = tmp_path / "afl.db"
    _database(db)
    p2024 = tmp_path / "2024.csv"
    _write(p2024, [
        _row(2024, "Cripps, Patrick", "CA", 45, "Patrick_Cripps", "carlton",
             winner=True, round_1=3, round_2=0),
        _row(2024, "Neale, Lachie", "BL", 35, "Lachie_Neale", "brisbanel",
             round_1=2, round_2=""),
        _row(2024, "Bontempelli, Marcus", "WB", 35, "Marcus_Bontempelli",
             "bullldogs", round_1=0, round_2=3),
    ])
    p1984 = tmp_path / "1984.csv"
    _write(p1984, [
        _row(1984, "Icke, Steven", "ME", 4, "Steven_Icke", "melbourne"),
    ])
    p1932 = tmp_path / "1932.csv"
    _write(p1932, [
        _row(1932, "Patterson, Jack", "North Melbourne", 2,
             "Jack_Patterson", "kangaroos"),
    ])

    result = load_sources(db, [p1932, p1984, p2024], verbose=False)
    assert result == {"rows": 5, "round_rows": 10, "trusted": 5,
                      "unique": 3, "resolved": 2}

    con = sqlite3.connect(db)
    try:
        assert brownlow.brownlow_available(con)
        assert brownlow.brownlow_count(con) == 5
        ranks = con.execute(
            "SELECT player, eligible_rank FROM brownlow_results "
            "WHERE season=2024 ORDER BY votes DESC, player"
        ).fetchall()
        assert ranks == [("Patrick Cripps", 1),
                         ("Lachie Neale", 2), ("Marcus Bontempelli", 2)]
        methods = dict(con.execute(
            "SELECT player_source, match_method FROM brownlow_results "
            "WHERE season IN (1932,1984)"))
        assert methods["Icke, Steven"] == "initial_surname_season_club"
        assert methods["Patterson, Jack"] == "one_edit_surname_season_club"

        # The Awards honour roll must use this full-history layer rather
        # than Draftguru's winner table, whose Brownlow coverage starts 1980.
        catalogue = awards_page._catalogue(con)
        summary = catalogue[catalogue["award_slug"] == "brownlow-medal"].iloc[0]
        assert (summary["winners"], summary["season_from"],
                summary["season_to"], summary["seasons"]) == (1, 2024, 2024, 1)
        honour_roll = awards_page._honour_roll(con, "brownlow-medal")
        assert honour_roll[["Season", "Player", "Votes"]].to_dict("records") == [
            {"Season": 2024, "Player": "Patrick Cripps", "Votes": 45}
        ]

        sql, params = brownlow.brownlow_top_finish(1)
        assert {row[0] for row in con.execute(sql, params)} == {1, 4, 5}
        sql, params = brownlow.brownlow_exact_finish(2)
        assert {row[0] for row in con.execute(sql, params)} == {2, 3}
        played, missed = con.execute(
            "SELECT played, votes FROM brownlow_round_votes "
            "WHERE result_id='2024:Lachie_Neale' ORDER BY round_number"
        ).fetchall()
        assert (played, missed) == ((1, 2), (0, None))
    finally:
        con.close()


def test_parser_understands_brownlow_finishes():
    cases = {
        "top 5 Brownlow finish": "top-5 Brownlow finish",
        "2x top 10 Brownlow finish": "top-10 Brownlow finish 2+ times",
        "Brownlow runner-up": "Brownlow runner-up",
        "finished 3rd in the Brownlow": "finished 3 in the Brownlow",
        "20+ Brownlow votes in a season": "20+ brownlow votes in a season",
    }
    for phrase, label in cases.items():
        constraint, actual = parse_criteria.parse(phrase)
        assert constraint is not None
        assert actual == label


def test_optional_placeholder_is_safe():
    con = sqlite3.connect(":memory:")
    brownlow.ensure_brownlow_table(con)
    sql, params = brownlow.brownlow_top_finish(5)
    assert con.execute(sql, params).fetchall() == []
    assert not brownlow.brownlow_available(con)
