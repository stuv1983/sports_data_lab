#!/usr/bin/env python3
"""Who took the field, and the two charts the app draws.

The MLB build is season-grain -- Lahman has no box scores -- so an MLB
match card had nothing per-player to show and said so. Retrosheet's game
logs do record both batting orders, and that is what these read back. What
is tested is the decoding: a lineup is stored as one ordered value per
side, and getting the order or the pitcher wrong would put a name in the
wrong slot without anything looking broken.

The charts are tested for the rules that are easy to break quietly: no
second y-scale, a missed season is a gap rather than a zero, and a hue
follows the entity it names rather than its position in a list.
"""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---

import sqlite3

import pandas as pd
import pytest

import charts
import sports
from mlb import lineups as L


# ------------------------------------------------------------- lineups

def _fixture():
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE mlb_game_lineups (
          source_game_key TEXT, team_position TEXT, lineup TEXT,
          PRIMARY KEY (source_game_key, team_position)
        );
        CREATE TABLE mlb_retro_players (
          retro_id TEXT PRIMARY KEY, player TEXT, player_id TEXT
        );
        INSERT INTO mlb_game_lineups VALUES
          -- Deliberately short of nine, with the pitcher appended: the
          -- shape that used to hand him a batting slot he never had.
          ('20241030-0-NYA-LAN','H',
           'torregl01:4:1;sotoju01:9:2;judgeaa01:8:3;colege01:1:'),
          ('20241030-0-NYA-LAN','A','ohtansh01:10:1;bettsmo01:9:2');
        INSERT INTO mlb_retro_players VALUES
          ('torregl01','Gleyber Torres','torregl01'),
          ('sotoju01','Juan Soto','sotoju01'),
          ('judgeaa01','Aaron Judge','judgeaa01'),
          ('colege01','Gerrit Cole','colege01'),
          ('ohtansh01','Shohei Ohtani','ohtansh01');
    """)
    return con


def test_a_lineup_reads_back_in_the_order_they_batted():
    con = _fixture()
    frame = L.read(con, "20241030-0-NYA-LAN", "H")
    assert frame["Player"].tolist() == [
        "Gleyber Torres", "Juan Soto", "Aaron Judge", "Gerrit Cole"]
    assert frame["Order"].tolist()[:3] == [1, 2, 3]


def test_fielding_positions_are_decoded_not_printed_as_codes():
    """A page shared by four sports has no business knowing that 6 means
    shortstop, so the sport's own module decodes them."""
    con = _fixture()
    frame = L.read(con, "20241030-0-NYA-LAN", "H")
    assert frame["Pos"].tolist() == ["2B", "RF", "CF", "P"]
    assert L.read(con, "20241030-0-NYA-LAN", "A")["Pos"].iat[0] == "DH"


def test_the_pitcher_a_designated_hitter_batted_for_has_no_batting_order():
    """He is a tenth man rather than one of the nine, which is exactly how
    a box score prints him."""
    con = _fixture()
    frame = L.read(con, "20241030-0-NYA-LAN", "H")
    cole = frame[frame["Player"] == "Gerrit Cole"].iloc[0]
    assert pd.isna(cole["Order"])
    assert cole["Pos"] == "P"


def test_a_name_the_crosswalk_never_resolved_still_takes_the_field():
    """Only the id is stored per game; a player missing from the name
    table is shown by that id rather than dropped from the lineup."""
    con = _fixture()
    frame = L.read(con, "20241030-0-NYA-LAN", "A")
    assert frame["Player"].tolist() == ["Shohei Ohtani", "bettsmo01"]
    assert pd.isna(frame["PlayerID"].iat[1])


def test_a_game_with_no_lineup_recorded_returns_nothing():
    """Most of the 19th century. The card then says so, rather than
    drawing an empty table."""
    con = _fixture()
    assert L.read(con, "18710504-0-FW1-CL1", "H").empty
    assert L.read(con, None, "H").empty
    assert L.read(con, "20241030-0-NYA-LAN", None).empty


def test_the_reader_is_silent_when_the_layer_was_never_loaded():
    con = sqlite3.connect(":memory:")
    assert L.available(con) is False
    assert L.read(con, "20241030-0-NYA-LAN", "H").empty


# --------------------------------------------------------- linking out

def test_a_box_score_link_lands_on_the_exact_game():
    """Built from the game key's own home code, date and game number, so
    the second half of a doubleheader gets its own link."""
    links = dict((label, url) for label, url in
                 L.game_links("20241030-0-NYA-LAN"))
    assert any("baseball-reference.com/boxes/NYA/NYA202410300.shtml" in url
               for url in links.values())
    second = dict(L.game_links("19270516-2-DET-NYA"))
    assert any("DET192705162.shtml" in url for url in second.values())


def test_a_key_in_an_unknown_shape_offers_no_links():
    """A wrong link is worse than none, so anything unrecognised gives
    nothing rather than a guess."""
    for key in ("", None, "not-a-key", "2024-10-30"):
        assert L.game_links(key) == []


# -------------------------------------------------------------- charts

def _career():
    return pd.DataFrame({
        "Season": [1991, 1992, 1994, 1994],   # 1993 missed; 1994 two clubs
        "Games": [22, 20, 5, 12],
        "Goals": [100, 80, 20, 40],
    })


def test_a_season_split_between_two_clubs_is_one_bar():
    """A player who changed clubs mid-season has two rows for it, and the
    season he played is one season."""
    chart = charts.career_chart(_career(), "Season", "Games", "Games")
    data = chart.to_dict()["datasets"]
    rows = data[list(data)[0]]
    assert len(rows) == 3
    assert {row["Season"]: row["Games"] for row in rows}[1994] == 17


def test_a_missed_season_is_a_gap_not_a_zero():
    """A bar of nothing says he played and scored none; no bar says he did
    not play. The distinction is the whole point of the chart."""
    chart = charts.career_chart(_career(), "Season", "Games", "Games")
    data = chart.to_dict()["datasets"]
    seasons = [row["Season"] for row in data[list(data)[0]]]
    assert 1993 not in seasons


def test_a_measure_nothing_was_recorded_for_draws_no_chart():
    empty = pd.DataFrame({"Season": [2000, 2001], "Goals": [0, 0]})
    assert charts.career_chart(empty, "Season", "Goals", "Goals") is None
    assert charts.career_chart(pd.DataFrame(), "Season", "Goals", "G") is None
    assert charts.career_chart(_career(), "Season", "Nope", "N") is None


def test_the_two_sides_of_a_match_keep_their_own_colour():
    """Colour follows the entity, never its position: the domain is named
    so the home side is blue whichever way the rows arrive."""
    rows = [(1, (20, 3, 2), (14, 2, 2)), (2, (45, 6, 9), (38, 5, 8))]
    spec = charts.progression_chart(rows, "Geelong", "Hawthorn").to_dict()
    scale = spec["encoding"]["color"]["scale"]
    assert scale["domain"] == ["Geelong", "Hawthorn"]
    assert len(scale["range"]) == 2 and scale["range"][0] != scale["range"][1]


def test_the_progression_starts_from_nothing_all():
    """The first period is a climb from the first bounce, not a mark
    floating above the axis."""
    rows = [(1, (20, 3, 2), (14, 2, 2))]
    spec = charts.progression_chart(rows, "Geelong", "Hawthorn").to_dict()
    data = spec["datasets"][list(spec["datasets"])[0]]
    assert {row["Points"] for row in data if row["Break"] == 0} == {0}


def test_a_match_with_no_scoring_recorded_draws_nothing():
    assert charts.progression_chart([], "A", "B") is None
    flat = [(1, (0, 0, 0), (0, 0, 0))]
    assert charts.progression_chart(flat, "A", "B") is None


def test_neither_chart_ever_carries_a_second_y_scale():
    """The one rule that quietly invents a relationship the data does not
    have. Both charts must resolve to a single y encoding."""
    rows = [(1, (20, 3, 2), (14, 2, 2))]
    for chart in (charts.career_chart(_career(), "Season", "Games", "Games"),
                  charts.progression_chart(rows, "A", "B")):
        encoding = chart.to_dict()["encoding"]
        assert [key for key in encoding if key.startswith("y")] == ["y"]


def test_both_themes_use_the_same_two_hues_restepped():
    """Dark mode is selected, not an automatic flip of the light values."""
    assert len(charts.SERIES_LIGHT) == len(charts.SERIES_DARK) == 2
    assert charts.SERIES_LIGHT != charts.SERIES_DARK
    for palette in (charts.SERIES_LIGHT, charts.SERIES_DARK):
        assert all(colour.startswith("#") and len(colour) == 7
                   for colour in palette)


# ============================================================ live data

def test_live_a_real_game_has_both_lineups():
    if not sports.MLB.exists():
        pytest.skip("no built MLB database")
    con = sqlite3.connect(f"file:{sports.MLB.db}?mode=ro", uri=True)
    try:
        if not L.available(con):
            pytest.skip("lineups not loaded")
        key = con.execute(
            "SELECT source_game_key FROM mlb_game_lineups "
            "WHERE source_game_key LIKE '2024%' LIMIT 1").fetchone()[0]
        home, away = L.read(con, key, "H"), L.read(con, key, "A")
        assert not home.empty and not away.empty
        # Nine batters and, in the designated-hitter era, a tenth man.
        assert 9 <= len(home) <= 10
        assert home["PlayerID"].notna().all()
    finally:
        con.close()
