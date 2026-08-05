#!/usr/bin/env python3
"""
build_mlb_db.py -- Build data/mlb/mlb.db from the Lahman baseball database.

    python -m mlb.build_mlb_db
    python -m mlb.build_mlb_db --db /tmp/mlb.db --raw data/mlb/raw
    python -m mlb.build_mlb_db --quiet
    python -m mlb.build_mlb_db --no-retrosheet   # Lahman only, no network

Reads the Lahman CSV export (People, Appearances, Batting, Pitching, their
postseason counterparts, Teams and TeamsFranchises) and writes the two
tables every page in this repository expects: `players`, one row per
person with a career summary and an obscurity score, and `games`, the
per-appearance table constraints intersect over. See core.Schema for the
contract and sports.MLB_SCHEMA for the names MLB gives each column.

WHAT A ROW OF `games` IS
------------------------
Not a game. Lahman has no box scores -- its finest grain is a player's
season with one team -- so a row here is a player-season-team carrying
that season's totals. Everything downstream is honest about this:

  * `games` on the row holds the appearance count the season is worth, so
    career_games still sums to a real number of games played.
  * constraints_mlb.py does not offer the per-game squares. There is no
    "X+ of a stat in one game" for the MLB, because this build cannot
    answer it, and a square that answers a different question than it asks
    is worse than a missing one.
  * `career_game_no` numbers a player's *seasons*, so "first career game
    for club" means "debuted for this club", which is the same question at
    this grain.

CLUB IDENTITY
-------------
`club_hist` is the team's name in that season ("Brooklyn Dodgers") and
`club_now` is the franchise's current name ("Los Angeles Dodgers"), which
is the AFL and NBA convention. The mapping is measured from Lahman's
franchID rather than hand-maintained, and written to
data/mlb/reference/mlb_reference.json for sports.py to read at import.

POSTSEASON
----------
BattingPost and PitchingPost give per-round postseason lines and
SeriesPost says who won each series, so the `is_postseason` rows carry a
real `round` ('WS', 'ALCS', ...) and a real `result` -- which is what makes
"won a final" and "no finals wins" mean something specific. Players whose
whole career fell in seasons with no postseason at all get NULL rather
than 0 for `postseason_played`; see obscurity_model.MODEL.

RETROSHEET
----------
The final step calls mlb/load_retrosheet.py, which fills
`mlb_player_rivalry_games` from Retrosheet's bulk game logs -- the one
source of per-game lineups and results, which Lahman does not have at any
grain. It is the only step that touches the network, runs after the
database is already written, and degrades to an empty table rather than
failing the build. `--no-retrosheet` skips it entirely.
"""

import argparse
import json
import sqlite3
import sys
import zipfile
from pathlib import Path

import pandas as pd

import data_paths
import names
import obscurity

from . import obscurity_model

#: Batting counting stats, Lahman column -> the name this repository uses.
BATTING_STATS = {
    "AB": "at_bats", "R": "runs", "H": "hits", "2B": "doubles",
    "3B": "triples", "HR": "home_runs", "RBI": "rbis", "SB": "stolen_bases",
    "BB": "walks", "SO": "strikeouts",
}

#: Pitching stats that are totals. ERA is an average, so it is weighted by
#: outs recorded rather than summed -- see season_stats().
PITCHING_SUMS = {"W": "wins", "L": "losses", "SV": "saves"}

STAT_COLUMNS = (list(BATTING_STATS.values())
                + list(PITCHING_SUMS.values()) + ["era"])


def log(verbose, message):
    if verbose:
        print(message, flush=True)


# ------------------------------------------------------------- source data

def find_source(raw):
    """The directory or ZIP holding the Lahman CSVs."""
    raw = Path(raw)
    if raw.is_dir() and (raw / "People.csv").exists():
        return raw, "dir"
    if raw.is_file() and raw.suffix.lower() == ".zip":
        return raw, "zip"
    if raw.is_dir():
        archives = sorted(raw.glob("*.zip"))
        if archives:
            return archives[0], "zip"
    raise SystemExit(
        f"No Lahman data under {raw}. Put the extracted CSVs (People.csv, "
        f"Batting.csv, ...) there, or drop the lahman *_csv.zip in beside "
        f"them.")


def read_csv(source, kind, filename, required=True):
    """One Lahman CSV as a frame, or an empty frame when it is optional.

    Lahman ships some files with a UTF-8 BOM and an unnamed leading ID
    column; both are stripped here so no caller has to know.
    """
    try:
        if kind == "zip":
            with zipfile.ZipFile(source) as archive:
                matches = [n for n in archive.namelist()
                           if n.rsplit("/", 1)[-1].lower() == filename.lower()]
                if not matches:
                    raise FileNotFoundError(filename)
                with archive.open(matches[0]) as handle:
                    frame = pd.read_csv(handle, encoding="utf-8-sig",
                                        low_memory=False)
        else:
            path = source / filename
            if not path.exists():
                raise FileNotFoundError(filename)
            frame = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    except FileNotFoundError:
        if required:
            raise SystemExit(f"{filename} is missing from {source}.")
        return pd.DataFrame()
    return frame.loc[:, [c for c in frame.columns
                         if not str(c).startswith("Unnamed")]]


# ------------------------------------------------------------- franchises

def franchise_map(teams, franchises):
    """(season, teamID) -> that season's team name, and -> current name.

    Lahman's franchID is the stable franchise key and TeamsFranchises names
    it. A franchise Lahman marks inactive keeps its own last name as its
    "current" one, which is what a solver asking about the Montreal Expos
    means.
    """
    names_by_franch = dict(zip(franchises["franchID"],
                               franchises["franchName"]))

    # Two distinct teams sometimes share a name in one season: the 1884
    # Washington Nationals played in both the American Association and the
    # Union Association, and the 1939 Crawfords have two teamIDs either
    # side of a relocation. Left alone they collapse into one club and the
    # rows become indistinguishable, so the league disambiguates them.
    collisions = set()
    seen = {}
    for season, team, name in zip(teams["yearID"], teams["teamID"],
                                  teams["name"]):
        key = (int(season), str(name))
        if seen.setdefault(key, team) != team:
            collisions.add(key)

    season_name = {}
    current = {}
    for season, team, franch, name, league in zip(
            teams["yearID"], teams["teamID"], teams["franchID"],
            teams["name"], teams["lgID"]):
        label = str(name)
        if (int(season), label) in collisions:
            label = f"{label} ({league})"
        season_name[(int(season), team)] = label
        current[(int(season), team)] = names_by_franch.get(franch, label)
    return season_name, current


def lineage(teams, franchises):
    """Current franchise name -> every historical name that counts as it.

    One-directional, exactly as core.Schema.club_identities defines it:
    asking for the Dodgers includes Brooklyn, asking for Brooklyn returns
    only Brooklyn. Franchises that never changed name are left out, because
    an entry mapping a name to itself is what club_identities already does.
    """
    names_by_franch = dict(zip(franchises["franchID"],
                               franchises["franchName"]))
    out = {}
    for franch, name in zip(teams["franchID"], teams["name"]):
        current = names_by_franch.get(franch)
        if not current:
            continue
        seen = out.setdefault(current, [])
        if str(name) not in seen:
            seen.append(str(name))
    for current, seen in out.items():
        if current in seen:
            seen.remove(current)
        seen.insert(0, current)
    return {k: v for k, v in out.items() if len(v) > 1}


# ----------------------------------------------------------------- games

def season_stats(batting, pitching, keys):
    """Batting and pitching totals for each key group.

    A player traded mid-season has one Lahman row per stint; a games row is
    one team-season, so the stints are summed. ERA is weighted by IPouts,
    because the mean of two ERAs is not an ERA.
    """
    if batting.empty:
        bat = pd.DataFrame(columns=[*keys, *BATTING_STATS.values()])
    else:
        bat = (batting.groupby(keys, as_index=False)[list(BATTING_STATS)]
               .sum().rename(columns=BATTING_STATS))

    if pitching.empty:
        pit = pd.DataFrame(columns=[*keys, *PITCHING_SUMS.values(), "era"])
    else:
        pit = pitching.copy()
        pit["_outs"] = pd.to_numeric(pit["IPouts"], errors="coerce").fillna(0.0)
        pit["_er_weighted"] = (pd.to_numeric(pit["ERA"], errors="coerce")
                               * pit["_outs"])
        pit = pit.groupby(keys, as_index=False).agg(
            **{new: (old, "sum") for old, new in PITCHING_SUMS.items()},
            _outs=("_outs", "sum"), _er_weighted=("_er_weighted", "sum"))
        pit["era"] = (pit["_er_weighted"] / pit["_outs"]
                      ).where(pit["_outs"] > 0).round(2)
        pit = pit.drop(columns=["_outs", "_er_weighted"])

    return bat, pit


def regular_season_games(appearances, batting, pitching, season_name,
                         current_name):
    """One row per player-season-team, with that season's totals.

    Built from Appearances rather than Batting because Appearances counts
    every player the same way whatever position they played -- a relief
    pitcher's 70 appearances are 70 games, and his batting line is not.
    """
    keys = ["playerID", "yearID", "teamID"]
    rows = appearances.groupby(keys, as_index=False).agg(
        games=("G_all", "sum"))
    bat, pit = season_stats(batting, pitching, keys)
    rows = rows.merge(bat, on=keys, how="left").merge(pit, on=keys, how="left")

    rows["is_postseason"] = 0
    #: Lahman has no rounds outside October. 'R' keeps `round` non-NULL so
    #: a round filter never silently matches on missing data.
    rows["round"] = "R"
    rows["result"] = None
    return _finish_games(rows, season_name, current_name)


def postseason_games(batting_post, pitching_post, series_post, season_name,
                     current_name):
    """One row per player-season-team-round, with that round's totals."""
    if batting_post.empty and pitching_post.empty:
        return pd.DataFrame()

    keys = ["playerID", "yearID", "teamID", "round"]
    counts = []
    for frame in (batting_post, pitching_post):
        if not frame.empty:
            counts.append(frame.groupby(keys, as_index=False)
                          .agg(games=("G", "sum")))
    # A player who both batted and pitched in a round appears in each file;
    # the larger of the two counts is the number of games he was in, and
    # summing them would count the same game twice.
    rows = (pd.concat(counts, ignore_index=True)
            .groupby(keys, as_index=False).agg(games=("games", "max")))

    bat, pit = season_stats(batting_post, pitching_post, keys)
    rows = rows.merge(bat, on=keys, how="left").merge(pit, on=keys, how="left")

    rows["is_postseason"] = 1
    rows["result"] = _series_results(rows, series_post)
    return _finish_games(rows, season_name, current_name)


def _series_results(rows, series_post):
    """'W' when the row's team won that series, 'L' when it lost.

    NULL rather than 'L' where SeriesPost has no entry for the round, so a
    "never won a final" square never counts a series nobody recorded.
    """
    if series_post.empty:
        return [None] * len(rows)
    winners = {(int(y), str(r)): str(t) for y, r, t in zip(
        series_post["yearID"], series_post["round"],
        series_post["teamIDwinner"])}
    losers = {(int(y), str(r)): str(t) for y, r, t in zip(
        series_post["yearID"], series_post["round"],
        series_post["teamIDloser"])}

    def result(year, round_, team):
        key = (int(year), str(round_))
        if winners.get(key) == str(team):
            return "W"
        if losers.get(key) == str(team):
            return "L"
        return None

    return [result(y, r, t) for y, r, t in
            zip(rows["yearID"], rows["round"], rows["teamID"])]


def _finish_games(rows, season_name, current_name):
    """Name the columns the schema uses and resolve club identity."""
    pairs = list(zip(rows["yearID"].astype(int), rows["teamID"]))
    rows["club_hist"] = [season_name.get(p, p[1]) for p in pairs]
    rows["club_now"] = [current_name.get(p, season_name.get(p, p[1]))
                        for p in pairs]
    rows = rows.rename(columns={"playerID": "player_id", "yearID": "season"})
    for column in STAT_COLUMNS:
        if column not in rows:
            rows[column] = None
    return rows


def number_seasons(games, parks):
    """Add `career_game_no`, `date` and `venue`.

    `career_game_no` numbers a player's team-seasons in order, so 1 is the
    debut season and core.Generic.debut_club answers "debuted for this
    club". `date` is the season's start, which is all explore.py's
    chronological ordering needs at this grain.
    """
    games = games.sort_values(
        ["player_id", "season", "is_postseason", "club_hist"],
        kind="mergesort").reset_index(drop=True)
    games["career_game_no"] = games.groupby("player_id").cumcount() + 1
    games["date"] = games["season"].astype(int).astype(str) + "-04-01"
    keys = list(zip(games["season"].astype(int), games["teamID"]))
    games["venue"] = [parks.get(k) for k in keys]
    #: Lahman records no opponent at season grain, and inventing one would
    #: make "played against" answerable and wrong.
    games["opponent"] = None
    return games


# ---------------------------------------------------------------- players

def build_players(people, games, postseason_seasons):
    """One row per player, with the career summary the schema names.

    Careers are summarised over the regular season -- a career home-run
    count means the regular season, and that is what the reconciliation in
    health.py checks. But the player *list* comes from every row: a handful
    of players appear in BattingPost with no Appearances line at all, and
    summarising only the regular season left their postseason rows pointing
    at a player who did not exist.
    """
    regular = games[games["is_postseason"] == 0]
    career = regular.groupby("player_id").agg(
        debut_season=("season", "min"),
        final_season=("season", "max"),
        career_games=("games", "sum"),
        career_hits=("hits", "sum"),
        career_home_runs=("home_runs", "sum"),
    ).reset_index()

    career = _add_postseason_only_players(career, games)

    # Club history over every row, so a postseason-only player still has
    # one, and so n_clubs always equals the length of clubs_hist.
    clubs = (games.sort_values(["player_id", "season"], kind="mergesort")
             .groupby("player_id")["club_now"]
             .apply(lambda s: "|".join(dict.fromkeys(s)))
             .reset_index(name="clubs_hist"))
    clubs["n_clubs"] = clubs["clubs_hist"].str.count(r"\|") + 1
    career = career.merge(clubs, on="player_id", how="left")

    played_post = (games[games["is_postseason"] == 1]
                   .groupby("player_id")["games"].sum()
                   .reset_index(name="postseason_played"))
    career = career.merge(played_post, on="player_id", how="left")
    career["postseason_played"] = _postseason_or_null(career,
                                                      postseason_seasons)

    people = people.copy()
    people["player"] = (
        people["nameFirst"].fillna("").astype(str).str.strip() + " "
        + people["nameLast"].fillna("").astype(str).str.strip()).str.strip()
    people = people.rename(columns={"playerID": "player_id",
                                    "birthYear": "birth_year"})
    players = career.merge(people[["player_id", "player", "birth_year"]],
                           on="player_id", how="left")
    players["player"] = players["player"].replace("", pd.NA)\
        .fillna(players["player_id"])
    players["name_key"] = [names.normalise_name(n) for n in players["player"]]

    for column in ("career_games", "career_hits", "career_home_runs",
                   "n_clubs"):
        players[column] = players[column].fillna(0).astype(int)
    return players


def _add_postseason_only_players(career, games):
    """Rows for players who appear in `games` but never in the regular season.

    Lahman has a handful: a September call-up who only got into October, and
    several Negro League championship lines with no season record. Their
    regular-season totals are a real zero, and their debut and final seasons
    come from the only rows they have.
    """
    missing = set(games["player_id"]) - set(career["player_id"])
    if not missing:
        return career
    extra = (games[games["player_id"].isin(missing)]
             .groupby("player_id").agg(debut_season=("season", "min"),
                                       final_season=("season", "max"))
             .reset_index())
    for column in ("career_games", "career_hits", "career_home_runs"):
        extra[column] = 0
    return pd.concat([career, extra], ignore_index=True)


def _postseason_or_null(career, postseason_seasons):
    """0 where a postseason existed and the player missed it, NULL where
    none was played at all.

    Several of Lahman's early seasons have no postseason series, and a
    career wholly inside them has not "failed to reach October" -- there
    was no October. obscurity_model.MODEL drops the term for those players
    rather than scoring them maximally obscure for a record-keeping
    artefact.
    """
    out = []
    for played, debut, final in zip(career["postseason_played"],
                                    career["debut_season"],
                                    career["final_season"]):
        if pd.notna(played):
            out.append(int(played))
            continue
        span = range(int(debut), int(final) + 1)
        out.append(0 if any(s in postseason_seasons for s in span) else None)
    return pd.Series(out, index=career.index, dtype="object")


# ------------------------------------------------------------------ write

def write(db, players, games, awards, hall_of_fame, verbose):
    db = Path(db)
    db.parent.mkdir(parents=True, exist_ok=True)
    building = db.with_suffix(db.suffix + ".building")
    building.unlink(missing_ok=True)

    # `player` is denormalised onto games because core.require_schema names
    # it there: explore.py's game lists read a name without a join.
    games = games.merge(players[["player_id", "player"]], on="player_id",
                        how="left")
    game_columns = [
        "player_id", "player", "season", "date", "round", "club_hist",
        "club_now", "venue", "opponent", "career_game_no", "games",
        "is_postseason", "result", *STAT_COLUMNS,
    ]
    player_columns = ([
        "player_id", "player", "name_key", "birth_year", "debut_season",
        "final_season", "career_games", "career_hits", "career_home_runs",
        "postseason_played", "clubs_hist", "n_clubs",
    ] + [c for c in players.columns if c.endswith("_component")]
        + ["obscurity", "obscurity_confidence", "obscurity_model"])

    # Closed explicitly, not with `with sqlite3.connect(...)`: that context
    # manager commits the transaction but leaves the connection -- and on
    # Windows the file handle -- open, and the swap below then fails with
    # "used by another process".
    con = sqlite3.connect(building)
    try:
        players[player_columns].to_sql("players", con, index=False)
        games[game_columns].to_sql("games", con, index=False)
        if not awards.empty:
            awards.to_sql("awards", con, index=False)
        if not hall_of_fame.empty:
            hall_of_fame.to_sql("hall_of_fame", con, index=False)
        for statement in (
            "CREATE UNIQUE INDEX ix_players_id ON players(player_id)",
            "CREATE INDEX ix_players_name ON players(name_key)",
            "CREATE INDEX ix_players_obsc ON players(obscurity)",
            "CREATE INDEX ix_games_player ON games(player_id)",
            "CREATE INDEX ix_games_season ON games(season)",
            "CREATE INDEX ix_games_club_now ON games(club_now)",
            "CREATE INDEX ix_games_club_hist ON games(club_hist)",
            "CREATE INDEX ix_games_post ON games(is_postseason)",
        ):
            con.execute(statement)
        # Empty until mlb/load_retrosheet.py is run separately -- declared
        # here too so the rivalry builders have a table to query (zero rows
        # rather than "no such table") on a fresh build, and so
        # constraints_mlb.rivalry_available() can tell "not built" apart
        # from "built, not yet loaded".
        from . import load_retrosheet
        load_retrosheet._ensure_table(con)
        con.commit()
    finally:
        con.close()

    # Atomic swap, so a failed build never leaves a half-written database
    # where the app expects a whole one.
    db.unlink(missing_ok=True)
    building.replace(db)
    log(verbose, f"wrote {db}")


def load_rivalries(db, people, verbose, refresh=False):
    """Augment the finished database with Retrosheet's rivalry game log.

    Runs after write()'s atomic swap rather than inside it, because this is
    the one step that needs the network: a Retrosheet outage must not throw
    away a Lahman build that has already succeeded. On failure the table is
    left empty, which constraints_mlb.rivalry_available() reads as "not
    loaded" and which hides the rivalry squares -- the same state as a
    database built before this step existed.

    The crosswalk comes from the People frame the build already parsed,
    rather than load_retrosheet re-reading People.csv: `raw` may have been
    a ZIP, or a fixture folder that is not data/mlb/raw at all.
    """
    from . import load_retrosheet

    crosswalk = {str(retro): str(player)
                 for retro, player in zip(people["retroID"],
                                          people["playerID"])
                 if pd.notna(retro) and pd.notna(player)}
    con = sqlite3.connect(db)
    try:
        rows = load_retrosheet.load(con, refresh=refresh, crosswalk=crosswalk)
        log(verbose, f"rivalry games: {rows:,} rows")
    except Exception as error:                                  # noqa: BLE001
        # Never silent, even under --quiet: the build otherwise looks
        # complete while two squares are missing from the board.
        print(f"warning: Retrosheet rivalry load skipped ({error})",
              file=sys.stderr)
    finally:
        con.close()


def write_reference(db, teams, franchises, games, verbose):
    """The measured franchise list, lineage and stat eras sports.py reads.

    Written beside the database, not at a fixed path. For the canonical
    data/mlb/mlb.db that resolves to data/mlb/reference/mlb_reference.json,
    which is exactly where mlb_reference.PATH looks -- and a build into a
    temporary database writes its reference next to itself instead of
    overwriting the real one with a four-player fixture's two franchises.
    build_nba_db.write_reference has the same rule for the same reason.
    """
    active = franchises[franchises["active"].astype(str).str.upper() == "Y"]
    current = sorted({str(n) for n in active["franchName"]})

    eras = {}
    for stat in STAT_COLUMNS:
        column = pd.to_numeric(games[stat], errors="coerce")
        seasons = games.loc[column.fillna(0) > 0, "season"]
        if len(seasons):
            eras[stat] = int(seasons.min())

    path = Path(db).resolve().parent / "reference" / "mlb_reference.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "teams": current,
        "club_lineage": lineage(teams, franchises),
        "venue_aliases": {},
        "stat_eras": eras,
    }, indent=2, sort_keys=True), encoding="utf-8")
    log(verbose, f"wrote {path} ({len(current)} franchises)")


# ------------------------------------------------------------------ build

def build(db=None, raw=None, verbose=True, retrosheet=True,
          refresh_retrosheet=False):
    db = Path(db or data_paths.default_db("mlb"))
    source, kind = find_source(raw or data_paths.raw_dir("mlb"))
    log(verbose, f"reading Lahman data from {source}")

    people = read_csv(source, kind, "People.csv")
    appearances = read_csv(source, kind, "Appearances.csv")
    batting = read_csv(source, kind, "Batting.csv")
    pitching = read_csv(source, kind, "Pitching.csv")
    teams = read_csv(source, kind, "Teams.csv")
    franchises = read_csv(source, kind, "TeamsFranchises.csv")
    batting_post = read_csv(source, kind, "BattingPost.csv", required=False)
    pitching_post = read_csv(source, kind, "PitchingPost.csv", required=False)
    series_post = read_csv(source, kind, "SeriesPost.csv", required=False)
    awards = read_csv(source, kind, "AwardsPlayers.csv", required=False)
    hall_of_fame = read_csv(source, kind, "HallOfFame.csv", required=False)

    season_name, current_name = franchise_map(teams, franchises)
    parks = {(int(y), t): (None if pd.isna(p) else str(p))
             for y, t, p in zip(teams["yearID"], teams["teamID"],
                                teams["park"])}

    log(verbose, "regular season...")
    games = regular_season_games(appearances, batting, pitching,
                                 season_name, current_name)
    log(verbose, "postseason...")
    post = postseason_games(batting_post, pitching_post, series_post,
                            season_name, current_name)
    if len(post):
        games = pd.concat([games, post], ignore_index=True)
    games = number_seasons(games, parks)

    postseason_seasons = ({int(y) for y in series_post["yearID"]}
                          if not series_post.empty else set())

    log(verbose, "career summaries...")
    players = build_players(people, games, postseason_seasons)

    log(verbose, f"obscurity (model v{obscurity_model.MODEL.version})...")
    for column, values in obscurity.components(
            players, obscurity_model.MODEL).items():
        players[column] = values

    if not awards.empty:
        awards = awards.rename(columns={"playerID": "player_id",
                                        "awardID": "award",
                                        "yearID": "season"})
    if not hall_of_fame.empty:
        hall_of_fame = hall_of_fame.rename(
            columns={"playerID": "player_id", "yearid": "season",
                     "yearID": "season"})

    write(db, players, games, awards, hall_of_fame, verbose)
    write_reference(db, teams, franchises, games, verbose)
    if retrosheet:
        log(verbose, "rivalry game logs (Retrosheet)...")
        load_rivalries(db, people, verbose, refresh=refresh_retrosheet)
    log(verbose, f"{len(players):,} players, {len(games):,} player-seasons "
                 f"({int(games['is_postseason'].sum()):,} postseason)")
    return db


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build data/mlb/mlb.db from the Lahman CSV export.")
    parser.add_argument("--db", default=None,
                        help="output database (default data/mlb/mlb.db)")
    parser.add_argument("--raw", default=None,
                        help="Lahman CSV folder or ZIP (default data/mlb/raw)")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--no-retrosheet", action="store_true",
                        help="skip the rivalry game-log step (no network)")
    parser.add_argument("--refresh-retrosheet", action="store_true",
                        help="re-download the Retrosheet logs even if cached")
    args = parser.parse_args(argv)
    build(db=args.db, raw=args.raw, verbose=not args.quiet,
          retrosheet=not args.no_retrosheet,
          refresh_retrosheet=args.refresh_retrosheet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
