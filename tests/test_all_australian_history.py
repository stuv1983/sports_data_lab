import csv
import sqlite3
from contextlib import closing

from afl import awards, constraints as C
from utils.afl import load_all_australian_history as loader


def test_official_history_resolves_by_season_club_and_known_source_aliases(
        tmp_path):
    db = tmp_path / "afl.db"
    source = tmp_path / "history.csv"
    with closing(sqlite3.connect(db)) as con:
        con.execute("""CREATE TABLE players (
            player_id INTEGER, player TEXT, debut_season INTEGER,
            final_season INTEGER, clubs_hist TEXT, clubs_now TEXT)""")
        con.executemany("INSERT INTO players VALUES (?,?,?,?,?,?)", [
            (1, "Nick Smith", 2008, 2018, "Sydney", "Sydney"),
            (2, "Gary Ablett", 1982, 1996, "Geelong", "Geelong"),
            (3, "Gary Ablett", 2002, 2020, "Geelong|Gold Coast", "Geelong|Gold Coast"),
            (4, "Tom Lynch", 2010, 2021, "Adelaide", "Adelaide"),
            (5, "Tom Lynch", 2011, 2026, "Gold Coast|Richmond", "Gold Coast|Richmond"),
            (6, "Isaac Heeney", 2014, 2026, "Sydney", "Sydney"),
        ])
        con.commit()
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("Year", "Player", "Club"))
        writer.writerows([
            (2014, "Nick Smith", "Sydney Swans"),
            (1995, "Gary Ablett snr", "Geelong"),
            (2014, "Gary Ablett jnr", "Gold Coast Suns"),
            (2018, "Tom Lynch", "Richmond"),
            (2022, "Issac Heeney", "Sydney Swans"),
        ])

    counts = loader.load(db, source, verbose=False)

    assert counts == {"resolved": 5}
    with closing(sqlite3.connect(db)) as con:
        rows = con.execute(
            "SELECT player_source, player_id FROM all_australian_history "
            "ORDER BY season, player_source"
        ).fetchall()
    assert {player_id for _, player_id in rows} == {1, 2, 3, 5, 6}


def test_three_grand_finals_counts_replay_matches_not_distinct_seasons():
    with closing(sqlite3.connect(":memory:")) as con:
        con.execute("CREATE TABLE games (player_id INTEGER, season INTEGER, round TEXT)")
        con.executemany("INSERT INTO games VALUES (?,?,?)", [
            (1, 1977, "GF"), (1, 1977, "GF"), (1, 1978, "GF"),
            (2, 1977, "GF"), (2, 1978, "GF"),
        ])
        sql, params = C.grand_finals_played_min(3)
        assert con.execute(sql, params).fetchall() == [(1,)]


def test_missing_history_layer_is_safe_on_app_read_only_connection(tmp_path):
    db = tmp_path / "legacy.db"
    with closing(sqlite3.connect(db)) as con:
        con.execute("CREATE TABLE all_australian (dg_person_id INTEGER, season INTEGER)")
        con.execute(
            "CREATE TABLE person_links (dg_person_id INTEGER, player_id INTEGER, "
            "match_status TEXT)"
        )
        con.execute("INSERT INTO all_australian VALUES (10, 2024)")
        con.execute("INSERT INTO person_links VALUES (10, 7, 'unique')")
        con.commit()

    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as con:
        con.execute("PRAGMA query_only = ON")
        C.ensure_all_australian_history_table(con)

        assert con.execute("PRAGMA query_only").fetchone() == (1,)
        assert con.execute(
            "SELECT name FROM temp.sqlite_master "
            "WHERE type='table' AND name='all_australian_history'"
        ).fetchone() == ("all_australian_history",)
        sql, params = awards.all_australian(1)
        assert con.execute(sql, params).fetchall() == [(7,)]
