"""A tiny deterministic sport for headless query-builder tests.

The state and restore tests must not depend on the multi-hundred-MB
production databases: this builds a few-row SQLite file in the system
temp directory and duck-types just enough of a Sport for
query_builder.page() -- including a staging table that must NOT appear
in the builder, and a TEXT-typed date column whose kind the sport
overrides, so the allowlist and type-metadata walls are testable.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import core
from registry import Vocab

DB_PATH = str(Path(tempfile.gettempdir()) / "sdl_synthetic_query_builder.db")


class _Constraints:
    """A builder catalogue shaped exactly like a sport's BUILDERS."""

    BUILDERS = {
        "Played for club": (
            lambda club: ("SELECT player_id FROM games "
                          "WHERE club_now = ?", [club]), ["club"]),
        "150+ / X+ career games": (
            lambda games: ("SELECT player_id FROM players "
                           "WHERE career_games >= ?", [games]), ["games"]),
    }
    BUILDER_GROUPS = {
        "Clubs & journeys": ("Played for club",),
        "Career milestones": ("150+ / X+ career games",),
    }

    @staticmethod
    def count(con, constraints):
        schema = make_sport().schema
        return core.count(con, constraints, schema)


class SyntheticSport:
    key = "syn"
    label = "Synthetic"
    C = _Constraints()
    vocab = Vocab()
    schema = core.Schema(
        career_score="career_goals", career_postseason="finals_played",
        game_score="goals", clubs=("A", "B"),
        required_games_cols=(), required_player_cols=(),
    )
    query_tables = ("players", "games")
    query_column_kinds = {"games.date": "date", "games.is_final": "boolean"}
    search_examples = ()

    def __init__(self, db: str = DB_PATH):
        self.db = str(db)

    def k(self, *parts):
        return ":".join((self.key,) + tuple(str(p) for p in parts))


def make_sport(db: str = DB_PATH) -> SyntheticSport:
    return SyntheticSport(db)


def build_db(path: str = DB_PATH) -> str:
    if Path(path).exists():
        return path
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE players (
          player_id INTEGER, player TEXT, debut_season INTEGER,
          final_season INTEGER, career_games INTEGER,
          career_goals INTEGER, finals_played INTEGER, clubs_hist TEXT,
          obscurity REAL
        );
        CREATE TABLE games (
          player_id INTEGER, club_now TEXT, date TEXT, goals INTEGER,
          is_final INTEGER
        );
        CREATE TABLE secret_staging (payload TEXT);
        INSERT INTO players VALUES
          (1,'Alpha',1990,2000,200,300,10,'A',80.0),
          (2,'Beta',2001,2005,50,20,0,'B',40.0),
          (3,'Gamma',1995,2010,120,150,5,'B',60.0);
        INSERT INTO games VALUES
          (1,'A','2020-06-01',3,0), (2,'B','2020-06-15',1,1),
          (3,'B','2020-06-30',2,0);
        INSERT INTO secret_staging VALUES ('should never be queryable');
    """)
    con.commit()
    con.close()
    return path
