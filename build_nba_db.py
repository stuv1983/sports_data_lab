#!/usr/bin/env python3
"""
build_nba_db.py -- Build data/nba/nba.db from any NBA source adapter.

    python build_nba_db.py --source csv
    python build_nba_db.py --source csv --seasons 2018-2023
    python build_nba_db.py --source nba_api --seasons 1946-2026
    python build_nba_db.py --reference-only

The build talks to nba_source.NbaSource and never to a website. Adapters
live in nba_source.py (CSV, the canonical one) and nba_source_api.py
(NBA.com, private prototype only -- read its licensing note first).
`--source csv` is the default because it is the one with no conditions
attached.

WHAT COMES OUT
--------------
Two tables the engine reads, and a handful it is derived from:

  players / games      satisfy core.Schema, so every existing page works
  franchises / teams   the normalised truth `club_now` and `club_hist` are
  / team_aliases       derived from, and the source of NBA_SCHEMA.clubs
  matches              one row per fixture
  player_seasons       per-player per-season totals, split by phase
  team_seasons         season record, standing, playoffs, championship
  player_team_history  which teams a player appeared for, and when
  stat_coverage        the measured era each statistic actually covers
  source_manifest      every retrieval: what, when, and its digest
  source_issues        anything reconciled rather than trusted

`club_now` and `club_hist` are deliberately TEXT rather than foreign keys.
core.Generic compares them as strings for every sport, and teaching the
shared engine to join a teams table for one sport would have put NBA
assumptions into the code the AFL depends on. The normalised tables are the
source of truth; the text columns are the denormalised read path.

NULL IS NOT ZERO
----------------
Steals, blocks, turnovers and the rebound split start in 1973-74; three
pointers in 1979-80; minutes are patchy before 1951-52. Those cells are
NULL, never 0. Writing 0 would be a claim about the players rather than
about the records, it would rank the entire early league as maximally
obscure on those terms, and it would make "never recorded a steal" and
"recorded no steals" the same answer. Every career aggregate below uses
min_count=1 for exactly this reason.

SEASONS ARE START YEARS
-----------------------
`season` is 1996 for the 1996-97 season. Every builder in core.Generic
compares season numerically, so it has to be an integer, and `season_label`
carries the display form. A user asking for "2005" gets 2005-06.

IDEMPOTENCE
-----------
Running this twice over the same source produces the same database, down to
the player_id values: ids are assigned by sorting on the source's own id,
not by row order. That is what makes a rebuild safe once anything else
references a player.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import data_paths
import names
import nba_source
import obscurity
import sports

#: Playoff round codes the build understands. `F` is the Finals, which
#: constraints_nba.played_in_the_finals() and the championship derivation
#: both read -- the NBA equivalent of the AFL grand final being one
#: nominated round rather than simply the last post-season game.
#:
#: Checked rather than assumed. If a source starts calling the Finals
#: something else, every Finals and championship square silently returns
#: nobody, which reads as "no player has ever won a title" -- so an
#: unrecognised code is recorded as an error issue and fails the build.
PLAYOFF_ROUNDS = ("R1", "CSF", "CF", "F")

#: Conferences, for the standings derived into team_seasons. Kept as a
#: mapping of current franchise name so a relocated team follows its
#: modern conference, which is what a "made the playoffs" square means.
CONFERENCES = {
    "east": {"Atlanta Hawks", "Boston Celtics", "Brooklyn Nets",
             "Charlotte Hornets", "Chicago Bulls", "Cleveland Cavaliers",
             "Detroit Pistons", "Indiana Pacers", "Miami Heat",
             "Milwaukee Bucks", "New York Knicks", "Orlando Magic",
             "Philadelphia 76ers", "Toronto Raptors", "Washington Wizards"},
    "west": {"Dallas Mavericks", "Denver Nuggets", "Golden State Warriors",
             "Houston Rockets", "Los Angeles Clippers", "Los Angeles Lakers",
             "Memphis Grizzlies", "Minnesota Timberwolves",
             "New Orleans Pelicans", "Oklahoma City Thunder",
             "Phoenix Suns", "Portland Trail Blazers", "Sacramento Kings",
             "San Antonio Spurs", "Utah Jazz"},
}

SCHEMA = sports.NBA_SCHEMA
STATS = list(sports.NBA_STATS)


# --------------------------------------------------------------- schema

DDL = f"""
CREATE TABLE IF NOT EXISTS franchises (
    franchise_id  TEXT PRIMARY KEY,
    current_name  TEXT NOT NULL UNIQUE,
    first_season  INTEGER,
    last_season   INTEGER,
    is_active     INTEGER NOT NULL DEFAULT 1,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS teams (
    team_id       TEXT PRIMARY KEY,
    franchise_id  TEXT NOT NULL REFERENCES franchises(franchise_id),
    name          TEXT NOT NULL,
    city          TEXT,
    nickname      TEXT,
    abbreviation  TEXT,
    first_season  INTEGER,
    last_season   INTEGER,
    is_current    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS team_aliases (
    alias    TEXT NOT NULL,
    team_id  TEXT NOT NULL REFERENCES teams(team_id),
    kind     TEXT NOT NULL,
    PRIMARY KEY (alias, team_id)
);

CREATE TABLE IF NOT EXISTS matches (
    match_id      TEXT PRIMARY KEY,
    season        INTEGER NOT NULL,
    season_label  TEXT NOT NULL,
    date          TEXT NOT NULL,
    phase         TEXT NOT NULL,
    round         TEXT,
    home_team_id  TEXT,
    away_team_id  TEXT,
    home_team     TEXT,
    away_team     TEXT,
    home_score    INTEGER,
    away_score    INTEGER,
    venue         TEXT,
    attendance    INTEGER
);

CREATE TABLE IF NOT EXISTS team_seasons (
    season               INTEGER NOT NULL,
    phase                TEXT NOT NULL,
    club_now             TEXT NOT NULL,
    played               INTEGER,
    wins                 INTEGER,
    losses               INTEGER,
    points_for           INTEGER,
    points_against       INTEGER,
    win_pct              REAL,
    conference           TEXT,
    conference_rank      INTEGER,
    league_rank          INTEGER,
    made_playoffs        INTEGER,
    reached_finals       INTEGER,
    champion             INTEGER,
    bottom_of_conference INTEGER,
    PRIMARY KEY (season, phase, club_now)
);

CREATE TABLE IF NOT EXISTS player_team_history (
    player_id     INTEGER NOT NULL,
    team_id       TEXT NOT NULL,
    club_hist     TEXT NOT NULL,
    club_now      TEXT NOT NULL,
    first_season  INTEGER,
    last_season   INTEGER,
    games         INTEGER,
    playoff_games INTEGER,
    PRIMARY KEY (player_id, team_id)
);

CREATE TABLE IF NOT EXISTS source_manifest (
    source_key TEXT NOT NULL,
    endpoint   TEXT NOT NULL,
    params     TEXT NOT NULL,
    season     INTEGER,
    phase      TEXT,
    fetched_at TEXT NOT NULL,
    digest     TEXT NOT NULL,
    rows       INTEGER,
    path       TEXT,
    PRIMARY KEY (source_key, endpoint, params)
);

CREATE TABLE IF NOT EXISTS source_issues (
    source_key TEXT,
    season     INTEGER,
    phase      TEXT,
    kind       TEXT NOT NULL,
    detail     TEXT NOT NULL,
    severity   TEXT NOT NULL,
    seen_at    TEXT NOT NULL
);
"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_games_player ON games(player_id)",
    "CREATE INDEX IF NOT EXISTS ix_games_club ON games(club_now)",
    "CREATE INDEX IF NOT EXISTS ix_games_hist ON games(club_hist)",
    "CREATE INDEX IF NOT EXISTS ix_games_season ON games(season)",
    "CREATE INDEX IF NOT EXISTS ix_games_points ON games(points)",
    "CREATE INDEX IF NOT EXISTS ix_games_minutes ON games(minutes)",
    "CREATE INDEX IF NOT EXISTS ix_games_playoff "
    "ON games(is_playoff, player_id, result)",
    "CREATE INDEX IF NOT EXISTS ix_games_pc ON games(player_id, club_now)",
    "CREATE INDEX IF NOT EXISTS ix_games_cs ON games(club_now, season)",
    "CREATE INDEX IF NOT EXISTS ix_games_name ON games(player)",
    "CREATE INDEX IF NOT EXISTS ix_games_result ON games(result)",
    "CREATE INDEX IF NOT EXISTS ix_games_venue ON games(venue)",
    "CREATE INDEX IF NOT EXISTS ix_games_match ON games(match_id)",
    "CREATE INDEX IF NOT EXISTS ix_games_round ON games(round)",
    "CREATE INDEX IF NOT EXISTS ix_players_obsc ON players(obscurity)",
    "CREATE INDEX IF NOT EXISTS ix_players_key ON players(name_key)",
    "CREATE INDEX IF NOT EXISTS ix_pseasons ON player_seasons(player_id, season)",
    "CREATE INDEX IF NOT EXISTS ix_tseasons ON team_seasons(club_now, season)",
)


# ------------------------------------------------------------- utilities

def log(verbose, message):
    if verbose:
        print(message)


def issue(issues, kind, detail, severity="warn", season=None, phase=None,
          source_key=""):
    """Record something the build reconciled rather than trusted."""
    issues.append({
        "source_key": source_key, "season": season, "phase": phase,
        "kind": kind, "detail": detail, "severity": severity,
        "seen_at": nba_source.now()})


def season_label(season):
    return f"{int(season)}-{(int(season) + 1) % 100:02d}"


# ------------------------------------------------------------- the build

def build(db_path, source, seasons=None, strict=True, write_reference=True,
          verbose=True):
    """Build `db_path` from `source`. Returns a summary dict."""
    import pandas as pd

    import dedupe_games

    db_path = str(db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    issues = []

    # 1. Teams and franchises -------------------------------------------
    log(verbose, "teams...")
    teams = source.teams()
    if teams is None or teams.empty:
        sys.exit("source returned no teams; nothing can be resolved without them")
    teams = teams.copy()
    teams["is_current"] = teams["is_current"].fillna(0).astype(int)

    # A franchise's current name is the name of its is_current team. That
    # single value is what every club_now cell becomes, and what
    # NBA_SCHEMA.clubs is built from.
    current = teams.loc[teams["is_current"] == 1]
    franchise_name = dict(zip(current["franchise_id"], current["name"]))
    missing = sorted(set(teams["franchise_id"]) - set(franchise_name))
    for fid in missing:
        # A franchise with no current identity is a defunct one. Its own
        # earliest name stands in, so its players are still findable.
        rows = teams.loc[teams["franchise_id"] == fid]
        franchise_name[fid] = rows["name"].iloc[0]
        issue(issues, "defunct_franchise",
              f"franchise {fid} has no current team; using "
              f"{franchise_name[fid]!r} as its club_now name",
              source_key=source.key)

    team_now = {row.team_id: franchise_name[row.franchise_id]
                for row in teams.itertuples()}
    team_hist = dict(zip(teams["team_id"], teams["name"]))

    # 2. Players ---------------------------------------------------------
    log(verbose, "players...")
    people = source.players()
    if people is None or people.empty:
        sys.exit("source returned no players")
    people = people.copy()
    people["source_player_id"] = people["source_player_id"].astype(str)
    people = (people.drop_duplicates(subset=["source_player_id"])
                    .sort_values("source_player_id", kind="stable")
                    .reset_index(drop=True))
    # Sorted on the source's own id, so the same source always yields the
    # same player_id. Anything holding an id survives a rebuild.
    people["player_id"] = range(1, len(people) + 1)
    pid_of = dict(zip(people["source_player_id"], people["player_id"]))
    people["name_key"] = people["player"].map(names.normalise_name)

    namesakes = people.groupby("name_key").size()
    shared = int((namesakes > 1).sum())
    if shared:
        log(verbose, f"  {shared:,} name keys are shared by more than one "
                     f"player -- searches must disambiguate by player_id")

    # 3. Matches ---------------------------------------------------------
    wanted = sorted(seasons) if seasons else list(source.seasons())
    if not wanted:
        sys.exit("no seasons to build; pass --seasons or check the source")
    log(verbose, f"matches for {len(wanted)} season(s) "
                 f"{wanted[0]}-{wanted[-1]}...")
    match_frames = []
    for season in wanted:
        frame = source.matches(season)
        if frame is None or frame.empty:
            issue(issues, "missing_season",
                  f"no matches for {season_label(season)}",
                  season=season, source_key=source.key)
            continue
        match_frames.append(frame)
    if not match_frames:
        sys.exit("no matches in any requested season")
    matches = pd.concat(match_frames, ignore_index=True)
    matches = matches.drop_duplicates(subset=["match_id"], keep="first")
    matches["match_id"] = matches["match_id"].astype(str)
    matches["home_team"] = matches["home_team_id"].map(team_now)
    matches["away_team"] = matches["away_team_id"].map(team_now)

    unknown = sorted(set(matches["home_team_id"].dropna())
                     | set(matches["away_team_id"].dropna()))
    unknown = [t for t in unknown if t not in team_now]
    for team_id in unknown:
        issue(issues, "unknown_team",
              f"match references team_id {team_id!r}, which is not in teams",
              severity="error", source_key=source.key)

    playoff_rounds = matches.loc[matches["phase"] == "playoff", "round"]
    seen = {str(r).strip().upper() for r in playoff_rounds.dropna()}
    unknown = sorted(r for r in seen if r and r not in PLAYOFF_ROUNDS)
    if unknown:
        issue(issues, "unknown_playoff_round",
              f"playoff round code(s) {', '.join(unknown)} are not in "
              f"PLAYOFF_ROUNDS {PLAYOFF_ROUNDS}; the Finals and championship "
              f"squares read 'F' and would silently answer nobody",
              severity="error", source_key=source.key)
    if "F" not in seen and len(matches.loc[matches["phase"] == "playoff"]):
        issue(issues, "no_finals_round",
              "playoff matches exist but none are round 'F'; no championship "
              "can be derived", severity="error", source_key=source.key)

    by_match = matches.set_index("match_id")

    # 4. Player-games ----------------------------------------------------
    log(verbose, "player games...")
    game_frames = []
    for season in wanted:
        for phase in nba_source.PHASES:
            frame = source.player_games(season, phase)
            if frame is None or frame.empty:
                continue
            frame = frame.copy()
            frame["phase"] = phase
            game_frames.append(frame)
    if not game_frames:
        sys.exit("no player games in any requested season")
    out = pd.concat(game_frames, ignore_index=True)
    out["source_player_id"] = out["source_player_id"].astype(str)
    out["match_id"] = out["match_id"].astype(str)
    out["team_id"] = out["team_id"].astype(str)

    known = out["source_player_id"].isin(pid_of)
    if not known.all():
        issue(issues, "unknown_player",
              f"{int((~known).sum()):,} player-game rows reference a player "
              f"not in the player list; dropped",
              severity="error", source_key=source.key)
        out = out.loc[known]
    in_schedule = out["match_id"].isin(by_match.index)
    if not in_schedule.all():
        issue(issues, "missing_match",
              f"{int((~in_schedule).sum()):,} player-game rows reference a "
              f"match not in the schedule; dropped",
              severity="error", source_key=source.key)
        out = out.loc[in_schedule]
    if out.empty:
        sys.exit("every player-game row was rejected; check the source")

    out = out.reset_index(drop=True)
    out["player_id"] = out["source_player_id"].map(pid_of)
    out["player"] = out["player_id"].map(
        dict(zip(people["player_id"], people["player"])))
    out["club_hist"] = out["team_id"].map(team_hist)
    out["club_now"] = out["team_id"].map(team_now)

    for column in ("season", "season_label", "date", "venue", "round"):
        out[column] = out["match_id"].map(by_match[column])
    out["is_playoff"] = (out["phase"] == "playoff").astype(int)

    home_id = out["match_id"].map(by_match["home_team_id"]).astype(str)
    out["is_home"] = (out["team_id"] == home_id).astype(int)
    home_score = out["match_id"].map(by_match["home_score"])
    away_score = out["match_id"].map(by_match["away_score"])
    out["points_for"] = home_score.where(out["is_home"] == 1, away_score)
    out["points_against"] = away_score.where(out["is_home"] == 1, home_score)
    out["opponent"] = out["match_id"].map(by_match["away_team"]).where(
        out["is_home"] == 1, out["match_id"].map(by_match["home_team"]))

    # 'W'/'L'/NULL and never 'D': the NBA plays overtime, so a game with a
    # known score always has a winner, and a game without one has no result
    # to report rather than a drawn one.
    decided = out["points_for"].notna() & out["points_against"].notna()
    out["result"] = None
    out.loc[decided & (out["points_for"] > out["points_against"]),
            "result"] = "W"
    out.loc[decided & (out["points_for"] < out["points_against"]),
            "result"] = "L"

    # `round` is a real fact, not a placeholder: the playoff round for a
    # playoff game, and the team's own game number within the season for a
    # regular-season one. That gives the NBA the same "one nominated round"
    # hook the AFL grand final uses.
    out["round"] = out["round"].astype("object")
    regular = out["is_playoff"] == 0
    order = (out.loc[regular]
                .sort_values(["date", "match_id"], kind="stable")
                .groupby(["team_id", "season"]).cumcount() + 1)
    out.loc[regular, "round"] = order.astype(str)
    out["round"] = out["round"].fillna("")

    # 5. career_game_no --------------------------------------------------
    out = out.sort_values(["player_id", "date", "match_id"], kind="stable")
    out["career_game_no"] = out.groupby("player_id").cumcount() + 1
    out = out.reset_index(drop=True)

    # 6. Deduplicate -----------------------------------------------------
    out, unresolved = _deduplicate(out, issues, source.key, strict, verbose)
    # career_game_no is derived, so it has to be renumbered after any drop
    # or the sequence has holes where the duplicates were.
    out = out.sort_values(["player_id", "date", "match_id"], kind="stable")
    out["career_game_no"] = out.groupby("player_id").cumcount() + 1
    out = out.reset_index(drop=True)

    # 7. Careers ---------------------------------------------------------
    log(verbose, "career totals...")
    grouped = out.groupby("player_id")
    # min_count=1 throughout: a career wholly before a statistic was
    # recorded gets NULL, not 0. This is the difference between "recorded
    # no steals" and "steals were not recorded", and the obscurity model
    # depends on it -- a 0 there would rank the entire early league as
    # maximally obscure for a reason that is an artefact of record-keeping.
    careers = pd.DataFrame({
        "career_games": grouped.size(),
        "debut_season": grouped["season"].min(),
        "final_season": grouped["season"].max(),
        "career_points": grouped["points"].sum(min_count=1),
        "career_minutes": grouped["minutes"].sum(min_count=1),
        "career_rebounds": grouped["rebounds"].sum(min_count=1),
        "career_assists": grouped["assists"].sum(min_count=1),
        "career_steals": grouped["steals"].sum(min_count=1),
        "career_blocks": grouped["blocks"].sum(min_count=1),
        "playoffs_played": grouped["is_playoff"].sum(),
        "clubs_hist": grouped["club_hist"].apply(
            lambda s: "|".join(sorted(set(s.dropna())))),
        "clubs_now": grouped["club_now"].apply(
            lambda s: "|".join(sorted(set(s.dropna())))),
        "n_clubs": grouped["club_now"].nunique(),
    })

    players = people.set_index("player_id").join(careers, how="inner")
    players.index.name = "player_id"
    players = players.reset_index()
    if len(players) < len(people):
        log(verbose, f"  {len(people) - len(players):,} listed players have "
                     f"no games in the requested seasons and are omitted")

    # 8. Obscurity -------------------------------------------------------
    log(verbose, f"obscurity (model v{obscurity.NBA_MODEL.version})...")
    for column, values in obscurity.components(
            players, obscurity.NBA_MODEL).items():
        players[column] = values

    # 9. Write -----------------------------------------------------------
    log(verbose, f"writing {db_path}...")
    con = sqlite3.connect(db_path)
    try:
        con.executescript(DDL)

        game_columns = ["player_id", "player", "match_id", "season",
                        "season_label", "date", "round", "club_now",
                        "club_hist", "opponent", "venue", "is_home",
                        "career_game_no", "is_playoff", "result",
                        "points_for", "points_against", *STATS]
        out.loc[:, game_columns].to_sql("games", con, index=False,
                                        if_exists="replace")

        player_columns = [
            "player_id", "source_player_id", "player", "name_key",
            "birth_year", "position", "height_cm", "weight_kg",
            "debut_season", "final_season", "career_games", "career_points",
            "career_minutes", "playoffs_played", "career_rebounds",
            "career_assists", "career_steals", "career_blocks",
            "n_clubs", "clubs_hist", "clubs_now",
            *[c for c in players.columns if c.endswith("_component")],
            "obscurity", "obscurity_confidence", "obscurity_model"]
        players.loc[:, player_columns].to_sql("players", con, index=False,
                                              if_exists="replace")

        _write_reference_tables(con, teams, franchise_name, matches)
        _write_derived(con, out, verbose)
        _write_manifest(con, source, issues)

        for statement in INDEXES:
            con.execute(statement)

        # Dropped and recreated rather than updated, so a rebuild cannot
        # leave a stale key behind claiming a source it no longer used.
        con.execute("DROP TABLE IF EXISTS meta")
        con.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        con.executemany("INSERT INTO meta VALUES (?,?)", [
            ("source", source.key),
            ("seasons", f"{min(wanted)}-{max(wanted)}"),
            ("season_labels",
             f"{season_label(min(wanted))}-{season_label(max(wanted))}"),
            ("obscurity_model", str(obscurity.NBA_MODEL.version)),
            ("built", nba_source.now()),
        ])
        con.commit()

        # 10. Stat coverage, measured from what was just written ----------
        import load_stat_coverage
        competition, source_note = load_stat_coverage.COMPETITIONS["nba"]
        load_stat_coverage.load(con, STATS,
                                source=f"{source_note} via {source.key}",
                                competition=competition)

        # 11. Strict health gate -----------------------------------------
        warnings = _health_gate(con, verbose)
    finally:
        con.close()

    if warnings and strict:
        sys.exit("Health checks failed; the database was written but should "
                 "not be trusted. Fix the source or re-run with --no-strict "
                 "to keep it anyway.")

    if write_reference:
        load_reference(db_path, verbose=verbose)

    summary = {"players": len(players), "games": len(out),
               "matches": len(matches), "teams": len(teams),
               "seasons": [min(wanted), max(wanted)],
               "issues": len(issues), "unresolved": len(unresolved),
               "warnings": warnings}
    log(verbose, f"\n{len(players):,} players, {len(out):,} player-games, "
                 f"{len(matches):,} matches, {len(teams):,} team identities")
    return summary


def _deduplicate(out, issues, source_key, strict, verbose):
    """Collapse duplicate player-game rows before any total is taken.

    Two passes, in this order and for a reason. The real NBA duplicate mode
    is the same box score arriving twice, which is an exact
    (player_id, match_id) repeat and needs no cleverness -- collapsing those
    first keeps the residue small. Only what survives goes to
    dedupe_games.resolve, whose career-sequence logic assumes the sequence
    came from the source; here it is derived, so leaning on it for the bulk
    of the work would be circular. If it still reports something
    unresolved, that is a genuine source problem worth failing over.
    """
    import pandas as pd

    import dedupe_games

    exact = out.duplicated(subset=["player_id", "match_id"], keep="first")
    if exact.any():
        n = int(exact.sum())
        log(verbose, f"  collapsed {n:,} exact duplicate (player, match) row(s)")
        issue(issues, "duplicate_collapsed",
              f"{n} exact duplicate (player_id, match_id) row(s) collapsed",
              source_key=source_key)
        out = out.loc[~exact].reset_index(drop=True)

    duplicated = out.duplicated(subset=["player_id", "date"], keep=False)
    if not duplicated.any():
        return out, []

    affected = out.loc[duplicated]
    log(verbose, f"  {len(affected):,} row(s) share a (player, date) key -- "
                 f"resolving before career totals are taken")
    rows = [
        {"player_id": r.player_id, "date": r.date,
         "career_game_no": (None if pd.isna(r.career_game_no)
                            else r.career_game_no),
         "ref": r.Index,
         "fields": (r.match_id, r.club_hist, r.points)}
        for r in affected.itertuples()
    ]
    drop, unresolved = dedupe_games.resolve(rows)
    if drop:
        log(verbose, f"  resolved {len(drop):,} row(s) by career sequence")
        issue(issues, "duplicate_resolved",
              f"{len(drop)} duplicate (player_id, date) row(s) resolved by "
              f"career sequence", source_key=source_key)
        out = out.drop(index=drop).reset_index(drop=True)

    if unresolved:
        for item in unresolved:
            detail = (f"player {item.player_id} on {item.date}: "
                      f"candidates {item.candidates} -- {item.reason}")
            log(verbose, f"    {detail}")
            issue(issues, "duplicate_unresolved", detail, severity="error",
                  source_key=source_key)
        if strict:
            sys.exit(
                f"{len(unresolved)} unresolved duplicate player-game key(s): "
                "career totals built from this source would be wrong. "
                "Re-run with --allow-duplicates to build anyway.")
    return out, unresolved


def _write_reference_tables(con, teams, franchise_name, matches):
    """franchises, teams, team_aliases and matches."""
    con.execute("DELETE FROM franchises")
    con.execute("DELETE FROM teams")
    con.execute("DELETE FROM team_aliases")
    con.execute("DELETE FROM matches")

    spans = teams.groupby("franchise_id")[["first_season", "last_season"]]
    first = spans.min()["first_season"].to_dict()
    last = spans.max()["last_season"].to_dict()
    active = set(teams.loc[teams["is_current"] == 1, "franchise_id"])
    con.executemany(
        "INSERT INTO franchises (franchise_id, current_name, first_season, "
        "last_season, is_active, notes) VALUES (?,?,?,?,?,?)",
        [(fid, name, _int(first.get(fid)), _int(last.get(fid)),
          int(fid in active), None)
         for fid, name in sorted(franchise_name.items())])

    con.executemany(
        "INSERT INTO teams (team_id, franchise_id, name, city, nickname, "
        "abbreviation, first_season, last_season, is_current) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [(r.team_id, r.franchise_id, r.name, r.city, r.nickname,
          r.abbreviation, _int(r.first_season), _int(r.last_season),
          int(r.is_current)) for r in teams.itertuples()])

    # Every spelling a person might type, lowercased. Duplicates across
    # teams are fine -- the primary key is (alias, team_id) -- but a
    # collision means a lookup is ambiguous and the caller has to say which.
    aliases = []
    for r in teams.itertuples():
        for kind, value in (("name", r.name), ("abbrev", r.abbreviation),
                            ("nickname", r.nickname), ("city", r.city)):
            if value and str(value).strip():
                aliases.append((str(value).strip().lower(), r.team_id, kind))
    con.executemany(
        "INSERT OR IGNORE INTO team_aliases (alias, team_id, kind) "
        "VALUES (?,?,?)", aliases)

    con.executemany(
        "INSERT OR REPLACE INTO matches (match_id, season, season_label, "
        "date, phase, round, home_team_id, away_team_id, home_team, "
        "away_team, home_score, away_score, venue, attendance) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r.match_id, _int(r.season), r.season_label, r.date, r.phase,
          r.round, r.home_team_id, r.away_team_id, r.home_team, r.away_team,
          _int(r.home_score), _int(r.away_score), r.venue,
          _int(r.attendance)) for r in matches.itertuples()])


def _write_derived(con, out, verbose):
    """player_seasons, team_seasons and player_team_history."""
    log(verbose, "derived tables...")

    # -- player_seasons: totals per player per season per phase per team.
    # Every stat summed with min_count=1 so an unrecorded era stays NULL.
    seasons = (out.groupby(["player_id", "season", "phase", "club_now"])
                  .agg(games=("match_id", "size"),
                       **{s: (s, lambda x: x.sum(min_count=1)) for s in STATS})
                  .reset_index())
    seasons.to_sql("player_seasons", con, index=False, if_exists="replace")

    # -- team_seasons: one row per team per season per phase.
    # Built from the de-duplicated match list rather than from player rows,
    # so a team's record does not depend on how many players it fielded.
    matches = con.execute(
        "SELECT season, phase, home_team, away_team, home_score, away_score, "
        "round FROM matches").fetchall()
    record = {}
    for season, phase, home, away, hs, as_, _round in matches:
        for club, own, other in ((home, hs, as_), (away, as_, hs)):
            if not club:
                continue
            slot = record.setdefault((season, phase, club), {
                "played": 0, "wins": 0, "losses": 0,
                "points_for": 0, "points_against": 0, "reached_finals": 0})
            slot["played"] += 1
            if own is not None and other is not None:
                slot["points_for"] += own
                slot["points_against"] += other
                slot["wins" if own > other else "losses"] += 1

    # The champion is the team that won the last Finals game of a season.
    # Derived rather than declared, so it cannot disagree with the games.
    champions = {}
    reached_finals = set()
    finals = con.execute(
        "SELECT season, date, home_team, away_team, home_score, away_score "
        "FROM matches WHERE phase = 'playoff' AND UPPER(TRIM(round)) = 'F' "
        "ORDER BY season, date").fetchall()
    for season, _date, home, away, hs, as_ in finals:
        reached_finals.update((season, club) for club in (home, away) if club)
        if hs is None or as_ is None:
            continue
        # Ordered by date, so the last Finals game of a season wins the
        # assignment. Derived from the games rather than declared, so it
        # cannot disagree with them.
        champions[season] = home if hs > as_ else away

    made_playoffs = {(s, c) for (s, phase, c) in record if phase == "playoff"}

    # A row describes one phase's record, but made_playoffs, reached_finals
    # and champion are outcomes of the *season*, not of a phase. Carrying
    # them on both rows would make "SELECT ... WHERE champion=1" return the
    # same team twice, so they live on the regular-season row alone, which
    # is the canonical row for a team's season. constraints_nba joins on
    # phase='regular' for exactly this reason.
    rows = []
    for (season, phase, club), slot in sorted(record.items()):
        played = slot.get("played", 0)
        wins = slot.get("wins", 0)
        losses = slot.get("losses", 0)
        decided = wins + losses
        conference = ("East" if club in CONFERENCES["east"]
                      else "West" if club in CONFERENCES["west"] else None)
        season_row = phase == "regular"
        rows.append((
            season, phase, club, played, wins, losses,
            slot.get("points_for"), slot.get("points_against"),
            round(wins / decided, 4) if decided else None,
            conference, None, None,
            int(season_row and (season, club) in made_playoffs),
            int(season_row and (season, club) in reached_finals),
            int(season_row and champions.get(season) == club),
            0))
    con.execute("DELETE FROM team_seasons")
    con.executemany(
        "INSERT INTO team_seasons (season, phase, club_now, played, wins, "
        "losses, points_for, points_against, win_pct, conference, "
        "conference_rank, league_rank, made_playoffs, reached_finals, "
        "champion, bottom_of_conference) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    _rank_standings(con)

    # -- player_team_history: which teams a player appeared for, and when.
    # This is what a team-eligibility question should read instead of
    # scanning every box score.
    history = (out.groupby(["player_id", "team_id", "club_hist", "club_now"])
                  .agg(first_season=("season", "min"),
                       last_season=("season", "max"),
                       games=("match_id", "size"),
                       playoff_games=("is_playoff", "sum"))
                  .reset_index())
    history.to_sql("player_team_history", con, index=False,
                   if_exists="replace")


def _rank_standings(con):
    """Fill conference_rank, league_rank and bottom_of_conference.

    Separate from the insert because ranking needs every row of a season to
    exist first. bottom_of_conference is the NBA's nearest equivalent to
    the AFL wooden spoon and is only meaningful where a conference is known.
    """
    seasons = [r[0] for r in con.execute(
        "SELECT DISTINCT season FROM team_seasons WHERE phase='regular'")]
    for season in seasons:
        rows = con.execute(
            "SELECT club_now, conference, win_pct FROM team_seasons "
            "WHERE season=? AND phase='regular' AND win_pct IS NOT NULL",
            (season,)).fetchall()
        if not rows:
            continue
        league = sorted(rows, key=lambda r: -r[2])
        for rank, (club, _conf, _pct) in enumerate(league, start=1):
            con.execute("UPDATE team_seasons SET league_rank=? WHERE "
                        "season=? AND phase='regular' AND club_now=?",
                        (rank, season, club))
        for conference in ("East", "West"):
            group = sorted((r for r in rows if r[1] == conference),
                           key=lambda r: -r[2])
            for rank, (club, _conf, _pct) in enumerate(group, start=1):
                con.execute(
                    "UPDATE team_seasons SET conference_rank=?, "
                    "bottom_of_conference=? WHERE season=? AND "
                    "phase='regular' AND club_now=?",
                    (rank, int(rank == len(group)), season, club))


def _write_manifest(con, source, issues):
    """source_manifest and source_issues."""
    con.executemany(
        "INSERT OR REPLACE INTO source_manifest (source_key, endpoint, "
        "params, season, phase, fetched_at, digest, rows, path) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [(f.source_key, f.endpoint, f.params, f.season, f.phase,
          f.fetched_at, f.digest, f.rows, f.path) for f in source.fetches()])
    con.execute("DELETE FROM source_issues")
    con.executemany(
        "INSERT INTO source_issues (source_key, season, phase, kind, detail, "
        "severity, seen_at) VALUES (?,?,?,?,?,?,?)",
        [(i["source_key"], i["season"], i["phase"], i["kind"], i["detail"],
          i["severity"], i["seen_at"]) for i in issues])


def _health_gate(con, verbose):
    """Every check that must pass before the database is worth trusting."""
    import core
    import health

    warnings = list(health.integrity_warnings(con, SCHEMA))
    try:
        core.require_schema(con, SCHEMA)
    except RuntimeError as exc:
        warnings.append(str(exc))
    errors = con.execute(
        "SELECT COUNT(*) FROM source_issues WHERE severity='error'"
    ).fetchone()[0]
    if errors:
        warnings.append(f"{errors} source issue(s) recorded at severity "
                        f"'error' -- see the source_issues table")
    if warnings:
        log(verbose, "\nHEALTH WARNINGS")
        for warning in warnings:
            log(verbose, f"  {warning}")
    else:
        log(verbose, "\nhealth: all checks clean")
    return warnings


def _int(value):
    """None-preserving int, for columns a source may leave blank."""
    if value is None:
        return None
    try:
        import math
        if isinstance(value, float) and math.isnan(value):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------- reference

def load_reference(db_path, out_path=None, verbose=True):
    """
    Write the reference file sports.py reads at import time.

    Everything here is measured from the built database. The team list is
    the current franchises; the lineage is each franchise's historical names
    *that actually appear in games.club_hist*, so a name nobody played under
    never becomes a pickable answer; the eras come from stat_coverage.

    Restart Streamlit after this runs. The values are frozen into
    core.Schema at import, and the db_revision cache key does not reach them.
    """
    # Beside the database, not at a fixed path. For the canonical
    # data/nba/nba.db that resolves to data/nba/reference/nba_reference.json,
    # which is exactly where nba_reference.PATH looks -- and a build into a
    # temporary database writes its reference next to itself instead of
    # overwriting the real one with a fixture's three teams.
    out_path = Path(out_path) if out_path else (
        Path(db_path).resolve().parent / "reference" / "nba_reference.json")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        teams = [r[0] for r in con.execute(
            "SELECT current_name FROM franchises WHERE is_active=1 "
            "ORDER BY current_name")]
        played = {r[0] for r in con.execute(
            "SELECT DISTINCT club_hist FROM games")}
        lineage = {}
        for current, in con.execute(
                "SELECT current_name FROM franchises WHERE is_active=1"):
            identities = [r[0] for r in con.execute(
                "SELECT DISTINCT t.name FROM teams t JOIN franchises f "
                "ON f.franchise_id = t.franchise_id "
                "WHERE f.current_name = ? ORDER BY t.name", (current,))]
            identities = [n for n in identities if n in played or n == current]
            # A franchise with only its current name needs no entry:
            # club_identities already falls back to [club].
            if len(identities) > 1:
                lineage[current] = [current] + sorted(
                    n for n in identities if n != current)
        eras = {stat: season for stat, season in con.execute(
            "SELECT stat_name, available_from FROM stat_coverage "
            "WHERE available_from IS NOT NULL")}
        lo, hi = con.execute("SELECT MIN(season), MAX(season) FROM games"
                             ).fetchone()
        venues = _measured_venue_aliases(con)
    finally:
        con.close()

    import nba_reference
    payload = {
        "generated_at": nba_source.now(),
        "db": str(db_path),
        "seasons": [lo, hi],
        "teams": teams,
        "club_lineage": lineage,
        # Aliases are a judgement about what people type, not something the
        # data can measure, so the checked-in map is kept and only filtered
        # to arenas that actually appear.
        "venue_aliases": venues,
        "stat_eras": eras,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if verbose:
        print(f"reference: {out_path} "
              f"({len(teams)} teams, {len(lineage)} lineages, "
              f"{len(eras)} measured eras)")
        print("  restart Streamlit to pick these up -- they are frozen into "
              "the schema at import.")
    return payload


def _measured_venue_aliases(con):
    """The checked-in alias map, narrowed to arenas the database has."""
    import nba_reference
    known = {r[0] for r in con.execute(
        "SELECT DISTINCT venue FROM games WHERE venue IS NOT NULL")}
    if not known:
        return dict(nba_reference.FALLBACK_VENUE_ALIASES)
    return {alias: canonical
            for alias, canonical in nba_reference.FALLBACK_VENUE_ALIASES.items()
            if canonical in known}


def refresh_layers(db_path, verbose=True):
    """
    Optional layers, in load order.

    Empty for these milestones. It exists so the draft, award, teammate and
    coach loaders have one obvious place to be wired in, and so the rule
    build_db.refresh_layers documents applies here from the start: every
    layer must write to `db_path`, or a rebuild ends up split across two
    files.
    """
    log(verbose, "no optional NBA layers are implemented yet")
    return {}


# ------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=data_paths.default_db("nba"))
    ap.add_argument("--source", default="csv",
                    help="csv (default) or nba_api; see nba_source_api.py "
                         "for the licensing conditions on nba_api")
    ap.add_argument("--csv-root", default=None,
                    help="where the csv source reads from "
                         "(default: data/nba/raw/csv)")
    ap.add_argument("--seasons", default=None,
                    help="start years, e.g. 1946-2026 or 2019,2021")
    ap.add_argument("--refresh", action="store_true",
                    help="re-request cached source responses")
    ap.add_argument("--allow-duplicates", action="store_true",
                    help="build even with unresolved duplicate player-games")
    ap.add_argument("--core-only", action="store_true",
                    help="skip the optional layers")
    ap.add_argument("--reference-only", action="store_true",
                    help="only rewrite data/nba/reference from an existing db")
    ap.add_argument("--no-strict", dest="strict", action="store_false",
                    help="keep the database even if health checks fail")
    ap.add_argument("--quiet", dest="verbose", action="store_false")
    args = ap.parse_args(argv)

    if args.reference_only:
        if not Path(args.db).exists():
            return _fail(f"No database at {args.db}.")
        load_reference(args.db, verbose=args.verbose)
        return 0

    if args.source == "csv":
        kwargs = {"root": args.csv_root} if args.csv_root else {}
    else:
        kwargs = {"refresh": args.refresh}
    try:
        source = nba_source.get_source(args.source, **kwargs)
    except nba_source.SourceError as exc:
        return _fail(str(exc))

    seasons = (nba_source.parse_seasons(args.seasons)
               if args.seasons else None)
    try:
        build(args.db, source, seasons=seasons,
              strict=args.strict and not args.allow_duplicates,
              verbose=args.verbose)
    except nba_source.SourceError as exc:
        return _fail(str(exc))

    if not args.core_only:
        refresh_layers(args.db, verbose=args.verbose)
    return 0


def _fail(message):
    print(message, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
