#!/usr/bin/env python3
"""Hand-entered Rising Star nominations, suspensions and vote counts."""

from __future__ import annotations

import sqlite3

import pytest

from utils.afl import load_rising_star as L
from utils.afl import rising_star_manual as M


def _db(path, players=(), games=()):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE players (
      player_id INTEGER PRIMARY KEY, player TEXT, name_key TEXT,
      debut_season INTEGER, final_season INTEGER, career_games INTEGER,
      clubs_now TEXT, clubs_hist TEXT
    );
    CREATE TABLE games (
      player_id INTEGER, season INTEGER, club_now TEXT, club_hist TEXT
    );
    """)
    con.executemany(
        "INSERT INTO players VALUES (?,?,?,?,?,?,?,?)", players)
    con.executemany("INSERT INTO games VALUES (?,?,?,?)", games)
    con.commit()
    con.close()


PLAYERS = [
    (1, "Ty Gallop", "ty gallop", 2025, 2026, 23, "Brisbane Lions", "Brisbane Lions"),
    (2, "Jacob Farrow", "jacob farrow", 2026, 2026, 12, "Essendon", "Essendon"),
    (3, "Bailey Williams", "bailey williams", 2016, 2026, 189,
     "Western Bulldogs", "Western Bulldogs"),
    (4, "Bailey Williams", "bailey williams", 2020, 2026, 105,
     "West Coast", "West Coast"),
]
GAMES = [
    (1, 2026, "Brisbane Lions", "Brisbane Lions"),
    (2, 2026, "Essendon", "Essendon"),
    (3, 2026, "Western Bulldogs", "Western Bulldogs"),
    (4, 2026, "West Coast", "West Coast"),
]


def _published(round_number, player, club, **extra):
    """A row as FootyWire would publish it, statistics and all."""
    row = {
        "source_key": f"fw-{round_number}", "season": 2026,
        "round_number": round_number, "nomination_round": str(round_number),
        "player": player, "name_key": L.normalise_name(player), "club": club,
        "source": "footywire", "disposals": 22, "ineligible": 0,
        "source_url": "https://www.footywire.com/", "player_display": player,
    }
    row.update(extra)
    return row


def _admin(entry: dict) -> dict:
    """One row of the manual CSV, as read_rows would hand it over."""
    row = {field: entry.get(field, "") for field in L.SOURCE_FIELDS}
    row["season"] = int(entry["season"])
    row["round_number"] = (int(entry["round_number"])
                           if str(entry.get("round_number") or "").strip()
                           else None)
    row["ineligible"] = int(entry.get("ineligible") or 0)
    row["votes"] = int(entry["votes"]) if entry.get("votes") else None
    row["is_season_winner"] = int(entry.get("is_season_winner") or 0)
    return row


# --------------------------------------------------------- the edit file

def test_one_entry_per_player_per_season_however_it_is_edited(tmp_path):
    """Nominating, suspending and vote-counting are facts about one row.

    Keyed on the round instead, a suspension entered without a round would
    become a second, contentless nomination.
    """
    path = tmp_path / "manual.csv"
    M.upsert(2026, "Ty Gallop", club="Brisbane Lions", round_number=16,
             path=path)
    M.upsert(2026, "Ty Gallop", ineligible=True, path=path)
    M.upsert(2026, "Ty Gallop", votes=12, path=path)

    entries = M.read_entries(path)
    assert len(entries) == 1
    assert entries[0]["round_number"] == "16"
    assert entries[0]["ineligible"] == "1"
    assert entries[0]["votes"] == "12"


def test_an_amendment_does_not_blank_what_it_did_not_mention(tmp_path):
    path = tmp_path / "manual.csv"
    M.upsert(2026, "Ty Gallop", club="Brisbane Lions", round_number=16,
             path=path)
    M.upsert(2026, "Ty Gallop", ineligible=True, path=path)

    entry = M.read_entries(path)[0]
    assert entry["club"] == "Brisbane Lions"
    assert entry["nomination_round"] == "16"


def test_the_same_player_in_two_seasons_is_two_entries(tmp_path):
    path = tmp_path / "manual.csv"
    M.upsert(2025, "Ty Gallop", round_number=1, path=path)
    M.upsert(2026, "Ty Gallop", round_number=16, path=path)
    assert len(M.read_entries(path)) == 2


def test_an_edit_records_who_made_it(tmp_path):
    path = tmp_path / "manual.csv"
    M.upsert(2026, "Ty Gallop", ineligible=True,
             edited_by="admin@example.com", path=path)
    entry = M.read_entries(path)[0]
    assert entry["edited_by"] == "admin@example.com"
    assert entry["edited_at"]


def test_a_name_that_normalises_to_nothing_is_refused(tmp_path):
    with pytest.raises(ValueError):
        M.upsert(2026, "   ", path=tmp_path / "manual.csv")


def test_an_edit_can_be_undone(tmp_path):
    path = tmp_path / "manual.csv"
    entry = M.upsert(2026, "Ty Gallop", ineligible=True, path=path)
    assert M.remove(entry["source_key"], path) is True
    assert M.read_entries(path) == []
    assert M.remove("nonexistent", path) is False


# ------------------------------------------------------------- searching

def test_search_shows_enough_to_tell_two_players_apart(tmp_path):
    """A name is not an identity: two Bailey Williamses played in 2026."""
    db = tmp_path / "afl.db"
    _db(db, PLAYERS, GAMES)
    con = sqlite3.connect(db)
    try:
        labels = [row["label"] for row in M.search_players(con, "Bailey")]
    finally:
        con.close()
    assert labels == [
        "Bailey Williams (2016-2026, 189 games, Western Bulldogs)",
        "Bailey Williams (2020-2026, 105 games, West Coast)",
    ]


def test_a_typed_wildcard_is_a_character_not_a_pattern(tmp_path):
    """SQLite's LIKE reads % and _ as wildcards, so before they were
    escaped "B_iley" matched Bailey ("_" swallowed the "a") and "%%"
    matched every player in the table."""
    db = tmp_path / "afl.db"
    _db(db, PLAYERS, GAMES)
    con = sqlite3.connect(db)
    try:
        assert M.search_players(con, "%%") == []
        assert M.search_players(con, "B_iley") == []
    finally:
        con.close()


def test_search_needs_something_to_search_for(tmp_path):
    db = tmp_path / "afl.db"
    _db(db, PLAYERS, GAMES)
    con = sqlite3.connect(db)
    try:
        assert M.search_players(con, "a") == []
        assert M.search_players(con, "") == []
    finally:
        con.close()


# ------------------------------------------- how an edit reaches the table

def test_a_suspension_annotates_the_published_row_and_keeps_its_statistics(
        tmp_path):
    """The admin source ranks last so an annotation loses the row.

    Ranked highest it would win the round and throw away the match
    statistics that are the whole reason FootyWire is preferred.
    """
    db = tmp_path / "afl.db"
    _db(db, PLAYERS, GAMES)
    path = tmp_path / "manual.csv"
    M.upsert(2026, "Ty Gallop", club="Brisbane Lions", ineligible=True,
             path=path)

    kept = L.preferred_rows([
        _published(16, "Ty Gallop", "Brisbane Lions"),
        _admin(M.read_entries(path)[0]),
    ])

    assert len(kept) == 1
    assert kept[0]["source"] == "footywire"
    assert kept[0]["disposals"] == 22
    assert kept[0]["ineligible"] == 1
    assert kept[0]["ineligible_reason"] == M.DEFAULT_REASON


def test_a_nomination_nobody_published_stands_on_its_own(tmp_path):
    path = tmp_path / "manual.csv"
    M.upsert(2026, "Jacob Farrow", club="Essendon", round_number=25,
             path=path)

    kept = L.preferred_rows([
        _published(16, "Ty Gallop", "Brisbane Lions"),
        _admin(M.read_entries(path)[0]),
    ])

    assert sorted(row["source"] for row in kept) == ["admin", "footywire"]
    assert next(row for row in kept if row["source"] == "admin")[
        "round_number"] == 25


def test_votes_and_a_winner_merge_onto_the_published_nomination(tmp_path):
    path = tmp_path / "manual.csv"
    M.upsert(2025, "Ty Gallop", club="Brisbane Lions", votes=45, winner=True,
             path=path)
    entry = _admin(M.read_entries(path)[0])
    entry["season"] = 2025

    published = _published(6, "Ty Gallop", "Brisbane Lions")
    published["season"] = 2025
    kept = L.preferred_rows([published, entry])

    assert len(kept) == 1
    assert kept[0]["votes"] == 45
    assert kept[0]["is_season_winner"] == 1


def test_an_annotation_that_fits_two_nominees_is_refused_not_guessed(
        tmp_path, capsys):
    """Both Bailey Williamses nominated in one season.

    Applying to both would mark a real but different person as suspended,
    so an annotation that does not resolve to exactly one row is dropped --
    the same rule the reviewed name overrides follow.
    """
    path = tmp_path / "manual.csv"
    M.upsert(2026, "Bailey Williams", ineligible=True, path=path)

    kept = L.preferred_rows([
        _published(3, "Bailey Williams", "Western Bulldogs"),
        _published(9, "Bailey Williams", "West Coast"),
        _admin(M.read_entries(path)[0]),
    ])

    assert len(kept) == 2
    assert not any(row["ineligible"] for row in kept)
    assert "annotation not applied" in capsys.readouterr().err


def test_naming_the_club_resolves_which_nominee_was_suspended(tmp_path):
    path = tmp_path / "manual.csv"
    M.upsert(2026, "Bailey Williams", club="West Coast", ineligible=True,
             path=path)

    kept = L.preferred_rows([
        _published(3, "Bailey Williams", "Western Bulldogs"),
        _published(9, "Bailey Williams", "West Coast"),
        _admin(M.read_entries(path)[0]),
    ])

    assert len(kept) == 2
    suspended = [row for row in kept if row["ineligible"]]
    assert [row["club"] for row in suspended] == ["West Coast"]


def test_an_edit_survives_the_table_being_rebuilt_from_its_sources(tmp_path):
    """The point of a file rather than an UPDATE.

    load_sources drops and rebuilds the table on every run, so an edit
    written into the table would last until the next Monday scan.
    """
    db = tmp_path / "afl.db"
    _db(db, PLAYERS, GAMES)
    published = tmp_path / "footywire.csv"
    L_fields = L.SOURCE_FIELDS
    import csv

    with published.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=L_fields,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerow(_published(16, "Ty Gallop", "Brisbane Lions"))

    path = tmp_path / "manual.csv"
    M.upsert(2026, "Ty Gallop", club="Brisbane Lions", ineligible=True,
             path=path)

    for _ in range(2):  # a second load must not lose the edit
        L.load_sources(db, [published, path], verbose=False)

    con = sqlite3.connect(db)
    try:
        assert con.execute(
            "SELECT player, ineligible, disposals, source "
            "FROM rising_star_nominees").fetchall() == [
                ("Ty Gallop", 1, 22, "footywire")]
    finally:
        con.close()


def main() -> None:
    pytest.main([__file__, "-q"])


if __name__ == "__main__":
    main()
