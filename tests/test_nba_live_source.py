#!/usr/bin/env python3
"""Keeping the NBA current from NBA.com without losing what the CSVs hold.

Three defects stopped `--source nba_api` being usable at all, and each one
failed quietly enough to be worth a test:

* `players()` never emitted `birth_country`, so `validate` rejected every
  build from the adapter before it read a single game;
* a neutral-site game reads '@' on *both* team rows, which left it with no
  home team and got it thrown out along with its player statistics;
* `--refresh` was all-or-nothing, so a scheduled rebuild re-requested all
  eighty seasons of settled history every run.

And one design point: NBA.com's static player list carries no biography,
so games come from there and biography from the CSV export.
"""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---

import pandas as pd
import pytest

from nba import nba_source
from nba.nba_source_api import NbaApiSource


# ------------------------------------------------- folding the game log

def _log(rows):
    """A leaguegamelog frame: two rows per game, one per team."""
    return pd.DataFrame(rows, columns=[
        "GAME_ID", "TEAM_ID", "TEAM_ABBREVIATION", "MATCHUP", "PTS",
        "GAME_DATE"])


class _FoldOnly(NbaApiSource):
    """Exercises matches() against a canned log, with no network."""

    def __init__(self, regular, playoff=None):
        super().__init__(cache="/nonexistent", verbose=False)
        self._logs = {"regular": regular,
                      "playoff": playoff if playoff is not None else _log([])}

    def _game_log(self, season, phase):
        return self._logs[phase]


def test_a_normal_game_keeps_the_orientation_matchup_gives_it():
    source = _FoldOnly(_log([
        ("0022500001", 1610612760, "OKC", "OKC vs. HOU", 125, "2025-10-21"),
        ("0022500001", 1610612745, "HOU", "HOU @ OKC", 124, "2025-10-21"),
    ]))
    match = source.matches(2025).iloc[0]
    assert match["home_team_id"] == "1610612760"
    assert match["home_score"] == 125
    assert match["away_team_id"] == "1610612745"
    assert match["away_score"] == 124


def test_a_neutral_site_game_survives_both_rows_saying_away():
    """'ORL @ NYK' and 'NYK @ ORL' describe one game at a neutral venue.

    Read a row at a time, both teams landed on the away side and the
    second overwrote the first, leaving no home team. The strict build
    then rejected the game and dropped its player statistics with it.
    """
    source = _FoldOnly(_log([
        ("0022501229", 1610612753, "ORL", "ORL @ NYK", 120, "2025-12-13"),
        ("0022501229", 1610612752, "NYK", "NYK @ ORL", 132, "2025-12-13"),
    ]))
    match = source.matches(2025).iloc[0]

    assert match["home_team_id"] is not None
    assert match["away_team_id"] is not None
    assert {match["home_team_id"], match["away_team_id"]} == {
        "1610612753", "1610612752"}


def test_a_neutral_site_game_keeps_each_score_with_its_own_team():
    """Orientation is nominal, but the scores must not be swapped.

    points_for, points_against and the win or loss all derive from this
    pairing, so a team taking the other's score would turn a win into a
    loss.
    """
    source = _FoldOnly(_log([
        ("0022501229", 1610612753, "ORL", "ORL @ NYK", 120, "2025-12-13"),
        ("0022501229", 1610612752, "NYK", "NYK @ ORL", 132, "2025-12-13"),
    ]))
    match = source.matches(2025).iloc[0]

    scored = {match["home_team_id"]: match["home_score"],
              match["away_team_id"]: match["away_score"]}
    assert scored == {"1610612753": 120, "1610612752": 132}


def test_the_neutral_site_orientation_is_stable_across_row_order():
    """Arbitrary is fine; unstable is not. Two builds must agree."""
    forward = _FoldOnly(_log([
        ("0022501229", 1610612753, "ORL", "ORL @ NYK", 120, "2025-12-13"),
        ("0022501229", 1610612752, "NYK", "NYK @ ORL", 132, "2025-12-13"),
    ])).matches(2025).iloc[0]
    reversed_ = _FoldOnly(_log([
        ("0022501229", 1610612752, "NYK", "NYK @ ORL", 132, "2025-12-13"),
        ("0022501229", 1610612753, "ORL", "ORL @ NYK", 120, "2025-12-13"),
    ])).matches(2025).iloc[0]

    assert forward["home_team_id"] == reversed_["home_team_id"]
    assert forward["home_score"] == reversed_["home_score"]


def test_a_game_with_only_one_team_row_is_left_incomplete():
    """The strict build exists to catch this; papering over it would hide
    a genuinely truncated log."""
    source = _FoldOnly(_log([
        ("0022500999", 1610612760, "OKC", "OKC vs. HOU", 125, "2025-10-21"),
    ]))
    match = source.matches(2025).iloc[0]
    assert match["home_team_id"] == "1610612760"
    assert match["away_team_id"] is None


# ------------------------------------------------------- bounded refresh

class _CountingSource(NbaApiSource):
    def __init__(self, tmp_path, refresh):
        super().__init__(cache=tmp_path, refresh=refresh, verbose=False,
                         throttle=0)
        self.calls = []

    def _game_log(self, season, phase):
        params = f"{season}/{phase}"
        return self._frame(self._cached(
            "leaguegamelog", params,
            lambda: self.calls.append(season) or {
                "resultSets": [{"headers": [], "rowSet": []}]},
            season=season))


def test_a_scheduled_refresh_asks_only_about_the_seasons_it_names(tmp_path):
    """A season finished in 1974 cannot change. Re-requesting all eighty
    every night is a few hundred pointless calls against undocumented
    endpoints, which is the surest way to be blocked."""
    warm = _CountingSource(tmp_path, refresh=False)
    for season in (2023, 2024, 2025):
        warm._game_log(season, "regular")
    assert warm.calls == [2023, 2024, 2025], "cold cache should fetch"

    bounded = _CountingSource(tmp_path, refresh=[2025])
    for season in (2023, 2024, 2025):
        bounded._game_log(season, "regular")
    assert bounded.calls == [2025]


def test_refresh_true_still_means_everything(tmp_path):
    warm = _CountingSource(tmp_path, refresh=False)
    for season in (2023, 2024):
        warm._game_log(season, "regular")

    everything = _CountingSource(tmp_path, refresh=True)
    for season in (2023, 2024):
        everything._game_log(season, "regular")
    assert everything.calls == [2023, 2024]


def test_refresh_false_asks_about_nothing_already_cached(tmp_path):
    warm = _CountingSource(tmp_path, refresh=False)
    warm._game_log(2024, "regular")

    again = _CountingSource(tmp_path, refresh=False)
    again._game_log(2024, "regular")
    assert again.calls == []


# ------------------------------------------ the composed "live" source

def test_the_api_player_list_satisfies_the_column_contract(monkeypatch):
    """birth_country was simply missing from the frame this builds, so
    validate() rejected every build from the adapter before it read a
    single game."""
    source = NbaApiSource(cache="/nonexistent", verbose=False)
    monkeypatch.setattr(source, "_require_nba_api", lambda: None)
    fake = type(_sys)("nba_api.stats.static.players")
    fake.get_players = lambda: [{"id": 100, "full_name": "Tim Legler"}]
    monkeypatch.setitem(_sys.modules, "nba_api.stats.static.players", fake)

    frame = source.players()

    assert list(frame.columns) == list(nba_source.PLAYER_COLUMNS)
    assert frame.iloc[0]["birth_country"] is None


def test_live_is_a_registered_source():
    with pytest.raises(nba_source.SourceError, match="csv, live, bbr"):
        nba_source.get_source("nonsense")


def _csv_root(tmp_path):
    """A minimal CSV export carrying biography and nothing else."""
    pd.DataFrame([
        {"source_player_id": "100", "player": "Tim Legler",
         "birth_year": 1966, "position": "G", "height_cm": 193.0,
         "weight_kg": 90.7, "birth_country": "USA"},
    ]).to_csv(tmp_path / "players.csv", index=False)
    pd.DataFrame([
        {"team_id": "1", "franchise_id": "1", "name": "Team", "city": "C",
         "nickname": "T", "abbreviation": "TTT", "first_season": 1946,
         "last_season": None, "is_current": 1},
    ]).to_csv(tmp_path / "teams.csv", index=False)
    return tmp_path


def test_live_takes_biography_from_the_csv_export(tmp_path, monkeypatch):
    """NBA.com's static player list has no biography at all, so a pure
    nba_api build nulls birth_country, birth_year, position, height_cm and
    weight_kg -- disabling the international-player square and the height
    and weight search."""
    from nba import nba_source_live

    source = nba_source_live.LiveNbaSource(root=_csv_root(tmp_path))
    monkeypatch.setattr(source.live, "players", lambda: pd.DataFrame(
        [{"source_player_id": "100", "player": "Tim Legler",
          "birth_year": None, "position": None, "height_cm": None,
          "weight_kg": None, "birth_country": None}]))

    players = source.players()
    row = players[players.source_player_id.astype(str) == "100"].iloc[0]
    assert row["birth_country"] == "USA"
    assert row["height_cm"] == 193.0
    assert row["birth_year"] == 1966


def test_live_still_lists_a_player_the_csv_export_predates(
        tmp_path, monkeypatch):
    """A debut after the export was taken still needs a row, or their game
    rows have nobody to attach to."""
    from nba import nba_source_live

    source = nba_source_live.LiveNbaSource(root=_csv_root(tmp_path))
    monkeypatch.setattr(source.live, "players", lambda: pd.DataFrame(
        [{"source_player_id": "9999", "player": "Rookie Newcomer",
          "birth_year": None, "position": None, "height_cm": None,
          "weight_kg": None, "birth_country": None}]))

    players = source.players()
    assert set(players.source_player_id.astype(str)) == {"100", "9999"}
    newcomer = players[players.source_player_id.astype(str) == "9999"].iloc[0]
    assert pd.isna(newcomer["birth_country"]), "unknown, not invented"


def test_live_takes_teams_from_the_same_side_as_the_games(
        tmp_path, monkeypatch):
    """Team ids are not shared across the two sources -- the CSV export
    keys historical identities as '1610612744-1946' while the game rows
    carry NBA.com's own ids, and none of the 58 CSV ids appear in NBA.com's
    30 -- so taking teams from the CSV would orphan every match row."""
    from nba import nba_source_live

    source = nba_source_live.LiveNbaSource(root=_csv_root(tmp_path))
    asked = []
    for name in ("teams", "matches", "player_games"):
        monkeypatch.setattr(
            source.live, name,
            (lambda label: lambda *a, **k: asked.append(label))(name))
    monkeypatch.setattr(
        source.files, "teams",
        lambda: pytest.fail("teams must not come from the CSV export"))

    source.teams()
    source.matches(2025)
    source.player_games(2025, "regular")
    assert asked == ["teams", "matches", "player_games"]


# ------------------------------------------------------ real doubleheaders

def _game(player_id, date, match_id, club, opponent, points, career_game_no):
    return {"player_id": player_id, "date": date, "match_id": match_id,
            "club_hist": club, "opponent": opponent, "points": points,
            "career_game_no": career_game_no}


def test_a_doubleheader_is_not_mistaken_for_a_duplicate():
    """The early NBA played two games in a day.

    Bob Cousy played New York and then Minneapolis on 1955-11-12, and both
    games are in the shipped database. The (player, date) duplicate pass
    leans on the career sequence to tell repeats apart, and that sequence
    is derived rather than sourced when the games come from NBA.com -- so
    it reported the pair unresolved and strict mode failed the whole
    build over a fixture that really happened.
    """
    from nba import build_nba_db

    frame = pd.DataFrame([
        _game(1, "1955-11-12", "A", "Boston Celtics", "New York Knicks", 13, 1),
        _game(1, "1955-11-12", "B", "Boston Celtics", "Los Angeles Lakers", 9, 2),
    ])
    issues = []
    out, unresolved = build_nba_db._deduplicate(
        frame, issues, "live", strict=True, verbose=False)

    assert unresolved == []
    assert sorted(out["match_id"]) == ["A", "B"], "a real game was dropped"
    assert any(i["kind"] == "doubleheader" for i in issues)


def test_the_same_fixture_recorded_twice_is_still_collapsed():
    """The carve-out must not become a way for real duplicates to survive:
    two rows naming the same club and the same opponent are one game."""
    from nba import build_nba_db

    frame = pd.DataFrame([
        _game(2, "2024-01-01", "C", "Boston Celtics", "New York Knicks", 5, 1),
        _game(2, "2024-01-01", "D", "Boston Celtics", "New York Knicks", 5, 2),
    ])
    out, unresolved = build_nba_db._deduplicate(
        frame, [], "live", strict=True, verbose=False)

    assert unresolved == []
    assert len(out) == 1


def test_an_exact_repeat_is_collapsed_before_anything_else():
    """The common NBA duplicate is the same box score arriving twice."""
    from nba import build_nba_db

    row = _game(3, "2024-01-01", "E", "Boston Celtics", "New York Knicks", 7, 1)
    out, unresolved = build_nba_db._deduplicate(
        pd.DataFrame([row, dict(row)]), [], "live", strict=True, verbose=False)

    assert unresolved == []
    assert len(out) == 1
