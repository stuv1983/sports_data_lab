#!/usr/bin/env python3
"""The build gate: what has to be true before an NBA database is worth using.

Three properties matter more than the rest.

IDEMPOTENCE. Building twice must produce the same database, down to the
player_id values -- ids are assigned by sorting on the source's own id, not
by row order, so a rebuild after adding a season does not renumber anybody.
Anything that later references a player depends on this.

RECONCILIATION. Every career column is a groupby over `games`. If they ever
disagree the number still looks plausible, it is just wrong, and a wrong
career_games silently moves an obscurity score and every star rating derived
from it. The build fails rather than writes that.

NULL IS NOT ZERO. A statistic that predates its recording era must be NULL
in `games`, NULL in the career total, and present in stat_coverage with a
null `available_from` rather than omitted -- "column present but never
populated" is a different fact from "not a column at all".
"""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---


import json
import sqlite3
import time
from pathlib import Path

import pytest

from nba import build_nba_db
import core
import health
import nba_fixture
from nba import nba_source
import sports

SCHEMA = sports.NBA_SCHEMA


@pytest.fixture(scope="module")
def con(nba_db):
    connection = sqlite3.connect(f"file:{nba_db}?mode=ro", uri=True)
    yield connection
    connection.close()


def build_into(root, seasons=None):
    """A fresh build under `root`. Returns (db_path, summary)."""
    nba_fixture.write(root / "csv")
    db = root / "nba.db"
    summary = build_nba_db.build(
        db, nba_source.CsvNbaSource(root / "csv"), seasons=seasons,
        verbose=False)
    return db, summary


def snapshot(db):
    """Everything a rebuild must reproduce exactly."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in tables if t != "source_manifest"}
        players = con.execute(
            "SELECT player_id, player, career_games, career_points, "
            "obscurity FROM players ORDER BY player_id").fetchall()
        return counts, players
    finally:
        con.close()


# --------------------------------------------------------- the schema gate

def test_require_schema_passes(con):
    core.require_schema(con, SCHEMA)


def test_the_engine_tables_carry_every_declared_stat(con):
    columns = {r[1] for r in con.execute("PRAGMA table_info(games)")}
    assert set(SCHEMA.stats) <= columns


def test_every_reference_and_derived_table_exists(con):
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"players", "games", "franchises", "teams", "team_aliases",
            "matches", "player_seasons", "team_seasons",
            "player_team_history", "stat_coverage", "source_manifest",
            "source_issues", "meta"} <= tables


# ------------------------------------------------------------- integrity

def test_the_health_checks_are_clean(con):
    assert health.integrity_warnings(con, SCHEMA) == []


def test_career_totals_reconcile_with_the_games_they_came_from(con):
    assert health.career_totals_reconcile(con, SCHEMA) == []


def test_there_are_no_duplicate_player_match_rows(con):
    dupes = con.execute(
        "SELECT COUNT(*) FROM (SELECT player_id, match_id FROM games "
        "GROUP BY player_id, match_id HAVING COUNT(*) > 1)").fetchone()[0]
    assert dupes == 0


def test_the_fixtures_duplicate_box_score_was_collapsed_and_recorded(con):
    """The fixture contains one exact repeat; it must be gone and logged."""
    kinds = {r[0] for r in con.execute("SELECT kind FROM source_issues")}
    assert "duplicate_collapsed" in kinds


def test_no_game_row_is_an_orphan(con):
    assert con.execute(
        "SELECT COUNT(*) FROM games g LEFT JOIN players p "
        "ON p.player_id = g.player_id WHERE p.player_id IS NULL"
    ).fetchone()[0] == 0
    assert con.execute(
        "SELECT COUNT(*) FROM games g LEFT JOIN matches m "
        "ON m.match_id = g.match_id WHERE m.match_id IS NULL"
    ).fetchone()[0] == 0


def test_career_game_numbers_are_a_gapless_sequence_per_player(con):
    """They are derived, so a dropped duplicate has to renumber the rest."""
    for player_id, games in con.execute(
            "SELECT player_id, career_games FROM players"):
        numbers = [r[0] for r in con.execute(
            "SELECT career_game_no FROM games WHERE player_id=? "
            "ORDER BY career_game_no", (player_id,))]
        assert numbers == list(range(1, games + 1)), player_id


def test_no_result_is_recorded_as_a_draw(con):
    """The NBA plays overtime. A game with a score has a winner."""
    assert con.execute(
        "SELECT COUNT(*) FROM games WHERE result NOT IN ('W','L')"
    ).fetchone()[0] == 0


def test_seasons_are_start_years(con):
    """1971 for 1971-72, so numeric season comparisons mean what they say."""
    season, label = con.execute(
        "SELECT season, season_label FROM games ORDER BY season LIMIT 1"
    ).fetchone()
    assert season == 1971
    assert label == "1971-72"


# ------------------------------------------------------ null is not zero

def test_a_pre_era_statistic_is_null_in_games(con):
    assert con.execute(
        "SELECT COUNT(*) FROM games WHERE season < 1973 AND steals = 0"
    ).fetchone()[0] == 0
    assert con.execute(
        "SELECT COUNT(*) FROM games WHERE season = 1971 "
        "AND steals IS NOT NULL").fetchone()[0] == 0


def test_a_wholly_pre_era_career_total_is_null_not_zero(con):
    row = con.execute(
        "SELECT career_steals, career_blocks, career_points FROM players "
        "WHERE debut_season = 1971 AND final_season = 1971").fetchone()
    assert row[0] is None
    assert row[1] is None
    assert row[2] > 0


def test_a_never_populated_stat_gets_a_coverage_row_not_an_omission(con):
    """"Loaded but empty" and "not a column" are different facts."""
    rows = dict(con.execute(
        "SELECT stat_name, available_from FROM stat_coverage"))
    assert set(rows) == set(SCHEMA.stats)
    assert rows["fouls"] is None                    # never populated
    assert rows["points"] == 1971


def test_measured_eras_match_what_is_in_the_games_table(con):
    measured = dict(con.execute(
        "SELECT stat_name, available_from FROM stat_coverage "
        "WHERE available_from IS NOT NULL"))
    for stat, season in measured.items():
        actual = con.execute(
            f"SELECT MIN(season) FROM games "
            f"WHERE {stat} IS NOT NULL AND {stat} != 0").fetchone()[0]
        assert actual == season, stat


# ---------------------------------------------------------- derived tables

def test_the_champion_is_derived_from_the_last_finals_game(con):
    champions = con.execute(
        "SELECT season, club_now FROM team_seasons WHERE champion = 1"
    ).fetchall()
    assert champions == [(2009, "Boston Celtics")]


def test_a_team_that_played_no_playoff_game_did_not_make_the_playoffs(con):
    for season, club, made in con.execute(
            "SELECT season, club_now, made_playoffs FROM team_seasons "
            "WHERE phase = 'regular'"):
        played = con.execute(
            "SELECT COUNT(*) FROM matches WHERE season=? AND phase='playoff' "
            "AND (home_team=? OR away_team=?)", (season, club, club)
        ).fetchone()[0]
        assert made == int(played > 0), (season, club)


def test_team_season_records_come_from_matches_not_from_player_rows(con):
    """Otherwise a team's record depends on how many players it fielded."""
    for season, club, played in con.execute(
            "SELECT season, club_now, played FROM team_seasons "
            "WHERE phase = 'regular'"):
        actual = con.execute(
            "SELECT COUNT(*) FROM matches WHERE season=? AND phase='regular' "
            "AND (home_team=? OR away_team=?)", (season, club, club)
        ).fetchone()[0]
        assert played == actual, (season, club)


def test_player_team_history_agrees_with_the_games(con):
    for player_id, team_id, games in con.execute(
            "SELECT player_id, team_id, games FROM player_team_history"):
        actual = con.execute(
            "SELECT COUNT(*) FROM games g JOIN teams t ON t.name = g.club_hist "
            "WHERE g.player_id=? AND t.team_id=?", (player_id, team_id)
        ).fetchone()[0]
        assert games == actual, (player_id, team_id)


def test_player_seasons_totals_match_the_games_they_summarise(con):
    for player_id, season, phase, club, games, points in con.execute(
            "SELECT player_id, season, phase, club_now, games, points "
            "FROM player_seasons"):
        actual, total = con.execute(
            "SELECT COUNT(*), SUM(points) FROM games WHERE player_id=? "
            "AND season=? AND is_playoff=? AND club_now=?",
            (player_id, season, int(phase == "playoff"), club)).fetchone()
        assert games == actual
        assert points == total


# ------------------------------------------------------------ idempotence

def test_building_twice_produces_the_same_database(tmp_path):
    db, _ = build_into(tmp_path)
    before = snapshot(db)
    build_into(tmp_path)
    assert snapshot(db) == before


def test_adding_a_season_does_not_renumber_existing_players(tmp_path):
    """player_id is assigned from the source's own id, not from row order."""
    early, _ = build_into(tmp_path / "a", seasons=[1971, 2006])
    ids_early = dict(sqlite3.connect(early).execute(
        "SELECT player, player_id FROM players"))

    full, _ = build_into(tmp_path / "b")
    ids_full = dict(sqlite3.connect(full).execute(
        "SELECT player, player_id FROM players"))

    shared = set(ids_early) & set(ids_full)
    assert shared
    for name in shared:
        assert ids_early[name] == ids_full[name], name


def test_meta_is_rewritten_rather_than_accumulated(tmp_path):
    db, _ = build_into(tmp_path)
    build_into(tmp_path)
    keys = [r[0] for r in sqlite3.connect(db).execute("SELECT key FROM meta")]
    assert len(keys) == len(set(keys))


# --------------------------------------------------------- the strict gate

def test_a_corrupted_career_total_fails_the_next_build(tmp_path):
    """The reconciliation check has to actually stop a build."""
    db, _ = build_into(tmp_path)
    con = sqlite3.connect(db)
    con.execute("UPDATE players SET career_games = career_games + 5 "
                "WHERE player_id = 1")
    con.commit()
    con.close()
    assert health.career_totals_reconcile(
        sqlite3.connect(db), SCHEMA) != []


def test_an_unrecognised_playoff_round_fails_the_build(tmp_path):
    """A renamed Finals round makes every championship square answer nobody,
    which reads as "no player has ever won a title" rather than as a bug."""
    root = tmp_path / "rounds"
    nba_fixture.write(root / "csv")
    path = root / "csv" / "matches_2009.csv"
    path.write_text(path.read_text(encoding="utf-8").replace(",F,", ",GF,"),
                    encoding="utf-8")
    with pytest.raises(build_nba_db.BuildError):
        build_nba_db.build(root / "nba.db",
                           nba_source.CsvNbaSource(root / "csv"),
                           verbose=False)
    kinds = {r[0] for r in sqlite3.connect(root / "nba.db").execute(
        "SELECT kind FROM source_issues")}
    assert "unknown_playoff_round" in kinds
    assert "no_finals_round" in kinds


def test_an_unknown_team_is_recorded_as_an_error_issue(tmp_path):
    """And an error issue fails the strict gate."""
    root = tmp_path / "broken"
    nba_fixture.write(root / "csv")
    path = root / "csv" / "matches_2010.csv"
    path.write_text(path.read_text(encoding="utf-8").replace("okc", "nope"),
                    encoding="utf-8")
    with pytest.raises(build_nba_db.BuildError):
        build_nba_db.build(root / "nba.db",
                           nba_source.CsvNbaSource(root / "csv"),
                           verbose=False)
    kinds = {r[0] for r in sqlite3.connect(root / "nba.db").execute(
        "SELECT kind FROM source_issues")}
    assert "unknown_team" in kinds


def test_a_player_named_by_game_logs_can_fill_a_static_index_gap(tmp_path):
    """NBA.com's static index omits some historical players even though its
    game log supplies both their id and name. Those games must not be lost."""
    root = tmp_path / "discovered-player"
    nba_fixture.write(root / "csv")
    source = nba_source.CsvNbaSource(root / "csv")
    players = source.players()
    missing_id = str(source.player_games(
        source.seasons()[0], "regular").iloc[0]["source_player_id"])
    discovered = players[
        players["source_player_id"].astype(str) == missing_id].copy()
    source.players = lambda: players[
        players["source_player_id"].astype(str) != missing_id].copy()
    source.discovered_players = lambda ids: discovered[
        discovered["source_player_id"].astype(str).isin(ids)].copy()

    db = root / "nba.db"
    build_nba_db.build(db, source, verbose=False)

    con = sqlite3.connect(db)
    assert con.execute(
        "SELECT COUNT(*) FROM games g JOIN players p USING (player_id) "
        "WHERE p.source_player_id=?", (missing_id,)).fetchone()[0] > 0
    assert con.execute(
        "SELECT COUNT(*) FROM source_issues WHERE kind='unknown_player'"
    ).fetchone()[0] == 0
    con.close()


def test_an_unresolved_match_team_takes_the_match_out_of_the_schedule(tmp_path):
    """Keeping it left a fixture whose result counted for nobody and whose
    player-games joined to a NULL club -- visible only in an issue row."""
    root = tmp_path / "dropped"
    nba_fixture.write(root / "csv")
    path = root / "csv" / "matches_2010.csv"
    path.write_text(path.read_text(encoding="utf-8").replace("okc", "nope"),
                    encoding="utf-8")
    with pytest.raises(build_nba_db.BuildError):
        build_nba_db.build(root / "nba.db",
                           nba_source.CsvNbaSource(root / "csv"),
                           verbose=False)
    con = sqlite3.connect(root / "nba.db")
    assert con.execute("SELECT COUNT(*) FROM matches WHERE season=2010"
                       ).fetchone()[0] == 0
    assert con.execute(
        "SELECT COUNT(*) FROM matches WHERE home_team IS NULL "
        "OR away_team IS NULL").fetchone()[0] == 0
    # And its box scores went with it, rather than surviving as rows whose
    # club_now is NULL.
    assert con.execute("SELECT COUNT(*) FROM games WHERE season=2010"
                       ).fetchone()[0] == 0


def test_a_match_named_twice_with_a_different_score_is_rejected(tmp_path):
    """drop_duplicates(keep='first') resolved this by row order. The losing
    copy's score decides a win, a standing and possibly a title."""
    root = tmp_path / "conflict"
    nba_fixture.write(root / "csv")
    path = root / "csv" / "matches_2010.csv"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines.append(lines[1].replace(",121,118,", ",118,121,"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(build_nba_db.BuildError):
        build_nba_db.build(root / "nba.db",
                           nba_source.CsvNbaSource(root / "csv"),
                           verbose=False)
    con = sqlite3.connect(root / "nba.db")
    kinds = {r[0] for r in con.execute("SELECT kind FROM source_issues")}
    assert "conflicting_match" in kinds
    assert con.execute("SELECT COUNT(*) FROM matches WHERE match_id='g2010a'"
                       ).fetchone()[0] == 0


def test_an_exact_duplicate_match_row_is_collapsed_not_rejected(tmp_path):
    """The same fixture arriving twice is the ordinary case and must not
    cost the build a game."""
    root = tmp_path / "twice"
    nba_fixture.write(root / "csv")
    path = root / "csv" / "matches_2010.csv"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines.append(lines[1])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    db = root / "nba.db"
    build_nba_db.build(db, nba_source.CsvNbaSource(root / "csv"),
                       verbose=False)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM matches WHERE match_id='g2010a'"
                       ).fetchone()[0] == 1
    kinds = {r[0] for r in con.execute("SELECT kind FROM source_issues")}
    assert "duplicate_match_collapsed" in kinds
    assert "conflicting_match" not in kinds


def test_a_player_game_for_a_team_not_in_its_match_is_rejected(tmp_path):
    """The row would otherwise add a game and a season to a career at a team
    that never played it."""
    root = tmp_path / "wrongteam"
    nba_fixture.write(root / "csv")
    path = root / "csv" / "player_games_2010_regular.csv"
    text = path.read_text(encoding="utf-8")
    # p4 played this game for the Lakers. Refile him under Boston, who were
    # not in it.
    path.write_text(text.replace("g2010a,lal", "g2010a,bos"), encoding="utf-8")

    with pytest.raises(build_nba_db.BuildError):
        build_nba_db.build(root / "nba.db",
                           nba_source.CsvNbaSource(root / "csv"),
                           verbose=False)
    con = sqlite3.connect(root / "nba.db")
    kinds = {r[0] for r in con.execute("SELECT kind FROM source_issues")}
    assert "team_not_in_match" in kinds
    assert con.execute(
        "SELECT COUNT(*) FROM games WHERE match_id='g2010a' "
        "AND club_now='Boston Celtics'").fetchone()[0] == 0


# ---------------------------------------------------------- atomic build

def test_a_failed_build_leaves_the_live_database_untouched(tmp_path):
    """The whole point. A source that has changed shape must not be able to
    replace a database the application is serving."""
    root = tmp_path / "atomic"
    nba_fixture.write(root / "csv")
    db = root / "nba.db"
    build_nba_db.build_atomic(db, nba_source.CsvNbaSource(root / "csv"),
                              verbose=False)
    good = snapshot(db)

    path = root / "csv" / "matches_2010.csv"
    path.write_text(path.read_text(encoding="utf-8").replace("okc", "nope"),
                    encoding="utf-8")
    with pytest.raises(build_nba_db.BuildError):
        build_nba_db.build_atomic(db, nba_source.CsvNbaSource(root / "csv"),
                                  verbose=False)

    assert snapshot(db) == good
    # The failed attempt is kept where it can be inspected, with the reason.
    assert (root / "nba.db.building").exists()
    report = json.loads(
        (root / "nba.db.build-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert "Health checks failed" in report["reason"]


def test_a_passing_build_is_promoted_and_the_previous_one_backed_up(tmp_path):
    root = tmp_path / "promote"
    nba_fixture.write(root / "csv")
    db = root / "nba.db"

    first = build_nba_db.build_atomic(
        db, nba_source.CsvNbaSource(root / "csv"), verbose=False)
    assert first["backup"] is None          # nothing to back up yet
    assert not (root / "nba.db.building").exists()

    second = build_nba_db.build_atomic(
        db, nba_source.CsvNbaSource(root / "csv"), verbose=False)
    backup = Path(second["backup"])
    assert backup.exists() and backup.parent.name == "backups"
    assert snapshot(backup) == snapshot(db)


def test_backups_are_pruned_to_the_requested_depth(tmp_path):
    root = tmp_path / "prune"
    nba_fixture.write(root / "csv")
    db = root / "nba.db"
    for _ in range(4):
        build_nba_db.build_atomic(db, nba_source.CsvNbaSource(root / "csv"),
                                  keep_backups=2, verbose=False)
        time.sleep(1.05)    # the backup stamp has one-second resolution
    assert len(list((root / "backups").glob("nba-*.db"))) == 2


def test_a_failed_build_writes_no_reference_file(tmp_path):
    """load_reference writes into the shared reference directory. A build
    that is never promoted must not leave its view of the teams there."""
    root = tmp_path / "noref"
    nba_fixture.write(root / "csv")
    path = root / "csv" / "matches_2010.csv"
    path.write_text(path.read_text(encoding="utf-8").replace("okc", "nope"),
                    encoding="utf-8")
    with pytest.raises(build_nba_db.BuildError):
        build_nba_db.build_atomic(root / "nba.db",
                                  nba_source.CsvNbaSource(root / "csv"),
                                  verbose=False)
    assert not (root / "reference" / "nba_reference.json").exists()


# -------------------------------------------------------------- reference

def test_the_reference_file_is_written_beside_its_own_database(tmp_path):
    """A fixture build must never overwrite the real data/nba reference."""
    db, _ = build_into(tmp_path)
    path = tmp_path / "reference" / "nba_reference.json"
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["teams"] == sorted(payload["teams"])
    assert "Oklahoma City Thunder" in payload["teams"]


def test_the_reference_lineage_only_lists_identities_that_were_played_under(
        tmp_path):
    db, _ = build_into(tmp_path)
    payload = json.loads(
        (tmp_path / "reference" / "nba_reference.json").read_text("utf-8"))
    lineage = payload["club_lineage"]["Oklahoma City Thunder"]
    assert lineage[0] == "Oklahoma City Thunder"
    assert "Seattle SuperSonics" in lineage


def test_the_reference_eras_are_measured_not_declared(tmp_path):
    db, _ = build_into(tmp_path)
    payload = json.loads(
        (tmp_path / "reference" / "nba_reference.json").read_text("utf-8"))
    assert payload["stat_eras"]["steals"] == 2006     # the fixture's first
    assert "fouls" not in payload["stat_eras"]        # never populated


def main():
    import subprocess
    return subprocess.call([_sys.executable, "-m", "pytest", __file__, "-q"])


if __name__ == "__main__":
    _sys.exit(main())
