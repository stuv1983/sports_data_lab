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

AUTHENTICATION
--------------
NBA.com's endpoints take no key, so none is required and the adapter works
with `api_key` empty. `config.nba_api_key()` (environment `NBA_API_KEY`, or
`.streamlit/secrets.toml`) supplies one for a provider that does require it,
and `_auth()` is the single place that decides how it is sent.

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

import config
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
                 verbose=True, api_key=None):
        self.cache = Path(cache) if cache else data_paths.cache_dir(
            "nba", "nba_api")
        #: ``True`` re-requests everything, ``False`` nothing, and a
        #: collection of season start years re-requests only those.
        #:
        #: The third form is what makes a scheduled rebuild reasonable.
        #: A season that finished in 1974 cannot change, so re-requesting
        #: all eighty of them every night is a few hundred pointless
        #: requests against undocumented endpoints -- the surest way to be
        #: blocked. Only seasons still being played need asking about.
        self.refresh = refresh
        self.refresh_seasons = (
            None if isinstance(refresh, bool)
            else {int(season) for season in refresh})
        self.throttle = throttle
        self.verbose = verbose
        #: Empty unless one is configured. NBA.com's own endpoints need no
        #: key; this exists so a provider that does can be dropped in
        #: without touching the request code. See config.py.
        self.api_key = (api_key if api_key is not None
                        else config.nba_api_key())
        self._fetches = []
        # NBA.com's static player index is not exhaustive for historical
        # logs. Names observed in playergamelogs let the builder retain those
        # real rows with unknown biography instead of dropping them.
        self._seen_player_names = {}

    # -- authentication ------------------------------------------------
    def _auth(self):
        """`headers=` kwarg for an nba_api endpoint, or {} with no key.

        nba_api's `headers` argument replaces its defaults wholesale, and
        those defaults (Referer, User-Agent, Origin) are what make the
        request succeed at all -- so the key is merged into them rather
        than sent on its own.
        """
        extra = config.nba_auth_headers(self.api_key)
        if not extra:
            return {}
        from nba_api.stats.library import http
        base = dict(getattr(http, "STATS_HEADERS", None)
                    or getattr(http.NBAStatsHTTP, "headers", None) or {})
        base.update(extra)
        return {"headers": base}

    # -- caching ------------------------------------------------------
    def _cache_path(self, endpoint, params):
        import hashlib
        stamp = hashlib.sha256(params.encode("utf-8")).hexdigest()[:16]
        return self.cache / endpoint / f"{stamp}.json"

    def _refreshing(self, season=None):
        """Whether this particular request has to go back to NBA.com."""
        if self.refresh_seasons is None:
            return bool(self.refresh)
        if season is None:
            # Not season-scoped, so there is nothing to bound it by.
            return True
        return int(season) in self.refresh_seasons

    def _cached(self, endpoint, params, call, season=None):
        """
        The response for one request, from disk when it is already there.

        `call` is a zero-argument callable performing the actual request, so
        nothing touches the network on a cache hit -- including importing
        nba_api, which is deliberately deferred into the callers.
        """
        path = self._cache_path(endpoint, params)
        meta = path.with_suffix(".meta.json")
        if path.exists() and not self._refreshing(season):
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
        rows.extend(self._folded_teams({row["team_id"] for row in rows}))
        return validate(pd.DataFrame(rows), TEAM_COLUMNS, "teams")

    def _folded_teams(self, known_ids):
        """Franchises only the game logs remember.

        The static list is the thirty clubs playing now, and a franchise
        that survives under a new name keeps its team id -- Rochester
        Royals games carry the id the Sacramento Kings carry today, so
        they resolve. The ones that folded outright do not: the Anderson
        Packers, the Sheboygan Redskins and thirteen others hold ids of
        their own that appear nowhere in the list, and the strict build
        rejects a match naming a team it has never heard of. That was
        1,771 matches and their player statistics.

        Their identity comes from the same rows the games do, which is the
        only place it is written down. Reading those logs costs no extra
        requests: `matches` is about to read every one of them, and both
        passes go through the same cache.
        """
        found = {}
        for season in self.seasons():
            for phase in PHASES:
                log = self._game_log(season, phase)
                if log.empty:
                    continue
                unseen = log[~log["TEAM_ID"].astype(str).isin(known_ids)]
                for team_id, name, abbreviation in zip(
                        unseen["TEAM_ID"].astype(str),
                        unseen["TEAM_NAME"], unseen["TEAM_ABBREVIATION"]):
                    row = found.get(team_id)
                    if row is None:
                        found[team_id] = {
                            "team_id": team_id, "franchise_id": team_id,
                            "name": str(name), "city": None,
                            "nickname": None,
                            "abbreviation": (None if abbreviation is None
                                             else str(abbreviation)),
                            "first_season": int(season),
                            "last_season": int(season), "is_current": 0}
                    else:
                        row["first_season"] = min(row["first_season"],
                                                  int(season))
                        row["last_season"] = max(row["last_season"],
                                                 int(season))
        return list(found.values())

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
            # birth_country belongs to that set and was simply missed when
            # it joined PLAYER_COLUMNS, which made validate() reject every
            # build from this adapter before it read a single game.
            "birth_year": None, "position": None,
            "height_cm": None, "weight_kg": None, "birth_country": None,
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
                player_or_team_abbreviation="T",
                **self._auth()).get_dict(),
            season=season))

    def matches(self, season):
        """
        One row per game, folded from the two team rows the log returns.

        MATCHUP normally carries the orientation: 'BOS vs. LAL' is a home
        game, 'BOS @ LAL' an away one.

        NEUTRAL SITES
        -------------
        At a neutral venue -- the NBA Cup semifinals and final in Las
        Vegas, the Mexico City and Paris games -- *both* rows read '@':
        'ORL @ NYK' and 'NYK @ ORL' for the same game. Reading each row
        independently put both teams on the away side, the second
        overwrote the first, and the game was left with no home team at
        all. The strict build then rejected it, taking ten games and two
        hundred and twenty player-game rows with it every season.

        Row order does not rescue it -- measured across 3,680 games it
        names the home side exactly 50.0% of the time -- and no second
        field in this endpoint carries the designation. So both teams are
        kept and the orientation is settled by team id, which is arbitrary
        but stable.

        That costs nothing real. Each team keeps its own score, so
        points_for, points_against and the win or loss stay correct; the
        only nominal value is `is_home`, which for a game at a neutral
        venue has no true answer anyway, and which no NBA constraint
        reads. Losing the game entirely was the worse trade.
        """
        import pandas as pd

        rows = {}
        sides = {}
        for phase in PHASES:
            log = self._game_log(season, phase)
            if log.empty:
                continue
            for _, r in log.iterrows():
                gid = str(r["GAME_ID"])
                rows.setdefault(gid, {
                    "match_id": gid, "season": int(season),
                    "season_label": season_label(season),
                    "date": str(r.get("GAME_DATE", ""))[:10],
                    "phase": phase,
                    "round": playoff_round(gid) if phase == "playoff" else None,
                    "home_team_id": None, "away_team_id": None,
                    "home_score": None, "away_score": None,
                    "venue": None, "attendance": None})
                sides.setdefault(gid, []).append((
                    str(r["TEAM_ID"]), numeric(r.get("PTS")),
                    " vs. " in str(r.get("MATCHUP", ""))))

        for gid, slot in rows.items():
            playing = sides[gid]
            hosts = [side for side in playing if side[2]]
            if len(playing) == 2 and len(hosts) == 1:
                home = hosts[0]
                away = next(side for side in playing if side is not home)
            elif len(playing) == 2:
                # Neutral site, or a log that lost the designation.
                home, away = sorted(playing, key=lambda side: side[0])
            else:
                # Only one team's row came back. Place it on the side its
                # own MATCHUP claims and leave the other missing, which is
                # what the strict build is there to catch.
                for team_id, score, is_host in playing:
                    which = "home" if is_host else "away"
                    slot[f"{which}_team_id"] = team_id
                    slot[f"{which}_score"] = score
                continue
            slot["home_team_id"], slot["home_score"] = home[0], home[1]
            slot["away_team_id"], slot["away_score"] = away[0], away[1]

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
                season_type_nullable=SEASON_TYPE[phase],
                **self._auth()).get_dict(),
            season=season)
        log = self._frame(payload)
        if log.empty:
            return None
        if "PLAYER_NAME" in log.columns:
            self._seen_player_names.update({
                str(player_id): str(player_name)
                for player_id, player_name in zip(
                    log["PLAYER_ID"], log["PLAYER_NAME"])
                if player_id is not None and player_name is not None
            })

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

    def discovered_players(self, player_ids=None):
        """Players present in game logs but absent from the static index."""
        import pandas as pd

        wanted = ({str(value) for value in player_ids}
                  if player_ids is not None else None)
        rows = [{
            "source_player_id": player_id, "player": player_name,
            "birth_year": None, "position": None, "height_cm": None,
            "weight_kg": None, "birth_country": None,
        } for player_id, player_name in sorted(self._seen_player_names.items())
            if wanted is None or player_id in wanted]
        return validate(pd.DataFrame(rows, columns=PLAYER_COLUMNS),
                        PLAYER_COLUMNS, "players discovered in game logs")

    def fetches(self):
        return list(self._fetches)
