#!/usr/bin/env python3
"""utils/nba/load_current_season.py: append a season without a rebuild.

The bug this module exists to avoid: NBA.com's own game log reports the
same numeric team_id for a franchise across its entire history, so a full
rebuild from --source live cannot tell the Thunder from the SuperSonics.
These tests guard the properties that make appending safe instead --

UNTOUCHED HISTORY. A season this loader was not asked about, and every
already-correct club_hist, must survive a run byte-for-byte.

STABLE IDENTITY. A player matched by source_player_id keeps their existing
player_id; a genuinely new one gets a fresh id without disturbing anyone
else's, in both directions across two seasons requested in the same run.

IDEMPOTENCE. Running the same fetch twice must replace, not double, the
target season's rows.
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

import nba_fixture
from nba import build_nba_db, nba_source
from utils.nba import load_current_season as lcs


def _row(pid, mid, team, pts, **extra):
    row = {"source_player_id": pid, "match_id": mid, "team_id": team}
    for column in nba_source.STAT_COLUMNS:
        row[column] = None
    row["points"] = pts
    row.update(extra)
    return row


def _match(mid, season, date, home, away, hs, aw, phase="regular", rnd=None):
    return {"match_id": mid, "season": season,
            "season_label": f"{season}-{(season + 1) % 100:02d}", "date": date,
            "phase": phase, "round": rnd, "home_team_id": home,
            "away_team_id": away, "home_score": hs, "away_score": aw,
            "venue": None, "attendance": None}


class FakeSource:
    """A minimal live-shaped source: whatever matches()/player_games()
    dicts are handed to it for a season, keyed by season."""

    key = "fake-live"

    def __init__(self, matches_by_season=None, games_by_season=None,
                new_players=None, discovered=None):
        self._matches = matches_by_season or {}
        self._games = games_by_season or {}
        self._new_players = (pd.DataFrame(new_players)
                             if new_players else pd.DataFrame(
                                 columns=nba_source.PLAYER_COLUMNS))
        self._discovered = discovered or {}

    def matches(self, season):
        rows = self._matches.get(season, [])
        return pd.DataFrame(rows, columns=nba_source.MATCH_COLUMNS) if not rows \
            else pd.DataFrame(rows)

    def player_games(self, season, phase):
        rows = [r for r in self._games.get(season, []) if r.get("_phase", "regular") == phase]
        if not rows:
            return pd.DataFrame(columns=nba_source.PLAYER_GAME_COLUMNS)
        return pd.DataFrame([{k: v for k, v in r.items() if k != "_phase"}
                             for r in rows])

    def players(self):
        return self._new_players

    def discovered_players(self, ids=None):
        wanted = {str(i) for i in (ids or [])}
        rows = [row for pid, row in self._discovered.items() if pid in wanted]
        return pd.DataFrame(rows) if rows else None

    def fetches(self):
        return []


@pytest.fixture
def base_db(tmp_path):
    """The synthetic fixture, built once via the real CSV builder -- the
    same starting point every consumer of nba_fixture uses, so its
    Sonics/Thunder split and 1971 no-steals-recorded season are already
    exactly right before this loader ever touches it."""
    nba_fixture.write(tmp_path / "csv")
    db = tmp_path / "nba.db"
    build_nba_db.build(db, nba_source.CsvNbaSource(tmp_path / "csv"),
                       verbose=False)
    return db


def _run(db, source, seasons, **kwargs):
    import unittest.mock as mock
    with mock.patch.object(nba_source, "get_source", return_value=source):
        return lcs.load(str(db), seasons=seasons, verbose=False, **kwargs)


def test_a_season_never_asked_about_is_untouched(base_db):
    before = sqlite3.connect(base_db).execute(
        "SELECT player, club_hist, club_now, points FROM games "
        "WHERE season=1971 ORDER BY match_id").fetchall()

    source = FakeSource(
        matches_by_season={2010: [_match("g2010new", 2010, "2010-12-01",
                                         "okc", "bos", 100, 90)]},
        games_by_season={2010: [_row("p3", "g2010new", "okc", 20)]})
    _run(base_db, source, [2010])

    after = sqlite3.connect(base_db).execute(
        "SELECT player, club_hist, club_now, points FROM games "
        "WHERE season=1971 ORDER BY match_id").fetchall()
    assert after == before


def test_the_historical_identity_split_survives_a_merge(base_db):
    """Marcus Oyelaran played for both the Sonics and the Thunder in the
    fixture. player_team_history must still show both after a season this
    loader never touched is reloaded through it -- the case
    _restore_join_columns exists for."""
    source = FakeSource(
        matches_by_season={2010: [_match("g2010new", 2010, "2010-12-01",
                                         "okc", "bos", 100, 90)]},
        games_by_season={2010: [_row("p3", "g2010new", "okc", 20)]})
    _run(base_db, source, [2010])

    con = sqlite3.connect(base_db)
    identities = {row[0] for row in con.execute(
        "SELECT club_hist FROM player_team_history pth "
        "JOIN players p USING (player_id) "
        "WHERE p.source_player_id='p3'")}
    assert identities == {"Seattle SuperSonics", "Oklahoma City Thunder"}


def test_a_matched_player_keeps_their_existing_id(base_db):
    con = sqlite3.connect(base_db)
    existing_id = con.execute(
        "SELECT player_id FROM players WHERE source_player_id='p3'"
    ).fetchone()[0]

    source = FakeSource(
        matches_by_season={2010: [_match("g2010new", 2010, "2010-12-01",
                                         "okc", "bos", 100, 90)]},
        games_by_season={2010: [_row("p3", "g2010new", "okc", 20)]})
    _run(base_db, source, [2010])

    con = sqlite3.connect(base_db)
    row = con.execute(
        "SELECT player_id FROM games g JOIN players p USING (player_id) "
        "WHERE p.source_player_id='p3' AND g.match_id='g2010new'").fetchone()
    assert row[0] == existing_id


def test_a_new_player_gets_a_fresh_id_without_disturbing_anyone_elses(base_db):
    con = sqlite3.connect(base_db)
    before_ids = {row[0] for row in con.execute(
        "SELECT player_id FROM players")}

    source = FakeSource(
        matches_by_season={2010: [_match("g2010new", 2010, "2010-12-01",
                                         "okc", "bos", 100, 90)]},
        games_by_season={2010: [_row("rookie1", "g2010new", "okc", 9)]},
        new_players=[{"source_player_id": "rookie1", "player": "Newcomer One",
                     "birth_year": 1999, "position": "G", "height_cm": None,
                     "weight_kg": None, "birth_country": None}])
    result = _run(base_db, source, [2010])

    assert result["new_players"] == 1
    con = sqlite3.connect(base_db)
    after_ids = {row[0] for row in con.execute("SELECT player_id FROM players")}
    assert before_ids < after_ids
    assert len(after_ids) == len(before_ids) + 1
    new_id = (after_ids - before_ids).pop()
    stored = con.execute(
        "SELECT player, career_games FROM players WHERE player_id=?",
        (new_id,)).fetchone()
    assert stored == ("Newcomer One", 1)


def test_a_debut_spanning_two_open_seasons_is_not_dropped_from_either(base_db):
    """A rookie who debuts late in the earlier open season and continues
    into the next must appear in both -- discovery has to happen before
    either season is assembled, not per season, or the second season's
    rows for that exact player silently vanish."""
    source = FakeSource(
        matches_by_season={
            2009: [_match("g2009new", 2009, "2010-02-01", "okc", "bos", 90, 88)],
            2010: [_match("g2010new", 2010, "2010-11-20", "okc", "lal", 100, 90)],
        },
        games_by_season={
            2009: [_row("rookie2", "g2009new", "okc", 8)],
            2010: [_row("rookie2", "g2010new", "okc", 14)],
        },
        discovered={"rookie2": {"source_player_id": "rookie2",
                                "player": "Late Bloomer", "birth_year": 1991,
                                "position": "F", "height_cm": None,
                                "weight_kg": None, "birth_country": None}})
    result = _run(base_db, source, [2009, 2010])

    assert result["new_players"] == 1
    con = sqlite3.connect(base_db)
    rows = con.execute(
        "SELECT season, career_game_no FROM games g "
        "JOIN players p USING (player_id) "
        "WHERE p.source_player_id='rookie2' ORDER BY season").fetchall()
    assert rows == [(2009, 1), (2010, 2)]


def test_reloading_a_season_replaces_it_rather_than_doubling_it(base_db):
    source = FakeSource(
        matches_by_season={2010: [_match("g2010new", 2010, "2010-12-01",
                                         "okc", "bos", 100, 90)]},
        games_by_season={2010: [_row("p3", "g2010new", "okc", 20)]})
    _run(base_db, source, [2010])
    _run(base_db, source, [2010])

    con = sqlite3.connect(base_db)
    assert con.execute(
        "SELECT COUNT(*) FROM games WHERE season=2010 AND "
        "match_id='g2010new'").fetchone()[0] == 1


def test_a_padded_live_game_id_is_stored_the_way_the_history_stores_it():
    """The live endpoints pad to 10 characters; every match already in the
    database is the unpadded 8. Mixing the two gives the touched seasons a
    match_id space that intersects nothing else."""
    assert lcs.normalise_match_id("0022500001") == "22500001"
    assert lcs.normalise_match_id("0042400101") == "42400101"   # playoffs
    assert lcs.normalise_match_id("0052400101") == "52400101"   # play-in
    # already in the stored form, or some other shape: left alone
    assert lcs.normalise_match_id("22500001") == "22500001"
    assert lcs.normalise_match_id("g2010new") == "g2010new"


def test_a_padded_live_id_lands_on_the_row_it_already_had(base_db):
    """End to end: a season fetched with padded ids must replace the rows
    it matches, not sit alongside them under a second key."""
    padded = FakeSource(
        matches_by_season={2010: [_match("0022500001", 2010, "2010-12-01",
                                         "okc", "bos", 100, 90)]},
        games_by_season={2010: [_row("p3", "0022500001", "okc", 20)]})
    _run(base_db, padded, [2010])

    con = sqlite3.connect(base_db)
    assert [r[0] for r in con.execute(
        "SELECT DISTINCT match_id FROM games WHERE season=2010")] \
        == ["22500001"]
    assert [r[0] for r in con.execute(
        "SELECT match_id FROM matches WHERE season=2010")] == ["22500001"]


def test_a_dnp_row_the_live_feed_cannot_report_is_carried_forward(base_db):
    """NBA.com's game log only returns players who appeared, but the
    database's CSV-built history carries a zero-minute row for everyone who
    was active and did not play -- a fifth of every season since 2005. A
    reload must not quietly strip them."""
    both = FakeSource(
        matches_by_season={2010: [_match("g1", 2010, "2010-12-01",
                                         "okc", "bos", 100, 90)]},
        games_by_season={2010: [_row("p3", "g1", "okc", 20, minutes=30),
                                _row("p4", "g1", "okc", 0, minutes=0)]})
    _run(base_db, both, [2010])

    # the feed now reports only the player who actually played
    played_only = FakeSource(
        matches_by_season={2010: [_match("g1", 2010, "2010-12-01",
                                         "okc", "bos", 100, 90)]},
        games_by_season={2010: [_row("p3", "g1", "okc", 25, minutes=33)]})
    _run(base_db, played_only, [2010])

    con = sqlite3.connect(base_db)
    rows = dict(con.execute(
        "SELECT g.player_id, g.minutes FROM games g WHERE g.match_id='g1'"))
    pid = dict(con.execute(
        "SELECT source_player_id, player_id FROM players"))
    assert len(rows) == 2, "the DNP row was dropped by the reload"
    assert rows[pid["p4"]] == 0
    # and the reported player's line is the fresher one
    assert con.execute(
        "SELECT points FROM games WHERE match_id='g1' AND player_id=?",
        (pid["p3"],)).fetchone()[0] == 25


def test_a_dnp_row_for_a_match_the_fetch_dropped_is_not_resurrected(base_db):
    """Carry-forward is scoped to matches the load re-fetched. A game that
    vanished from the schedule stays gone."""
    first = FakeSource(
        matches_by_season={2010: [_match("g1", 2010, "2010-12-01",
                                         "okc", "bos", 100, 90),
                                  _match("g2", 2010, "2010-12-03",
                                         "bos", "okc", 95, 99)]},
        games_by_season={2010: [_row("p3", "g1", "okc", 20, minutes=30),
                                _row("p4", "g2", "okc", 0, minutes=0)]})
    _run(base_db, first, [2010])

    without_g2 = FakeSource(
        matches_by_season={2010: [_match("g1", 2010, "2010-12-01",
                                         "okc", "bos", 100, 90)]},
        games_by_season={2010: [_row("p3", "g1", "okc", 20, minutes=30)]})
    _run(base_db, without_g2, [2010])

    con = sqlite3.connect(base_db)
    assert con.execute(
        "SELECT COUNT(*) FROM games WHERE match_id='g2'").fetchone()[0] == 0


def test_the_builds_indexes_survive_a_load(base_db):
    """players and player_seasons are rewritten with pandas to_sql, which
    drops the table and its indexes with it. A load that left them off
    would quietly cost every name lookup and obscurity sort its index."""
    con = sqlite3.connect(base_db)
    before = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name NOT LIKE 'sqlite_%'")}
    assert {"ix_players_key", "ix_players_obsc", "ix_pseasons"} <= before

    source = FakeSource(
        matches_by_season={2010: [_match("g2010new", 2010, "2010-12-01",
                                         "okc", "bos", 100, 90)]},
        games_by_season={2010: [_row("p3", "g2010new", "okc", 20)]})
    _run(base_db, source, [2010])

    con = sqlite3.connect(base_db)
    after = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name NOT LIKE 'sqlite_%'")}
    assert before <= after, f"load dropped indexes: {sorted(before - after)}"


def test_franchises_and_teams_are_never_touched(base_db):
    con = sqlite3.connect(base_db)
    before_teams = con.execute(
        "SELECT team_id, franchise_id, name, is_current FROM teams "
        "ORDER BY team_id").fetchall()
    before_franchises = con.execute(
        "SELECT franchise_id, current_name FROM franchises "
        "ORDER BY franchise_id").fetchall()

    source = FakeSource(
        matches_by_season={2010: [_match("g2010new", 2010, "2010-12-01",
                                         "okc", "bos", 100, 90)]},
        games_by_season={2010: [_row("p3", "g2010new", "okc", 20)]})
    _run(base_db, source, [2010])

    con = sqlite3.connect(base_db)
    assert con.execute(
        "SELECT team_id, franchise_id, name, is_current FROM teams "
        "ORDER BY team_id").fetchall() == before_teams
    assert con.execute(
        "SELECT franchise_id, current_name FROM franchises "
        "ORDER BY franchise_id").fetchall() == before_franchises


def test_a_current_season_game_is_stamped_with_the_current_identity(base_db):
    """The whole point: club_hist and club_now agree for a season this
    loader writes, even for the Thunder franchise, which the fixture's own
    historical build correctly splits into two identities."""
    source = FakeSource(
        matches_by_season={2010: [_match("g2010new", 2010, "2010-12-01",
                                         "okc", "bos", 100, 90)]},
        games_by_season={2010: [_row("p3", "g2010new", "okc", 20)]})
    _run(base_db, source, [2010])

    con = sqlite3.connect(base_db)
    row = con.execute(
        "SELECT club_hist, club_now FROM games WHERE match_id='g2010new'"
    ).fetchone()
    assert row == ("Oklahoma City Thunder", "Oklahoma City Thunder")


def test_an_unknown_team_id_is_rejected_not_silently_dropped_wholesale(base_db):
    """A match naming a team_id this database has never heard of must not
    take the whole season down with it -- the rest of the fetch still
    loads, the same fail-narrow behaviour build()'s own validation gives."""
    source = FakeSource(
        matches_by_season={2010: [
            _match("g2010good", 2010, "2010-12-01", "okc", "bos", 100, 90),
            _match("g2010bad", 2010, "2010-12-02", "zzz-unknown", "bos", 80, 70),
        ]},
        games_by_season={2010: [_row("p3", "g2010good", "okc", 20)]})
    _run(base_db, source, [2010])

    con = sqlite3.connect(base_db)
    assert con.execute(
        "SELECT COUNT(*) FROM matches WHERE match_id='g2010good'"
    ).fetchone()[0] == 1
    assert con.execute(
        "SELECT COUNT(*) FROM matches WHERE match_id='g2010bad'"
    ).fetchone()[0] == 0


def test_open_seasons_names_both_the_finishing_and_starting_season():
    import datetime as dt
    # August: the 2025-26 season (started 2025) is the most recent one to
    # have played, and is still open in case of late corrections.
    assert lcs.open_seasons(dt.date(2026, 8, 9)) == [2024, 2025]
    # October: the 2026-27 season (started 2026) has now begun.
    assert lcs.open_seasons(dt.date(2026, 10, 15)) == [2025, 2026]


def test_dry_run_reports_without_writing(base_db):
    before = sqlite3.connect(base_db).execute(
        "SELECT COUNT(*) FROM games").fetchone()[0]

    source = FakeSource(
        matches_by_season={2010: [_match("g2010new", 2010, "2010-12-01",
                                         "okc", "bos", 100, 90)]},
        games_by_season={2010: [_row("p3", "g2010new", "okc", 20)]})
    result = _run(base_db, source, [2010], dry_run=True)

    assert result["seasons"][0]["would_insert_games"] == 1
    after = sqlite3.connect(base_db).execute(
        "SELECT COUNT(*) FROM games").fetchone()[0]
    assert after == before
