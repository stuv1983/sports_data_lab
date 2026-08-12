#!/usr/bin/env python3
"""
utils/nba/load_current_season.py -- Add the open NBA season(s) to a built
database, without ever rebuilding it from scratch.

    python -m utils.nba.load_current_season
    python -m utils.nba.load_current_season --season 2025
    python -m utils.nba.load_current_season --seasons 2024,2025 --refresh
    python -m utils.nba.load_current_season --dry-run

WHY THIS EXISTS
---------------
`nba/build_nba_db.py --source live` can build a full history, but NBA.com's
own game log reports the *same* numeric team_id for a franchise across its
entire history -- it has no notion that the Thunder were the SuperSonics in
1985. A full rebuild from `--source live` therefore cannot produce an
historically-accurate `club_hist` for any relocated or renamed franchise:
every game, however old, gets stamped with the modern name. (See
`nba/nba_source_api.py`'s `teams()` docstring, and the incident that
prompted this module: a live-sourced rebuild silently dropped 126,000 games
and every Seattle SuperSonics record.)

The database's full history has to come from `--source csv` (or a real
scrape), built *once*. This module is the incremental half of that split --
the same role `utils/mlb/load_statsapi.py` plays for MLB, which this
mirrors closely. It appends/replaces only the season(s) still in progress,
using NBA.com's live game log, and never touches an already-loaded season.
Since a franchise's *current* season is definitionally played under its
*current* name, this sidesteps the source's historical-identity limitation
entirely rather than working around it -- club_hist and club_now are always
the same value here, by construction.

WHAT IT TOUCHES
----------------
`games` / `matches`   the target season's rows are replaced wholesale, so a
                      re-run mid-season corrects yesterday's totals instead
                      of duplicating them. Every other season is untouched.
`players`             a row is inserted for anyone new, matched against the
                      existing table by source_player_id -- NBA.com's own
                      person id, shared by every source (see
                      nba/nba_source_live.py's docstring). Career totals
                      and obscurity are recomputed for everyone, from
                      `games`, the same as a full build does.
`player_seasons`, `team_seasons`, `player_team_history`
                      fully recomputed via nba.build_nba_db._write_derived,
                      the same function a full build uses.

NOT TOUCHED: `franchises`, `teams`, `team_aliases` -- nothing about them can
change from an in-season update -- and data/nba/reference/nba_reference.json,
matching load_statsapi.py's treatment of mlb_reference.json.
database_updates.py refreshes that file after promotion, as a safety net
for the rare deliberate full rebuild.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import names  # noqa: E402
import obscurity  # noqa: E402
from data_paths import default_db  # noqa: E402
from nba import nba_playoff_rounds, nba_source, obscurity_model  # noqa: E402
from nba.build_nba_db import (  # noqa: E402
    INDEXES, STATS, _deduplicate, _int, _reconcile_matches, _text,
    _validate_match_teams, _validate_player_game_teams, _write_derived,
    issue, season_label,
)


def normalise_match_id(value) -> str:
    """NBA.com's 10-character game id in the form this database stores.

    The live endpoints return a zero-padded id ("0022500001"); the CSV
    source the history was built from stores the same value unpadded
    ("22500001"), and all 71,396 existing matches follow that convention
    without exception. The padding is the only difference -- character 3 is
    the season-type digit (2 regular, 4 playoffs, 5 play-in) and is never
    itself zero, so dropping the leading "00" is lossless and cannot
    collide.

    Without this the two id spaces do not intersect at all, and an
    incremental load would silently re-key the seasons it touches away from
    every other season in the database.
    """
    text = str(value).strip()
    if len(text) == 10 and text.startswith("00"):
        return text[2:]
    return text


def open_seasons(today: dt.date | None = None) -> list[int]:
    """The NBA seasons a load still has to ask NBA.com about.

    A season starting in year N is played into N+1, so through the first
    half of a calendar year the season in progress started last year. Both
    are named: the previous one can still gain late playoff games, and
    naming one season too many costs a handful of requests where naming
    one too few silently misses games.

    Duplicated from database_updates.py's private _nba_open_seasons()
    rather than imported, so this module has no dependency on the update
    orchestrator -- the same way load_statsapi.current_season() is
    self-contained.
    """
    today = today or dt.date.today()
    started = today.year if today.month >= 9 else today.year - 1
    return [started - 1, started]


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


def _existing_team_identity(con) -> dict:
    """franchise_id -> current name, from the database already built.

    Keyed by franchise_id rather than teams.team_id: a CSV/scraped build's
    team_id encodes an era ("1610612760-1967" for the Seattle-era Thunder
    row), which nothing NBA.com's live game log ever reports. franchise_id
    is NBA.com's own bare numeric franchise id instead, the same one every
    source uses (confirmed against the live nba.db this session: the
    Thunder's franchise_id is "1610612760", matching nba_api's own team
    id) -- so it is what a live-sourced team_id actually joins to.

    club_hist is not derived here at all: the season this module ever
    writes is always the current one, so club_hist and club_now are the
    same value by construction -- there is no relocation to represent.
    """
    return dict(con.execute(
        "SELECT franchise_id, current_name FROM franchises"))


def _existing_players(con):
    """The full players table, as the biography frame build() calls `people`."""
    import pandas as pd
    return pd.read_sql_query(
        "SELECT player_id, source_player_id, player, birth_year, "
        "birth_country, position, height_cm, weight_kg FROM players", con)


def _fetch_season(source, season):
    """Raw matches and player-games for one season, straight from the source.

    Kept separate from validation/reconciliation so every season can be
    fetched once, up front, before any player is matched to an id -- see
    load()'s docstring note on why player discovery has to happen before
    assembly, not per season.
    """
    import pandas as pd

    matches = source.matches(season)
    game_frames = []
    # fetch_phases, not PHASES, so the play-in tournament is asked for --
    # NBA.com serves it as a separate season type that a "Playoffs" fetch
    # omits entirely. See nba_source_api.FETCH_PHASES. Sources that do not
    # draw that distinction fall back to the phases they do store.
    phases = getattr(source, "fetch_phases", None) \
        or {phase: phase for phase in nba_source.PHASES}
    for fetch_phase, phase in phases.items():
        frame = source.player_games(season, fetch_phase)
        if frame is None or frame.empty:
            continue
        frame = frame.copy()
        frame["phase"] = phase
        game_frames.append(frame)
    games = pd.concat(game_frames, ignore_index=True) if game_frames else None
    if games is not None:
        games["source_player_id"] = games["source_player_id"].astype(str)
        games["match_id"] = games["match_id"].map(normalise_match_id)
        games["team_id"] = games["team_id"].astype(str)
    if matches is not None and not matches.empty:
        matches = matches.copy()
        matches["match_id"] = matches["match_id"].map(normalise_match_id)
    return matches, games


def _merge_new_players(existing, source, incoming_ids, verbose):
    """Existing biography rows plus any genuinely new ones.

    Mirrors build()'s two-tier lookup: source.players() first, then
    source.discovered_players() for anyone the static roster still does not
    name -- the same fallback for a player NBA.com's static index omits but
    whose own game log carries their id and name.

    Returns (people, pid_of, new_count). Existing player_id values are
    never reassigned; a new one gets the next free id, in source_player_id
    order for determinism across re-runs.
    """
    import pandas as pd

    existing = existing.copy()
    existing["source_player_id"] = existing["source_player_id"].astype(str)
    known = set(existing["source_player_id"])
    missing = {str(i) for i in incoming_ids} - known
    if not missing:
        pid_of = dict(zip(existing["source_player_id"], existing["player_id"]))
        return existing, pid_of, 0

    people = source.players().copy()
    people["source_player_id"] = people["source_player_id"].astype(str)
    found = people[people["source_player_id"].isin(missing)]
    still_missing = missing - set(found["source_player_id"])
    discover = getattr(source, "discovered_players", None)
    if still_missing and callable(discover):
        discovered = discover(still_missing)
        if discovered is not None and not discovered.empty:
            discovered = discovered.copy()
            discovered["source_player_id"] = discovered[
                "source_player_id"].astype(str)
            discovered = discovered[
                discovered["source_player_id"].isin(still_missing)]
            found = pd.concat([found, discovered], ignore_index=True)
    found = (found.drop_duplicates(subset=["source_player_id"])
                  .sort_values("source_player_id", kind="stable")
                  .reset_index(drop=True))

    if found.empty:
        if verbose:
            print(f"  {len(missing):,} player id(s) named by games could "
                  f"not be identified by any source; their rows will be "
                  f"dropped")
        pid_of = dict(zip(existing["source_player_id"], existing["player_id"]))
        return existing, pid_of, 0

    next_id = int(existing["player_id"].max()) + 1 if len(existing) else 1
    found["player_id"] = range(next_id, next_id + len(found))
    people_new = pd.concat(
        [existing, found[["player_id", "source_player_id", "player",
                          "birth_year", "birth_country", "position",
                          "height_cm", "weight_kg"]]],
        ignore_index=True)
    pid_of = dict(zip(people_new["source_player_id"], people_new["player_id"]))
    if verbose:
        print(f"  {len(found):,} new player(s): "
              f"{', '.join(found['player'].head(5))}"
              f"{', ...' if len(found) > 5 else ''}")
    return people_new, pid_of, len(found)


def _assemble_season(season, matches, games, team_now, pid_of, name_of,
                     rounds, issues, verbose, source_key):
    """(matches, out) for one season, validated and reconciled like build().

    Takes already-fetched frames rather than a source, so it never needs to
    ask twice: `matches`/`games` come from _fetch_season(), and pid_of/
    name_of already reflect every player discovered across every requested
    season, not just this one.

    Reuses nba.build_nba_db's own validation/reconciliation functions --
    the home/away/neutral-site orientation logic in particular was hard-won
    (see test_a_neutral_site_game_... in tests/test_nba_live_source.py) and
    re-deriving it here would risk quietly regressing what those fixes cover.
    """
    if matches is None or matches.empty:
        issue(issues, "missing_season", f"no matches for {season_label(season)}",
              season=season, source_key=source_key)
        return None, None
    matches = matches.copy()
    matches["match_id"] = matches["match_id"].astype(str)
    for column in ("home_team_id", "away_team_id"):
        matches[column] = matches[column].map(_text)

    matches = _reconcile_matches(matches, issues, source_key, verbose)
    matches = _validate_match_teams(matches, team_now, issues, source_key,
                                    verbose)
    if matches.empty:
        return None, None

    matches["home_team"] = matches["home_team_id"].map(team_now)
    matches["away_team"] = matches["away_team_id"].map(team_now)
    # club_hist == club_now throughout this module -- see
    # _existing_team_identity -- so the playoff-round join's "historical
    # name" side is just the current one.
    matches["home_hist"] = matches["home_team"]
    matches["away_hist"] = matches["away_team"]

    # Playoff rounds, from the same pinned reference build() reads -- see
    # nba_playoff_rounds.py. A season still in progress commonly has no
    # entry there yet; assign() leaves round unset rather than guessing,
    # and utils/nba/load_nba_playoff_series.py backfills it once the
    # reference is updated. This loader is not strict, so an unresolved
    # round is not fatal here the way it is inside build().
    if rounds:
        nba_playoff_rounds.assign(
            matches, rounds,
            lambda kind, detail, severity="warn", season=None: issue(
                issues, kind, detail, severity=severity, season=season,
                source_key=source_key),
            verbose_log=(lambda text: print(text) if verbose else None))

    if games is None or games.empty:
        return matches, None
    out = games.copy()

    by_match = matches.set_index("match_id")
    out = out[out["match_id"].isin(by_match.index)].reset_index(drop=True)
    if out.empty:
        return matches, None
    out = _validate_player_game_teams(out, by_match, team_now, issues,
                                      source_key, verbose)
    if out.empty:
        return matches, None

    known = out["source_player_id"].isin(pid_of)
    if not known.all():
        issue(issues, "unknown_player",
              f"{int((~known).sum()):,} player-game row(s) reference a "
              f"player not identifiable by any source; dropped",
              severity="error", source_key=source_key)
        out = out.loc[known].reset_index(drop=True)
    if out.empty:
        return matches, None

    out["player_id"] = out["source_player_id"].map(pid_of)
    out["player"] = out["player_id"].map(name_of)
    out["club_now"] = out["team_id"].map(team_now)
    out["club_hist"] = out["club_now"]

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

    decided = out["points_for"].notna() & out["points_against"].notna()
    out["result"] = None
    out.loc[decided & (out["points_for"] > out["points_against"]),
            "result"] = "W"
    out.loc[decided & (out["points_for"] < out["points_against"]),
            "result"] = "L"

    out["round"] = out["round"].astype("object")
    regular = out["is_playoff"] == 0
    order = (out.loc[regular]
                .sort_values(["date", "match_id"], kind="stable")
                .groupby(["team_id", "season"]).cumcount() + 1)
    out.loc[regular, "round"] = order.astype(str)
    out["round"] = out["round"].fillna("")

    # A season-local career_game_no, good enough for _deduplicate's
    # candidate comparison below -- the global sequence is recomputed by
    # SQL over the whole games table once every season is merged in.
    out = out.sort_values(["player_id", "date", "match_id"], kind="stable")
    out["career_game_no"] = out.groupby("player_id").cumcount() + 1
    out = out.reset_index(drop=True)

    out, _unresolved = _deduplicate(out, issues, source_key, strict=False,
                                    verbose=verbose)
    return matches, out


GAME_COLUMNS = ["player_id", "player", "match_id", "season", "season_label",
                "date", "round", "club_now", "club_hist", "opponent", "venue",
                "is_home", "career_game_no", "is_playoff", "result",
                "points_for", "points_against", *STATS]


def _carry_forward_dnps(con, season, out):
    """Keep roster rows the live game log structurally cannot report.

    NBA.com's playergamelogs endpoint returns a row only for players who
    actually *appeared*. The database's own history, built from the CSV
    source, additionally carries a zero-minute row for every player who was
    active but did not play -- about a fifth of every season since 2005. A
    plain replace would therefore silently strip ~7,000 roster rows per
    season and leave the reloaded seasons counting "games" differently from
    every season around them.

    So: for matches this load is re-fetching anyway, any existing DNP row
    whose player the live feed did not report is carried across unchanged.
    Rows for matches the fetch did not return are not resurrected -- a game
    dropped from the schedule should stay dropped -- and a player the feed
    *does* report always wins, since that is the fresher record.
    """
    import pandas as pd

    if out is None or out.empty:
        return out
    fetched_matches = set(out["match_id"])
    fetched_keys = set(zip(out["player_id"], out["match_id"]))

    existing = pd.read_sql_query(
        f"SELECT {', '.join(GAME_COLUMNS)} FROM games "
        "WHERE season=? AND (minutes IS NULL OR minutes=0)",
        con, params=(season,))
    if existing.empty:
        return out

    keep = existing[
        existing["match_id"].isin(fetched_matches)
        & ~pd.Series(list(zip(existing["player_id"], existing["match_id"])),
                     index=existing.index).isin(fetched_keys)
    ]
    if keep.empty:
        return out
    # reindex, not [out.columns]: `out` also carries the join columns
    # (team_id, phase) that games itself does not store. They are not
    # inserted, so leaving them blank on a carried row is harmless.
    return pd.concat([out, keep.reindex(columns=out.columns)],
                     ignore_index=True)


def _replace_season(con, season, matches, out):
    """Swap one season's games and matches for a freshly fetched set.

    Replacing rather than appending is what makes a mid-season re-run
    safe: yesterday's totals are superseded, not added to. Mirrors
    load_statsapi.replace_season().
    """
    removed_games = con.execute(
        "SELECT COUNT(*) FROM games WHERE season=?", (season,)).fetchone()[0]
    out = _carry_forward_dnps(con, season, out)
    con.execute("DELETE FROM games WHERE season=?", (season,))
    con.execute("DELETE FROM matches WHERE season=?", (season,))

    if matches is not None and not matches.empty:
        con.executemany(
            "INSERT OR REPLACE INTO matches (match_id, season, season_label, "
            "date, phase, round, home_team_id, away_team_id, home_team, "
            "away_team, home_score, away_score, venue, attendance) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r.match_id, _int(r.season), r.season_label, r.date, r.phase,
              r.round, r.home_team_id, r.away_team_id, r.home_team,
              r.away_team, _int(r.home_score), _int(r.away_score), r.venue,
              _int(r.attendance)) for r in matches.itertuples()])

    inserted = 0
    if out is not None and not out.empty:
        game_columns = GAME_COLUMNS
        marks = ", ".join("?" * len(game_columns))
        con.executemany(
            f"INSERT INTO games ({', '.join(game_columns)}) "
            f"VALUES ({marks})",
            [tuple(getattr(r, c) for c in game_columns)
             for r in out.itertuples()])
        inserted = len(out)
    return {"season": season, "removed_games": removed_games,
            "inserted_games": inserted,
            "matches": 0 if matches is None else len(matches)}


def _recompute_career_game_no(con):
    """1 for a player's first game *in the whole games table*, renumbered
    globally so a merge never leaves gaps or a stale ordering behind."""
    con.execute("DROP TABLE IF EXISTS temp._career_game_no")
    con.execute("""CREATE TEMP TABLE _career_game_no AS
                   SELECT rowid AS rid,
                          ROW_NUMBER() OVER (PARTITION BY player_id
                                             ORDER BY date, match_id) AS n
                   FROM games""")
    con.execute("CREATE INDEX temp.ix_lcn ON _career_game_no(rid)")
    con.execute("""UPDATE games SET career_game_no =
                   (SELECT n FROM _career_game_no WHERE rid = games.rowid)""")
    con.execute("DROP TABLE temp._career_game_no")


def _recompute_players(con, people, verbose):
    """Career totals and obscurity, for the whole players table.

    Same shape as build()'s "7. Careers" / "8. Obscurity" steps, run over
    the reloaded, now-merged games table -- obscurity is population-
    relative, so it has to be recomputed for everyone, not just the players
    a season touched, the same reasoning utils/mlb/load_statsapi.py's
    recompute_careers() gives for doing a full-table recompute every time.

    Returns the reloaded games frame, for _write_derived below.
    """
    import pandas as pd

    out = pd.read_sql_query("SELECT * FROM games", con)
    grouped = out.groupby("player_id")
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

    people = people.copy()
    people["name_key"] = people["player"].map(names.normalise_name)
    players = people.set_index("player_id").join(careers, how="inner")
    players.index.name = "player_id"
    players = players.reset_index()

    if verbose:
        print(f"  obscurity (model v{obscurity_model.MODEL.version})...")
    for column, values in obscurity.components(
            players, obscurity_model.MODEL).items():
        players[column] = values

    player_columns = [
        "player_id", "source_player_id", "player", "name_key",
        "birth_year", "birth_country", "position", "height_cm",
        "weight_kg",
        "debut_season", "final_season", "career_games", "career_points",
        "career_minutes", "playoffs_played", "career_rebounds",
        "career_assists", "career_steals", "career_blocks",
        "n_clubs", "clubs_hist", "clubs_now",
        *[c for c in players.columns if c.endswith("_component")],
        "obscurity", "obscurity_confidence", "obscurity_model"]
    players.loc[:, player_columns].to_sql(
        "players", con, index=False, if_exists="replace")
    return out


def _restore_join_columns(con, out):
    """Add `phase` and `team_id` back to a reloaded games frame.

    Neither is a column games.py actually persists -- phase is redundant
    with is_playoff and build() never writes both, and team_id was never
    written at all, because club_hist already carries the same per-era
    identity as text. _write_derived (reused unmodified from
    nba.build_nba_db, below) needs both, so they are rebuilt here: phase
    from is_playoff directly, and team_id via a join back through
    teams/franchises on (club_now, club_hist) -- which round-trips exactly,
    since (a franchise's current name, one of its team rows' own name) is
    unique by construction (franchises.current_name is UNIQUE, and a
    single franchise's team names are never repeated -- see build()'s
    defunct-franchise disambiguation).
    """
    import pandas as pd

    out = out.copy()
    out["phase"] = out["is_playoff"].map({1: "playoff", 0: "regular"})
    identity = pd.read_sql_query(
        "SELECT f.current_name AS club_now, t.name AS club_hist, t.team_id "
        "FROM teams t JOIN franchises f ON f.franchise_id = t.franchise_id",
        con)
    return out.merge(identity, on=["club_now", "club_hist"], how="left")


#: A fetched season may not shrink the stored one below this fraction of
#: its matches (or its player-game rows). Mid-season a re-fetch always
#: returns at least what it returned yesterday, so a genuine snapshot only
#: ever grows; a response half the stored size means the upstream dropped
#: games, not that they un-happened.
MIN_MATCH_RATIO = 0.5
MIN_GAME_RATIO = 0.5

#: The shrink guards only engage once the stored season is at least this
#: big. Below them, a ratio is noise -- reloading a two-game synthetic or
#: very-early season with one corrected game is routine -- while a real
#: mid-season NBA store passes both within the first fortnight.
MIN_GUARDED_MATCHES = 50
MIN_GUARDED_GAMES = 500


def validate_snapshot(con, season, matches, out) -> tuple[str, str]:
    """Decide whether a fetched season may replace the stored one.

    Returns (verdict, reason): "ok" to proceed, "skip" for the harmless
    nothing-fetched-nothing-stored case, "reject" when stored data had to
    be protected from an empty or severely partial upstream response
    (NBA.com outage, schema drift, a source silently returning nothing).
    _replace_season deletes before it inserts, so this gate runs first --
    and it runs *before* the DNP carry-forward, which only ever adds rows,
    so the fetched counts compared here understate what would land: a
    rejection is conservative, never optimistic.
    """
    stored_matches = con.execute(
        "SELECT COUNT(*) FROM matches WHERE season=?",
        (season,)).fetchone()[0]
    stored_games = con.execute(
        "SELECT COUNT(*) FROM games WHERE season=?", (season,)).fetchone()[0]
    fetched_matches = 0 if matches is None else len(matches)
    fetched_games = 0 if out is None else len(out)

    if fetched_matches == 0:
        if stored_matches or stored_games:
            return "reject", (
                f"upstream returned no matches for {season_label(season)} "
                f"while {stored_matches:,} matches / {stored_games:,} games "
                f"are stored -- season left untouched")
        return "skip", (f"nothing fetched for {season_label(season)} and "
                        f"nothing stored -- skipped")
    if stored_games and fetched_games == 0:
        return "reject", (
            f"upstream returned matches but no player-games for "
            f"{season_label(season)} while {stored_games:,} games are "
            f"stored -- season left untouched")
    if (stored_matches >= MIN_GUARDED_MATCHES
            and fetched_matches < stored_matches * MIN_MATCH_RATIO):
        return "reject", (
            f"upstream returned {fetched_matches:,} matches for "
            f"{season_label(season)} where {stored_matches:,} are stored "
            f"(below the {MIN_MATCH_RATIO:.0%} floor) -- season left "
            f"untouched")
    # A complete-looking schedule with a gutted game log is the sneakier
    # failure: matches pass their ratio while the player-game fetch came
    # back one page short of everything. Guard the two independently.
    if (stored_games >= MIN_GUARDED_GAMES
            and fetched_games < stored_games * MIN_GAME_RATIO):
        return "reject", (
            f"upstream returned {fetched_games:,} player-games for "
            f"{season_label(season)} where {stored_games:,} are stored "
            f"(below the {MIN_GAME_RATIO:.0%} floor) -- season left "
            f"untouched")
    return "ok", ""


#: A lock older than this is presumed abandoned (a crashed run cannot
#: release it). Far beyond any real load, which takes minutes.
LOCK_STALE_SECONDS = 6 * 3600


def _acquire_update_lock(live: Path) -> Path:
    """One updater per live database, enforced with an O_EXCL lock file.

    database_updates.py holds its own lock around the whole orchestration,
    but this module is also a documented standalone CLI -- two direct
    invocations must not race each other through copy/verify/promote. The
    lock lives beside the database so it guards the file, not the process.
    """
    lock = live.with_suffix(live.suffix + ".update-lock")
    payload = f"{os.getpid()} {dt.datetime.now().astimezone().isoformat()}\n"
    for attempt in (1, 2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as handle:
                handle.write(payload)
            return lock
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                continue          # released between the open and the stat
            if attempt == 1 and age > LOCK_STALE_SECONDS:
                lock.unlink(missing_ok=True)   # abandoned by a dead run
                continue
            raise RuntimeError(
                f"another update holds {lock.name} "
                f"(age {age:.0f}s); not starting a second one") from None
    raise RuntimeError(f"could not acquire {lock.name}")


def update_atomically(db_path, work, verbose=True):
    """Run `work(staging_path)` against a copy, verify it, then swap it in.

    The live database is never written: `work` gets a private staging copy
    (suffixed with this pid, so two runs can never scribble on the same
    file), the copy must pass PRAGMA integrity_check, and only then does
    os.replace -- atomic on POSIX and Windows alike -- put it where the
    live file was. An exclusive lock file beside the database serialises
    whole runs, so promote cannot race promote. A crash or a failed check
    at any point leaves the live database exactly as it started.

    `work` returns (outcome, changed); when nothing changed the staging
    copy is simply discarded and the live file is not touched at all.
    """
    live = Path(db_path)
    lock = _acquire_update_lock(live)
    staging = live.with_suffix(live.suffix + f".{os.getpid()}.updating")
    try:
        staging.unlink(missing_ok=True)
        shutil.copy2(live, staging)
        outcome, changed = work(staging)
        if not changed:
            return outcome
        with closing(sqlite3.connect(staging)) as check:
            verdict = check.execute("PRAGMA integrity_check").fetchone()[0]
        if verdict != "ok":
            raise RuntimeError(
                f"staging database failed integrity_check: {verdict}")
        if verbose:
            print(f"promoting {staging.name} -> {live.name}")
        os.replace(staging, live)
        return outcome
    finally:
        staging.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def _load_into(con, db_dir, source, seasons, verbose, dry_run) -> tuple[dict, bool]:
    """The load itself, against whatever database `con` holds.

    Returns (result, changed): changed is False when every season was
    skipped by validate_snapshot, so the caller knows the database needs
    no promotion.
    """
    result = {"seasons": [], "new_players": 0, "dry_run": dry_run}
    team_now = _existing_team_identity(con)
    existing_people = _existing_players(con)
    rounds = nba_playoff_rounds.load(db_dir)

    # Fetched up front, across every requested season, before any
    # player is matched to an id: two open seasons can share a debut
    # (a rookie's first games span the season boundary), and assembling
    # season-by-season against a pid_of that only grows afterwards
    # would drop the second season's rows for exactly that player.
    raw = {season: _fetch_season(source, season) for season in seasons}
    all_incoming_ids = set()
    for _matches, games in raw.values():
        if games is not None:
            all_incoming_ids |= set(games["source_player_id"])

    people, pid_of, new_players = _merge_new_players(
        existing_people, source, all_incoming_ids, verbose)
    result["new_players"] = new_players
    name_of = dict(zip(people["player_id"], people["player"]))

    issues = []
    assembled = {}
    for season, (matches, games) in raw.items():
        if verbose:
            print(f"season {season}...")
        m, out = _assemble_season(
            season, matches, games, team_now, pid_of, name_of, rounds,
            issues, verbose, source.key)
        assembled[season] = (m, out)

    if dry_run:
        for season, (matches, out) in assembled.items():
            # counted through the same carry-forward a real run applies,
            # so the reported figure is the one that would land
            merged = _carry_forward_dnps(con, season, out)
            result["seasons"].append({
                "season": season,
                "would_insert_games": 0 if merged is None else len(merged),
                "would_insert_matches": 0 if matches is None else len(matches),
            })
        return result, False

    changed = False
    rejected = 0
    for season, (matches, out) in assembled.items():
        # The gate between fetching and deleting: a season the upstream
        # answered badly is reported and rejected, never wiped. A harmless
        # skip (nothing fetched, nothing stored) is kept distinct so the
        # caller can tell "protected stored data" from "nothing to do".
        verdict, reason = validate_snapshot(con, season, matches, out)
        if verdict != "ok":
            if verbose:
                print(f"  ! {reason}")
            if verdict == "reject":
                issue(issues, "rejected_snapshot", reason, severity="error",
                      season=season, source_key=source.key)
                rejected += 1
                result["seasons"].append({"season": season,
                                          "rejected": reason})
            else:
                result["seasons"].append({"season": season,
                                          "skipped": reason})
            continue
        outcome = _replace_season(con, season, matches, out)
        result["seasons"].append(outcome)
        changed = True
    result["rejected_seasons"] = rejected

    if not changed:
        return result, False

    if verbose:
        print("renumbering career_game_no...")
    _recompute_career_game_no(con)
    if verbose:
        print("recomputing career totals and obscurity...")
    full_out = _recompute_players(con, people, verbose)
    # _write_derived announces itself ("derived tables...") already.
    _write_derived(con, _restore_join_columns(con, full_out), verbose)
    # players and player_seasons are rewritten wholesale above (pandas
    # to_sql replaces the table, which takes its indexes with it), so
    # the build's own index set is re-applied. IF NOT EXISTS throughout,
    # so this only ever restores what the rewrite dropped.
    for statement in INDEXES:
        con.execute(statement)
    con.commit()
    return result, True


def load(db_path, seasons=None, refresh=False, verbose=True,
        dry_run=False) -> dict:
    seasons = seasons or open_seasons()
    source = nba_source.get_source("live", refresh=True)
    db_dir = Path(db_path).resolve().parent

    if dry_run:
        # Nothing is written, so the live file can safely be read directly.
        with closing(sqlite3.connect(db_path)) as con:
            result, _changed = _load_into(
                con, db_dir, source, seasons, verbose, dry_run=True)
        result["database"] = str(db_path)
        return result

    def work(staging):
        with closing(sqlite3.connect(staging)) as con:
            return _load_into(con, db_dir, source, seasons, verbose,
                              dry_run=False)

    result = update_atomically(db_path, work, verbose=verbose)
    result["database"] = str(db_path)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--db", default=None)
    parser.add_argument("--season", type=int, default=None)
    parser.add_argument("--seasons", default=None,
                        help="e.g. 2024,2025 or 2024-2026")
    parser.add_argument("--refresh", action="store_true",
                        help="accepted for CLI parity with load_statsapi.py; "
                             "this loader always re-requests the seasons it "
                             "is given, since it is only ever asked about "
                             "seasons that can still change")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", dest="verbose", action="store_false")
    args = parser.parse_args(argv)

    db_path = args.db or default_db("nba")
    if not Path(db_path).exists():
        print(f"No NBA database at {db_path}. Build it first with "
              f"`python -m nba.build_nba_db --source csv`.", file=sys.stderr)
        return 1
    if args.seasons:
        seasons = parse_seasons(args.seasons)
    elif args.season:
        seasons = [args.season]
    else:
        seasons = open_seasons()

    try:
        result = load(db_path, seasons, refresh=args.refresh,
                      verbose=args.verbose, dry_run=args.dry_run)
    except (nba_source.SourceError, sqlite3.Error, RuntimeError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    for season in result["seasons"]:
        print(f"  {season}")
    if not args.dry_run:
        print(f"new players: {result['new_players']}")
    if result.get("rejected_seasons"):
        # A season with stored data was refused an empty/partial snapshot.
        # The store is safe, but the update did not happen -- exiting 0
        # would let an orchestrator record the step as a success.
        print(f"{result['rejected_seasons']} season(s) rejected a bad "
              f"upstream snapshot; stored data retained", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
