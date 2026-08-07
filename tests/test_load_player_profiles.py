from pathlib import Path

import pandas as pd

from afl.load_match_scores import SourceMatch
from afl.load_player_profiles import (
    _profile_metadata, cached_profile_players, parse_profile,
)


def _profile(path: Path, *, player_id=42, player="Test Player",
             born="01-Jan-2000", club="Test Club", season=2020):
    path.write_text(f"""<html><head><title>{player}</title></head><body>
      <h1>{player}</h1><b>Born:</b>{born}
      <script>document.write(r[{player_id}]);</script>
      <table><thead><tr><th colspan="28">{club} - {season}</th></tr>
      <tr><th>Gm</th><th>Opponent</th><th>Rd</th><th>R</th><th>#</th>
      <th>KI</th><th>MK</th><th>HB</th><th>DI</th><th>GL</th><th>BR</th></tr></thead>
      <tbody><tr><td>1</td><td>Other Club</td>
      <td><a href="../../games/2020/000020200101.html">1</a></td><td>W</td>
      <td>9</td><td>4</td><td>2</td><td>3</td><td>7</td><td>&nbsp;</td><td>&nbsp;</td>
      </tr></tbody><tfoot><tr><td></td><td>Totals</td></tr></tfoot></table>
    </body></html>""", encoding="windows-1252")


def test_profile_metadata_uses_afl_tables_id_and_url(tmp_path):
    path = tmp_path / "Test_Player.html"
    _profile(path)
    assert _profile_metadata(path) == (
        42, "Test Player", "2000-01-01",
        "https://afltables.com/afl/stats/players/T/Test_Player.html")
    assert cached_profile_players(tmp_path) == [
        (42, "Test Player",
         "https://afltables.com/afl/stats/players/T/Test_Player.html")]


def test_parse_profile_joins_score_and_preserves_unavailable_stats(tmp_path):
    path = tmp_path / "Test_Player.html"
    _profile(path)
    match = SourceMatch(1, "2020-01-01", 2020, "1", "Test Club",
                        "10.10.70", 70, "Other Club", "9.9.63", 63,
                        "Test Ground")
    row = parse_profile(path, [match]).iloc[0]
    assert (row.player_id, row.player, row.date, row.venue) == (
        42, "Test Player", "2020-01-01", "Test Ground")
    assert (row.is_home, row.result, row.points_for, row.points_against) == (
        1, "W", 70, 63)
    assert row.disposals == 7
    assert pd.isna(row.goals)  # blank and unavailable within the table
    assert pd.isna(row.tackles)
