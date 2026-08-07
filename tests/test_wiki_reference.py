"""The Wikipedia scrape must not be trusted just because it finished.

wiki_reference.py stages a scrape whose shape is decided by Wikipedia
editors, so the risks are all in what the import is willing to accept:

  * a run that completed with fewer teams than the league has, or with a
    page that failed, must not reach the database;
  * a re-import must be idempotent, and must not turn a table an editor
    moved down the page into three thousand new records;
  * ``team_slug`` must never become the team key, because it changes on a
    rebrand -- four MLB clubs are already named differently by Wikipedia
    and by this database;
  * an existing curated value must not be overwritten by an infobox cell.
"""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---

import csv
import json
import sqlite3
import tempfile
from pathlib import Path

import club_reference as CR
from utils.shared import load_wiki_reference as L
import wiki_reference as W

TEAM_COLUMNS = ["conference", "division", "team", "location", "arena",
                "capacity", "founded", "joined", "team_page_title",
                "team_url", "source_page", "source_url", "source_revision",
                "scraped_at_utc", "coordinates"]

RECORD_COLUMNS = ["sport", "team", "team_slug", "section", "record_type",
                  "table_index", "row_index", "label", "value", "data_json",
                  "source_page", "source_url", "source_revision",
                  "scraped_at_utc"]

STAMP = "2026-08-06T12:00:54+00:00"


def _team_row(team: str, **overrides) -> dict:
    row = {
        "conference": "Eastern Conference", "division": "Atlantic",
        "team": team, "location": "Boston, Massachusetts",
        "arena": "TD Garden", "capacity": "19156",
        "founded": "1946", "joined": "1946",
        "team_page_title": team,
        "team_url": f"https://en.wikipedia.org/wiki/{team.replace(' ', '_')}",
        "source_page": "National Basketball Association",
        "source_url": "https://en.wikipedia.org/wiki/"
                      "National_Basketball_Association",
        "source_revision": "1363765440", "scraped_at_utc": STAMP,
        "coordinates": "42°21′59″N",
    }
    row.update(overrides)
    return row


def _record(team: str, section: str, **overrides) -> dict:
    row = {
        "sport": "nba", "team": team,
        "team_slug": team.lower().replace(" ", "_"),
        "section": section, "record_type": "table_row",
        "table_index": "0", "row_index": "1",
        "label": "Bob Cousy", "value": "",
        "data_json": json.dumps({"name": "Bob Cousy", "tenure": "1950-1963"}),
        "source_page": team,
        "source_url": f"https://en.wikipedia.org/wiki/{team}",
        "source_revision": "123", "scraped_at_utc": STAMP,
    }
    row.update(overrides)
    return row


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def build_scrape(root: Path, *, teams: int = 30, failures: int = 0,
                 records: list[dict] | None = None) -> Path:
    """A miniature scrape output tree, in the real one's shape."""
    folder = root / "nba"
    names = [f"Club {index:02d}" for index in range(teams)]
    _write_csv(folder / "team.csv", TEAM_COLUMNS,
               [_team_row(name) for name in names])

    if records is None:
        records = [_record(names[0], "Captains")]
    _write_csv(folder / "team_stats.csv", RECORD_COLUMNS, records)
    _write_csv(folder / "league_stats.csv", RECORD_COLUMNS,
               [_record("", "Championships", team_slug="",
                        source_page="National Basketball Association")])
    _write_csv(folder / "hall_of_fame.csv", RECORD_COLUMNS,
               [_record("", "Players", team_slug="",
                        source_page="List of players")])

    (folder / "metadata.json").write_text(json.dumps({
        "sport": "nba", "label": "NBA", "teams": teams,
        "team_page_failures": failures,
        "team_reference_rows": len(records),
        "league_reference_rows": 1, "hall_of_fame_rows": 1,
        "generated_at_utc": STAMP,
    }), encoding="utf-8")

    _write_csv(root / "scrape_log.csv",
               ["timestamp", "sport", "item", "status", "message",
                "output_path"],
               [{"timestamp": STAMP, "sport": "NBA", "item": "team.csv",
                 "status": "PASS", "message": f"Saved {teams} teams",
                 "output_path": str(folder / "team.csv")}])
    (root / "scrape_metadata.json").write_text(json.dumps({
        "application": "Wikipedia Sports Scraper", "version": "2.0.0",
        "generated_at_utc": STAMP,
    }), encoding="utf-8")
    return root


def build_db(path: Path, names: list[str]) -> None:
    """A database with just the `clubs` table the import reads."""
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE clubs (
        club_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
        abbreviation TEXT NOT NULL, db_club_now TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL)""")
    con.executemany(
        "INSERT INTO clubs VALUES (?,?,?,?,1,?)",
        [(name.lower().replace(" ", "_"), name, name[:3].upper(), name, STAMP)
         for name in names])
    con.commit()
    con.close()


class _Scrape:
    """A scrape tree and a database, torn down together."""

    def __init__(self, **kwargs):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "wiki"
        build_scrape(self.root, **kwargs)
        self.db = Path(self._tmp.name) / "nba.db"
        teams = W.read_csv(self.root / "nba" / "team.csv")
        build_db(self.db, [row["team"] for row in teams])

    def run(self, **kwargs) -> bool:
        return L.import_sport("nba", self.root, str(self.db), **kwargs)

    def con(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db)

    def close(self) -> None:
        self._tmp.cleanup()


# ----------------------------------------------------------- validation

def test_a_short_team_list_is_an_error():
    """29 NBA teams means the league table changed, not that one folded."""
    scrape = _Scrape(teams=29)
    try:
        report = W.validate(scrape.root, "nba")
        assert not report.ok
        assert any("expected 30" in line for line in report.errors)
        assert scrape.run() is False
        con = scrape.con()
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert W.REFERENCE_STAGE not in tables, "wrote despite failing"
        con.close()
    finally:
        scrape.close()


def test_a_failed_team_page_blocks_the_import():
    scrape = _Scrape(failures=2)
    try:
        report = W.validate(scrape.root, "nba")
        assert not report.ok
        assert any("failed to scrape" in line for line in report.errors)
    finally:
        scrape.close()


def test_broken_json_is_an_error_but_a_missing_file_is_not_silent():
    scrape = _Scrape(records=[_record("Club 00", "Captains",
                                      data_json="{not json")])
    try:
        report = W.validate(scrape.root, "nba")
        assert any("invalid data_json" in line for line in report.errors)
    finally:
        scrape.close()

    scrape = _Scrape()
    try:
        (scrape.root / "nba" / "hall_of_fame.csv").unlink()
        report = W.validate(scrape.root, "nba")
        assert any("hall_of_fame.csv" in line for line in report.errors)
    finally:
        scrape.close()


def test_an_empty_record_file_warns_rather_than_failing():
    """MLB's league_stats.csv is genuinely empty in the shipped run."""
    scrape = _Scrape()
    try:
        _write_csv(scrape.root / "nba" / "league_stats.csv",
                   RECORD_COLUMNS, [])
        report = W.validate(scrape.root, "nba")
        assert report.ok
        assert any("league_stats" in line for line in report.warnings)
    finally:
        scrape.close()


# ---------------------------------------------------------------- import

def test_records_are_staged_with_their_provenance():
    scrape = _Scrape()
    try:
        assert scrape.run() is True
        con = scrape.con()
        row = con.execute(
            f"SELECT dataset_type, section, record_type, json_key_set, "
            f"       source_url, source_revision, import_batch_id "
            f"FROM {W.REFERENCE_STAGE} WHERE dataset_type = 'team'"
        ).fetchone()
        assert row[0] == "team"
        assert row[1] == "Captains"
        assert row[2] == "table_row"
        assert row[3] == "name|tenure", "keys are not inventoried"
        assert row[4].startswith("https://")
        assert row[5] == 123
        assert row[6] == 1

        # A league record carries no team, by design.
        team = con.execute(
            f"SELECT team FROM {W.REFERENCE_STAGE} "
            f"WHERE dataset_type = 'league'").fetchone()[0]
        assert team is None
        con.close()
    finally:
        scrape.close()


def test_reimporting_the_same_scrape_changes_nothing():
    scrape = _Scrape()
    try:
        scrape.run()
        con = scrape.con()
        before = con.execute(
            f"SELECT COUNT(*) FROM {W.REFERENCE_STAGE}").fetchone()[0]
        hashes = {row[0] for row in con.execute(
            f"SELECT source_record_hash FROM {W.REFERENCE_STAGE}")}
        con.close()

        scrape.run()
        con = scrape.con()
        after = con.execute(
            f"SELECT COUNT(*) FROM {W.REFERENCE_STAGE}").fetchone()[0]
        assert after == before
        assert hashes == {row[0] for row in con.execute(
            f"SELECT source_record_hash FROM {W.REFERENCE_STAGE}")}
        # Both runs are still on the record, even though the rows are not.
        assert con.execute(
            f"SELECT COUNT(*) FROM {W.BATCH_TABLE}").fetchone()[0] == 2
        con.close()
    finally:
        scrape.close()


def test_a_table_moved_down_the_page_is_the_same_record():
    """table_index and row_index are excluded from the hash on purpose."""
    moved = _record("Club 00", "Captains", table_index="7", row_index="42")
    original = _record("Club 00", "Captains")
    assert W.record_hash("team", moved) == W.record_hash("team", original)

    scrape = _Scrape()
    try:
        scrape.run()
        _write_csv(scrape.root / "nba" / "team_stats.csv", RECORD_COLUMNS,
                   [moved])
        scrape.run()
        con = scrape.con()
        rows = con.execute(
            f"SELECT COUNT(*), MAX(table_index) FROM {W.REFERENCE_STAGE} "
            f"WHERE dataset_type = 'team'").fetchone()
        assert rows[0] == 1, "an editor's reshuffle duplicated the record"
        assert rows[1] == 7, "the new position was not recorded"
        con.close()
    finally:
        scrape.close()


def test_a_record_that_left_the_page_leaves_the_staging_table():
    scrape = _Scrape(records=[_record("Club 00", "Captains"),
                              _record("Club 00", "Captains", label="Bill",
                                      data_json='{"name": "Bill"}')])
    try:
        scrape.run()
        _write_csv(scrape.root / "nba" / "team_stats.csv", RECORD_COLUMNS,
                   [_record("Club 00", "Captains")])
        scrape.run()
        con = scrape.con()
        labels = [row[0] for row in con.execute(
            f"SELECT label FROM {W.REFERENCE_STAGE} "
            f"WHERE dataset_type = 'team'")]
        assert labels == ["Bob Cousy"]
        con.close()
    finally:
        scrape.close()


def test_the_data_json_check_constraint_holds():
    scrape = _Scrape()
    try:
        scrape.run()
        con = scrape.con()
        try:
            con.execute(
                f"INSERT INTO {W.REFERENCE_STAGE} (source_record_hash, sport,"
                f" dataset_type, record_type, data_json) "
                f"VALUES ('x', 'nba', 'team', 'table_row', 'not json')")
            raise AssertionError("invalid JSON was accepted")
        except sqlite3.IntegrityError:
            pass
        con.close()
    finally:
        scrape.close()


# --------------------------------------------------------- team mapping

def test_a_renamed_franchise_matches_by_alias_not_by_slug():
    """The four MLB clubs Wikipedia and this database name differently."""
    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "mlb.db"
        build_db(db, ["Cleveland Indians", "Florida Marlins",
                      "Oakland Athletics", "Los Angeles Angels of Anaheim"])
        con = sqlite3.connect(db)
        W.ensure_schema(con)
        resolved = W.map_teams(con, "mlb", [
            {"team": "Cleveland Guardians", "team_page_title": "x"},
            {"team": "Miami Marlins", "team_page_title": "x"},
            {"team": "Athletics", "team_page_title": "x"},
            {"team": "Los Angeles Angels", "team_page_title": "x"},
        ])
        assert resolved["Cleveland Guardians"] == "cleveland_indians"
        assert resolved["Athletics"] == "oakland_athletics"
        assert len(resolved) == 4

        statuses = {row[0] for row in con.execute(
            f"SELECT match_status FROM {W.MAP_TABLE}")}
        assert statuses == {"alias_match"}, "an alias was recorded as exact"
        con.close()


def test_an_unknown_team_is_recorded_unmatched_rather_than_guessed():
    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "nba.db"
        build_db(db, ["Boston Celtics"])
        con = sqlite3.connect(db)
        W.ensure_schema(con)
        resolved = W.map_teams(con, "nba", [
            {"team": "Boston Celtics", "team_page_title": "Boston Celtics"},
            {"team": "Seattle Whatevers", "team_page_title": "x"},
        ])
        assert set(resolved) == {"Boston Celtics"}
        assert W.unresolved(con, "nba") == [("Seattle Whatevers", "unmatched")]
        con.close()


# -------------------------------------------------------------- cleaning

def test_the_raw_cell_survives_alongside_the_parsed_one():
    assert W.year("1901*") == 1901
    assert W.year("1960 (AFL) 1970 (NFL)") == 1960, "took the wrong league"
    assert W.year("1871* (NA)") == 1871
    assert W.year("Various") is None

    assert W.numeric("19156") == 19156
    assert W.numeric("19,156") == 19156
    # Prose is not a capacity; the raw column keeps it instead.
    assert W.numeric("65,878 expandable to 71,000") is None
    assert W.numeric("Various") is None
    assert W.numeric(None) is None


def test_the_founding_footnote_is_not_thrown_away():
    scrape = _Scrape()
    try:
        rows = W.read_csv(scrape.root / "nba" / "team.csv")
        rows[0]["founded"] = "1901*"
        _write_csv(scrape.root / "nba" / "team.csv", TEAM_COLUMNS, rows)
        scrape.run()
        con = scrape.con()
        raw, parsed = con.execute(
            f"SELECT founded_raw, founded_year FROM {W.TEAM_STAGE} "
            f"WHERE team = ?", (rows[0]["team"],)).fetchone()
        assert raw == "1901*", "the footnote was dropped"
        assert parsed == 1901
        con.close()
    finally:
        scrape.close()


def test_canonical_json_ignores_key_order():
    assert W.canonical_json('{"b": 1, "a": 2}') == W.canonical_json(
        '{"a": 2, "b": 1}')
    assert W.key_set('{"b": 1, "a": 2}') == "a|b"
    assert W.key_set("[1, 2]") == "", "a list has no key set"


# ------------------------------------------------- club_wikipedia_fields

def test_an_existing_value_is_never_overwritten():
    scrape = _Scrape()
    try:
        con = scrape.con()
        CR.ensure_table(con)
        CR.write_fields(con, [("club_00", "ground", "Arena", "The Garden")],
                        "a source we trust more")
        con.commit()
        con.close()

        scrape.run()
        con = scrape.con()
        value, source = con.execute(
            f"SELECT field_value, revision_id FROM {CR.TABLE} "
            f"WHERE club_id = 'club_00' AND field_key = 'ground'").fetchone()
        assert value == "The Garden"
        assert source == "a source we trust more"
        # The keys it had nothing for are filled all the same.
        assert con.execute(
            f"SELECT field_value FROM {CR.TABLE} WHERE club_id = 'club_00' "
            f"AND field_key = 'arena_capacity'").fetchone()[0] == "19,156"
        con.close()
    finally:
        scrape.close()


def test_no_fields_leaves_the_field_table_alone():
    scrape = _Scrape()
    try:
        scrape.run(write_fields=False)
        con = scrape.con()
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert W.REFERENCE_STAGE in tables, "the records were not staged"
        count = con.execute(
            f"SELECT COUNT(*) FROM {CR.TABLE}").fetchone()[0] \
            if CR.TABLE in tables else 0
        assert count == 0
        con.close()
    finally:
        scrape.close()


def test_check_only_writes_nothing():
    scrape = _Scrape()
    try:
        assert scrape.run(check_only=True) is True
        con = scrape.con()
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert W.REFERENCE_STAGE not in tables
        con.close()
    finally:
        scrape.close()


def run():
    failures = []
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            try:
                function()
            except Exception as exc:            # noqa: BLE001 -- reported
                failures.append(f"{name}: {exc}")
    for line in failures:
        print(f"FAIL {line}")
    print(f"wiki reference tests: "
          f"{'passed' if not failures else f'{len(failures)} failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
