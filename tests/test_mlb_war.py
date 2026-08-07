"""WAR loading preserves team stints and two-way-player totals."""

import csv
import sqlite3

from utils.mlb import load_war


def _write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def test_load_war_maps_bbref_teams_and_does_not_duplicate_traded_seasons(tmp_path):
    _write(tmp_path / "war_daily_bat.txt",
           ["player_ID", "year_ID", "team_ID", "WAR"],
           [("playera", 2020, "AAA", 2.0),
            ("playera", 2020, "BBB", 1.0),
            ("bbrefb", 2020, "AAA", 4.0),
            ("bbrefb", 2021, "NEW", 1.0)])
    _write(tmp_path / "war_daily_pitch.txt",
           ["player_ID", "year_ID", "team_ID", "WAR"],
           [("playera", 2020, "AAA", 0.5)])
    _write(tmp_path / "People.csv", ["playerID", "bbrefID"],
           [("playerb", "bbrefb")])
    _write(tmp_path / "Teams.csv",
           ["yearID", "teamIDBR", "name"],
           [(2020, "AAA", "Club A"), (2020, "BBB", "Club B")])

    con = sqlite3.connect(":memory:")
    con.execute("""CREATE TABLE games (
        player_id TEXT, season INTEGER, club_hist TEXT,
        is_postseason INTEGER)""")
    con.execute("CREATE TABLE players (player_id TEXT PRIMARY KEY)")
    con.executemany("INSERT INTO players VALUES (?)",
                    [("playera",), ("playerb",)])
    con.executemany("INSERT INTO games VALUES (?,?,?,?)", [
        ("playera", 2020, "Club A", 0),
        ("playera", 2020, "Club B", 0),
        ("playera", 2020, "Club A", 1),
        ("playerb", 2020, "Club A", 0),
        # No Teams.csv mapping for NEW, but one club makes fallback exact.
        ("playerb", 2021, "Club C", 0),
    ])

    report = load_war.load(con, tmp_path)
    rows = con.execute(
        "SELECT player_id,season,club_hist,is_postseason,war FROM games "
        "ORDER BY player_id,season,club_hist,is_postseason"
    ).fetchall()
    assert rows == [
        ("playera", 2020, "Club A", 0, 2.5),
        ("playera", 2020, "Club A", 1, None),
        ("playera", 2020, "Club B", 0, 1.0),
        ("playerb", 2020, "Club A", 0, 4.0),
        ("playerb", 2021, "Club C", 0, 1.0),
    ]
    assert con.execute(
        "SELECT career_war FROM players WHERE player_id='playera'"
    ).fetchone()[0] == 3.5
    assert report == {
        "file_pairs": 3, "season_rows": 4, "players": 2, "unmatched": 0}
