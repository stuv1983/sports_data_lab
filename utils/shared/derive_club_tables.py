#!/usr/bin/env python3
"""Build the Club Explorer tables from a sport's own database.

    python -m utils.shared.derive_club_tables --sport nba
    python -m utils.shared.derive_club_tables --sport all --report

The AFL's club layer is scraped: utils/afl/fetch_club_sources.py pulls
AFLTables and Wikipedia, and utils/afl/load_club_sources.py parses them. No
equivalent scrape exists for the other sports, but nothing in the player
tabs actually needs one -- an all-time club register, per-club career
totals and club record leaderboards are all questions the match database
already answers, and deriving them keeps those numbers agreeing with every
other page by construction.

So this writes the same six tables from `games` and the sport's measured
club list. Two are placeholders: club_wikipedia_fields is empty because
nothing here fetches Wikipedia, and club_source_snapshots records the
derivation itself rather than a downloaded page.

The AFL is deliberately excluded. Its tables are the scraped ones, they
carry cap numbers, jumper numbers and Brownlow votes that no derivation
can produce, and overwriting them with a weaker version would be a
regression.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import sports

#: Scraped, not derived. See the module docstring.
EXCLUDED = {"afl"}

#: How many rows a record leaderboard keeps per (club, scope, stat).
TOP_N = 25

#: Rates, not counts. Summing an earned run average across seasons is a
#: meaningless number, and a career one needs innings pitched to weight it.
#: Left out of the totals rather than shown wrong; a per-season record
#: leaderboard for them is still fine, since there the row is the season.
RATE_STATS = {"era"}

SCHEMA = """
DROP TABLE IF EXISTS clubs;
CREATE TABLE clubs (
    club_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    abbreviation TEXT NOT NULL,
    db_club_now TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

DROP TABLE IF EXISTS club_wikipedia_fields;
CREATE TABLE club_wikipedia_fields (
    club_id TEXT NOT NULL,
    field_key TEXT NOT NULL,
    field_label TEXT NOT NULL,
    field_value TEXT,
    field_html TEXT,
    revision_id INTEGER,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (club_id, field_key)
);

DROP TABLE IF EXISTS club_source_snapshots;
CREATE TABLE club_source_snapshots (
    club_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_path TEXT NOT NULL,
    fetched_at TEXT,
    sha256 TEXT NOT NULL,
    revision_id INTEGER,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (club_id, source_type)
);

DROP TABLE IF EXISTS club_player_register;
CREATE TABLE club_player_register (
    row_id INTEGER PRIMARY KEY,
    club_id TEXT NOT NULL,
    cap_number INTEGER,
    jumper_number TEXT,
    player_name_source TEXT NOT NULL,
    player_name TEXT NOT NULL,
    player_url TEXT,
    player_id INTEGER,
    match_status TEXT NOT NULL,
    candidate_count INTEGER NOT NULL,
    dob TEXT,
    height_cm INTEGER,
    weight_kg INTEGER,
    games INTEGER,
    wins INTEGER,
    draws INTEGER,
    losses INTEGER,
    goals INTEGER,
    seasons_text TEXT,
    debut_age_text TEXT,
    last_age_text TEXT,
    raw_row_json TEXT NOT NULL,
    imported_at TEXT NOT NULL
);

DROP TABLE IF EXISTS club_player_records;
CREATE TABLE club_player_records (
    record_id INTEGER PRIMARY KEY,
    club_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    stat TEXT NOT NULL,
    source_heading TEXT NOT NULL,
    source_rank INTEGER NOT NULL,
    player_name_source TEXT NOT NULL,
    player_name TEXT NOT NULL,
    player_url TEXT,
    player_id INTEGER,
    match_status TEXT NOT NULL,
    candidate_count INTEGER NOT NULL,
    source_team TEXT,
    value REAL,
    games INTEGER,
    average REAL,
    season INTEGER,
    round TEXT,
    opponent TEXT,
    match_date TEXT,
    match_description TEXT,
    raw_row_json TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_club_totals_club ON club_player_totals(club_id)",
    "CREATE INDEX IF NOT EXISTS ix_club_register_club ON club_player_register(club_id)",
    "CREATE INDEX IF NOT EXISTS ix_club_records_club ON club_player_records(club_id, scope, stat)",
)


class DeriveError(RuntimeError):
    pass


def slug(name: str) -> str:
    """'Los Angeles Lakers' -> 'los_angeles_lakers'."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return cleaned or "club"


def abbreviate(name: str) -> str:
    """Initials, for a club list that has no abbreviation of its own."""
    words = [w for w in re.split(r"\s+", name) if w]
    if len(words) == 1:
        return words[0][:3].upper()
    return "".join(w[0] for w in words[:4]).upper()


def _abbreviations(con, clubs) -> dict:
    """teams.abbreviation where the build wrote one, else initials."""
    found = {}
    try:
        for name, abbrev in con.execute(
                "SELECT name, abbreviation FROM teams WHERE is_current = 1"):
            if name and abbrev:
                found[name] = abbrev.strip()
    except sqlite3.OperationalError:
        pass
    return {c: found.get(c) or abbreviate(c) for c in clubs}


def _stat_columns(con, games_table, stats):
    """The sport's stats that are really columns on `games`."""
    present = {row[1] for row in con.execute(f"PRAGMA table_info({games_table})")}
    return [s for s in stats if s in present]


def _games_played(con, games_table):
    """How to count games played from the rows.

    An MLB row is a player's season with one team and carries its own
    `games`; counting rows there would have given Babe Ruth 22 games for
    the Yankees instead of 22 seasons' worth.
    """
    present = {row[1] for row in con.execute(f"PRAGMA table_info({games_table})")}
    return "SUM(g.games)" if "games" in present else "COUNT(*)"


def derive(sport, verbose=True):
    """Write the six tables into `sport`'s database."""
    key = sport.key
    if key in EXCLUDED:
        raise DeriveError(f"{key}: club tables are scraped, not derived")

    db = sport.db
    if not Path(db).exists():
        raise DeriveError(f"{key}: no database at {db}")

    schema = sport.schema
    games = schema.games
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con = sqlite3.connect(db)
    try:
        stats = _stat_columns(con, games, schema.stats)
        if not stats:
            raise DeriveError(f"{key}: no stat columns on {games}")

        con.executescript(SCHEMA)
        # Built here rather than in SCHEMA because the stat columns are the
        # sport's own -- points and rebounds for the NBA, home runs and
        # RBIs for baseball.
        columns = ", ".join(f"{s} REAL" for s in stats
                            if s not in RATE_STATS)
        con.executescript(
            "DROP TABLE IF EXISTS club_player_totals;\n"
            "CREATE TABLE club_player_totals ("
            "  row_id INTEGER PRIMARY KEY,"
            "  club_id TEXT NOT NULL,"
            "  player_name_source TEXT NOT NULL,"
            "  player_name TEXT NOT NULL,"
            "  player_url TEXT,"
            "  player_id INTEGER,"
            "  match_status TEXT NOT NULL,"
            "  candidate_count INTEGER NOT NULL,"
            "  games INTEGER,"
            f" {columns},"
            "  raw_row_json TEXT NOT NULL,"
            "  imported_at TEXT NOT NULL);")

        clubs = list(schema.clubs)
        abbrevs = _abbreviations(con, clubs)
        con.executemany(
            "INSERT INTO clubs (club_id, name, abbreviation, db_club_now, "
            "active, updated_at) VALUES (?,?,?,?,1,?)",
            [(slug(c), c, abbrevs[c], c, now) for c in clubs])
        con.executemany(
            "INSERT INTO club_source_snapshots (club_id, source_type, "
            "source_url, source_path, fetched_at, sha256, imported_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [(slug(c), "derived", f"sqlite://{db}", str(db), now,
              "derived-from-match-database", now) for c in clubs])

        counts = {"clubs": len(clubs)}
        counts["club_player_totals"] = _totals(con, schema, stats, now)
        counts["club_player_register"] = _register(con, schema, now)
        counts["club_player_records"] = _records(con, sport, stats, now)

        for statement in INDEXES:
            con.execute(statement)
        con.commit()
    finally:
        con.close()

    if verbose:
        print(f"  {key}: " + ", ".join(
            f"{name.replace('club_player_', '')} {n:,}"
            for name, n in counts.items()))
    return counts


def _named(schema, alias="g"):
    """Rows that name a player.

    The NFL build carries 591 rows under player_id '0' with no name --
    team-level placeholders, not people. A register entry for them would be
    a blank row in every club's all-time list.
    """
    return (f"{alias}.{schema.player} IS NOT NULL "
            f"AND TRIM({alias}.{schema.player}) <> ''")


def _club_expression(schema):
    """Which club a row belongs to.

    club_now, so a Seattle SuperSonics game counts towards Oklahoma City --
    the same one-directional lineage every club square uses, and the reason
    the register is an all-time list of the current franchise.
    """
    return schema.club_now


def _totals(con, schema, stats, now):
    """Per-club career totals, one row per (club, player)."""
    club = _club_expression(schema)
    countable = [s for s in stats if s not in RATE_STATS]
    sums = ", ".join(f"SUM(g.{s})" for s in countable)
    columns = ", ".join(countable)
    con.execute(f"""
        INSERT INTO club_player_totals (
            club_id, player_name_source, player_name, player_id,
            match_status, candidate_count, games, {columns},
            raw_row_json, imported_at)
        SELECT c.club_id,
               g.{schema.player}, g.{schema.player}, g.{schema.player_id},
               'derived', 1, {_games_played(con, schema.games)},
               {sums}, '{{}}', :now
        FROM {schema.games} g
        JOIN clubs c ON c.name = g.{club}
        WHERE {_named(schema)}
        GROUP BY g.{schema.player_id}, c.club_id
    """, {"now": now})
    return con.execute("SELECT COUNT(*) FROM club_player_totals").fetchone()[0]


def _register(con, schema, now):
    """The all-time club list: every player, with their record for the club."""
    club = _club_expression(schema)
    con.execute(f"""
        INSERT INTO club_player_register (
            club_id, player_name_source, player_name, player_id,
            match_status, candidate_count, games, wins, draws, losses,
            seasons_text, raw_row_json, imported_at)
        SELECT c.club_id,
               g.{schema.player}, g.{schema.player}, g.{schema.player_id},
               'derived', 1, {_games_played(con, schema.games)},
               SUM(CASE WHEN g.{schema.result} = 'W' THEN 1 ELSE 0 END),
               SUM(CASE WHEN g.{schema.result} = 'D' THEN 1 ELSE 0 END),
               SUM(CASE WHEN g.{schema.result} = 'L' THEN 1 ELSE 0 END),
               CASE WHEN MIN(g.{schema.season}) = MAX(g.{schema.season})
                    THEN CAST(MIN(g.{schema.season}) AS TEXT)
                    ELSE MIN(g.{schema.season}) || '-' || MAX(g.{schema.season})
               END,
               '{{}}', :now
        FROM {schema.games} g
        JOIN clubs c ON c.name = g.{club}
        WHERE {_named(schema)}
        GROUP BY g.{schema.player_id}, c.club_id
    """, {"now": now})
    return con.execute("SELECT COUNT(*) FROM club_player_register").fetchone()[0]


def _records(con, sport, stats, now):
    """Top-N leaderboards per club, per scope, per stat.

    Two scopes where a row is one game -- the best single game and the best
    season -- and one where it is already a season, which is every scope
    that data can carry.
    """
    schema = sport.schema
    club = _club_expression(schema)
    per_season_rows = sport.games_row_label == "Player-seasons"

    scopes = [("season", True)]
    if not per_season_rows:
        scopes.append(("game", False))

    total = 0
    for scope, aggregate in scopes:
        for stat in stats:
            if aggregate:
                sql = f"""
                    SELECT c.club_id, g.{schema.player}, g.{schema.player_id},
                           SUM(g.{stat}) AS value,
                           {_games_played(con, schema.games)} AS games,
                           g.{schema.season}, NULL, NULL, NULL
                    FROM {schema.games} g
                    JOIN clubs c ON c.name = g.{club}
                    WHERE g.{stat} IS NOT NULL AND {_named(schema)}
                    GROUP BY g.{schema.player_id}, c.club_id, g.{schema.season}
                """
            else:
                sql = f"""
                    SELECT c.club_id, g.{schema.player}, g.{schema.player_id},
                           g.{stat} AS value, 1 AS games, g.{schema.season},
                           g.{schema.round}, g.{schema.opponent}, g.{schema.date}
                    FROM {schema.games} g
                    JOIN clubs c ON c.name = g.{club}
                    WHERE g.{stat} IS NOT NULL AND {_named(schema)}
                """
            rows = con.execute(f"""
                SELECT * FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY club_id ORDER BY value DESC) AS rank
                    FROM ({sql})
                ) WHERE rank <= {TOP_N} AND value IS NOT NULL
            """).fetchall()

            heading = f"{scope.title()} {stat.replace('_', ' ')}"
            con.executemany(
                "INSERT INTO club_player_records ("
                "club_id, scope, stat, source_heading, source_rank, "
                "player_name_source, player_name, player_id, match_status, "
                "candidate_count, value, games, average, season, round, "
                "opponent, match_date, raw_row_json, imported_at) "
                "VALUES (?,?,?,?,?,?,?,?,'derived',1,?,?,?,?,?,?,?,'{}',?)",
                [(r[0], scope, stat, heading, r[9], r[1], r[1], r[2],
                  r[3], r[4], (r[3] / r[4]) if r[4] else None,
                  r[5], r[6], r[7], r[8], now) for r in rows])
            total += len(rows)
    return total


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--sport", default="all",
                        help="a sport key, or 'all' for every derivable sport")
    parser.add_argument("--quiet", dest="verbose", action="store_false")
    args = parser.parse_args(argv)

    if args.sport == "all":
        wanted = [s for key, s in sports.SPORTS.items() if key not in EXCLUDED]
    else:
        sport = sports.SPORTS.get(args.sport.strip().lower())
        if sport is None:
            print(f"unknown sport {args.sport!r}", file=sys.stderr)
            return 2
        wanted = [sport]

    failures = 0
    for sport in wanted:
        try:
            if args.verbose:
                print(f"{sport.label}...")
            derive(sport, verbose=args.verbose)
        except DeriveError as error:
            print(f"  skipped: {error}", file=sys.stderr)
            failures += 1
    return 1 if failures and failures == len(wanted) else 0


if __name__ == "__main__":
    sys.exit(main())
