#!/usr/bin/env python3
"""Offline tests for the weekly Wikipedia Rising Star refresh.

No network: every test drives the parser from fixture HTML shaped like the
article's rendered nominations table.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from afl import fetch_wikipedia_rising_star as W
from utils.afl import load_rising_star as L


def _row(round_number: int, player: str, club_slug: str, club_text: str,
         reference: int = 2) -> str:
    return (
        "<tr>"
        f'<td style="text-align: center;">{round_number}</td>'
        f'<td><span data-sort-value="x"><span class="vcard"><span class="fn">'
        f'<a href="/wiki/{player.replace(" ", "_")}" title="{player}">{player}'
        "</a></span></span></span></td>"
        f'<td><a href="/wiki/{club_slug}" title="{club_slug}">{club_text}</a></td>'
        f'<td><sup id="cite_ref-{reference}" class="reference">'
        f'<a href="#cite_note-{reference}">[{reference}]</a></sup></td>'
        "</tr>"
    )


HTML = (
    '<div class="mw-parser-output">'
    "<h2>Eligibility</h2>"
    '<table class="wikitable"><tr><th>Criterion</th><th>Detail</th></tr>'
    "<tr><td>Age</td><td>Under 21</td></tr></table>"
    "<h2>Nominations</h2>"
    '<table class="wikitable sortable" style="text-align:left">'
    "<tr><th>Round</th><th>Player</th><th>Club</th>"
    '<th class="unsortable">Ref.</th></tr>'
    + _row(0, "Leo Lombard", "Gold_Coast_Suns", "Gold Coast")
    + _row(1, "Jagga Smith", "Carlton_Football_Club", "Carlton")
    + _row(2, "Phoenix Gothard", "Greater_Western_Sydney_Giants",
           "Greater Western Sydney")
    # An eligibility footnote is marked with an asterisk beside the name.
    + '<tr><td style="text-align: center;">3</td>'
      '<td><span class="fn"><a href="/wiki/Ty_Gallop" title="Ty Gallop">'
      "Ty Gallop</a></span>*</td>"
      '<td><a href="/wiki/Brisbane_Lions" title="Brisbane Lions">Brisbane '
      "Lions</a></td><td></td></tr>"
    # A club cell with no link at all still has to resolve.
    + '<tr><td style="text-align: center;">4</td>'
      '<td><span class="fn">Jesse Dattoli</span></td>'
      "<td>Sydney</td><td></td></tr>"
    + "</table></div>"
)


def test_the_nominations_table_is_found_by_its_header_not_its_position():
    """The article carries other wikitables, and their order is not fixed."""
    rows = W.parse_page(HTML, 2026)
    assert [row["round_number"] for row in rows] == [0, 1, 2, 3, 4]
    assert rows[0]["player"] == "Leo Lombard"


def test_club_identity_comes_from_the_article_link_not_the_display_text():
    """Display text is editorial and drifts; the link target does not."""
    rows = W.parse_page(HTML, 2026)
    assert rows[2]["team_display"] == "Greater Western Sydney"
    assert rows[2]["club"] == "GWS"
    assert rows[0]["club"] == "Gold Coast"


def test_an_unlinked_club_cell_falls_back_to_its_display_name():
    rows = W.parse_page(HTML, 2026)
    assert rows[4]["club"] == "Sydney"


def test_footnote_markers_and_references_are_not_part_of_a_name():
    """A suspended nominee is marked with an asterisk beside the name."""
    rows = W.parse_page(HTML, 2026)
    assert rows[3]["player"] == "Ty Gallop"
    assert rows[3]["name_key"] == "ty gallop"
    assert "[2]" not in rows[0]["player"]


def test_missing_statistics_are_recorded_rather_than_written_as_zero():
    """Wikipedia publishes no match statistics, and nor does this.

    A zero here would read as a nominee who registered nothing, which is
    a different and false claim.
    """
    rows = W.parse_page(HTML, 2026)
    assert "disposals" in rows[0]["unavailable_stats"]
    assert "supercoach" in rows[0]["unavailable_stats"]
    assert rows[0].get("disposals") is None


def test_a_table_holding_no_nominations_is_an_error_not_an_empty_season():
    empty = (
        '<table class="wikitable"><tr><th>Round</th><th>Player</th>'
        "<th>Club</th></tr></table>"
    )
    with pytest.raises(ValueError):
        W.parse_page(empty, 2026)


def test_an_article_without_the_table_is_reported_clearly():
    with pytest.raises(ValueError, match="Round/Player/Club"):
        W.parse_page("<div><p>Nothing here yet.</p></div>", 2027)


# ------------------------------------------------ refreshing the CSV file

def test_a_week_with_no_new_nomination_does_not_rewrite_the_file(tmp_path):
    first = W.refresh_season(2026, tmp_path, html_text=HTML)
    assert first["changed"] is True
    assert first["added"] == 5
    written = Path(first["path"])
    stamp = written.stat().st_mtime_ns

    again = W.refresh_season(2026, tmp_path, html_text=HTML)
    assert again["changed"] is False
    assert again["added"] == 0
    assert written.stat().st_mtime_ns == stamp


def test_a_new_nomination_is_reported_by_name_and_club(tmp_path):
    W.refresh_season(2026, tmp_path, html_text=HTML)
    extended = HTML.replace(
        "</table></div>",
        _row(5, "Mitchell Edwards", "Geelong_Football_Club", "Geelong")
        + "</table></div>",
    )
    result = W.refresh_season(2026, tmp_path, html_text=extended)
    assert result["changed"] is True
    assert result["latest_round"] == 5
    assert result["new_nominations"] == [
        {"round": 5, "player": "Mitchell Edwards", "club": "Geelong"}
    ]


# ------------------------------------------ which source owns which round

def _source_row(season, round_number, player, club, source):
    return {
        "season": season, "round_number": round_number, "player": player,
        "name_key": L.normalise_name(player), "club": club, "source": source,
        "source_key": f"{source}-{season}-{round_number}",
    }


def test_footywire_owns_a_round_both_sources_publish():
    """One nomination, not two, and the richer source keeps it.

    Left unresolved, a Wikipedia row written in August and a FootyWire row
    saved by hand in September would both survive, and the same player
    would appear twice for the round with statistics on only one of them.
    """
    kept = L.preferred_rows([
        _source_row(2026, 20, "Talor Byrne", "Carlton", "wikipedia"),
        _source_row(2026, 20, "Talor Byrne", "Carlton", "footywire"),
        _source_row(2026, 21, "Mitchell Edwards", "Geelong", "wikipedia"),
    ])
    assert [(row["round_number"], row["source"]) for row in kept] == [
        (20, "footywire"), (21, "wikipedia"),
    ]


def test_the_same_round_in_a_different_season_is_a_different_nomination():
    kept = L.preferred_rows([
        _source_row(2025, 21, "Someone Else", "Hawthorn", "footywire"),
        _source_row(2026, 21, "Mitchell Edwards", "Geelong", "wikipedia"),
    ])
    assert len(kept) == 2


def test_two_rows_from_one_source_are_both_kept_for_the_health_check():
    """A source contradicting itself is a fault to surface, not to hide."""
    kept = L.preferred_rows([
        _source_row(2026, 21, "One Player", "Geelong", "footywire"),
        _source_row(2026, 21, "Another Player", "Sydney", "footywire"),
    ])
    assert len(kept) == 2


# ------------------------------------------------------ CSV -> SQLite link

def _build_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE players (
      player_id INTEGER PRIMARY KEY, player TEXT, name_key TEXT,
      debut_season INTEGER, final_season INTEGER, career_games INTEGER
    );
    CREATE TABLE games (
      player_id INTEGER, season INTEGER, club_now TEXT, club_hist TEXT
    );
    """)
    con.executemany("INSERT INTO players VALUES (?,?,?,?,?,?)", [
        (1, "Leo Lombard", "leo lombard", 2025, 2026, 20),
        (2, "Jagga Smith", "jagga smith", 2025, 2026, 18),
        (3, "Phoenix Gothard", "phoenix gothard", 2026, 2026, 12),
        (4, "Ty Gallop", "ty gallop", 2026, 2026, 9),
        (5, "Jesse Dattoli", "jesse dattoli", 2026, 2026, 6),
    ])
    con.executemany("INSERT INTO games VALUES (?,?,?,?)", [
        (1, 2026, "Gold Coast", "Gold Coast"),
        (2, 2026, "Carlton", "Carlton"),
        (3, 2026, "GWS", "GWS"),
        (4, 2026, "Brisbane Lions", "Brisbane Lions"),
        (5, 2026, "Sydney", "Sydney"),
    ])
    con.commit()
    con.close()


def test_a_wikipedia_row_links_to_a_player_and_is_trusted_by_the_solver(
        tmp_path):
    """Statistics are not what makes a nomination usable.

    Every builder in afl/rising_star.py filters on player_id, season and
    club, so a row carrying only those is as usable as one with a full
    stat line -- which is the whole reason this source is worth having.
    """
    from afl import rising_star as R

    source = tmp_path / "rising_star_nominees_2026.csv"
    W.write_csv(source, W.parse_page(HTML, 2026))
    db = tmp_path / "afl.db"
    _build_db(db)

    result = L.load_sources(db, [source], verbose=False)
    assert result["rows"] == 5
    assert result["trusted"] == 5
    assert result["sources"] == {"wikipedia": 5}

    con = sqlite3.connect(db)
    try:
        assert R.rising_star_available(con)
        sql, params = R.rising_star_nominee_for("GWS")
        assert con.execute(sql, params).fetchall() == [(3,)]
        assert con.execute(
            "SELECT source FROM rising_star_nominees WHERE round_number = 0"
        ).fetchone() == ("wikipedia",)
    finally:
        con.close()


def test_a_round_is_not_loaded_twice_when_both_sources_have_it(tmp_path):
    from afl import fetch_footywire_rising_star as F

    wiki_csv = tmp_path / "wikipedia.csv"
    W.write_csv(wiki_csv, W.parse_page(HTML, 2026))

    footywire = F.parse_page(FOOTYWIRE_HTML, 2026)
    footywire_csv = tmp_path / "footywire.csv"
    F.write_csv(footywire_csv, footywire)

    db = tmp_path / "afl.db"
    _build_db(db)
    result = L.load_sources(db, [footywire_csv, wiki_csv], verbose=False)

    con = sqlite3.connect(db)
    try:
        rounds = con.execute(
            "SELECT round_number, source, disposals FROM rising_star_nominees "
            "WHERE season = 2026 ORDER BY round_number"
        ).fetchall()
    finally:
        con.close()
    # Round 0 exists in both. FootyWire keeps it, and brings its statistics.
    assert rounds == [
        (0, "footywire", 15), (1, "wikipedia", None), (2, "wikipedia", None),
        (3, "wikipedia", None), (4, "wikipedia", None),
    ]
    assert result["sources"] == {"footywire": 1, "wikipedia": 4}


FOOTYWIRE_HTML = """
<html><body>
<div>2026 AFL Rising Star Nominations</div>
<table>
<tr><th>Rd</th><th>Player</th><th>Team</th><th>Opp</th><th>K</th><th>H</th>
<th>D</th><th>M</th><th>G</th><th>B</th><th>T</th><th>HO</th></tr>
<tr><td>0</td>
<td><a href="/afl/footy/pp-gold-coast-suns--leo-lombard">L Lombard</a></td>
<td><a href="/afl/footy/ty-gold-coast-suns">Suns</a></td>
<td><a href="/afl/footy/ty-geelong-cats">Cats</a></td>
<td>9</td><td>6</td><td>15</td><td>4</td><td>2</td><td>2</td><td>4</td><td>0</td>
</tr>
</table></body></html>
"""


def main() -> None:
    pytest.main([__file__, "-q"])


if __name__ == "__main__":
    main()
