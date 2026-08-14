#!/usr/bin/env python3
"""
load_statsapi.py -- Add a current MLB season to the database already built.

    python -m utils.mlb.load_statsapi                       # the season now
    python -m utils.mlb.load_statsapi --season 2026
    python -m utils.mlb.load_statsapi --seasons 2025,2026 --refresh
    python -m utils.mlb.load_statsapi --dry-run

The Lahman CSVs built this database once and are not read again. Lahman
publishes a season only after it ends, so from April to November the
database sits a season behind with no way to close the gap; this closes it
from MLB's own Stats API.

WHAT IT TOUCHES
---------------
`games`     the season's regular-season rows are replaced wholesale, so a
            re-run mid-season corrects yesterday's totals instead of
            doubling them.
`players`   a row is inserted for anyone new, and the career columns are
            recomputed for everyone -- from `games`, not from Lahman.
`club_match_sources`
            the season's completed fixtures, but only for a season no
            fuller source has already covered. See replace_match_sources.

Obscurity is not scored here. utils/shared/recompute_obscurity.py owns
that formula and already runs as the next step of every update job.

See mlb/statsapi_source.py for the grain, the identifier crosswalk, the
licensing note, and why the postseason is left alone.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import names  # noqa: E402
from data_paths import default_db  # noqa: E402
from mlb import statsapi_source  # noqa: E402


def current_season(today: dt.date | None = None) -> int:
    """The season the Stats API has something to say about.

    MLB runs inside one calendar year, so the season is the year -- until
    January and February, when the most recent completed season is still
    last year's.
    """
    today = today or dt.date.today()
    return today.year if today.month >= 3 else today.year - 1


def parse_seasons(text) -> list[int]:
    out: set[int] = set()
    for piece in str(text).replace(" ", "").split(","):
        if not piece:
            continue
        if "-" in piece.lstrip("-"):
            lo, hi = piece.split("-", 1)
            if int(hi) < int(lo):
                raise ValueError(f"season range {piece} runs backwards")
            out.update(range(int(lo), int(hi) + 1))
        else:
            out.add(int(piece))
    return sorted(out)


def _table_exists(con, name) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone())


def _existing_players(con) -> dict[str, str]:
    return {row[0]: row[1] for row in
            con.execute("SELECT player_id, player FROM players")}


#: Columns the Stats API does not serve. WAR is a derived rating that came
#: in with the original build, and the CSVs it came from are not read any
#: more -- so the value already in the database is the only one there will
#: ever be. Replacing a season blindly would blank it for everyone in that
#: season, which is why the old rows are read before they are deleted.
CARRIED_COLUMNS = ("war",)


def _carried(con, season: int) -> dict:
    """(player_id, club) -> the values only the old rows know."""
    selected = ", ".join(CARRIED_COLUMNS)
    return {
        (row[0], row[1]): row[2:]
        for row in con.execute(
            f"SELECT player_id, club_now, {selected} FROM games "
            "WHERE season=? AND is_postseason=0", (season,))
        if any(value is not None for value in row[2:])
    }


def replace_season(con, season: int, rows: list[dict]) -> dict:
    """Swap one season's regular-season rows for a freshly fetched set.

    Replacing rather than appending is what makes a mid-season re-run
    safe: yesterday's totals are superseded, not added to.

    The postseason rows of that season, if the database has any, are left
    untouched -- this loader does not produce them and must not delete
    what an earlier Lahman build did. CARRIED_COLUMNS are moved across for
    the same reason: they cannot be re-fetched, so they must survive.
    """
    carried = _carried(con, season)
    before = con.execute(
        "SELECT COUNT(*) FROM games WHERE season=? AND is_postseason=0",
        (season,)).fetchone()[0]
    con.execute(
        "DELETE FROM games WHERE season=? AND is_postseason=0", (season,))

    kept = 0
    for row in rows:
        old = carried.get((row["player_id"], row["club_now"]))
        if not old:
            continue
        for column, value in zip(CARRIED_COLUMNS, old):
            if row.get(column) is None and value is not None:
                row[column] = value
                kept += 1

    columns = statsapi_source.GAME_COLUMNS
    marks = ", ".join("?" * len(columns))
    con.executemany(
        f"INSERT INTO games ({', '.join(columns)}) VALUES ({marks})",
        [tuple(row.get(column) for column in columns) for row in rows])
    return {"season": season, "removed": before, "inserted": len(rows),
            "carried": kept}


#: Every match row this loader writes is keyed with this prefix, which is
#: what lets a re-run replace its own work and nothing else.
MATCH_KEY_PREFIX = "statsapi-"


def replace_match_sources(con, season: int, matches: list[dict],
                          verbose=True) -> int:
    """Swap this loader's club_match_sources rows for a freshly fetched set.

    Two things are deliberately narrow here.

    The delete is scoped to rows this loader wrote. Deleting the season
    outright would take October with it -- the Retrosheet build's 'WS',
    'LCS', 'DS' and 'WC' rows are what mlb/constraints_mlb.py reads for
    the World Series square, and only regular-season games come back.

    And a season Retrosheet already covers is left alone entirely.
    Retrosheet's rows carry attendance and real game keys; overlaying a
    thinner second copy of the same fixtures would double every game.
    This loader exists to fill the seasons Retrosheet has not reached.
    """
    if not _table_exists(con, "club_match_sources"):
        return 0
    # Coverage is judged on regular-season rows alone -- those are the
    # only ones this loader would duplicate. A season whose October came
    # from Retrosheet but whose summer has not been loaded yet is still
    # ours to fill.
    foreign = con.execute(
        "SELECT COUNT(*) FROM club_match_sources "
        "WHERE season=? AND round IS NULL AND source_game_key NOT LIKE ?",
        (season, f"{MATCH_KEY_PREFIX}%")).fetchone()[0]
    if foreign:
        if verbose:
            print(f"  {season} matches already come from a fuller source "
                  f"({foreign:,} rows) -- left as they are")
        return 0

    # The same empty/severely-partial gate the player rows get: this
    # function deletes its own rows before inserting, so a schedule
    # response that came back empty (or gutted) must leave the stored
    # fixtures standing rather than wipe a season of match history.
    stored = con.execute(
        "SELECT COUNT(*) FROM club_match_sources "
        "WHERE season=? AND source_game_key LIKE ?",
        (season, f"{MATCH_KEY_PREFIX}%")).fetchone()[0]
    if stored and not matches:
        if verbose:
            print(f"  ! the schedule returned no matches for {season} "
                  f"while {stored:,} are stored -- left as they are")
        return 0
    if (stored >= MIN_GUARDED_ROWS
            and len(matches) < stored * MIN_ROW_RATIO):
        if verbose:
            print(f"  ! the schedule returned {len(matches):,} matches for "
                  f"{season} where {stored:,} are stored (below the "
                  f"{MIN_ROW_RATIO:.0%} floor) -- left as they are")
        return 0

    # The delete goes by the key prefix, not by `round`: the prefix is what
    # marks a row as this loader's own, and rows written before the round
    # convention was settled have to be replaceable too. (source_game_key,
    # source_club_id) is the primary key, so a row left behind is not a
    # duplicate but an IntegrityError.
    con.execute(
        "DELETE FROM club_match_sources WHERE season=? AND source_game_key "
        "LIKE ?", (season, f"{MATCH_KEY_PREFIX}%"))
    if not matches:
        return 0
    columns = list(matches[0].keys())
    marks = ", ".join("?" * len(columns))
    con.executemany(
        f"INSERT INTO club_match_sources ({', '.join(columns)}) VALUES ({marks})",
        [tuple(row[col] for col in columns) for row in matches])
    return len(matches)


#: How many people to ask about at once. The `people` endpoint takes a
#: list, which turns one request per newcomer -- hundreds in the first
#: season after a Lahman export -- into a handful.
BIOGRAPHY_BATCH = 50


def biographies(source, mlbam_ids, verbose=True) -> dict:
    """MLBAM id -> (birth_year, birth_country, birth_state).

    Absence is not failure: a player MLB has no biography for still gets
    their games, with NULLs where the facts are unknown.
    """
    found: dict[str, tuple] = {}
    ids = sorted(set(mlbam_ids))
    api = source._api()
    for start in range(0, len(ids), BIOGRAPHY_BATCH):
        batch = ids[start:start + BIOGRAPHY_BATCH]
        try:
            payload = api.get("people", {"personIds": ",".join(batch)})
        except Exception as exc:                      # noqa: BLE001
            if verbose:
                print(f"  no biography for {len(batch)} player(s): {exc}")
            continue
        for person in payload.get("people", []):
            born = str(person.get("birthDate") or "")[:4]
            found[str(person.get("id"))] = (
                int(born) if born.isdigit() else None,
                person.get("birthCountry"),
                person.get("birthStateProvince"),
            )
    return found


def add_missing_players(con, rows: list[dict], source, verbose=True) -> int:
    """Give anyone new a `players` row, with biography where MLB has it.

    Their game rows have to attach to a player that exists, so this runs
    before the rows are inserted. Only newcomers are asked about --
    everyone else already has their biography from the original build.
    """
    known = set(_existing_players(con))
    fresh = {}
    for row in rows:
        if row["player_id"] not in known and row["player_id"] not in fresh:
            fresh[row["player_id"]] = row
    if not fresh:
        return 0

    if verbose:
        print(f"  fetching biography for {len(fresh)} new player(s)")
    facts = biographies(
        source, [row["mlbam_id"] for row in fresh.values()], verbose=verbose)

    con.executemany(
        "INSERT INTO players (player_id, player, name_key, birth_year, "
        "birth_country, birth_state) VALUES (?, ?, ?, ?, ?, ?)",
        [(player_id, row["player"], names.normalise_name(row["player"]),
          *facts.get(row["mlbam_id"], (None, None, None)))
         for player_id, row in fresh.items()])
    return len(fresh)


def recompute_careers(con) -> int:
    """Rebuild every career column in `players` from `games`.

    The same derivation mlb/build_mlb_db.py performs, expressed against
    the table rather than the CSVs, because the CSVs are not read any
    more. Recomputed for everybody rather than for the season's players:
    `final_season`, `clubs_hist` and `n_clubs` are whole-career values,
    and a partial pass leaves them disagreeing with the rows beneath them.
    """
    # Set-based throughout. Row-at-a-time was unusable: numbering
    # career_game_no with one UPDATE ... WHERE rowid=? per row meant
    # 130,000 statements and the recompute never finished.
    con.executescript("""
        UPDATE players SET
            debut_season = career.debut,
            final_season = career.final,
            career_games = career.played,
            career_hits = career.hits,
            career_home_runs = career.home_runs
        FROM (
            SELECT player_id,
                   MIN(season) AS debut, MAX(season) AS final,
                   COALESCE(SUM(games), 0) AS played,
                   -- Bare SUM, never COALESCE(..., 0): SQLite's SUM is
                   -- NULL when every input is NULL, which is exactly what
                   -- an unrecorded 19th-century statistic must stay. A
                   -- zero here would claim the player was measured at 0.
                   SUM(hits) AS hits,
                   SUM(home_runs) AS home_runs
            FROM games WHERE is_postseason = 0 GROUP BY player_id
        ) AS career
        WHERE players.player_id = career.player_id;

        UPDATE players SET postseason_played = post.played
        FROM (
            SELECT player_id, SUM(games) AS played
            FROM games WHERE is_postseason = 1 GROUP BY player_id
        ) AS post
        WHERE players.player_id = post.player_id;

        -- clubs_hist is in first-appearance order, so the clubs are
        -- reduced to one row each carrying the season they debuted for
        -- it before being folded.
        UPDATE players SET clubs_hist = folded.clubs, n_clubs = folded.n
        FROM (
            SELECT player_id,
                   group_concat(club_now, '|' ORDER BY first_season,
                                club_now) AS clubs,
                   COUNT(*) AS n
            FROM (
                SELECT player_id, club_now, MIN(season) AS first_season
                FROM games WHERE club_now IS NOT NULL
                GROUP BY player_id, club_now
            ) GROUP BY player_id
        ) AS folded
        WHERE players.player_id = folded.player_id;

        -- career_game_no numbers a player's seasons, so it is knowable
        -- only once every season of theirs is in place.
        UPDATE games SET career_game_no = numbered.n
        FROM (
            SELECT rowid AS rid,
                   ROW_NUMBER() OVER (PARTITION BY player_id
                                      ORDER BY season) AS n
            FROM games WHERE is_postseason = 0
        ) AS numbered
        WHERE games.rowid = numbered.rid;
    """)
    return con.execute(
        "SELECT COUNT(DISTINCT player_id) FROM games").fetchone()[0]


#: A fetched season may not shrink the stored one below this fraction of
#: its rows, once the stored season is big enough for a ratio to mean
#: anything. A full MLB season is ~1,500 player-stints; 100 stored rows is
#: past any synthetic fixture while still early-season for the real thing.
MIN_ROW_RATIO = 0.5
MIN_GUARDED_ROWS = 100


def validate_snapshot(con, season: int, rows: list) -> tuple[str, str]:
    """Decide whether a fetched season may replace the stored one.

    Returns (verdict, reason): "ok" to proceed, "skip" for the harmless
    nothing-fetched-nothing-stored case, "reject" when stored rows had to
    be protected from an empty or severely partial Stats API response (an
    outage, an upstream schema change, a cache gone wrong). replace_season
    deletes before it inserts, so this gate runs first.
    """
    stored = con.execute(
        "SELECT COUNT(*) FROM games WHERE season=? AND is_postseason=0",
        (season,)).fetchone()[0]
    if not rows:
        if stored:
            return "reject", (
                f"the Stats API returned no rows for {season} while "
                f"{stored:,} regular-season rows are stored -- season left "
                f"untouched")
        return "skip", (f"nothing fetched for {season} and nothing stored "
                        f"-- skipped")
    if stored >= MIN_GUARDED_ROWS and len(rows) < stored * MIN_ROW_RATIO:
        return "reject", (
            f"the Stats API returned {len(rows):,} rows for {season} where "
            f"{stored:,} are stored (below the {MIN_ROW_RATIO:.0%} floor) "
            f"-- season left untouched")
    return "ok", ""


def live_seasons(seasons, today=None) -> set:
    """Of the seasons asked for, the ones that can still change.

    Stats API responses are cached to disk, which is right for a season
    that has ended and wrong for the one being played: a nightly job
    would read yesterday's file, insert yesterday's numbers and report
    success, leaving the database quietly frozen on the day it first ran.
    """
    now = current_season(today)
    return {int(season) for season in seasons if int(season) >= now}


def load(db_path, seasons=None, refresh=False, verbose=True,
         dry_run=False) -> dict:
    seasons = seasons or [current_season()]
    source = statsapi_source.StatsApiSource(
        refresh=True if refresh else live_seasons(seasons), verbose=verbose)

    result = {"database": str(db_path), "seasons": [], "new_players": 0,
              "dry_run": dry_run}
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("PRAGMA foreign_keys=ON")
        replaced_any = False
        rejected = 0
        for season, rows in ((season, source.season_rows(season))
                             for season in seasons):
            if verbose:
                print(f"season {season}...")
            if dry_run:
                result["seasons"].append(
                    {"season": season, "would_insert": len(rows)})
                continue
            # The gate between fetching and deleting: a season the Stats
            # API answered badly is reported and rejected, never wiped.
            verdict, reason = validate_snapshot(con, season, rows)
            if verdict != "ok":
                if verbose:
                    print(f"  ! {reason}")
                if verdict == "reject":
                    rejected += 1
                    result["seasons"].append({"season": season,
                                              "rejected": reason})
                else:
                    result["seasons"].append({"season": season,
                                              "skipped": reason})
                continue
            result["new_players"] += add_missing_players(
                con, rows, source, verbose=verbose)
            outcome = replace_season(con, season, rows)
            if verbose and outcome["removed"] > outcome["inserted"]:
                # Only reachable when a season that already has a full
                # build is reloaded. Worth saying out loud: the Stats API
                # does not list quite everyone the original build did.
                print(f"  warning: {season} went from "
                      f"{outcome['removed']:,} rows to "
                      f"{outcome['inserted']:,}")
            outcome["matches"] = replace_match_sources(
                con, season, source.season_schedule(season), verbose=verbose)
            result["seasons"].append(outcome)
            replaced_any = True
        if dry_run:
            return result
        result["rejected_seasons"] = rejected
        if replaced_any:
            if verbose:
                print("recomputing career totals from games...")
            result["players_touched"] = recompute_careers(con)
        con.commit()
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--db", default=None)
    parser.add_argument("--season", type=int, default=None)
    parser.add_argument("--seasons", default=None,
                        help="e.g. 2025,2026 or 2024-2026")
    parser.add_argument("--refresh", action="store_true",
                        help="re-request cached Stats API responses")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", dest="verbose", action="store_false")
    args = parser.parse_args(argv)

    db_path = args.db or default_db("mlb")
    if not Path(db_path).exists():
        print(f"No MLB database at {db_path}. Build it first.", file=sys.stderr)
        return 1
    if args.seasons:
        seasons = parse_seasons(args.seasons)
    elif args.season:
        seasons = [args.season]
    else:
        seasons = [current_season()]

    try:
        result = load(db_path, seasons, refresh=args.refresh,
                      verbose=args.verbose, dry_run=args.dry_run)
    except (statsapi_source.SourceError, sqlite3.Error) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    for season in result["seasons"]:
        print(f"  {season}")
    if not args.dry_run:
        print(f"new players: {result['new_players']}")
    if result.get("rejected_seasons"):
        # A season with stored rows was refused an empty/partial snapshot.
        # The store is safe, but the update did not happen -- exiting 0
        # would let an orchestrator record the step as a success.
        print(f"{result['rejected_seasons']} season(s) rejected a bad "
              f"Stats API snapshot; stored rows retained", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
