"""
mlb/statsapi_source.py -- Current MLB seasons from MLB's Stats API.

    python -m utils.mlb.load_statsapi --season 2026

WHAT THIS IS FOR
----------------
The MLB database was built once from the Lahman CSV export and is not
rebuilt from it again. Lahman publishes a season only after it finishes,
so between April and November the database is a season behind and nothing
in the update pipeline could close the gap.

This reads the season in progress from MLB's own Stats API and appends it
to the database that already exists. It never opens a Lahman CSV: the
identifiers it needs come from the database's own `players` table, and the
career totals it rewrites are recomputed from the `games` table.

THE GRAIN IS NOT NEGOTIABLE
---------------------------
`games` holds one row per player per season per team, with the `games`
column carrying how many appearances that season was worth -- it is what
Lahman's finest grain supports, and `career_games` is a SUM over it.

The Stats API could give a row per game, and mixing those in would be a
silent catastrophe: every career total would count a season's games as a
season. So the per-team season splits endpoint is used, which is the same
grain, and it is also 60 requests a season rather than 2,430.

Traded players are why the per-team split matters. Querying the season
without a team returns one collapsed row per player; querying team by team
returns Jonah Heim's 57 games for the Athletics and 12 for Atlanta
separately, which is what the existing rows look like.

IDENTIFIERS
-----------
The Stats API keys players by MLBAM id, the database by Lahman playerID,
and no column in either connects them. The Chadwick Bureau register does:
its `key_mlbam` -> `key_bbref` pairs resolve to a `players.player_id` for
96% of the 23,781 players it lists. Anyone left over is either a debut the
database has never seen or an unregistered player, and both get a minted
`mlbam-<id>` rather than a guessed match to somebody else.

LICENSING -- READ BEFORE USING
------------------------------
MLB's Stats API is undocumented for third parties and its terms cover
personal, non-commercial use. Treat a database built with this exactly as
nba/nba_source_api.py is treated: fine to hold and query locally, **not**
cleared for redistribution or for backing a hosted service.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
The postseason. `round` carries a specific series code ('WS', 'ALDS1') and
`result` carries the *series* outcome, which mlb/constraints_mlb.py reads
for the World Series and postseason-win squares. Deriving those from this
endpoint means reconstructing series identity and outcome, and a wrong
'WS' row would make "won the World Series" answer the wrong players. A
regular season loaded correctly and an October left alone is the honest
trade; the postseason still arrives with the next Lahman export.
"""

from __future__ import annotations

import csv
import io
import json
import urllib.request
from pathlib import Path

import data_paths

#: Chadwick Bureau's register, sharded by the first character of a UUID.
CHADWICK_SHARDS = "0123456789abcdef"
CHADWICK_URL = ("https://raw.githubusercontent.com/chadwickbureau/register/"
                "master/data/people-{shard}.csv")
USER_AGENT = "sports-data-lab/1.0 (personal, non-commercial)"

#: MLB's current club names mapped onto the ones the database already
#: uses. `club_now` came from Lahman's franchise table, which lags MLB's
#: renames, and sports.MLB_SCHEMA.clubs is frozen to those 30 names at
#: import. Emitting 'Cleveland Guardians' would not add a club, it would
#: split a franchise: half its history under one name and half under
#: another, with `played_for` answering neither properly. Renaming the
#: franchises is a migration of its own.
FRANCHISE_ALIASES = {
    "Athletics": "Oakland Athletics",
    "Cleveland Guardians": "Cleveland Indians",
    "Los Angeles Angels": "Los Angeles Angels of Anaheim",
    "Miami Marlins": "Florida Marlins",
}

#: Batting statistics, as the Stats API names them -> as `games` does.
#: Only the hitting group is read for these. The pitching group carries
#: `hits`, `runs`, `strikeOuts` and `baseOnBalls` too, but those are what
#: the pitcher *allowed*, and adding them to a batting column would invent
#: a season at the plate that never happened.
HITTING_STATS = {
    "atBats": "at_bats", "runs": "runs", "hits": "hits",
    "doubles": "doubles", "triples": "triples", "homeRuns": "home_runs",
    "rbi": "rbis", "stolenBases": "stolen_bases",
    "baseOnBalls": "walks", "strikeOuts": "strikeouts",
}

PITCHING_STATS = {"wins": "wins", "losses": "losses", "saves": "saves"}

#: Every column `games` carries, in order.
GAME_COLUMNS = (
    "player_id", "player", "season", "date", "round", "club_hist",
    "club_now", "venue", "opponent", "career_game_no", "games",
    "is_postseason", "result", "at_bats", "runs", "hits", "doubles",
    "triples", "home_runs", "rbis", "stolen_bases", "walks", "strikeouts",
    "wins", "losses", "saves", "era", "war",
)


class SourceError(RuntimeError):
    """The source could not supply what was asked of it."""


def _number(value):
    """A statistic as a number, or None when the API left it blank.

    Never 0 for a blank. A pitcher with no plate appearances did not bat
    zero times in the sense a batter did, and `-.--` is how the API writes
    an ERA that does not exist yet.
    """
    if value in (None, "", "-", ".---", "-.--", "*.--"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------- crosswalk

def _cache_dir(cache=None) -> Path:
    return Path(cache) if cache else data_paths.cache_dir("mlb", "statsapi")


def chadwick_crosswalk(cache=None, refresh=False, verbose=True) -> dict:
    """MLBAM id -> Baseball-Reference key, which is the database's own key.

    Cached to disk: it is sixteen files and about 65 MB, and it changes
    only when somebody debuts.
    """
    path = _cache_dir(cache) / "chadwick_mlbam_to_bbref.json"
    if path.exists() and not refresh:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass

    pairs: dict[str, str] = {}
    for shard in CHADWICK_SHARDS:
        if verbose:
            print(f"  fetching Chadwick register shard {shard}")
        request = urllib.request.Request(
            CHADWICK_URL.format(shard=shard), headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                body = response.read().decode("utf-8")
        except OSError as exc:
            raise SourceError(
                f"could not fetch the Chadwick register: {exc}") from exc
        for row in csv.DictReader(io.StringIO(body)):
            mlbam, bbref = row.get("key_mlbam"), row.get("key_bbref")
            if mlbam and bbref:
                pairs[str(mlbam).strip()] = str(bbref).strip()

    if not pairs:
        raise SourceError("the Chadwick register returned no usable rows")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pairs), encoding="utf-8")
    return pairs


# ---------------------------------------------------------------- source

class StatsApiSource:
    """Season totals per player per club, from MLB's Stats API."""

    key = "statsapi"

    def __init__(self, cache=None, refresh=False, verbose=True):
        self.cache = _cache_dir(cache)
        #: True re-requests everything; False serves whatever is cached.
        #: A collection of seasons is the third and important form: a
        #: season that ended cannot change and stays on disk forever,
        #: while the season being played must never be served from cache
        #: -- a nightly job reading yesterday's file would insert
        #: yesterday's data and report success.
        self.refresh = refresh
        self.refresh_seasons = (
            None if isinstance(refresh, bool)
            else {int(season) for season in refresh})
        self.verbose = verbose
        self._crosswalk = None

    # -- plumbing ------------------------------------------------------
    def _api(self):
        try:
            import statsapi
        except ImportError as exc:  # pragma: no cover - install-time only
            raise SourceError(
                "MLB-StatsAPI is not installed. Run: "
                "python -m pip install MLB-StatsAPI") from exc
        return statsapi

    def _refreshing(self, season=None) -> bool:
        """Whether this particular request has to go back to MLB."""
        if self.refresh_seasons is None:
            return bool(self.refresh)
        return season is not None and int(season) in self.refresh_seasons

    def _cached(self, name, call, season=None):
        path = self.cache / f"{name}.json"
        if path.exists() and not self._refreshing(season):
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                pass
        if self.verbose:
            print(f"  fetching {name}")
        payload = call()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def crosswalk(self):
        if self._crosswalk is None:
            # Explicitly the bool form: the register is sixteen files and
            # 65 MB, and naming a season to refresh is no reason to fetch
            # it again.
            self._crosswalk = chadwick_crosswalk(
                self.cache, refresh=self.refresh is True, verbose=self.verbose)
        return self._crosswalk

    # -- reading -------------------------------------------------------
    def teams(self, season):
        """The clubs playing that season, named as the database names them."""
        payload = self._cached(
            f"teams_{season}",
            lambda: self._api().get("teams", {"sportId": 1, "season": season}),
            season=season)
        out = []
        for team in payload.get("teams", []):
            name = str(team.get("name", "")).strip()
            out.append({
                "team_id": team.get("id"),
                "club_now": FRANCHISE_ALIASES.get(name, name),
                "venue": (team.get("venue") or {}).get("name"),
            })
        if not out:
            raise SourceError(f"no clubs listed for {season}")
        return out

    def _splits(self, season, team_id, group):
        payload = self._cached(
            f"stats_{season}_{team_id}_{group}",
            lambda: self._api().get("stats", {
                "stats": "season", "group": group, "season": season,
                "sportId": 1, "playerPool": "All", "teamId": team_id,
                "limit": 500,
            }), season=season)
        blocks = payload.get("stats") or []
        return blocks[0].get("splits", []) if blocks else []

    def season_rows(self, season):
        """Regular-season rows for one season, in `games` order.

        `career_game_no` is left None: it numbers a player's seasons and
        cannot be known without the history the loader holds.
        """
        crosswalk = self.crosswalk()
        rows: dict[tuple, dict] = {}

        for team in self.teams(season):
            for group, mapping in (("hitting", HITTING_STATS),
                                   ("pitching", PITCHING_STATS)):
                for split in self._splits(season, team["team_id"], group):
                    person = split.get("player") or {}
                    mlbam = str(person.get("id") or "").strip()
                    if not mlbam:
                        continue
                    key = (mlbam, team["club_now"])
                    row = rows.get(key)
                    if row is None:
                        row = rows[key] = {
                            "player_id": crosswalk.get(mlbam) or f"mlbam-{mlbam}",
                            "mlbam_id": mlbam,
                            "player": str(person.get("fullName", "")).strip(),
                            "season": int(season),
                            "date": f"{int(season)}-04-01",
                            "round": "R",
                            "club_hist": team["club_now"],
                            "club_now": team["club_now"],
                            "venue": team["venue"],
                            "opponent": None,
                            "career_game_no": None,
                            "games": 0,
                            "is_postseason": 0,
                            "result": None,
                            "war": None,
                            **{column: None for column in
                               (*HITTING_STATS.values(),
                                *PITCHING_STATS.values(), "era")},
                        }
                    stat = split.get("stat") or {}
                    for source_name, column in mapping.items():
                        value = _number(stat.get(source_name))
                        if value is not None:
                            row[column] = value
                    if group == "pitching":
                        row["era"] = _number(stat.get("era"))
                    # A two-way player has a hitting and a pitching line
                    # for the same club. The season is worth the larger
                    # appearance count, not their sum.
                    played = _number(stat.get("gamesPlayed")) or 0
                    row["games"] = max(row["games"], int(played))

        if not rows:
            raise SourceError(f"the Stats API returned no players for {season}")
        return list(rows.values())

    def season_schedule(self, season):
        """Completed regular-season matches, shaped for club_match_sources.

        Note the `round` convention differs from `games`. In `games` a
        regular-season row reads 'R' and October carries the series code;
        in club_match_sources the Retrosheet build left regular-season
        rows NULL and used 'WS', 'LCS', 'DS', 'WC'. These rows follow the
        table they land in, so a season loaded here sorts and filters
        alongside the seasons already there.
        """
        start_date = f"01/01/{season}"
        end_date = f"12/31/{season}"
        payload = self._cached(
            f"schedule_{season}",
            lambda: self._api().get("schedule", {
                "sportId": 1, "startDate": start_date, "endDate": end_date,
                "gameType": "R"
            }), season=season)

        matches = []
        seen_games = set()
        team_dict = {str(t["team_id"]): t["club_now"] for t in self.teams(season)}

        for date_node in payload.get("dates", []):
            for game in date_node.get("games", []):
                if game.get("status", {}).get("statusCode") != "F":
                    continue

                game_id = str(game.get("gamePk"))
                if game_id in seen_games:
                    continue
                seen_games.add(game_id)
                
                match_date = game.get("gameDate", "")[:10]
                venue = game.get("venue", {}).get("name")

                teams = game.get("teams", {})
                away = teams.get("away", {})
                home = teams.get("home", {})

                away_id = str(away.get("team", {}).get("id"))
                home_id = str(home.get("team", {}).get("id"))

                away_club = team_dict.get(away_id)
                home_club = team_dict.get(home_id)

                if not away_club or not home_club:
                    continue

                away_score = away.get("score")
                home_score = home.get("score")

                if away_score is None or home_score is None:
                    continue

                margin = abs(home_score - away_score)

                for team_pos, club_id, pts_for, pts_against in [
                    ("A", away_club, away_score, home_score),
                    ("H", home_club, home_score, away_score)
                ]:
                    if pts_for > pts_against:
                        result = "W"
                    elif pts_for < pts_against:
                        result = "L"
                    else:
                        result = "T"

                    matches.append({
                        "source_game_key": f"statsapi-{game_id}",
                        "source_club_id": club_id,
                        "season": int(season),
                        # gameType="R" above: these are regular-season
                        # fixtures, never postseason rounds.
                        "round": None,
                        "is_final": 0,
                        "match_date": match_date,
                        "venue_raw": venue,
                        "team_position": team_pos,
                        "result": result,
                        "points_for": pts_for,
                        "points_against": pts_against,
                        "margin": margin,
                        "attendance": None,
                        "match_id": None,
                        "match_status": "unique"
                    })
        return matches
