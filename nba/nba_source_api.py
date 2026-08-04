"""
nba/nba_source_api.py -- NBA.com via the community `nba_api` package.

LICENSING -- READ BEFORE USING
------------------------------
This adapter is for a **private, local prototype only**.

NBA.com's terms permit statistics to be used for private, non-commercial
purposes and require attribution, but they do not clear offering a
comprehensive, regularly updated NBA statistics database through a website
or service without prior consent. A database built by this adapter is
therefore fine to hold and query on your own machine, and is **not** cleared
for redistribution or for backing a hosted game.

`nba_api` is a community package. NBA.com does not document these endpoints
and does not announce changes to them, so this adapter will break without
warning. That is the reason nba/build_nba_db.py talks to nba_source.NbaSource
and not to this module: when the endpoints move, or when a licensed source
becomes available, only this file changes.

For those two reasons `--source csv` is the default and this adapter has to
be asked for by name.

CACHING
-------
Every response is written to data/nba/cache/nba_api/<endpoint>/<hash>.json
with a sidecar recording when it was fetched and the digest of the bytes.
Nothing is re-requested unless `refresh=True`. This is what makes a rebuild
idempotent and what lets the whole app work with the network disconnected
once the cache is warm -- and it also keeps the request count against
NBA.com to the minimum the data actually requires.
"""

import json
import time
from pathlib import Path

import data_paths
from . import nba_source
from .nba_source import (MATCH_COLUMNS, PLAYER_COLUMNS, PLAYER_GAME_COLUMNS,
                        PHASES, STAT_COLUMNS, TEAM_COLUMNS, Fetch, SourceError,
                        canonical_params, digest_bytes, now, numeric,
                        parse_minutes, validate)

#: Seconds between requests. NBA.com throttles aggressively and a build over
#: the full history is thousands of calls; this is politeness, not tuning.
THROTTLE = 0.7

#: nba_api's own labels for the two halves of a season.
SEASON_TYPE = {"regular": "Regular Season", "playoff": "Playoffs"}

#: The first NBA season, as a start year. 1946 means 1946-47.
FIRST_SEASON = 1946


def season_label(season):
    """1996 -> '1996-97'. The display form; `season` stays the start year."""
    return f"{int(season)}-{(int(season) + 1) % 100:02d}"


#: `parse_minutes` and `numeric` are imported from nba_source and re-exported
#: here, where they used to live. Every adapter has to give the same answer
#: to "what is an empty cell", so there is one definition of it.


def playoff_round(game_id):
    """The playoff round encoded in an NBA game id, or None.

    `leaguegamelog` does not carry a round, so without this every Finals
    and championship square would answer nobody -- which reads as "no
    player has ever won a title" rather than as a missing field.

    Playoff game ids are ten digits: `004` marks the playoffs, the next two
    are the season's start year, then a zero, then the round, the matchup
    within that round, and the game number. `0042300405` is the 2023-24
    playoffs, round 4, first matchup, game 5.

    Verified against the 2023-24 playoffs rather than assumed: the four
    round digits carry 16, 8, 4 and 2 distinct teams, and the two teams at
    round 4 are Boston and Dallas, who contested that year's Finals.
    """
    text = str(game_id).strip()
    if len(text) != 10 or not text.isdigit() or not text.startswith("004"):
        return None
    return {"1": "R1", "2": "CSF", "3": "CF", "4": "F"}.get(text[7])


class NbaApiSource:
    """NBA.com through `nba_api`, cached to disk. See the module docstring."""

    key = "nba_api"

    def __init__(self, cache=None, refresh=False, throttle=THROTTLE,
                 verbose=True):
        self.cache = Path(cache) if cache else data_paths.cache_dir(
            "nba", "nba_api")
        self.refresh = refresh
        self.throttle = throttle
        self.verbose = verbose
        self._fetches = []

    # -- caching ------------------------------------------------------
    def _cache_path(self, endpoint, params):
        import hashlib
        stamp = hashlib.sha256(params.encode("utf-8")).hexdigest()[:16]
        return self.cache / endpoint / f"{stamp}.json"

    def _cached(self, endpoint, params, call):
        """
        The response for one request, from disk when it is already there.

        `call` is a zero-argument callable performing the actual request, so
        nothing touches the network on a cache hit -- including importing
        nba_api, which is deliberately deferred into the callers.
        """
        path = self._cache_path(endpoint, params)
        meta = path.with_suffix(".meta.json")
        if path.exists() and not self.refresh:
            raw = path.read_bytes()
            info = {}
            if meta.exists():
                try:
                    info = json.loads(meta.read_text(encoding="utf-8"))
                except ValueError:
                    info = {}
            payload = json.loads(raw.decode("utf-8"))
            self._record(endpoint, params, raw, path,
                         fetched_at=info.get("fetched_at", now()),
                         rows=info.get("rows", 0))
            return payload

        if self.verbose:
            print(f"  fetching {endpoint} {params}")
        payload = call()
        time.sleep(self.throttle)
        raw = json.dumps(payload).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        stamp = now()
        meta.write_text(json.dumps(
            {"endpoint": endpoint, "params": params, "fetched_at": stamp,
             "digest": digest_bytes(raw)}, indent=2), encoding="utf-8")
        self._record(endpoint, params, raw, path, fetched_at=stamp)
        return payload

    def _record(self, endpoint, params, raw, path, fetched_at, rows=0,
                season=None, phase=None):
        self._fetches.append(Fetch(
            source_key=self.key, endpoint=endpoint, params=params,
            fetched_at=fetched_at, digest=digest_bytes(raw),
            rows=rows, path=str(path), season=season, phase=phase))

    # -- nba_api ------------------------------------------------------
    @staticmethod
    def _require_nba_api():
        """Imported here, not at module scope, so the repo installs without it."""
        try:
            import nba_api                                   # noqa: F401
        except ImportError as exc:
            raise SourceError(
                "the nba_api package is not installed. Either `pip install "
                "nba_api`, or build from CSV exports with "
                "`nba/build_nba_db.py --source csv`.") from exc

    @staticmethod
    def _frame(payload, name=None):
        """A resultSets block from an nba_api JSON response as a DataFrame."""
        import pandas as pd
        sets = payload.get("resultSets") or payload.get("resultSet") or []
        if isinstance(sets, dict):
            sets = [sets]
        for block in sets:
            if name is None or block.get("name") == name:
                return pd.DataFrame(block.get("rowSet", []),
                                    columns=block.get("headers", []))
        return pd.DataFrame()

    # -- the protocol -------------------------------------------------
    def seasons(self):
        from datetime import date
        # A season starting in year N is played into N+1, so the current
        # season's start year rolls over in the northern autumn.
        today = date.today()
        latest = today.year if today.month >= 9 else today.year - 1
        return list(range(FIRST_SEASON, latest + 1))

    def teams(self):
        """
        The static team list, widened with historical identities.

        nba_api ships a static team table with the 30 current franchises.
        Historical identities (Seattle SuperSonics, Vancouver Grizzlies) are
        not in it, so they come from nba_reference.FALLBACK_LINEAGE, which is
        where that judgement is already written down. A source that later
        carries real franchise history should override this wholesale.
        """
        import pandas as pd

        from . import nba_reference

        self._require_nba_api()
        from nba_api.stats.static import teams as static_teams

        current = static_teams.get_teams()
        raw = json.dumps(current).encode("utf-8")
        self._record("static.teams", canonical_params(kind="teams"), raw,
                     self.cache / "static" / "teams.json", now(), len(current))

        rows, seen = [], set()
        lineage = nba_reference.club_lineage()
        for team in current:
            name = team["full_name"]
            rows.append({
                "team_id": str(team["id"]), "franchise_id": str(team["id"]),
                "name": name, "city": team.get("city"),
                "nickname": team.get("nickname"),
                "abbreviation": team.get("abbreviation"),
                "first_season": team.get("year_founded"),
                "last_season": None, "is_current": 1})
            seen.add(name)
            for historical in lineage.get(name, []):
                if historical in seen:
                    continue
                seen.add(historical)
                rows.append({
                    "team_id": f"{team['id']}:{historical}",
                    "franchise_id": str(team["id"]),
                    "name": historical, "city": None, "nickname": None,
                    "abbreviation": None, "first_season": None,
                    "last_season": None, "is_current": 0})
        return validate(pd.DataFrame(rows), TEAM_COLUMNS, "teams")

    def players(self):
        import pandas as pd

        self._require_nba_api()
        from nba_api.stats.static import players as static_players

        found = static_players.get_players()
        raw = json.dumps(found).encode("utf-8")
        self._record("static.players", canonical_params(kind="players"), raw,
                     self.cache / "static" / "players.json", now(), len(found))
        frame = pd.DataFrame([{
            "source_player_id": str(p["id"]),
            "player": p["full_name"],
            # The static list carries no biography. Left NULL rather than
            # guessed; a later layer can fill it from commonplayerinfo.
            "birth_year": None, "position": None,
            "height_cm": None, "weight_kg": None,
        } for p in found])
        return validate(frame, PLAYER_COLUMNS, "players")

    def _game_log(self, season, phase):
        """One leaguegamelog call: two rows per game, one per team."""
        self._require_nba_api()
        from nba_api.stats.endpoints import leaguegamelog

        params = canonical_params(season=season_label(season),
                                  season_type=SEASON_TYPE[phase],
                                  player_or_team="T")
        return self._frame(self._cached(
            "leaguegamelog", params,
            lambda: leaguegamelog.LeagueGameLog(
                season=season_label(season),
                season_type_all_star=SEASON_TYPE[phase],
                player_or_team_abbreviation="T").get_dict()))

    def matches(self, season):
        """
        One row per game, folded from the two team rows the log returns.

        MATCHUP carries the orientation: 'BOS vs. LAL' is a home game,
        'BOS @ LAL' an away one. Without it there is no way to tell which
        score belongs to which side, and every venue answer would be a
        coin toss.
        """
        import pandas as pd

        rows = {}
        for phase in PHASES:
            log = self._game_log(season, phase)
            if log.empty:
                continue
            for _, r in log.iterrows():
                gid = str(r["GAME_ID"])
                home = " vs. " in str(r.get("MATCHUP", ""))
                slot = rows.setdefault(gid, {
                    "match_id": gid, "season": int(season),
                    "season_label": season_label(season),
                    "date": str(r.get("GAME_DATE", ""))[:10],
                    "phase": phase,
                    "round": playoff_round(gid) if phase == "playoff" else None,
                    "home_team_id": None, "away_team_id": None,
                    "home_score": None, "away_score": None,
                    "venue": None, "attendance": None})
                side = "home" if home else "away"
                slot[f"{side}_team_id"] = str(r["TEAM_ID"])
                slot[f"{side}_score"] = numeric(r.get("PTS"))
        if not rows:
            return None
        return validate(pd.DataFrame(list(rows.values())), MATCH_COLUMNS,
                        f"matches {season}")

    def player_games(self, season, phase):
        import pandas as pd

        if phase not in PHASES:
            raise SourceError(f"unknown phase {phase!r}")
        self._require_nba_api()
        from nba_api.stats.endpoints import playergamelogs

        params = canonical_params(season=season_label(season),
                                  season_type=SEASON_TYPE[phase])
        payload = self._cached(
            "playergamelogs", params,
            lambda: playergamelogs.PlayerGameLogs(
                season_nullable=season_label(season),
                season_type_nullable=SEASON_TYPE[phase]).get_dict())
        log = self._frame(payload)
        if log.empty:
            return None

        # Provider header -> our column. Anything absent stays NULL, which
        # is what an unrecorded statistic is.
        header = {"points": "PTS", "rebounds": "REB", "assists": "AST",
                  "steals": "STL", "blocks": "BLK", "turnovers": "TOV",
                  "fgm": "FGM", "fga": "FGA", "fg3m": "FG3M", "fg3a": "FG3A",
                  "ftm": "FTM", "fta": "FTA", "oreb": "OREB", "dreb": "DREB",
                  "plus_minus": "PLUS_MINUS", "fouls": "PF"}
        out = pd.DataFrame({
            "source_player_id": log["PLAYER_ID"].astype(str),
            "match_id": log["GAME_ID"].astype(str),
            "team_id": log["TEAM_ID"].astype(str),
        })
        out["minutes"] = log["MIN"].map(parse_minutes) if "MIN" in log else None
        for column in STAT_COLUMNS:
            if column == "minutes":
                continue
            source = header.get(column)
            out[column] = (log[source].map(numeric)
                           if source and source in log.columns else None)
        return validate(out, PLAYER_GAME_COLUMNS,
                        f"player games {season} {phase}")

    def fetches(self):
        return list(self._fetches)
