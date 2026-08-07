import sqlite3

from afl.constraints import venue_profiles_available, venue_profiles_count
from afl.load_venues import parse_profile


def test_parse_venue_profile_preserves_each_record_family(tmp_path):
    path = tmp_path / "test_ground.html"
    match_row = """<tr><td>50</td><td>Alpha</td><td>1.1 5.5</td><td>35</td>
      <td>Beta</td><td>1.1 2.2</td><td>14</td><td>01-Jan-2000</td></tr>"""
    path.write_text(f"""<html><body>
      <table><tr><th colspan='13'>Test Ground</th></tr>
       <tr><th>Team</th><th>P</th><th>W</th><th>D</th><th>L</th>
       <th>GF-BF</th><th>For</th><th>GA-BA</th><th>Agn</th><th>%</th>
       <th>Win%</th><th>100+F</th><th>100+A</th></tr>
       <tr><td>Alpha</td><td>10</td><td>6</td><td>1</td><td>3</td>
       <td>100.100</td><td>700</td><td>90.90</td><td>630</td>
       <td>111.11</td><td>65.00</td><td>2</td><td>1</td></tr></table>
      <table><tr><th colspan='13'>Biggest Wins</th></tr>{match_row}</table>
      <table><tr><th colspan='13'>Highest Scores</th></tr>{match_row}</table>
      <table><tr><th colspan='13'>Lowest Scores</th></tr>{match_row}</table>
      <table><tr><th colspan='3'>Most Games Played</th>
       <th colspan='3'>Most Goals Kicked</th></tr>
       <tr><td>20</td><td>A Player</td><td>AL</td>
       <td>30</td><td>B Player</td><td>BE</td></tr></table>
      <table><tr><th colspan='3'>Most Goals in a Game</th>
       <th colspan='3'>Most Disposals in a Game</th></tr>
       <tr><td>8</td><td>A Player</td><td>Alpha v Beta, 2000</td>
       <td>40</td><td>B Player</td><td>Beta v Alpha, 2000</td></tr></table>
      </body></html>""", encoding="windows-1252")

    result = parse_profile(
        path, "Test Ground", "https://afltables.com/afl/venues/test.html")
    assert {key: len(rows) for key, rows in result.items()} == {
        "teams": 1, "matches": 3, "careers": 2, "single_games": 2}
    assert result["teams"][0][2:7] == ("Alpha", 10, 6, 1, 3)
    assert result["matches"][0][3:11] == (
        50, "Alpha", "1.1 5.5", 35, "Beta", "1.1 2.2", 14,
        "2000-01-01")


def test_venue_profile_status_counts_loaded_catalogue():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE venue_summary (venue TEXT, profile_url TEXT)")
    con.executemany("INSERT INTO venue_summary VALUES (?,?)", [
        ("Ground A", "https://example/a"), ("Ground B", "https://example/b")])
    assert venue_profiles_available(con)
    assert venue_profiles_count(con) == 2
