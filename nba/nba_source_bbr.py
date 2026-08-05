"""
nba/nba_source_bbr.py -- The Basketball-Reference scrape, as an NbaSource.

    python -m nba.build_nba_db --source bbr --source-root C:/nbaData/data

This is the adapter for the local scrape: the two index CSVs it writes
(`players.csv`, `games.csv`) plus one JSON box score per game. It maps that
shape onto nba_source's column vocabularies and nothing else, which is what
keeps nba/build_nba_db.py free of any knowledge that Basketball-Reference
exists.

LICENSING -- READ BEFORE USING
------------------------------
Sports Reference permits its data to be used for personal, non-commercial
research. It does not clear republishing a comprehensive copy of the site's
data, or using it to back a public service, without permission. A database
built by this adapter is fine to hold and query locally; treat it as
**not** cleared for redistribution or for a hosted game, exactly as
`nba/nba_source_api.py` is treated. Attribution belongs in ACKNOWLEDGEMENTS.md.

EXPECTED LAYOUT
---------------
`root` is the scrape's data directory::

    games.csv                                  the schedule index
    players.csv                                the player index
    seasons/<season_label>/<phase>/<key>.json  one box score per game
    players/<player_key>/player.json           optional profile

Both index files are also accepted under their `sample_` names, so
`data/nba/sample` works as a root unchanged -- which is how the tests run
against the real scrape output rather than a hand-made fixture.

Box scores are found by the `game_path` column first and by filename
second, so a flattened export (everything in one directory) loads too.

DATES COME FROM THE GAME KEY
----------------------------
`games.csv` currently repeats the game key in its `game_date` column, so a
row reads `194611010TRH` where a date belongs. The key's first eight digits
*are* the date, so the date is parsed from there and the column is only
used when it already looks like a date. That is a scraper bug worth fixing
at the source; until then this is the honest reading rather than a build
full of unparseable dates.

A PARTIAL SCRAPE IS A LEGITIMATE INPUT
--------------------------------------
The scrape indexes every game long before it has every box score, so a
match with no box score is expected, not an error -- it builds as a fixture
nobody has stats for. `complete_only=True` narrows the schedule to games
whose box score has actually landed, which is what to use for a trial build
mid-scrape; the default keeps the full schedule.

NULL IS NOT ZERO
----------------
A 1946 box score has no steals column. Those cells stay None. See the note
in nba/nba_source.py -- writing 0 there would rank the entire early league as
maximally obscure for a reason that is an artefact of record-keeping.
"""

import csv
import hashlib
import json
from pathlib import Path

import data_paths
from . import nba_reference
from .nba_source import (MATCH_COLUMNS, PHASES, PLAYER_COLUMNS,
                        PLAYER_GAME_COLUMNS, STAT_COLUMNS, TEAM_COLUMNS,
                        Fetch, SourceError, canonical_params, digest_bytes,
                        now, numeric, parse_minutes, validate)

#: Leagues that count as NBA history. The BAA is the NBA's own predecessor
#: and the league counts its titles; the ABA was a separate league that
#: merged in, and folding its nine championships into "champion" would be
#: wrong for nine seasons in a way nothing downstream could detect. Pass
#: `leagues=None` to build everything the scrape holds.
NBA_LINEAGE = ("NBA", "BAA")

#: Box-score field -> our stat column. `plus_minus` has no entry: the
#: Basketball-Reference basic box score does not carry it, so it stays NULL
#: rather than becoming a fabricated zero.
STAT_FIELDS = {
    "points": "pts", "rebounds": "trb", "assists": "ast", "steals": "stl",
    "blocks": "blk", "turnovers": "tov", "fgm": "fg", "fga": "fga",
    "fg3m": "three_p", "fg3a": "three_pa", "ftm": "ft", "fta": "fta",
    "oreb": "orb", "dreb": "drb", "fouls": "pf",
}

LBS_TO_KG = 0.45359237
INCHES_TO_CM = 2.54


# ------------------------------------------------------------- parsing

def game_date(key, given=None):
    """'194611010TRH' -> '1946-11-01'. The key is the authority.

    `given` is used only when it already looks like a date, so this keeps
    working when the scraper's `game_date` column is fixed.
    """
    text = str(given or "").strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text
    digits = str(key or "").strip()[:8]
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def birth_year(value):
    """'19631004' or '1963-10-04' -> 1963, else None."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) < 4:
        return None
    year = int(digits[:4])
    return year if 1850 <= year <= 2030 else None


def height_cm(value):
    """'6-9' or '81.0' (inches) -> centimetres, or None.

    The index CSV writes inches as a float and the profile JSON writes
    feet-inches, and both reach here depending on which file was read.
    """
    text = str(value or "").strip()
    if not text or text.lower() in ("nan", "none", "null"):
        return None
    if "-" in text:
        feet, _, inches = text.partition("-")
        try:
            total = int(feet) * 12 + int(float(inches or 0))
        except ValueError:
            return None
    else:
        try:
            total = float(text)
        except ValueError:
            return None
    # A plausible professional basketball player, in inches. Anything else
    # is a column that does not hold what this function was told it holds.
    if not 48 <= total <= 100:
        return None
    return round(total * INCHES_TO_CM, 1)


def weight_kg(value):
    pounds = numeric(value)
    if pounds is None or not 50 <= pounds <= 500:
        return None
    return round(pounds * LBS_TO_KG, 1)


def _slug(text):
    return "".join(ch if ch.isalnum() else "-"
                   for ch in str(text).lower()).strip("-")


def _read_csv(path):
    """Rows of a scrape index, as dicts. utf-8-sig: the scraper writes BOMs."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _read_json(path):
    """A box score or profile. Always utf-8 -- player names are not ASCII."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# -------------------------------------------------------------- adapter

class BbrNbaSource:
    """The local Basketball-Reference scrape. See the module docstring."""

    key = "bbr"

    #: Accepted names for each index, in preference order. The `sample_`
    #: forms let data/nba/sample be used as a root as-is.
    INDEXES = {
        "games": ("games.csv", "sample_games.csv"),
        "players": ("players.csv", "sample_players.csv"),
    }

    def __init__(self, root=None, leagues=NBA_LINEAGE, complete_only=False,
                 verbose=True):
        self.root = Path(root) if root else data_paths.nba_scrape_root()
        self.leagues = tuple(leagues) if leagues else None
        self.complete_only = bool(complete_only)
        self.verbose = verbose
        self._fetches = []
        self._notes = []
        self._games = None
        self._teams = None
        self._boxscores = None

    # -- files ---------------------------------------------------------
    def _index_path(self, kind):
        for name in self.INDEXES[kind]:
            path = self.root / name
            if path.exists():
                return path
        raise SourceError(
            f"{kind}: none of {', '.join(self.INDEXES[kind])} found under "
            f"{self.root}. Point --source-root at the scrape's data "
            f"directory.")

    def _record(self, endpoint, params, digest, rows, path, season=None,
                phase=None):
        self._fetches.append(Fetch(
            source_key=self.key, endpoint=endpoint, params=params,
            fetched_at=now(), digest=digest, rows=rows, path=str(path),
            season=season, phase=phase))

    def _note(self, text):
        self._notes.append(text)
        if self.verbose:
            print(f"  bbr: {text}")

    def notes(self):
        """Everything reconciled rather than trusted, for the build log."""
        return list(self._notes)

    # -- the schedule index --------------------------------------------
    def _schedule(self):
        """games.csv, once, filtered to the leagues asked for."""
        if self._games is not None:
            return self._games
        path = self._index_path("games")
        raw = path.read_bytes()
        rows = _read_csv(path)
        kept, dropped = [], 0
        for row in rows:
            league = (row.get("league") or "").strip().upper()
            if self.leagues and league not in self.leagues:
                dropped += 1
                continue
            phase = (row.get("phase") or "").strip().lower()
            if phase not in PHASES:
                dropped += 1
                continue
            row["_season"] = _int(row.get("season"))
            row["_phase"] = phase
            row["_date"] = game_date(row.get("bbr_game_key"),
                                     row.get("game_date"))
            if row["_season"] is None or row["_date"] is None:
                dropped += 1
                continue
            kept.append(row)
        if dropped:
            self._note(f"{dropped:,} of {len(rows):,} indexed games are "
                       f"outside {'/'.join(self.leagues or ('every league',))}"
                       f" or unparseable, and were left out")
        if not kept:
            raise SourceError(f"games: {path} carries no usable rows")
        self._record("games.csv", canonical_params(path=str(path)),
                     digest_bytes(raw), len(kept), path)
        self._games = kept
        return kept

    def _boxscore_index(self):
        """Every box-score JSON on disk, by game key. Built once.

        By filename rather than by `game_path`, so a flattened export or a
        directory reorganised since the index was written still resolves.
        """
        if self._boxscores is not None:
            return self._boxscores
        found = {}
        for path in self.root.rglob("*.json"):
            stem = path.stem
            # players/<key>/player.json and its sidecars are not box scores.
            if stem in ("player", "image") or path.name.endswith(".meta.json"):
                continue
            found.setdefault(stem, path)
        self._boxscores = found
        return found

    def _boxscore_path(self, row):
        """Where one game's box score is, or None if it has not landed yet."""
        declared = (row.get("game_path") or "").strip()
        if declared:
            path = self.root / declared
            if path.exists():
                return path
        return self._boxscore_index().get(str(row.get("bbr_game_key")))

    # -- teams ---------------------------------------------------------
    def _team_rows(self):
        """Team identities, discovered from the schedule.

        The scrape has no teams file: it names a team on every game row, so
        the identity list is whatever appeared. A Basketball-Reference team
        key is already era-specific -- PHW, SFW and GSW are three keys for
        one franchise -- so the key is the historical identity and the
        franchise comes from nba_reference's lineage.
        """
        if self._teams is not None:
            return self._teams

        seen = {}
        for row in self._schedule():
            season = row["_season"]
            for side in ("home", "visitor"):
                key = (row.get(f"{side}_team_key") or "").strip()
                name = (row.get(f"{side}_team_name") or "").strip()
                if not key or not name:
                    continue
                slot = seen.setdefault(key, {"names": {}, "first": season,
                                             "last": season})
                slot["names"].setdefault(name, [season, season])
                span = slot["names"][name]
                span[0], span[1] = min(span[0], season), max(span[1], season)
                slot["first"] = min(slot["first"], season)
                slot["last"] = max(slot["last"], season)

        franchise_of = self._franchise_index()
        current = set(nba_reference.teams())
        rows = []
        for key, slot in sorted(seen.items()):
            # One key, several names is not how Basketball-Reference numbers
            # teams, but if it happens the latest name is the identity and
            # the collision is reported rather than silently resolved.
            names = sorted(slot["names"].items(), key=lambda kv: kv[1][1])
            name = names[-1][0]
            if len(names) > 1:
                self._note(f"team key {key!r} carries {len(names)} names "
                           f"({', '.join(n for n, _ in names)}); using "
                           f"{name!r}")
            franchise = franchise_of.get(_norm(name))
            if franchise is None:
                # Defunct, or a name the lineage does not know. Its own
                # identity becomes the franchise, so its players stay
                # findable under the name they actually played for, and the
                # build records it as a defunct franchise rather than this
                # module quietly deciding the Toronto Huskies still exist.
                franchise, known = f"bbr-{_slug(key)}", False
            else:
                known = True
            rows.append({
                "team_id": key, "franchise_id": franchise, "name": name,
                "city": None, "nickname": None, "abbreviation": key,
                "first_season": slot["first"], "last_season": slot["last"],
                "is_current": 1 if known and name in current else 0,
                "_known": known})

        # A live franchise whose *current* identity is outside the seasons
        # scraped -- build 1946-1960 and Golden State is only ever the
        # Philadelphia Warriors -- would otherwise leave the build with no
        # club_now name for it. Its latest identity stands in. A genuinely
        # defunct franchise is left alone, so the build still reports it.
        by_franchise = {}
        for row in rows:
            by_franchise.setdefault(row["franchise_id"], []).append(row)
        for franchise, group in by_franchise.items():
            if any(r["is_current"] for r in group) or not group[0]["_known"]:
                continue
            latest = max(group, key=lambda r: r["last_season"])
            latest["is_current"] = 1
            self._note(f"franchise {franchise!r} has no current identity in "
                       f"the seasons scraped; {latest['name']!r} stands in "
                       f"as its club_now name")
        for row in rows:
            row.pop("_known")

        self._teams = rows
        return rows

    @staticmethod
    def _franchise_index():
        """Historical team name -> a stable franchise id.

        Built from nba_reference.club_lineage(), which is where the
        judgement about which identities belong together is already
        written down and argued for.
        """
        index = {}
        for current, identities in nba_reference.club_lineage().items():
            franchise = _slug(current)
            for name in identities:
                index[_norm(name)] = franchise
        for name in nba_reference.teams():
            index.setdefault(_norm(name), _slug(name))
        return index

    # -- the protocol --------------------------------------------------
    def seasons(self):
        return sorted({row["_season"] for row in self._schedule()})

    def teams(self):
        import pandas as pd
        return validate(pd.DataFrame(self._team_rows()), TEAM_COLUMNS, "teams")

    def players(self):
        import pandas as pd

        path = self._index_path("players")
        raw = path.read_bytes()
        rows = _read_csv(path)
        out = []
        for row in rows:
            key = (row.get("bbr_player_key") or "").strip()
            if not key:
                continue
            out.append({
                "source_player_id": key,
                "player": (row.get("player_name") or "").strip(),
                "birth_year": birth_year(row.get("birth_date")),
                "position": (row.get("position") or "").strip() or None,
                "height_cm": height_cm(row.get("height_text")),
                "weight_kg": weight_kg(row.get("weight_lb")),
                # The scrape's index does not carry a birthplace. NULL
                # rather than absent, so the column exists and the
                # born-outside-the-US square hides instead of erroring.
                "birth_country": (row.get("birth_country") or "").strip()
                                 or None,
            })
        if not out:
            raise SourceError(f"players: {path} carries no usable rows")
        self._record("players.csv", canonical_params(path=str(path)),
                     digest_bytes(raw), len(out), path)
        return validate(pd.DataFrame(out), PLAYER_COLUMNS, "players")

    def matches(self, season):
        import pandas as pd

        wanted = [r for r in self._schedule() if r["_season"] == int(season)]
        if not wanted:
            return None
        rows, waiting = [], 0
        for row in wanted:
            if self.complete_only and self._boxscore_path(row) is None:
                waiting += 1
                continue
            rows.append({
                "match_id": (row.get("bbr_game_key") or "").strip(),
                "season": int(season),
                "season_label": (row.get("season_label")
                                 or _label(season)).strip(),
                "date": row["_date"],
                "phase": row["_phase"],
                # The box score says a game was a playoff game, never which
                # round. nba_playoff_rounds fills that from the pinned
                # reference; guessing here would put a first-round game in
                # the Finals.
                "round": None,
                "home_team_id": (row.get("home_team_key") or "").strip(),
                "away_team_id": (row.get("visitor_team_key") or "").strip(),
                "home_score": numeric(row.get("home_points")),
                "away_score": numeric(row.get("visitor_points")),
                "venue": (row.get("arena") or "").strip() or None,
                "attendance": numeric(row.get("attendance")),
            })
        if waiting:
            self._note(f"{_label(season)}: {waiting:,} game(s) have no box "
                       f"score yet and were held back (complete_only)")
        if not rows:
            return None
        return validate(pd.DataFrame(rows), MATCH_COLUMNS,
                        f"matches {season}")

    def player_games(self, season, phase):
        import pandas as pd

        if phase not in PHASES:
            raise SourceError(f"unknown phase {phase!r}; expected one of "
                              f"{', '.join(PHASES)}")
        wanted = [r for r in self._schedule()
                  if r["_season"] == int(season) and r["_phase"] == phase]
        if not wanted:
            return None

        rows, digests, missing, unreadable = [], [], 0, []
        for row in wanted:
            path = self._boxscore_path(row)
            if path is None:
                missing += 1
                continue
            try:
                payload = _read_json(path)
            except (OSError, ValueError) as exc:
                # A half-written file is what an interrupted scrape leaves.
                # Skipped and counted, never partially parsed.
                unreadable.append(f"{path.name}: {exc}")
                continue
            digests.append(str(payload.get("source_sha256") or path.name))
            match_id = str(payload.get("bbr_game_key")
                           or row.get("bbr_game_key") or "").strip()
            for line in payload.get("players") or []:
                player = (line.get("player_key") or "").strip()
                team = (line.get("team_key") or "").strip()
                if not player or not team:
                    continue
                out = {"source_player_id": player, "match_id": match_id,
                       "team_id": team,
                       "minutes": parse_minutes(line.get("minutes"))}
                for column in STAT_COLUMNS:
                    if column == "minutes":
                        continue
                    field = STAT_FIELDS.get(column)
                    # No entry, or an absent key, means the box score does
                    # not record it. None, never 0.
                    out[column] = numeric(line.get(field)) if field else None
                rows.append(out)

        if missing:
            self._note(f"{_label(season)} {phase}: {missing:,} of "
                       f"{len(wanted):,} box score(s) not scraped yet")
        if unreadable:
            self._note(f"{_label(season)} {phase}: {len(unreadable):,} box "
                       f"score(s) could not be read ({unreadable[0]})")
        if not rows:
            return None

        # One digest for the season's box scores, from the per-file digests
        # the scraper recorded, so the manifest can tell a rebuild over the
        # same files from a rebuild over re-scraped ones.
        digest = hashlib.sha256(
            "".join(sorted(digests)).encode("utf-8")).hexdigest()
        self._record("boxscores", canonical_params(season=int(season),
                                                   phase=phase),
                     digest, len(rows), self.root, season=int(season),
                     phase=phase)
        return validate(pd.DataFrame(rows), PLAYER_GAME_COLUMNS,
                        f"player games {season} {phase}")

    def fetches(self):
        return list(self._fetches)

    # -- readiness -----------------------------------------------------
    def coverage(self):
        """How much of the indexed schedule has a box score on disk.

        For checking whether the scrape is far enough along to build,
        without building. Returns a dict; `nba/check_nba_scrape.py` prints it.
        """
        schedule = self._schedule()
        index = self._boxscore_index()
        complete = sum(1 for row in schedule
                       if (row.get("bbr_game_key") or "").strip() in index
                       or self._boxscore_path(row) is not None)
        seasons = sorted({row["_season"] for row in schedule})
        return {"games": len(schedule), "boxscores": complete,
                "missing": len(schedule) - complete,
                "percent": round(100.0 * complete / len(schedule), 2)
                if schedule else 0.0,
                "seasons": seasons,
                "first_season": seasons[0] if seasons else None,
                "last_season": seasons[-1] if seasons else None}


def _norm(name):
    """Team names as compared. 'LA Clippers' and 'L.A. Clippers' are one."""
    text = str(name or "").lower().replace(".", "").replace("-", " ")
    if text.startswith("la "):
        text = "los angeles " + text[3:]
    return " ".join(text.split())


def _label(season):
    return f"{int(season)}-{(int(season) + 1) % 100:02d}"


def _int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
