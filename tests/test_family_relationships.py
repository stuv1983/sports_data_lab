#!/usr/bin/env python3
"""Regression tests for the broad Wikipedia family relationship layer."""

from __future__ import annotations

# Run standalone from anywhere: the project root is one level up.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import csv
import sqlite3
import tempfile
from pathlib import Path

import family_relationships as F
import load_family_relationships as L
import scrape_wikipedia_families as S


FIXTURE_HTML = """
<div class="mw-heading mw-heading2"><h2><span class="mw-headline">A</span></h2></div>
<div class="mw-heading mw-heading3"><h3><span class="mw-headline">Abbott</span></h3></div>
<ul>
  <li><a href="/wiki/Clarence_Abbott">Clarence Abbott</a> (<a href="/wiki/Collingwood_Football_Club">Collingwood</a>)</li>
  <li><a href="/wiki/Les_Abbott">Les Abbott</a> (<a href="/wiki/Melbourne_Football_Club">Melbourne</a>)</li>
</ul>
<p>Clarence and Les were brothers.</p>
<div class="mw-heading mw-heading3"><h3><span class="mw-headline">Aish</span></h3></div>
<ul>
  <li><a href="/wiki/Andrew_Aish">Andrew Aish</a> (Norwood)
    <ul><li>Son: <a href="/wiki/James_Aish">James Aish</a> (Brisbane Lions, Collingwood, Fremantle)</li></ul>
  </li>
</ul>
<p>Andrew is James's father.</p>
<div class="mw-heading mw-heading3"><h3><span class="mw-headline">Archer</span></h3></div>
<ul>
  <li><a href="/wiki/Glenn_Archer">Glenn Archer</a></li>
  <li><a href="/wiki/Jackson_Archer">Jackson Archer</a></li>
</ul>
<p>Glenn is the father of Jackson.</p>
<div class="mw-heading mw-heading3"><h3><span class="mw-headline">Antonio</span></h3></div>
<ul>
  <li><a href="/wiki/Ebony_Antonio">Ebony Antonio</a></li>
  <li><a href="/wiki/Kara_Antonio">Kara Antonio</a></li>
</ul>
<p>Ebony and Kara are married.</p>
<div class="mw-heading mw-heading3"><h3><span class="mw-headline">Shared club links</span></h3></div>
<ul>
  <li>Alpha Example (<a href="/wiki/Norwood_Football_Club">Norwood</a>)</li>
  <li>Beta Example (<a href="/wiki/Norwood_Football_Club">Norwood</a>)</li>
</ul>
<p>Alpha and Beta were brothers.</p>
<div class="mw-heading mw-heading3"><h3><span class="mw-headline">Ablett</span></h3></div>
<ul>
  <li><a href="/wiki/Gary_Ablett_Sr.">Gary Ablett Sr.</a> (Hawthorn, Geelong)
    <ul>
      <li>Son: <a href="/wiki/Gary_Ablett_Jr.">Gary Ablett Jr.</a> (Geelong, Gold Coast)</li>
      <li>Son: <a href="/wiki/Nathan_Ablett">Nathan Ablett</a> (Geelong)</li>
      <li>Cousin: <a href="/wiki/Shane_Tuck">Shane Tuck</a> (Richmond)</li>
    </ul>
  </li>
</ul>
<p>Gary is the father of Gary Junior and Nathan.</p>
<div class="mw-heading mw-heading3"><h3><span class="mw-headline">Alias collision</span></h3></div>
<ul>
  <li><a href="/wiki/Alex_Example_Sr.">Alex Example Sr.</a></li>
  <li><a href="/wiki/Alex_Example_Jr.">Alex Example Jr.</a></li>
</ul>
<p>Alex is Alex Example's father.</p>
<div class="mw-heading mw-heading2"><h2><span class="mw-headline">Sources</span></h2></div>
"""


def payload():
    return {
        "parse": {
            "title": S.PAGE_TITLE,
            "displaytitle": S.PAGE_TITLE,
            "revid": 123456,
            "text": FIXTURE_HTML,
        }
    }


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def build_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("""
        CREATE TABLE players (
            player_id INTEGER PRIMARY KEY,
            player TEXT,
            debut_season INTEGER,
            final_season INTEGER,
            career_games INTEGER,
            clubs_hist TEXT,
            clubs_now TEXT
        )
    """)
    players = [
        (1, "Clarence Abbott", 1905, 1912, 90, "Collingwood", "Collingwood"),
        (2, "Les Abbott", 1904, 1912, 50, "Melbourne", "Melbourne"),
        (3, "James Aish", 2014, 2026, 250, "Brisbane Lions|Collingwood|Fremantle", "Brisbane Lions|Collingwood|Fremantle"),
        (4, "Glenn Archer", 1992, 2007, 311, "North Melbourne", "North Melbourne"),
        (5, "Jackson Archer", 2022, 2026, 60, "North Melbourne", "North Melbourne"),
        (6, "Gary Ablett", 1982, 1997, 248, "Hawthorn|Geelong", "Hawthorn|Geelong"),
        (7, "Gary Ablett", 2002, 2020, 357, "Geelong|Gold Coast", "Geelong|Gold Coast"),
        (8, "Nathan Ablett", 2005, 2007, 32, "Geelong", "Geelong"),
        (9, "Shane Tuck", 2004, 2013, 173, "Richmond", "Richmond"),
    ]
    con.executemany("INSERT INTO players VALUES (?,?,?,?,?,?,?)", players)
    con.execute("""
        CREATE TABLE games (
            player_id INTEGER,
            club_now TEXT,
            club_hist TEXT
        )
    """)
    for pid, _, _, _, games, hist, current in players:
        clubs = sorted(set((hist + "|" + current).split("|")))
        for club in clubs:
            # One row is enough for membership evidence in this fixture.
            con.execute("INSERT INTO games VALUES (?,?,?)", (pid, club, club))
    con.commit()
    return con


def scalar_ids(con: sqlite3.Connection, constraint) -> set[int]:
    sql, params = constraint
    return {int(row[0]) for row in con.execute(sql, params)}


def run() -> None:
    members, relationships, info = S.parse_payload(
        payload(), "2026-08-02T00:00:00+00:00"
    )
    assert info["families"] == 7, info
    assert len(members) == 16, len(members)
    member_ids = [row["source_member_id"] for row in members]
    assert len(member_ids) == len(set(member_ids)), member_ids
    shared = [row for row in members if row["family_name"] == "Shared club links"]
    assert len(shared) == 2
    assert all(not row["member_wikipedia_url"] for row in shared), shared
    assert any(
        row["relationship_type"] == "sibling"
        and {row["person_a_name"], row["person_b_name"]}
        == {"Clarence Abbott", "Les Abbott"}
        for row in relationships
    )
    assert any(
        row["relationship_type"] == "parent_child"
        and row["person_a_name"] == "Andrew Aish"
        and row["person_b_name"] == "James Aish"
        and row["extraction_method"] == "list_label"
        for row in relationships
    )
    assert any(
        row["relationship_type"] == "parent_child"
        and row["person_a_name"] == "Glenn Archer"
        and row["person_b_name"] == "Jackson Archer"
        and row["extraction_method"] == "prose_rule"
        for row in relationships
    )
    assert any(row["relationship_type"] == "spouse" for row in relationships)
    assert any(row["relationship_type"] == "cousin" for row in relationships)
    assert all(
        row["person_a_source_member_id"] != row["person_b_source_member_id"]
        for row in relationships
    ), relationships
    # The deliberately ambiguous prose above tokenises both ``Alex`` and the
    # prefix of ``Alex Example`` to the senior member. It must be ignored, not
    # emitted as a self relationship.
    assert not any(
        row["family_name"] == "Alias collision" for row in relationships
    ), relationships

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        member_csv = root / "members.csv"
        relationship_csv = root / "relationships.csv"
        db = root / "afl.db"
        write_csv(member_csv, members, S.MEMBER_COLUMNS)
        write_csv(relationship_csv, relationships, S.RELATIONSHIP_COLUMNS)
        con = build_db(db)
        con.close()

        linked, loaded_relationships = L.load(
            db,
            [member_csv],
            [relationship_csv],
            show_report=False,
        )
        by_name = {row["member_name"]: row for row in linked}
        assert by_name["Andrew Aish"]["match_status"] == "out_of_scope"
        assert by_name["James Aish"]["player_id"] == 3
        assert by_name["Gary Ablett Sr"]["player_id"] == 6
        assert by_name["Gary Ablett Jr"]["player_id"] == 7
        assert by_name["Ebony Antonio"]["match_status"] == "unmatched"
        assert loaded_relationships

        con = sqlite3.connect(db)
        try:
            assert F.family_relationships_available(con)
            assert {1, 2} <= scalar_ids(con, F.sibling_also_played())
            assert {4, 5, 6, 7, 8} <= scalar_ids(
                con, F.parent_or_child_also_played()
            )
            assert {6, 7, 8} <= scalar_ids(con, F.father_or_son_also_played())
            assert {6, 9} <= scalar_ids(con, F.extended_family_also_played())
            assert scalar_ids(con, F.same_listed_family_as(7)) == {6, 8, 9}
            assert 7 in scalar_ids(con, F.relative_played_for("Hawthorn"))
            assert F.family_member_count(con) >= 8
            assert F.trusted_relationship_count(con) >= 5
        finally:
            con.close()

    # Missing optional tables must not crash the application's read-only
    # connection. SQLite query_only rejects CREATE TEMP TABLE writes.
    with tempfile.TemporaryDirectory() as temp:
        db = Path(temp) / "readonly.db"
        writable = sqlite3.connect(db)
        writable.execute("CREATE TABLE players (player_id INTEGER)")
        writable.commit()
        writable.close()
        readonly = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        readonly.execute("PRAGMA query_only = ON")
        try:
            F.ensure_family_relationship_tables(readonly)
            assert not F.family_relationships_available(readonly)
        finally:
            readonly.close()

    print("family relationship regression tests: passed")


if __name__ == "__main__":
    run()
