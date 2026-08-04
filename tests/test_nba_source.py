#!/usr/bin/env python3
"""The source contract: reject a missing column, never invent a zero.

`validate` is four lines and it is the most important four lines in
nba_source.py. A source that cannot produce `steals` and a source that
produces NULL steals are different situations: the first is broken and the
build must stop, the second is 1946-1973 and the build must carry it
through untouched. A `reindex(fill_value=0)` would collapse both into
"every player recorded zero steals", which reads as a fact about the
players and would rank the entire early league as maximally obscure.

Also covers the manifest: one row per retrieval, keyed so a repeat fetch
updates rather than accumulates, and carrying the digest of the bytes as
received so two disagreeing builds can be traced to a source that changed.
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
import nba_source
import nba_source_api


@pytest.fixture
def csv_root(tmp_path):
    return nba_fixture.write(tmp_path / "csv")


@pytest.fixture
def source(csv_root):
    return nba_source.CsvNbaSource(csv_root)


# ----------------------------------------------------------- the adapter

def test_the_csv_adapter_reads_every_part_of_the_contract(source):
    assert source.key == "csv"
    assert source.seasons() == sorted(nba_fixture.SEASONS)
    assert list(source.teams().columns) == list(nba_source.TEAM_COLUMNS)
    assert list(source.players().columns) == list(nba_source.PLAYER_COLUMNS)
    assert list(source.matches(2009).columns) == list(nba_source.MATCH_COLUMNS)
    games = source.player_games(2009, "regular")
    assert list(games.columns) == list(nba_source.PLAYER_GAME_COLUMNS)


def test_a_season_the_source_does_not_have_is_absent_not_an_error(source):
    assert source.matches(1999) is None
    assert source.player_games(1999, "regular") is None


def test_a_missing_required_file_is_an_error(tmp_path):
    empty = nba_source.CsvNbaSource(tmp_path / "nothing")
    with pytest.raises(nba_source.SourceError, match="teams"):
        empty.teams()


def test_an_unknown_phase_is_rejected(source):
    with pytest.raises(nba_source.SourceError, match="phase"):
        source.player_games(2009, "preseason")


# ------------------------------------------------------------- validate

def test_validate_names_the_missing_columns_and_does_not_fill_them():
    frame = pd.DataFrame({"points": [1], "assists": [2]})
    with pytest.raises(nba_source.SourceError) as exc:
        nba_source.validate(frame, ("points", "assists", "steals"), "games")
    assert "steals" in str(exc.value)
    assert "0" not in str(exc.value).split("Got:")[0].replace("games", "")


def test_validate_returns_the_requested_column_order():
    frame = pd.DataFrame({"b": [1], "a": [2], "extra": [3]})
    out = nba_source.validate(frame, ("a", "b"), "kind")
    assert list(out.columns) == ["a", "b"]


def test_a_blank_stat_cell_stays_null(source):
    """1971: steals, blocks and three-pointers were not recorded."""
    games = source.player_games(1971, "regular")
    assert games["steals"].isna().all()
    assert games["blocks"].isna().all()
    assert games["fg3m"].isna().all()
    assert (games["points"] > 0).all()


# ------------------------------------------------------------- utilities

def test_parse_seasons_ranges_are_inclusive_at_both_ends():
    assert nba_source.parse_seasons("1996-1999") == [1996, 1997, 1998, 1999]
    assert nba_source.parse_seasons("2020") == [2020]
    assert nba_source.parse_seasons("1996,2000-2001") == [1996, 2000, 2001]


def test_a_backwards_season_range_is_rejected():
    with pytest.raises(nba_source.SourceError, match="backwards"):
        nba_source.parse_seasons("2005-1999")


def test_digest_is_stable_across_str_and_bytes():
    assert nba_source.digest_bytes("x") == nba_source.digest_bytes(b"x")
    assert nba_source.digest_bytes(b"x") != nba_source.digest_bytes(b"y")


def test_canonical_params_is_order_independent():
    assert (nba_source.canonical_params(b=2, a=1)
            == nba_source.canonical_params(a=1, b=2))


def test_get_source_defaults_to_csv_and_rejects_the_unknown():
    assert nba_source.get_source("csv").key == "csv"
    assert nba_source.get_source(None).key == "csv"
    with pytest.raises(nba_source.SourceError, match="unknown source"):
        nba_source.get_source("basketball-reference")


# ------------------------------------------------------------- manifest

def test_every_read_records_a_fetch(source):
    source.teams()
    source.players()
    fetches = source.fetches()
    assert len(fetches) == 2
    for fetch in fetches:
        assert fetch.source_key == "csv"
        assert fetch.digest and len(fetch.digest) == 64
        assert fetch.rows > 0
        assert fetch.fetched_at


def test_the_same_file_read_twice_digests_the_same(csv_root):
    first = nba_source.CsvNbaSource(csv_root)
    first.teams()
    second = nba_source.CsvNbaSource(csv_root)
    second.teams()
    assert first.fetches()[0].digest == second.fetches()[0].digest


def test_fetches_round_trip_into_the_manifest_without_accumulating(source):
    source.teams()
    con = sqlite3.connect(":memory:")
    con.execute("""CREATE TABLE source_manifest (
        source_key TEXT NOT NULL, endpoint TEXT NOT NULL, params TEXT NOT NULL,
        season INTEGER, phase TEXT, fetched_at TEXT NOT NULL,
        digest TEXT NOT NULL, rows INTEGER, path TEXT,
        PRIMARY KEY (source_key, endpoint, params))""")
    rows = [(f.source_key, f.endpoint, f.params, f.season, f.phase,
             f.fetched_at, f.digest, f.rows, f.path) for f in source.fetches()]
    for _ in range(2):          # a repeat build must not duplicate the row
        con.executemany("INSERT OR REPLACE INTO source_manifest VALUES "
                        "(?,?,?,?,?,?,?,?,?)", rows)
    assert con.execute("SELECT COUNT(*) FROM source_manifest").fetchone()[0] \
        == len(rows)


# ---------------------------------------------------------- the API adapter
# Pure functions only. Nothing here touches the network, and nba_api does
# not need to be installed.

@pytest.mark.parametrize("raw,expected", [
    ("34:30", 34.5), ("28", 28.0), ("0:00", 0.0),
    ("", None), (None, None), ("nan", None), ("--", None),
])
def test_minutes_parse_to_float_or_none_but_never_a_stray_zero(raw, expected):
    assert nba_source_api.parse_minutes(raw) == expected


def test_a_blank_minute_is_none_and_not_zero():
    """Minutes are not recorded before 1951-52. 0.0 would be a claim."""
    assert nba_source_api.parse_minutes("") is None
    assert nba_source_api.parse_minutes("0") == 0.0     # a real zero survives


@pytest.mark.parametrize("season,label", [
    (1946, "1946-47"), (1996, "1996-97"), (1999, "1999-00"),
    (2009, "2009-10"), (2025, "2025-26"),
])
def test_season_labels_render_the_crossover_year(season, label):
    assert nba_source_api.season_label(season) == label


def test_numeric_keeps_a_real_zero_and_drops_a_blank():
    assert nba_source_api.numeric("0") == 0.0
    assert nba_source_api.numeric("") is None
    assert nba_source_api.numeric("12.5") == 12.5


def test_the_api_adapter_says_so_when_nba_api_is_absent(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake(name, *args, **kwargs):
        if name == "nba_api":
            raise ImportError("no nba_api")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(nba_source.SourceError, match="nba_api"):
        nba_source_api.NbaApiSource._require_nba_api()


def main():
    import subprocess
    return subprocess.call([_sys.executable, "-m", "pytest", __file__, "-q"])


if __name__ == "__main__":
    _sys.exit(main())
