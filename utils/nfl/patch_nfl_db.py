#!/usr/bin/env python3
"""
utils/nfl/patch_nfl_db.py -- Adapt the nflverse build to the app's schema.

    python -m utils.nfl.patch_nfl_db
    python -m utils.nfl.patch_nfl_db --dry-run

build_nfl_db.py imports nflreadpy faithfully: `games` is one weekly
player-statistics row, keyed to a game in `matches`, and it carries no
notion of a club name, a venue, a result or a career game number. core.py
asks every sport's `games` table for exactly those, so this script derives
them in place rather than teaching core.py about nflverse.

Everything it writes is derived from `games`, so rerunning it after a
rebuild is the intended workflow. It is separate from the builder because
the builder is standalone and is regenerated wholesale -- a patch that
lives here survives that.

What it adds to `games`:

    player           display name, from player_display_name / player_name
    club_hist        the team name the row was played under
    club_now         the franchise that team counts as today
    date, venue      from the row's game in `matches`
    round            the game type: REG, WC, DIV, CON, SB
    is_playoff       season_type is POST
    result           W / L / T, from the game's scores
    career_game_no   1 for a player's first game in the data
    touchdowns       passing + rushing + receiving + returns, where present

and to `players`: `birth_year`, an empty `obscurity` column for
recompute_obscurity.py to fill, and `teams_hist` / `n_teams` restated in
franchise terms -- the builder writes them as team codes ("KC|OAK"), and
this leaves them as the names every other club column now uses, with the
Raiders counted once rather than as Oakland plus Las Vegas.

It then writes data/nfl/reference/nfl_reference.json, which sports.py reads
at import: the statistic list actually present and, from the builder's own
`stat_coverage` table, the first season each one carries a real value.

CAVEAT, INHERITED FROM THE BUILD
--------------------------------
Weekly player statistics begin in 1999. `career_game_no`, `career_games`
and every career total are therefore statistics-era values, not whole NFL
careers: a player who debuted in 1994 has his 1999 game numbered 1. The
rosters table reaches back to 1920 and is not used for this, because a
roster line is not evidence of an appearance.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import data_paths
from nfl import nfl_reference

#: Every candidate touchdown column. Whichever exist are added together,
#: which makes `touchdowns` total touchdowns *responsible for*, passing
#: included -- the same definition the builder gives career_touchdowns.
#:
#: `pt_return_tds` is deliberately absent: punt-return scores are already
#: inside special_teams_tds, and adding both would count them twice.
TOUCHDOWN_COLUMNS = ("passing_tds", "rushing_tds", "receiving_tds",
                     "special_teams_tds", "def_tds", "defensive_tds",
                     "fumble_recovery_tds")

#: Columns this script derives on `games`, with their types.
GAMES_COLUMNS = {
    "player": "TEXT",
    "club_hist": "TEXT",
    "club_now": "TEXT",
    "date": "TEXT",
    "venue": "TEXT",
    "round": "TEXT",
    "is_playoff": "INTEGER",
    "result": "TEXT",
    "career_game_no": "INTEGER",
    "touchdowns": "INTEGER",
}


def columns(con, table):
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def tables(con):
    return {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}


def first_present(present, *candidates):
    for name in candidates:
        if name in present:
            return name
    return None


def add_columns(con, table, wanted, say):
    present = columns(con, table)
    for name, kind in wanted.items():
        if name in present:
            continue
        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        say(f"  + {table}.{name}")


# ------------------------------------------------------------------ games

def patch_games(con, say):
    present = columns(con, "games")
    add_columns(con, "games", GAMES_COLUMNS, say)

    name_column = first_present(present, "player_display_name", "player_name",
                                "player")
    if name_column and name_column != "player":
        con.execute(f"UPDATE games SET player = {name_column}")
        say(f"  games.player <- {name_column}")

    _patch_teams(con, say)
    _patch_from_matches(con, say)

    con.execute("""UPDATE games SET is_playoff =
                   CASE WHEN UPPER(TRIM(COALESCE(season_type, ''))) IN
                        ('POST', 'POSTSEASON') THEN 1 ELSE 0 END""")

    scoring = [c for c in TOUCHDOWN_COLUMNS if c in present]
    if scoring:
        total = " + ".join(f"COALESCE({c}, 0)" for c in scoring)
        con.execute(f"UPDATE games SET touchdowns = {total}")
        say(f"  games.touchdowns <- {' + '.join(scoring)}")
    else:
        say("  ! no touchdown columns found; games.touchdowns left NULL")

    _patch_career_game_no(con, say)


def _patch_teams(con, say):
    """club_hist from the team catalogue, club_now from the code map."""
    catalogue = {}
    if "teams" in tables(con):
        team_cols = columns(con, "teams")
        code = first_present(team_cols, "team_abbr", "team", "abbreviation")
        label = first_present(team_cols, "team_name", "full_name", "name")
        if code and label:
            catalogue = {str(a).strip().upper(): str(b).strip()
                         for a, b in con.execute(
                             f"SELECT {code}, {label} FROM teams")
                         if a and b}

    codes = [row[0] for row in con.execute(
        "SELECT DISTINCT team FROM games WHERE team IS NOT NULL")]
    unknown = []
    for raw in codes:
        code = str(raw).strip().upper()
        now = nfl_reference.ABBREVIATION_NOW.get(code)
        historical = catalogue.get(code, now or code)
        if now is None:
            now = historical
            unknown.append(code)
        con.execute("UPDATE games SET club_hist = ?, club_now = ? "
                    "WHERE team = ?", (historical, now, raw))
    say(f"  games.club_hist / club_now <- {len(codes)} team codes")
    if unknown:
        # Not fatal: the code still resolves to itself, so no row is lost.
        # It does mean a club square for that franchise will miss those
        # rows, which is worth saying out loud.
        say(f"  ! no franchise mapping for {', '.join(sorted(unknown))} "
            f"-- add them to nfl_reference.ABBREVIATION_NOW")


def _patch_from_matches(con, say):
    """date, venue, round and result, all properties of the game."""
    if "matches" not in tables(con):
        say("  ! no matches table; date, venue, round and result left NULL")
        return
    match_cols = columns(con, "matches")
    day = first_present(match_cols, "gameday", "game_date", "date")
    stadium = first_present(match_cols, "stadium", "venue")
    kind = first_present(match_cols, "game_type")

    if day:
        con.execute(f"""UPDATE games SET date = (SELECT m.{day} FROM matches m
                        WHERE m.game_id = games.game_id)""")
    if stadium:
        con.execute(f"""UPDATE games SET venue =
                        (SELECT m.{stadium} FROM matches m
                         WHERE m.game_id = games.game_id)""")
    if kind:
        con.execute(f"""UPDATE games SET round =
                        COALESCE((SELECT m.{kind} FROM matches m
                                  WHERE m.game_id = games.game_id), 'REG')""")
    else:
        con.execute("UPDATE games SET round = 'REG'")

    if {"home_team", "away_team", "home_score", "away_score"} <= match_cols:
        con.execute("""UPDATE games SET result = (
            SELECT CASE
                WHEN m.home_score IS NULL OR m.away_score IS NULL THEN NULL
                WHEN m.home_score = m.away_score THEN 'T'
                WHEN (games.team = m.home_team
                      AND m.home_score > m.away_score)
                  OR (games.team = m.away_team
                      AND m.away_score > m.home_score) THEN 'W'
                ELSE 'L' END
            FROM matches m WHERE m.game_id = games.game_id)""")
        say("  games.date / venue / round / result <- matches")
    else:
        say("  ! matches has no scores; games.result left NULL")


def _patch_career_game_no(con, say):
    """1 for a player's first game *in the weekly data* -- see the caveat."""
    order = "season, " + ("week, " if "week" in columns(con, "games") else "")
    con.execute("DROP TABLE IF EXISTS temp._career_game_no")
    con.execute(f"""CREATE TEMP TABLE _career_game_no AS
                    SELECT rowid AS rid,
                           ROW_NUMBER() OVER (PARTITION BY player_id
                                              ORDER BY {order} game_id) AS n
                    FROM games""")
    con.execute("CREATE INDEX temp.ix_cgn ON _career_game_no(rid)")
    con.execute("""UPDATE games SET career_game_no =
                   (SELECT n FROM _career_game_no WHERE rid = games.rowid)""")
    con.execute("DROP TABLE temp._career_game_no")
    say("  games.career_game_no <- season, week, game_id")


# ---------------------------------------------------------------- players

def patch_players(con, say):
    present = columns(con, "players")
    add_columns(con, "players", {"birth_year": "INTEGER",
                                 "obscurity": "REAL"}, say)

    born = first_present(present, "birth_date", "birthdate", "birth_day")
    if born:
        con.execute(f"""UPDATE players SET birth_year =
                        CAST(SUBSTR({born}, 1, 4) AS INTEGER)
                        WHERE {born} IS NOT NULL AND LENGTH({born}) >= 4""")
        say(f"  players.birth_year <- {born}")

    _patch_team_history(con, say)

    expected = ("player_id", "player", "name_key", "debut_season",
                "final_season", "career_games", "career_touchdowns",
                "career_postseason_games", "teams_hist", "n_teams")
    missing = [c for c in expected if c not in columns(con, "players")]
    if missing:
        say(f"  ! players is missing {', '.join(missing)} -- the app will "
            f"decline to load until the build supplies them")


def _patch_team_history(con, say):
    """teams_hist and n_teams in franchise names, matching `games`.

    Both are restated rather than added: the builder derives them from team
    codes, so a player who moved with the Raiders reads as two teams and
    fails "One-club player" for a move he never made.
    """
    if not {"teams_hist", "n_teams"} <= columns(con, "players"):
        return
    con.execute("""UPDATE players SET teams_hist = (
        SELECT GROUP_CONCAT(name, '|') FROM (
            SELECT club_hist AS name, MIN(season) AS first_season
            FROM games g
            WHERE g.player_id = players.player_id AND g.club_hist IS NOT NULL
            GROUP BY club_hist ORDER BY first_season))
        WHERE EXISTS (SELECT 1 FROM games g
                      WHERE g.player_id = players.player_id)""")
    con.execute("""UPDATE players SET n_teams = (
        SELECT COUNT(DISTINCT club_now) FROM games g
        WHERE g.player_id = players.player_id AND g.club_now IS NOT NULL)
        WHERE EXISTS (SELECT 1 FROM games g
                      WHERE g.player_id = players.player_id)""")
    say("  players.teams_hist / n_teams <- games franchise names")


# -------------------------------------------------------------- reference

def _reference_path(db) -> Path:
    """Beside its own database, not at nfl_reference.PATH directly.

    database_updates.py patches a staging file that sits next to the live
    database (same directory, ``nfl.db.update-building`` beside
    ``nfl.db``) and is not promoted until later checks pass. A fixed write
    target could not tell a staging patch from the real one and would
    leave the live nfl_reference.json reflecting a build the outer job may
    yet reject -- nba/build_nba_db.py's load_reference has the same rule
    for the same reason. For the live database itself this resolves to the
    exact same file as nfl_reference.PATH.
    """
    return Path(db).resolve().parent / "reference" / "nfl_reference.json"


def write_reference(con, say, path):
    """Measured statistics and their eras, for sports.NFL_SCHEMA."""
    present = columns(con, "games")
    stats = [s for s in nfl_reference.FALLBACK_STATS if s in present]
    absent = [s for s in nfl_reference.FALLBACK_STATS if s not in present]
    if absent:
        # nflreadpy renames columns between versions; a declared stat that
        # is not there would raise on every square that names it.
        say(f"  ! declared statistics not in games: {', '.join(absent)}")

    eras = {}
    if "stat_coverage" in tables(con):
        for stat, first in con.execute(
                "SELECT stat_name, available_from FROM stat_coverage "
                "WHERE table_name = 'games' AND available_from IS NOT NULL"):
            if stat in stats:
                eras[stat] = int(first)
    for stat in stats:
        eras.setdefault(stat, nfl_reference.FIRST_STAT_SEASON)

    teams = [row[0] for row in con.execute(
        "SELECT DISTINCT club_now FROM games "
        "WHERE club_now IS NOT NULL ORDER BY club_now")]
    lo, hi = con.execute("SELECT MIN(season), MAX(season) FROM games").fetchone()

    payload = {
        "stats": stats,
        "stat_eras": eras,
        "teams": teams or list(nfl_reference.FALLBACK_TEAMS),
        "club_lineage": nfl_reference.FALLBACK_LINEAGE,
        "seasons": [lo, hi],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    say(f"  wrote {path} ({len(stats)} statistics, {len(teams)} teams, "
        f"{lo}-{hi})")


# --------------------------------------------------------------- indexes

def add_indexes(con, say):
    for name, sql in (
        ("ix_games_club_now", "games(club_now)"),
        ("ix_games_club_hist", "games(club_hist)"),
        ("ix_games_playoff", "games(is_playoff)"),
        ("ix_games_player_game_no", "games(player_id, career_game_no)"),
        ("ix_games_venue", "games(venue)"),
    ):
        con.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {sql}")
    say("  indexes created")


# ------------------------------------------------------------------- main

def patch(db, dry_run=False, verbose=True, write_reference_file=True):
    say = (lambda text: print(text)) if verbose else (lambda text: None)
    con = sqlite3.connect(db)
    try:
        have = tables(con)
        for required in ("games", "players"):
            if required not in have:
                raise SystemExit(
                    f"{db} has no {required} table -- build it first with "
                    f"`python .\\build_nfl_db.py --all-history`.")

        say(f"Patching {db}")
        patch_games(con, say)
        patch_players(con, say)
        add_indexes(con, say)

        if dry_run:
            con.rollback()
            say("\n--dry-run: nothing written, reference file not rewritten.")
            return
        if write_reference_file:
            write_reference(con, say, _reference_path(db))
        con.commit()
    finally:
        con.close()

    say("\nDone. Next:\n"
        "  python -m utils.shared.recompute_obscurity --sport nfl\n"
        "then restart Streamlit -- the schema is read at import.")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=data_paths.default_db("nfl"))
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    ap.add_argument("--reference-only", action="store_true",
                    help="only rewrite the reference file, from an already-"
                         "patched database")
    ap.add_argument("--no-reference", action="store_true",
                    help="patch the database but do not write the reference "
                         "file; for a staging file a caller will promote and "
                         "refresh the reference itself with --reference-only")
    ap.add_argument("--quiet", dest="verbose", action="store_false")
    args = ap.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        building = db.parent / f"{db.name}.building"
        if building.exists():
            raise SystemExit(f"{building} is still being built -- wait for "
                             f"it to be renamed to {db.name}.")
        raise SystemExit(f"No database at {db}.")

    if args.reference_only:
        say = (lambda text: print(text)) if args.verbose else (lambda t: None)
        con = sqlite3.connect(str(db))
        try:
            write_reference(con, say, _reference_path(db))
            con.commit()
        finally:
            con.close()
        return 0

    patch(str(db), dry_run=args.dry_run, verbose=args.verbose,
         write_reference_file=not args.no_reference)
    return 0


if __name__ == "__main__":
    sys.exit(main())
