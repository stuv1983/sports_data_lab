#!/usr/bin/env python3
"""Regression tests for broad-family Advanced Search filters."""

from __future__ import annotations

# Run standalone from anywhere: the project root is one level up.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import sqlite3
from types import SimpleNamespace

import query_filters_family as Q
from afl import search_tokens


def _afl():
    """The AFL's registered extensions, which now carry the family tokens."""
    return search_tokens.extensions()


SCHEMA = SimpleNamespace(
    players="players",
    games="games",
    player_id="player_id",
    player="player",
    debut_season="debut_season",
    final_season="final_season",
    career_games="career_games",
    career_score="career_goals",
    career_postseason="finals_played",
    clubs_hist="clubs_hist",
    obscurity="obscurity",
    club_now="club_now",
    club_hist="club_hist",
    season="season",
    is_final="is_final",
    stats=(),
)


def build_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(
        """
        CREATE TABLE players (
            player_id INTEGER PRIMARY KEY,
            player TEXT,
            debut_season INTEGER,
            final_season INTEGER,
            career_games INTEGER,
            career_goals INTEGER,
            finals_played INTEGER,
            clubs_hist TEXT,
            obscurity REAL
        );
        CREATE TABLE games (
            player_id INTEGER,
            club_now TEXT,
            club_hist TEXT,
            season INTEGER,
            is_final INTEGER
        );
        CREATE TABLE family_members (
            source_member_id TEXT PRIMARY KEY,
            family_key TEXT,
            player_id INTEGER,
            match_status TEXT
        );
        CREATE TABLE family_relationships (
            person_a_source_member_id TEXT,
            person_b_source_member_id TEXT,
            relationship_type TEXT,
            relationship_label TEXT,
            person_a_role TEXT,
            person_b_role TEXT
        );
        """
    )
    con.executemany(
        "INSERT INTO players VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (1, "Alpha Brother", 1990, 2000, 100, 20, 1, "Carlton", 80.0),
            (2, "Beta Brother", 1992, 2002, 120, 30, 0, "Geelong", 60.0),
            (3, "Unrelated Player", 1995, 2005, 150, 40, 2, "Geelong", 70.0),
        ],
    )
    con.executemany(
        "INSERT INTO games VALUES (?,?,?,?,?)",
        [
            (1, "Carlton", "Carlton", 1995, 1),
            (2, "Geelong", "Geelong", 1996, 0),
            (3, "Geelong", "Geelong", 1997, 1),
        ],
    )
    con.executemany(
        "INSERT INTO family_members VALUES (?,?,?,?)",
        [
            ("a", "family-1", 1, "unique"),
            ("b", "family-1", 2, "resolved"),
            ("c", "family-2", 3, "unique"),
        ],
    )
    con.execute(
        "INSERT INTO family_relationships VALUES (?,?,?,?,?,?)",
        ("a", "b", "sibling", "brothers", "brother", "brother"),
    )
    return con


def names(con: sqlite3.Connection, query: str) -> list[str]:
    sql, params, _ = Q.compile_query(SCHEMA, query, con=con,
                                     extensions=_afl())
    return [row[0] for row in con.execute(sql, params)]


def run() -> None:
    con = build_db()
    try:
        assert names(con, "family_relation:brother sort:obscurity") == [
            "Alpha Brother",
            "Beta Brother",
        ]
        assert names(
            con,
            "family_relation:brother postseason:true sort:obscurity",
        ) == ["Alpha Brother"]
        assert names(con, "relative_club:Geelong sort:obscurity") == [
            "Alpha Brother"
        ]
        assert names(con, 'related_to:"Beta Brother"') == ["Alpha Brother"]
        assert "family_relation:brother" in Q.query_from_params(
            {"family_relation": ["brother"]}
        )
    finally:
        con.close()

    missing = sqlite3.connect(":memory:")
    try:
        missing.execute(
            "CREATE TABLE players (player_id INTEGER, player TEXT, "
            "debut_season INTEGER, final_season INTEGER, career_games INTEGER, "
            "career_goals INTEGER, finals_played INTEGER, clubs_hist TEXT, "
            "obscurity REAL)"
        )
        missing.execute(
            "CREATE TABLE games (player_id INTEGER, club_now TEXT, "
            "club_hist TEXT, season INTEGER, is_final INTEGER)"
        )
        try:
            Q.compile_query(SCHEMA, "family_relation:brother", con=missing,
                            extensions=_afl())
        except Q.QuerySyntaxError as exc:
            assert "family relationship data is not loaded" in str(exc)
        else:
            raise AssertionError("missing family tables should be rejected")
    finally:
        missing.close()

    print("family search regression tests: passed")


if __name__ == "__main__":
    run()
