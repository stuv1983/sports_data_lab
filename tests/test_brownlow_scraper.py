from pathlib import Path

from afl.scrape_afl_tables_brownlow import parse_index, parse_season


DOWNLOADS = Path(r"C:\Users\stuar\Downloads")
INDEX = DOWNLOADS / "AFL Tables - Brownlow Medal Winners.html"
SEASON_2018 = DOWNLOADS / "AFL Tables - 2018 Brownlow Medal.html"


def test_saved_index_and_2018_page():
    if not INDEX.exists() or not SEASON_2018.exists():
        return

    urls, winners = parse_index(INDEX.read_bytes())
    assert len(urls) == 98
    assert 1942 not in urls
    assert urls[2018].endswith("brownlow2018.html")
    assert len(winners[2012]) == 2

    fields, rows = parse_season(
        SEASON_2018.read_bytes(), 2018, urls[2018], winners[2018]
    )
    assert len(rows) == 210
    assert "round_23" in fields
    assert rows[0]["player"] == "Mitchell, Tom"
    assert rows[0]["votes"] == "28"
    assert rows[0]["ineligible"] is False
    assert rows[0]["round_1"] == "3"
    assert rows[0]["round_4"] == "0"
    assert rows[0]["winner"] is True
    assert rows[0]["player_id"] == "Tom_Mitchell"
    assert rows[2]["round_1"] == ""


def test_historical_totals_only_layout():
    html = b"""
    <table>
      <tr><th>Player</th><th>Teams</th><th>Votes</th><th>Games</th>
          <th>3</th><th>2</th><th>1</th><th>GP</th></tr>
      <tr><td><a href='/afl/stats/players/E/Edward_Greeves.html'>Greeves, Edward</a></td>
          <td><a href='/afl/brownlow/geelong_totals.html'>GE</a></td>
          <td>7*</td><td>16</td><td>1</td><td>2</td><td>0</td><td>3</td></tr>
    </table>
    """
    player_url = "https://afltables.com/afl/stats/players/E/Edward_Greeves.html"
    fields, rows = parse_season(
        html, 1924, "https://afltables.com/afl/brownlow/brownlow1924.html", {player_url}
    )
    assert not any(field.startswith("round_") for field in fields)
    assert rows[0]["games"] == "16"
    assert rows[0]["votes"] == "7"
    assert rows[0]["ineligible"] is True
    assert rows[0]["three_vote_games"] == "1"
    assert rows[0]["winner"] is True
