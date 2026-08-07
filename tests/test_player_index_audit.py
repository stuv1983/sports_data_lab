import sqlite3

import pandas as pd

from afl.build_db import repair_missing_player_ids
from afl.player_index_audit import (
    audit, parse_index, player_index_available, player_index_count,
)

HTML = """<html><h1>All Players - A</h1>
<a href="players/G/Gary_Ablett0.html">Ablett, Gary</a>
<a href="https://afltables.com/afl/stats/players/G/Gary_Ablett1.html">Ablett, Gary</a>
</html>"""


def database():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE players(player_id INTEGER, player TEXT)")
    con.executemany("INSERT INTO players VALUES (?,?)", [
        (1, "Gary Ablett"), (2, "Gary Ablett")])
    return con


def test_parse_index_keeps_namesakes_separate_by_url():
    rows = parse_index(HTML, "A")
    assert len(rows) == 2
    assert rows[0].profile_key != rows[1].profile_key


def test_audit_links_index_profiles_to_database_ids():
    con = database()
    rows = parse_index(HTML, "A")
    counts = audit(con, rows, [
        (1, "Gary Ablett", rows[0].profile_url),
        (2, "Gary Ablett", rows[1].profile_url),
    ])
    assert counts == {"matched": 2}
    assert player_index_available(con)
    assert player_index_count(con) == 2


def test_missing_fitzroy_ids_are_recovered_from_profile_url():
    url = "https://afltables.com/afl/stats/players/B/Billy_Wilson2.html"
    frame = pd.DataFrame({
        "url": [url], "ID": [None], "Player": ["Billy Wilson"],
        "First.name": ["Billy"], "Surname": ["Wilson"],
    })
    fixed = repair_missing_player_ids(frame)
    assert int(fixed.loc[0, "ID"]) == 13244
