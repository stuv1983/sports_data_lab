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


def _existing_players(con) -> dict[str, str]:
    return {row[0]: row[1] for row in
            con.execute("SELECT player_id, player FROM players")}


def replace_season(con, season: int, rows: list[dict]) -> dict:
    """Swap one season's regular-season rows for a freshly fetched set.

    Replacing rather than appending is what makes a mid-season re-run
    safe: yesterday's totals are superseded, not added to.

    The postseason rows of that season, if the database has any, are left
    untouched -- this loader does not produce them and must not delete
    what an earlier Lahman build did.
    """
    before = con.execute(
        "SELECT COUNT(*) FROM games WHERE season=? AND is_postseason=0",
        (season,)).fetchone()[0]
    con.execute(
        "DELETE FROM games WHERE season=? AND is_postseason=0", (season,))

    columns = statsapi_source.GAME_COLUMNS
    marks = ", ".join("?" * len(columns))
    con.executemany(
        f"INSERT INTO games ({', '.join(columns)}) VALUES ({marks})",
        [tuple(row.get(column) for column in columns) for row in rows])
    return {"season": season, "removed": before, "inserted": len(rows)}


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
                   COALESCE(SUM(hits), 0) AS hits,
                   COALESCE(SUM(home_runs), 0) AS home_runs
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


def load(db_path, seasons=None, refresh=False, verbose=True,
         dry_run=False) -> dict:
    seasons = seasons or [current_season()]
    source = statsapi_source.StatsApiSource(refresh=refresh, verbose=verbose)

    result = {"database": str(db_path), "seasons": [], "new_players": 0,
              "dry_run": dry_run}
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("PRAGMA foreign_keys=ON")
        for season in seasons:
            if verbose:
                print(f"season {season}...")
            rows = source.season_rows(season)
            if dry_run:
                result["seasons"].append(
                    {"season": season, "would_insert": len(rows)})
                continue
            result["new_players"] += add_missing_players(
                con, rows, source, verbose=verbose)
            result["seasons"].append(replace_season(con, season, rows))
        if dry_run:
            return result
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
