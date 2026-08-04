"""
nba_source.py -- The contract every NBA data source has to satisfy.

build_nba_db.py is written against this protocol and never against a
website. That is the point: NBA.com's stats endpoints are undocumented and
change without notice, and its terms do not clear a comprehensive database
for redistribution. A build tied directly to one provider is a build that
breaks when the provider does, and that cannot be pointed at a licensed
source later without rewriting.

Three pieces:

  * the column vocabularies, which are the actual interface -- an adapter
    hands back frames with these columns and nothing else matters;
  * `validate`, which rejects a frame missing a column rather than filling
    it in;
  * `CsvNbaSource`, the canonical adapter. Every test uses it, and it is
    the default for build_nba_db.py.

`nba_source_api.py` adds `NbaApiSource` for NBA.com. Read its docstring
before using it -- it carries licensing conditions this module does not.

A NOTE ON ZEROS
---------------
`validate` will not invent a missing column and the adapters must not turn
a blank cell into 0. Steals were not recorded before 1973-74 and minutes
not before 1951-52; writing 0 for those seasons would be a claim about
players that the data does not support, and it would rank the entire early
league as maximally obscure for a reason that is an artefact of
record-keeping. NULL is the honest value and the engine already handles it
(every builder in core.Generic filters on IS NOT NULL).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

import data_paths

#: Statistic columns, in the order sports.NBA_STATS declares them.
STAT_COLUMNS = ("points", "rebounds", "assists", "steals", "blocks",
                "turnovers", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta",
                "oreb", "dreb", "minutes", "plus_minus", "fouls")

TEAM_COLUMNS = ("team_id", "franchise_id", "name", "city", "nickname",
                "abbreviation", "first_season", "last_season", "is_current")

PLAYER_COLUMNS = ("source_player_id", "player", "birth_year", "position",
                  "height_cm", "weight_kg")

#: `season` is the START year: 1996 means the 1996-97 season. Every builder
#: in core.Generic compares season numerically, so it has to be an integer,
#: and picking the end year would make "played in 2005" quietly mean the
#: 2004-05 season. `season_label` carries the display form.
MATCH_COLUMNS = ("match_id", "season", "season_label", "date", "phase",
                 "round", "home_team_id", "away_team_id",
                 "home_score", "away_score", "venue", "attendance")

PLAYER_GAME_COLUMNS = ("source_player_id", "match_id", "team_id",
                       *STAT_COLUMNS)

#: The two halves of a season. Kept separate all the way through the build
#: because a playoff game is a different question from a regular-season one
#: and merging them early is how a "20 points a game" square silently starts
#: averaging across both.
PHASES = ("regular", "playoff")


class SourceError(RuntimeError):
    """A source handed back something the build cannot honestly use."""


@dataclass(frozen=True)
class Fetch:
    """One retrieval, recorded so the build is auditable.

    Becomes a row in `source_manifest`. What was taken, from where, with
    which parameters, when, and the digest of the bytes as received -- so a
    disagreement between two builds can be traced to a source that changed
    under them rather than argued about.
    """
    source_key: str                 # "csv" | "nba_api"
    endpoint: str                   # "teams.csv" | "leaguegamelog"
    params: str                     # canonical JSON of the request
    fetched_at: str                 # ISO-8601
    digest: str                     # sha256 of the raw bytes
    rows: int = 0
    path: str = ""                  # where the bytes were cached
    season: Optional[int] = None
    phase: Optional[str] = None


class NbaSource(Protocol):
    """What build_nba_db.py requires of a source. Nothing more."""
    key: str

    def seasons(self) -> list: ...
    def teams(self): ...
    def players(self): ...
    def matches(self, season: int): ...
    def player_games(self, season: int, phase: str): ...
    def fetches(self) -> list: ...


# ------------------------------------------------------------ validation

def validate(frame, columns, kind):
    """
    Check a source frame carries `columns`, and return it in that order.

    Raises rather than repairing. A source that cannot produce `points` is
    not a source that produces zero points, and the difference is the whole
    reason this function exists instead of a `reindex(columns, fill_value=0)`
    one-liner.

    Extra columns are dropped silently -- an adapter is allowed to carry
    provider fields the build has no use for.
    """
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise SourceError(
            f"{kind}: source is missing {', '.join(missing)}. "
            f"Got: {', '.join(map(str, frame.columns))}")
    return frame.loc[:, list(columns)]


def digest_bytes(raw):
    """sha256 of exactly the bytes received, for the manifest."""
    import hashlib
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def canonical_params(**params):
    """Sorted, separator-stable JSON, so the same request digests the same."""
    import json
    return json.dumps(params, sort_keys=True, separators=(",", ":"),
                      default=str)


def now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def numeric(value):
    """A stat cell as float, or None. A blank is unrecorded, not zero.

    Lives here rather than in an adapter because every adapter needs the
    same answer to "what is an empty cell", and two of them disagreeing is
    exactly the bug the note at the top of this module is about.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "null"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_minutes(value):
    """
    'MM:SS' or '34' or '' -> float minutes, or None.

    Blank returns None, never 0.0. Minutes are not recorded before 1951-52
    and a zero there would be a claim that a player was on the roster and
    did not play, which is a different and unsupported fact.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "null"):
        return None
    if ":" in text:
        mins, _, secs = text.partition(":")
        try:
            return int(mins) + int(float(secs or 0)) / 60.0
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_seasons(text):
    """'1996-2005' or '2019' or '1996,1999' -> a sorted list of start years.

    Ranges are inclusive at both ends, because "--seasons 1946-2026" reads
    as the whole history and losing the last season to a half-open range
    would be a silent one-season gap.
    """
    out = set()
    for piece in str(text).replace(" ", "").split(","):
        if not piece:
            continue
        if "-" in piece.lstrip("-"):
            lo, hi = piece.split("-", 1)
            lo, hi = int(lo), int(hi)
            if hi < lo:
                raise SourceError(f"season range {piece} runs backwards")
            out.update(range(lo, hi + 1))
        else:
            out.add(int(piece))
    return sorted(out)


# ------------------------------------------------------------ CSV adapter

class CsvNbaSource:
    """
    Reads normalised CSVs from disk. The canonical adapter.

    This is what the build and every test are written against, and the
    default for `build_nba_db.py --source csv`. It has no network access, no
    licensing conditions and no undocumented endpoints, so a build from it
    is reproducible and a failure in it is a bug here rather than a website
    changing shape.

    Expected layout under data/nba/raw/csv (or `root`)::

        teams.csv
        players.csv
        matches_<season>.csv
        player_games_<season>_<phase>.csv

    A missing per-season file is not an error -- it means that season was
    not exported -- but a missing teams.csv or players.csv is, because the
    build cannot resolve anything without them.
    """

    key = "csv"

    def __init__(self, root=None):
        self.root = Path(root) if root else data_paths.raw_dir("nba") / "csv"
        self._fetches = []

    # -- reading ------------------------------------------------------
    def _read(self, name, columns, kind, required=True):
        import pandas as pd
        path = self.root / name
        if not path.exists():
            if required:
                raise SourceError(f"{kind}: {path} does not exist")
            return None
        raw = path.read_bytes()
        # keep_default_na leaves a blank cell as NaN, which is what an
        # unrecorded statistic is. Never 0.
        frame = pd.read_csv(path)
        frame = validate(frame, columns, kind)
        self._fetches.append(Fetch(
            source_key=self.key, endpoint=name,
            params=canonical_params(path=str(path)),
            fetched_at=now(), digest=digest_bytes(raw),
            rows=len(frame), path=str(path)))
        return frame

    # -- the protocol -------------------------------------------------
    def seasons(self):
        """Start years with a matches file present, discovered from disk."""
        found = []
        for path in sorted(self.root.glob("matches_*.csv")):
            stem = path.stem.split("matches_", 1)[1]
            if stem.isdigit():
                found.append(int(stem))
        return sorted(found)

    def teams(self):
        return self._read("teams.csv", TEAM_COLUMNS, "teams")

    def players(self):
        return self._read("players.csv", PLAYER_COLUMNS, "players")

    def matches(self, season):
        return self._read(f"matches_{int(season)}.csv", MATCH_COLUMNS,
                          f"matches {season}", required=False)

    def player_games(self, season, phase):
        if phase not in PHASES:
            raise SourceError(f"unknown phase {phase!r}; expected one of "
                              f"{', '.join(PHASES)}")
        return self._read(f"player_games_{int(season)}_{phase}.csv",
                          PLAYER_GAME_COLUMNS,
                          f"player games {season} {phase}", required=False)

    def fetches(self):
        return list(self._fetches)


# ------------------------------------------------------------- registry

#: Adapter name -> a zero-argument factory. `csv` is first and is the
#: default everywhere: it is the one with no licensing conditions attached.
def get_source(name, **kwargs):
    name = (name or "csv").strip().lower()
    if name == "csv":
        return CsvNbaSource(**kwargs)
    if name in ("nba_api", "nba-api", "nbaapi"):
        import nba_source_api
        return nba_source_api.NbaApiSource(**kwargs)
    if name in ("bbr", "basketball_reference", "basketball-reference"):
        import nba_source_bbr
        return nba_source_bbr.BbrNbaSource(**kwargs)
    raise SourceError(
        f"unknown source {name!r}; expected csv, bbr or nba_api")
