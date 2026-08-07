"""Normalization and conservative linking for NBA/NFL Wikipedia awards."""

import csv
import sqlite3

from utils.shared import load_wiki_awards as W


def _csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


def _db(path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE players (
          player_id INTEGER, player TEXT, debut_season INTEGER,
          final_season INTEGER
        );
        INSERT INTO players VALUES
          (1, 'Nikola Jokic', 2015, 2030),
          (2, 'Shared Name', 1980, 1990),
          (3, 'Shared Name', 2010, 2020);
    """)
    con.commit()
    con.close()


def test_nba_season_labels_and_accented_names_link(tmp_path):
    _csv(tmp_path / "nba" / "awards" / "mvp.csv", [{
        "Season": "2023–24", "Player": "Nikola Jokić", "Position": "C",
        "Nationality": "Serbia", "Team": "Denver Nuggets",
    }])
    db = tmp_path / "nba.db"
    _db(db)
    counts = W.load(db, "nba", tmp_path, verbose=False)
    assert counts == {"resolved": 1}
    con = sqlite3.connect(db)
    assert con.execute(
        "SELECT award_key, season, player_id FROM wiki_awards"
    ).fetchone() == ("nba_mvp", 2023, 1)


def test_award_season_disambiguates_a_shared_name(tmp_path):
    _csv(tmp_path / "nfl" / "awards" / "mvp.csv", [{
        "Season": "2015", "Player": "Shared Name", "Position": "QB",
        "Team": "Example", "Votes[24]": "",
    }])
    db = tmp_path / "nfl.db"
    _db(db)
    W.load(db, "nfl", tmp_path, verbose=False)
    con = sqlite3.connect(db)
    assert con.execute(
        "SELECT player_id, match_status, candidate_count FROM wiki_awards"
    ).fetchone() == (3, "unique", 1)


def test_coaches_are_preserved_but_never_linked_as_players(tmp_path):
    _csv(tmp_path / "nfl" / "awards" / "coach_of_year.csv", [{
        "Season": "2015", "Coach": "Shared Name", "Team": "Example",
        "Record": "12-4",
    }])
    db = tmp_path / "nfl.db"
    _db(db)
    W.load(db, "nfl", tmp_path, verbose=False)
    con = sqlite3.connect(db)
    assert con.execute(
        "SELECT recipient_type, player_id, match_status FROM wiki_awards"
    ).fetchone() == ("coach", None, "not_player")
