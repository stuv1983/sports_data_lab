"""
nba/nba_source_live.py -- Live games from NBA.com, biography from the CSVs.

    python -m nba.build_nba_db --source live --refresh-seasons 2025

WHY THIS EXISTS
---------------
Neither adapter alone gives a current database worth having.

`CsvNbaSource` carries a complete player biography -- birth_country,
birth_year, position, height_cm and weight_kg are populated for all 4,937
players -- but it is a static export. It cannot learn that a game was
played last night, so a scheduled rebuild from it changes nothing.

`NbaApiSource` has every game NBA.com has, but its player list is
`nba_api`'s *static* table, which is names and identifiers and no
biography at all. A build from it alone nulls all five of those columns,
which silently disables the "born outside the USA" square in
constraints_nba.py and the height and weight filters in Advanced Search.

Taking each from the side that actually has it costs one small class.

WHY PLAYERS SPLIT FROM TEAMS
----------------------------
Player identifiers are shared: both sides key players by NBA.com's person
id, and every one of the 569 players appearing in the 2024 game rows is
present in players.csv. Joining across the two is therefore sound.

Team identifiers are *not* shared. The CSV export keys historical
identities as `1610612744-1946`, while this module's game rows carry
NBA.com's own ids -- zero of the 58 CSV team ids appear in NBA.com's 30.
So teams come from NBA.com along with the games that reference them, and
mixing the two sides on teams would orphan every match row.

LICENSING
---------
Inherits nba/nba_source_api.py's conditions in full, because it makes the
same requests: private, local, non-commercial use only. Read that module's
note before selecting this one.
"""

from . import nba_source
from .nba_source import PLAYER_COLUMNS, validate


class LiveNbaSource:
    """Games and teams from NBA.com; player biography from the CSV export."""

    key = "live"

    def __init__(self, root=None, refresh=False, api_key=None, verbose=True,
                 throttle=None):
        from .nba_source_api import NbaApiSource, THROTTLE

        self.files = nba_source.CsvNbaSource(root=root)
        self.live = NbaApiSource(
            refresh=refresh, api_key=api_key, verbose=verbose,
            throttle=THROTTLE if throttle is None else throttle)

    # -- from NBA.com, because the game rows reference them ------------
    def seasons(self):
        return self.live.seasons()

    def teams(self):
        return self.live.teams()

    def matches(self, season):
        return self.live.matches(season)

    def player_games(self, season, phase):
        return self.live.player_games(season, phase)

    def discovered_players(self, player_ids=None):
        return self.live.discovered_players(player_ids)

    # -- from the CSV export, because NBA.com's static list has none ---
    def players(self):
        """The CSV biography, widened with anyone NBA.com knows and it does not.

        A player who debuted after the export was taken still has to exist
        as a row or their game rows have nobody to attach to, so the two
        lists are unioned rather than intersected. The newcomers carry the
        same NULL biography a pure NBA.com build would have given
        everybody, which is the honest value: unknown, not zero.
        """
        import pandas as pd

        known = self.files.players()
        seen = set(known["source_player_id"].astype(str))
        extra = self.live.players()
        extra = extra[~extra["source_player_id"].astype(str).isin(seen)]
        if extra.empty:
            return known
        combined = pd.concat([known, extra], ignore_index=True)
        return validate(combined, PLAYER_COLUMNS, "players")

    def fetches(self):
        return [*self.files.fetches(), *self.live.fetches()]
