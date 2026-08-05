#!/usr/bin/env python3
"""Player-level rivalry game log, from Retrosheet's bulk game logs.

Runs as the last step of ``python -m mlb.build_mlb_db``, so a normal build
produces the rivalry table too. Also runs standalone, to reload the table
without rebuilding the whole database from Lahman:

    python -m mlb.load_retrosheet            # fetch (cached), load database
    python -m mlb.load_retrosheet --refresh   # re-download even if cached

Lahman (mlb.db's main source, see build_mlb_db.py) has no box scores --
its finest grain is a player's season with one team. It cannot answer "has
this player won more Yankees-Red Sox games than they've lost", because that
needs to know who actually started which games and who won them.

Retrosheet's game logs do carry that: one row per game since 1871, unlike
the play-by-play event files, with the winning and losing sides plus both
teams' nine starters (Retrosheet ID and lineup position) inline. Full field
layout: https://www.retrosheet.org/gamelogs/glfields.txt -- the indices
below (0-based) are read directly against it, not rediscovered.

Retrosheet's team codes are the same 3-letter codes Lahman's teamID uses
(NYA, BOS, BRO, LAN, ...), so mlb_reference.RIVALRIES's franchise codes
apply to both without a crosswalk. The Retrosheet-ID -> Lahman player_id
crosswalk does need one: it is People.csv's retroID column, which the
Lahman project fills in for exactly this purpose.

Writes two tables:

``mlb_player_rivalry_games(player_id, game_date, game_number, season,
rivalry_key, team_id, opponent_id, is_win)`` -- one row per starter per
qualifying game, feeding the rivalry squares in constraints_mlb.py. Only
starters are recorded: a bench player who entered a rivalry game late was
not part of the lineup Retrosheet records inline, and crediting them would
need the play-by-play files this loader deliberately avoids.

``club_match_sources`` -- every game ever played, as the two
club-perspective rows afl/club_history.py expects. That module is generic
despite where it lives, so filling this table is the whole of what turns
the Past Games page on for the MLB. Nothing else can feed it: Lahman's
finest grain is a player-season, so before this there were no MLB matches
in the database to list.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sqlite3
import urllib.request
import zipfile
from pathlib import Path

from data_paths import cache_dir, raw_dir, sport_db

from . import mlb_reference

GAMELOGS_URL = "https://www.retrosheet.org/gamelogs/gl1871_2025.zip"
USER_AGENT = "SportsDataLab/1.0 (personal research; contact via repository)"

#: The club-history table afl/club_history.py reads. Populating it is what
#: turns the Past Games page on for a sport -- the module is generic, only
#: its location is historical.
MATCH_TABLE = "club_match_sources"

#: Postseason game-log files -> the round label they hold. These are named
#: files rather than year files, in the same 161-field format.
#:
#: glas.txt (the All-Star game) is deliberately absent: it is an exhibition
#: between two leagues rather than a match between two clubs, and a
#: club-history table listing it would be claiming the American League is a
#: franchise you can look up.
POSTSEASON_FILES = {
    "glwc.txt": "WC",       # Wild Card
    "gldv.txt": "DS",       # Division Series
    "gllc.txt": "LCS",      # League Championship Series
    "glws.txt": "WS",       # World Series
}

# 0-based field indices into a Retrosheet game-log row. See the module
# docstring for where this layout is documented.
DATE = 0
GAME_NUMBER = 1
VIS_TEAM = 3
HOME_TEAM = 6
VIS_SCORE = 9
HOME_SCORE = 10
PARK_ID = 16
ATTENDANCE = 17
VIS_STARTING_PITCHER = 101
HOME_STARTING_PITCHER = 103
VIS_STARTERS = 105       # 9 x (id, name, position) = 27 fields, to 132
HOME_STARTERS = 132      # 27 fields, to 159

YEAR_FILE = re.compile(r"gl(1[89]\d{2}|20\d{2})\.txt", re.I)


def _download(refresh: bool = False) -> Path:
    folder = cache_dir("mlb", "retrosheet")
    folder.mkdir(parents=True, exist_ok=True)
    zip_path = folder / "gamelogs.zip"
    if refresh or not zip_path.exists():
        request = urllib.request.Request(
            GAMELOGS_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=180) as response:
            zip_path.write_bytes(response.read())
    return zip_path


def _game_rows(zip_path: Path):
    """Every regular-season game-log row, oldest file first.

    The zip also carries glas/gldv/gllc/glwc/glws.txt -- All-Star and
    postseason-round logs in the same format but not what "won more
    Yankees-Red Sox games" means, so only the year files are read.
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = sorted(n for n in zf.namelist() if YEAR_FILE.fullmatch(n))
        for name in names:
            with zf.open(name) as raw:
                yield from csv.reader(io.TextIOWrapper(raw, encoding="latin-1"))


def _postseason_game_rows(zip_path: Path):
    """Every postseason game-log row, as (row, round_label)."""
    with zipfile.ZipFile(zip_path) as zf:
        available = set(zf.namelist())
        for name, round_label in POSTSEASON_FILES.items():
            if name not in available:
                continue
            with zf.open(name) as raw:
                for row in csv.reader(io.TextIOWrapper(raw, encoding="latin-1")):
                    yield row, round_label


def _lahman_rows(filename: str):
    """A Lahman CSV as dicts, or nothing if it is not there.

    utf-8-sig, not utf-8: every Lahman export carries a BOM, which under
    plain utf-8 becomes part of the *first* column's name -- 'yearID'
    silently turns into '\\ufeffyearID' and every lookup on it misses.
    """
    path = raw_dir("mlb") / filename
    if not path.exists():
        return
    with open(path, encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def _team_names() -> dict[tuple[int, str], str]:
    """(season, Retrosheet team code) -> that club's name in that season.

    Keyed on `teamIDretro` rather than `teamID`: the two disagree for 469
    of Lahman's team-seasons, and the game logs speak Retrosheet.

    Season-specific on purpose. A 1950 Dodgers match should read 'Brooklyn
    Dodgers', not 'Los Angeles Dodgers' -- this is a history page, and the
    AFL side lists Fitzroy and Brisbane Bears as their own clubs for the
    same reason.
    """
    names = {}
    for row in _lahman_rows("Teams.csv"):
        code, year, name = (row.get("teamIDretro"), row.get("yearID"),
                            row.get("name"))
        if code and year and name:
            names[(int(year), code)] = name
    return names


def _park_names() -> dict[str, str]:
    """Retrosheet park id ('CHI11') -> ballpark name ('Wrigley Field')."""
    return {row["parkkey"]: row["parkname"] for row in _lahman_rows("Parks.csv")
            if row.get("parkkey") and row.get("parkname")}


def _attendance(raw: str):
    """Attendance as an int, or None where the figure is not known.

    Retrosheet writes both an empty field and a literal 0 for an unrecorded
    crowd -- 16,846 and 14,116 games respectively, which is far too many
    zeroes to be real lockouts. Both become NULL, so the Past Games page
    leaves them blank and excludes them from an average rather than
    dragging it down with fictional zeroes. The cost is that the handful of
    genuinely crowdless games are indistinguishable from unknown ones.
    """
    value = (raw or "").strip()
    if not value or value == "0":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _retro_to_player_id() -> dict[str, str]:
    """Retrosheet ID -> Lahman player_id, from People.csv's retroID column.

    Read directly from the raw CSV rather than mlb.db: the player_id this
    loader needs to match is the one build_mlb_db.py already put in
    mlb.db's players/games tables, and going straight to the source avoids
    a dependency on the build having just run.

    Only used when this module runs standalone. build_mlb_db.py has People
    parsed in memory already -- and may have read it from a ZIP rather than
    this path -- so it passes its own crosswalk to load() instead.
    """
    path = raw_dir("mlb") / "People.csv"
    crosswalk: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            retro_id, player_id = row.get("retroID"), row.get("playerID")
            if retro_id and player_id:
                crosswalk[retro_id] = player_id
    return crosswalk


def _rivalry_index(rivalries: dict) -> dict[str, tuple[str, str, str]]:
    """team code -> (rivalry_key, this side's franchise name, side).

    A code belongs to at most one side of at most one rivalry in practice;
    if a franchise ever appeared on both sides of two different rivalries
    under the same code, the last one declared would win, which is an
    acceptable ambiguity for a hand-curated list this short.
    """
    index: dict[str, tuple[str, str, str]] = {}
    for key, rivalry in rivalries.items():
        for side in ("team_a", "team_b"):
            info = rivalry[side]
            for code in info["codes"]:
                index[code] = (key, info["name"], side)
    return index


def _starters(row: list[str], offset: int, pitcher_id: str):
    """Retrosheet IDs of a team's nine lineup starters plus its starting
    pitcher, deduplicated -- the pitcher is already one of the nine in a
    non-DH game and a tenth name in a DH game."""
    ids = {row[i] for i in range(offset, offset + 27, 3) if row[i]}
    if pitcher_id:
        ids.add(pitcher_id)
    return ids


def _ensure_table(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS mlb_player_rivalry_games (
            player_id TEXT NOT NULL,
            game_date TEXT NOT NULL,
            game_number TEXT NOT NULL,
            season INTEGER NOT NULL,
            rivalry_key TEXT NOT NULL,
            team_id TEXT NOT NULL,
            opponent_id TEXT NOT NULL,
            is_win INTEGER NOT NULL,
            PRIMARY KEY (player_id, game_date, game_number, rivalry_key)
        )
    """)
    con.execute(
        "CREATE INDEX IF NOT EXISTS ix_rivalry_player "
        "ON mlb_player_rivalry_games(rivalry_key, player_id)")


def _ensure_match_table(con: sqlite3.Connection) -> None:
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {MATCH_TABLE} (
            source_game_key TEXT NOT NULL,
            source_club_id  TEXT NOT NULL,
            season          INTEGER NOT NULL,
            round           TEXT,
            is_final        INTEGER NOT NULL DEFAULT 0,
            match_date      TEXT NOT NULL,
            venue_raw       TEXT,
            team_position   TEXT NOT NULL,
            result          TEXT,
            points_for      INTEGER,
            points_against  INTEGER,
            margin          INTEGER,
            attendance      INTEGER,
            match_id        INTEGER,
            match_status    TEXT NOT NULL DEFAULT 'unique',
            PRIMARY KEY (source_game_key, source_club_id)
        )
    """)
    for statement in (
        f"CREATE INDEX IF NOT EXISTS ix_cms_club ON {MATCH_TABLE}(source_club_id)",
        f"CREATE INDEX IF NOT EXISTS ix_cms_season ON {MATCH_TABLE}(season)",
        f"CREATE INDEX IF NOT EXISTS ix_cms_date ON {MATCH_TABLE}(match_date)",
    ):
        con.execute(statement)


def _match_rows(row, round_label, team_names, park_names):
    """One game-log row as its two club-perspective rows.

    club_history's whole model is that a match is two rows sharing a
    source_game_key, which it self-joins to put the opponent beside the
    club. So the visitor and the home side are emitted together here or
    neither is: a single-sided row would join to nothing and vanish.
    """
    date = row[DATE]
    season = int(date[:4])
    iso = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    number = row[GAME_NUMBER] or "0"
    visitor, home = row[VIS_TEAM], row[HOME_TEAM]
    try:
        vis_score, home_score = int(row[VIS_SCORE]), int(row[HOME_SCORE])
    except (ValueError, IndexError):
        return

    # Includes the game number so both halves of a doubleheader survive the
    # primary key instead of the second silently replacing the first.
    key = f"{date}-{number}-{home}-{visitor}"
    venue = park_names.get(row[PARK_ID]) or row[PARK_ID] or None
    crowd = _attendance(row[ATTENDANCE])
    is_final = 1 if round_label else 0

    # Ties are real baseball, not a data error: games called for darkness
    # or weather before extra innings stood as ties for a century.
    if home_score > vis_score:
        home_result, vis_result = "W", "L"
    elif vis_score > home_score:
        home_result, vis_result = "L", "W"
    else:
        home_result = vis_result = "D"

    for code, position, result, scored, conceded in (
        (home, "H", home_result, home_score, vis_score),
        (visitor, "A", vis_result, vis_score, home_score),
    ):
        yield (key, team_names.get((season, code), code), season, round_label,
               is_final, iso, venue, position, result, scored, conceded,
               scored - conceded, crowd, None, "unique")


def load_matches(con: sqlite3.Connection, refresh: bool = False,
                 team_names: dict | None = None) -> int:
    """Fill club_match_sources from every Retrosheet game log.

    This is what the Past Games and Club Explorer pages read. Lahman cannot
    feed them at all -- it has no game-level rows -- so the entire layer
    comes from here.

    Returns the number of matches (not rows; there are two rows each).
    """
    _ensure_match_table(con)
    con.execute(f"DELETE FROM {MATCH_TABLE}")

    if team_names is None:
        team_names = _team_names()
    park_names = _park_names()
    zip_path = _download(refresh=refresh)

    def every_row():
        for row in _game_rows(zip_path):
            yield from _match_rows(row, None, team_names, park_names)
        for row, round_label in _postseason_game_rows(zip_path):
            yield from _match_rows(row, round_label, team_names, park_names)

    con.executemany(
        f"INSERT OR REPLACE INTO {MATCH_TABLE} VALUES "
        f"({','.join('?' * 15)})", every_row())
    con.commit()
    return con.execute(
        f"SELECT COUNT(DISTINCT source_game_key) FROM {MATCH_TABLE}"
    ).fetchone()[0]


def load(con: sqlite3.Connection, refresh: bool = False,
         crosswalk: dict[str, str] | None = None) -> int:
    """Fill mlb_player_rivalry_games. Returns the row count.

    `crosswalk` is retroID -> Lahman player_id; build_mlb_db.py passes the
    one it already parsed, and standalone runs read People.csv themselves.
    """
    _ensure_table(con)
    con.execute("DELETE FROM mlb_player_rivalry_games")

    rivalries = mlb_reference.rivalries()
    code_index = _rivalry_index(rivalries)
    if crosswalk is None:
        crosswalk = _retro_to_player_id()

    zip_path = _download(refresh=refresh)
    for row in _game_rows(zip_path):
        vis_code, home_code = row[VIS_TEAM], row[HOME_TEAM]
        vis_match, home_match = code_index.get(vis_code), code_index.get(home_code)
        # Both sides must resolve to the same rivalry on opposite sides --
        # a Yankees-Orioles game is not a Yankees-Red Sox one just because
        # one team matched.
        if not (vis_match and home_match):
            continue
        v_key, v_name, v_side = vis_match
        h_key, h_name, h_side = home_match
        if v_key != h_key or v_side == h_side:
            continue

        date = row[DATE]
        season = int(date[:4])
        home_won = int(row[HOME_SCORE]) > int(row[VIS_SCORE])

        sides = (
            (VIS_STARTERS, row[VIS_STARTING_PITCHER], v_key,
             v_name, h_name, not home_won),
            (HOME_STARTERS, row[HOME_STARTING_PITCHER], h_key,
             h_name, v_name, home_won),
        )
        for offset, sp, rivalry_key, team_name, opp_name, won in sides:
            for retro_id in _starters(row, offset, sp):
                player_id = crosswalk.get(retro_id)
                if not player_id:
                    continue
                con.execute(
                    "INSERT OR IGNORE INTO mlb_player_rivalry_games "
                    "(player_id, game_date, game_number, season, "
                    " rivalry_key, team_id, opponent_id, is_win) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (player_id, date, row[GAME_NUMBER], season, rivalry_key,
                     team_name, opp_name, int(won)))

    con.commit()
    return con.execute(
        "SELECT COUNT(*) FROM mlb_player_rivalry_games").fetchone()[0]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true",
                         help="re-download the Retrosheet game logs even if cached")
    args = parser.parse_args()

    connection = sqlite3.connect(sport_db("mlb"))
    try:
        total = load(connection, refresh=args.refresh)
        print(f"mlb_player_rivalry_games: {total:,} rows")
        matches = load_matches(connection, refresh=args.refresh)
        print(f"{MATCH_TABLE}: {matches:,} matches")
    finally:
        connection.close()
