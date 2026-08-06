import sqlite3

from afl.load_match_scores import load, parse_biglist


TEXT = """All games in chronological order
1.     8-May-1897       R1   Fitzroy           6.13.49          Carlton           2.4.16            Brunswick St
2.     15-May-1897      R2   GW Sydney         5.5.35           Sydney            5.5.35            S.C.G.
"""


def database():
    con = sqlite3.connect(":memory:")
    con.execute("""CREATE TABLE matches (
        match_id INTEGER, match_key TEXT, season INTEGER, round TEXT,
        match_date TEXT, venue TEXT, home_team TEXT, away_team TEXT,
        home_team_now TEXT, away_team_now TEXT, home_score REAL,
        away_score REAL, winner TEXT, margin REAL, is_final INTEGER,
        home_away_known INTEGER, home_players INTEGER, away_players INTEGER,
        attendance TEXT, home_q1 TEXT, home_q2 TEXT, home_q3 TEXT, home_q4 TEXT,
        away_q1 TEXT, away_q2 TEXT, away_q3 TEXT, away_q4 TEXT
    )""")
    con.execute("""INSERT INTO matches VALUES (
        1,'key',1897,'1','1897-05-08','Brunswick St','Fitzroy','Carlton',
        'Brisbane Lions','Carlton',49,16,'Fitzroy',33,0,1,20,20,NULL,
        NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL)""")
    return con


def test_parser_validates_scores_and_normalises_names():
    rows = parse_biglist(TEXT)
    assert rows[0].home_score == 49
    assert rows[1].home_team == "Greater Western Sydney"
    assert rows[1].round == "2"


def test_loader_audits_and_appends_score_only_matches():
    con = database()
    counts = load(con, parse_biglist(TEXT))
    assert counts == {
        "matched": 1, "score_only": 1,
        "missing_from_db": 0, "identity_mismatch": 0,
    }
    assert con.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 2
    assert tuple(con.execute(
        "SELECT game_status,data_status,home_team_now FROM matches WHERE match_id=2"
    ).fetchone()) == ("played", "score_only", "GWS")
    assert dict(con.execute(
        "SELECT audit_status,COUNT(*) FROM afltables_match_scores GROUP BY 1"
    )) == {"matched": 1, "score_only": 1}


def test_audit_only_does_not_fill_the_gap():
    con = database()
    counts = load(con, parse_biglist(TEXT), append_missing=False)
    assert counts["missing_from_db"] == 1
    assert con.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 1
