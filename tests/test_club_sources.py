#!/usr/bin/env python3
"""Focused regression tests for the AFL club source utilities."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.afl.club_sources import (CLUBS, CLUB_BY_ID, fallback_name_key,
                                    source_name_to_display)
from utils.afl.fetch_club_sources import validate_afltables
from utils.afl.load_club_sources import (link_record, load, parse_all_time,
                                         parse_player_totals, parse_records,
                                         parse_wikipedia)

TOTALS_HTML = """
<html><h1>Adelaide Player Totals (1991-2026)</h1>
<div><table><thead><tr><th>Player</th><th>GM</th><th>KI</th><th>GL</th></tr></thead>
<tbody><tr><td><a href='/Andrew_McLeod.html'>McLeod, Andrew</a></td><td>340</td><td>4440</td><td>275</td></tr></tbody></table></div>
<div><table><thead><tr><th>Player</th><th>GM</th><th>KI</th><th>GL</th></tr></thead>
<tbody><tr><td><a href='/Andrew_McLeod.html'>McLeod, Andrew</a></td><td>340</td><td>13.06</td><td>0.81</td></tr></tbody></table></div></html>
"""
ALL_TIME_HTML = """
<html><h1>Adelaide - All Time Player List</h1><table><thead><tr>
<th>Cap</th><th>#</th><th>Player</th><th>DOB</th><th>HT</th><th>WT</th>
<th>Games (W-D-L)</th><th>Goals</th><th>Seasons</th><th>Debut</th><th>Last</th>
</tr></thead><tbody><tr><td>66</td><td>23</td><td><a href='/Andrew_McLeod.html'>McLeod, Andrew</a></td>
<td>1976-08-04</td><td>181cm</td><td>81kg</td><td>340 (185-0-155)</td><td>275</td>
<td>1995-2010</td><td>18y 274d</td><td>33y 346d</td></tr></tbody></table></html>
"""
RECORDS_HTML = """
<html><h1>Adelaide - Season and Game Records</h1>
<table><tr><th colspan="12">Most Disposals In A Season</th></tr><tbody><tr>
<th>Player</th><th>TM</th><th>#</th><th>GM</th><th>Ave.</th><th>Year</th>
<th>Player</th><th>TM</th><th>#</th><th>GM</th><th>Ave.</th><th>Year</th></tr>
<tr><td><a href="/Rory_Laird.html">Rory Laird</a></td><td>AD</td><td>737</td><td>22</td><td>33.50</td><td>2022</td>
<td>Andrew McLeod</td><td>AD</td><td>650</td><td>23</td><td>28.26</td><td>2001</td></tr></tbody></table>
<table><tr><th colspan="8">Most Goals In A Game</th></tr><tbody><tr>
<th>Player</th><th>TM</th><th>#</th><th>Match</th>
<th>Player</th><th>TM</th><th>#</th><th>Match</th></tr>
<tr><td>Modra, Tony</td><td>AD</td><td>13</td><td>1993 v RI</td>
<td>Walker, Taylor</td><td>AD</td><td>10</td><td>2015 v WC</td></tr>
</tbody></table></html>
"""
WIKI_JSON = {
    "parse": {
        "revid": 12345,
        "displaytitle": "Adelaide Football Club",
        "text": "<table class='infobox vcard'><tbody><tr><th>Full name</th><td>Adelaide Football Club</td></tr><tr><th>Nickname</th><td>Crows</td></tr></tbody></table>",
    }
}


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="windows-1252")


def test_fetch_validation_aliases() -> None:
    north = (
        b"<html><h1>Kangaroos - Season and Game Records</h1>"
        b"Most Disposals In A Season" + b"x" * 1200 + b"</html>"
    )
    bulldogs = (
        b"<html><h1>Footscray - Season and Game Records</h1>"
        b"Most Disposals In A Season" + b"x" * 1200 + b"</html>"
    )
    validate_afltables(north, "North Melbourne", "afltables_records")
    validate_afltables(
        bulldogs, "Western Bulldogs", "afltables_records"
    )


def test_manifest() -> None:
    assert len(CLUBS) == 18
    assert len({c.club_id for c in CLUBS}) == 18
    assert CLUB_BY_ID["western_bulldogs"].afltables_slug == "bullldogs"
    assert CLUB_BY_ID["brisbane_lions"].db_club_now == "Brisbane Lions"
    assert "brisbane_bears" not in CLUB_BY_ID
    assert "fitzroy" not in CLUB_BY_ID


def test_parsers(tmp: Path) -> None:
    totals_path = tmp / "totals.html"
    all_time_path = tmp / "alltime.html"
    records_path = tmp / "records.html"
    wiki_path = tmp / "wikipedia.json"
    write(totals_path, TOTALS_HTML)
    write(all_time_path, ALL_TIME_HTML)
    write(records_path, RECORDS_HTML)
    wiki_path.write_text(json.dumps(WIKI_JSON), encoding="utf-8")

    totals, averages = parse_player_totals(totals_path)
    assert totals[0]["player_name"] == "Andrew McLeod"
    assert totals[0]["games"] == 340 and totals[0]["kicks"] == 4440
    assert averages[0]["kicks"] == 13.06

    register = parse_all_time(all_time_path)
    assert register[0]["cap_number"] == 66
    assert (register[0]["wins"], register[0]["draws"], register[0]["losses"]) == (185, 0, 155)

    records = parse_records(records_path)
    assert len(records) == 4
    assert records[0]["scope"] == "season" and records[0]["stat"] == "disposals"
    assert records[0]["source_rank"] == 1 and records[1]["source_rank"] == 2
    assert records[2]["scope"] == "game" and records[2]["value"] == 13
    assert records[2]["season"] == 1993 and records[2]["opponent"] == "RI"

    revision, title, fields = parse_wikipedia(wiki_path)
    assert revision == 12345 and title == "Adelaide Football Club"
    assert {f["field_key"] for f in fields} == {"full_name", "nickname"}


def test_loader(tmp: Path) -> None:
    db = tmp / "test.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE players (
            player_id INTEGER PRIMARY KEY, player TEXT, name_key TEXT,
            dob TEXT, birth_year INTEGER, clubs_now TEXT
        );
        CREATE TABLE games (player_id INTEGER, club_now TEXT);
    """)
    con.execute("INSERT INTO players VALUES (1,'Andrew McLeod',?,?,?,?)",
                (fallback_name_key("Andrew McLeod"), "1976-08-04", 1976, "Adelaide"))
    con.execute("INSERT INTO games VALUES (1,'Adelaide')")
    con.commit(); con.close()

    raw = tmp / "raw"
    club = raw / "adelaide"
    write(club / "afltables_player_totals.html", TOTALS_HTML)
    write(club / "afltables_all_time_players.html", ALL_TIME_HTML)
    write(club / "afltables_records.html", RECORDS_HTML)
    (club / "wikipedia.json").write_text(json.dumps(WIKI_JSON), encoding="utf-8")

    result = load(db, raw, ["adelaide"], verbose=False)
    assert result["clubs"] == 1
    assert result["totals"] == 1 and result["register"] == 1
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM clubs").fetchone()[0] == 1
    assert con.execute("SELECT player_id, match_status FROM club_player_register").fetchone() == (1, "unique")
    assert con.execute("SELECT COUNT(*) FROM club_player_records").fetchone()[0] == 4
    con.close()



def test_era_linking() -> None:
    index = {
        "mauricerioli": [
            {"player_id": 10, "debut_season": 1982, "final_season": 1987},
            {"player_id": 20, "debut_season": 2012, "final_season": 2021},
        ]
    }
    older = {"player_name": "Maurice Rioli", "season": 1985}
    younger = {"player_name": "Maurice Rioli", "season": 2017}
    link_record(older, index)
    link_record(younger, index)
    assert older["player_id"] == 10 and older["match_status"] == "unique"
    assert younger["player_id"] == 20 and younger["match_status"] == "unique"


def test_real_saved_records_fixture() -> None:
    fixtures = [
        ROOT / "data" / "afl" / "raw" / "clubs" / "richmond" / "afltables_records.html",
        Path(r"/mnt/data/AFL Tables - Richmond - Player Season And Game Records (1965-2026).html"),
    ]
    fixture = next((path for path in fixtures if path.exists()), None)
    if fixture is None:
        return
    records = parse_records(fixture)
    assert records, f"no records parsed from {fixture}"
    assert any(row["scope"] == "season" for row in records)
    assert any(row["scope"] == "game" for row in records)


def test_cached_record_pages() -> None:
    raw = ROOT / "data" / "afl" / "raw" / "clubs"
    if not raw.exists():
        return
    pages = sorted(raw.glob("*/afltables_records.html"))
    if not pages:
        return
    failures = []
    for page in pages:
        try:
            records = parse_records(page)
            if not records:
                failures.append(f"{page.parent.name}: zero rows")
            elif not any(row["scope"] == "season" for row in records):
                failures.append(f"{page.parent.name}: no season rows")
            elif not any(row["scope"] == "game" for row in records):
                failures.append(f"{page.parent.name}: no game rows")
        except Exception as exc:
            failures.append(f"{page.parent.name}: {type(exc).__name__}: {exc}")
    assert not failures, "cached record parse failures: " + "; ".join(failures)

def main() -> None:
    test_manifest()
    test_fetch_validation_aliases()
    test_era_linking()
    test_real_saved_records_fixture()
    test_cached_record_pages()
    assert source_name_to_display("McLeod, Andrew") == "Andrew McLeod"
    with tempfile.TemporaryDirectory() as folder:
        tmp = Path(folder)
        test_parsers(tmp)
        test_loader(tmp)
    print("club source utility tests: passed")


if __name__ == "__main__":
    main()
