#!/usr/bin/env python3
"""
nba/convert_kaggle_nba.py -- Kaggle/NBA-stats dump -> the normalised CSV layout.

    python -m nba.convert_kaggle_nba
    python -m nba.convert_kaggle_nba --seasons 2015-2025 --out data/nba/raw/csv

Writes teams.csv, players.csv, matches_<season>.csv and
player_games_<season>_<phase>.csv, so `build_nba_db --source csv` runs
against the dump unchanged.

Two things the export gets wrong and this script has to correct:

* It writes 0 where a statistic was not recorded, never a blank. Passed
  through, `sum(min_count=1)` gives a 1950s career `career_steals = 0.0`
  instead of NULL and the obscurity model ranks the whole early league as
  maximally obscure. ERAS blanks them, and is verified against the data on
  every run so a wrong boundary fails loudly instead of hiding real values.

* Season comes from `gameId`, never the date: the 2019-20 season finished
  in the October 2020 bubble, which a month cutoff files under 2020.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

import data_paths
from . import nba_source

csv.field_size_limit(1 << 30)

DEFAULT_SOURCE = data_paths.ROOT / "data" / "nba" / "sample" / "csv"

#: `gameId[0]` -> phase. None means the game is not a career stat.
GAME_TYPES = {
    "1": None,        # Preseason
    "2": "regular",   # Regular Season, incl. NBA Cup group games
    "3": None,        # All-Star Game
    "4": "playoff",   # Playoffs
    "5": "playoff",   # Play-in Tournament
    "6": None,        # NBA Cup final
}

#: stat -> first season recorded. Earlier seasons are blanked. Absent from
#: this table means recorded from 1946-47.
ERAS = {
    "rebounds": 1950,
    "minutes": 1951,
    "oreb": 1973,
    "dreb": 1973,
    "steals": 1973,
    "blocks": 1973,
    "turnovers": 1977,
    "fg3m": 1979,
    "fg3a": 1979,
    "plus_minus": 1996,
}

#: normalised stat -> the export's column.
STAT_SOURCE = {
    "points": "points",
    "rebounds": "reboundsTotal",
    "assists": "assists",
    "steals": "steals",
    "blocks": "blocks",
    "turnovers": "turnovers",
    "fgm": "fieldGoalsMade",
    "fga": "fieldGoalsAttempted",
    "fg3m": "threePointersMade",
    "fg3a": "threePointersAttempted",
    "ftm": "freeThrowsMade",
    "fta": "freeThrowsAttempted",
    "oreb": "reboundsOffensive",
    "dreb": "reboundsDefensive",
    "minutes": "numMinutes",
    "plus_minus": "plusMinusPoints",
    "fouls": "foulsPersonal",
}

#: TeamHistories lists every league a *player* has played in, EuroLeague
#: and CBA included. Only these two are the NBA's own lineage.
LEAGUES = ("NBA", "BAA")

#: Franchises are 1610612xxx. The four-digit ids are the All-Star sides
#: ("Team LeBron"), which would otherwise land in the club picker.
FRANCHISE_ID_PREFIX = "1610612"
FRANCHISE_ID_LENGTH = 10


def is_franchise(team_id: str) -> bool:
    return (len(team_id) == FRANCHISE_ID_LENGTH
            and team_id.startswith(FRANCHISE_ID_PREFIX))


#: seasonActiveTill's "still active" sentinel.
STILL_ACTIVE = 2100


class ConvertError(RuntimeError):
    """The export is not shaped the way this converter requires."""


def season_of(game_id: str) -> int | None:
    """`gameId` -> the season's START year, or None if unparseable."""
    if len(game_id) != 8 or not game_id.isdigit():
        return None
    year = int(game_id[1:3])
    return 1900 + year if year >= 46 else 2000 + year


def masked(value: str, stat: str, season: int) -> str:
    """A stat cell, blanked when the season predates its record-keeping."""
    if season < ERAS.get(stat, 0):
        return ""
    return (value or "").strip()


# ----------------------------------------------------------------- teams

def load_team_identities(source: Path, verbose: bool):
    """TeamHistories.csv -> {franchise_id: [identity, ...]} sorted by season.

    A row is one name a franchise played under. The file repeats an
    identity across split spans (the Hawks have two Tri-Cities rows), so
    rows are merged on the name and the span taken as min..max.
    """
    path = source / "TeamHistories.csv"
    if not path.exists():
        raise ConvertError(f"{path} does not exist")

    spans: dict[tuple, list] = {}
    with open(path, encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if row["league"] not in LEAGUES:
                continue
            if not is_franchise(row["teamId"]):
                continue
            key = (row["teamId"], row["teamCity"].strip(),
                   row["teamName"].strip())
            start, end = int(row["seasonFounded"]), int(row["seasonActiveTill"])
            if key in spans:
                spans[key][0] = min(spans[key][0], start)
                spans[key][1] = max(spans[key][1], end)
            else:
                spans[key] = [start, end, row["teamAbbrev"].strip()]

    identities: dict[str, list] = defaultdict(list)
    for (franchise, city, nickname), (start, end, abbrev) in spans.items():
        identities[franchise].append({
            "team_id": f"{franchise}-{start}",
            "franchise_id": franchise,
            "name": f"{city} {nickname}".strip(),
            "city": city,
            "nickname": nickname,
            "abbreviation": abbrev,
            "first_season": start,
            "last_season": end,
            "is_current": 1 if end >= STILL_ACTIVE else 0,
        })
    for rows in identities.values():
        rows.sort(key=lambda r: r["first_season"])

    if not identities:
        raise ConvertError(
            f"{path} yielded no {'/'.join(LEAGUES)} franchises")

    if verbose:
        current = sum(1 for rows in identities.values()
                      for r in rows if r["is_current"])
        print(f"  {len(identities)} franchises, "
              f"{sum(map(len, identities.values()))} identities, "
              f"{current} current")
    return identities


def write_teams(identities, out: Path, last_season: int) -> dict:
    """Write teams.csv, and return the season resolvers."""
    rows = []
    for franchise, spans in sorted(identities.items()):
        for identity in spans:
            record = dict(identity)
            if record["last_season"] >= STILL_ACTIVE:
                record["last_season"] = last_season
            rows.append(record)

    path = out / "teams.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=nba_source.TEAM_COLUMNS)
        writer.writeheader()
        for record in sorted(rows, key=lambda r: r["team_id"]):
            writer.writerow(record)

    # Spans rather than a flat name mapping: the 1988-2002 Charlotte
    # Hornets are the modern Pelicans, and the Bobcats took the name in 2014.
    by_name = defaultdict(list)
    for franchise, spans in identities.items():
        for identity in spans:
            key = (identity["city"].lower(), identity["nickname"].lower())
            by_name[key].append((identity["first_season"],
                                 identity["last_season"], franchise))
    for spans in by_name.values():
        spans.sort()

    # TeamHistories has gaps between spans (the Hawks are unlisted for
    # 1950-51), so a season outside every span takes the nearest identity.
    resolver = {}
    for franchise, spans in identities.items():
        for season in range(1946, last_season + 1):
            chosen = spans[0]
            for identity in spans:
                if identity["first_season"] <= season:
                    chosen = identity
                else:
                    break
            resolver[(franchise, season)] = chosen["team_id"]
    return resolver, dict(by_name)


def franchise_by_name(by_name, city: str, nickname: str, season: int):
    """The franchise that played under this name in this season, or None."""
    spans = by_name.get((city.strip().lower(), nickname.strip().lower()))
    if not spans:
        return None
    for first, last, franchise in spans:
        if first <= season <= last:
            return franchise
    return min(spans, key=lambda s: min(abs(season - s[0]),
                                        abs(season - s[1])))[2]


# --------------------------------------------------------------- matches

def write_matches(source: Path, out: Path, resolver, wanted, verbose: bool):
    """Games.csv -> matches_<season>.csv.

    Returns {match_id: (season, home franchise, away franchise)}; the
    franchises are carried for the blank-playerteamId recovery below.
    """
    path = source / "Games.csv"
    if not path.exists():
        raise ConvertError(f"{path} does not exist")

    by_season = defaultdict(list)
    match_index = {}
    skipped = defaultdict(int)
    unresolved = set()

    with open(path, encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            game_id = row["gameId"].strip()
            season = season_of(game_id)
            if season is None:
                skipped["unparseable gameId"] += 1
                continue
            phase = GAME_TYPES.get(game_id[0])
            if phase is None:
                skipped[f"excluded type ({row['gameType']})"] += 1
                continue
            if wanted and season not in wanted:
                continue

            home_franchise = row["hometeamId"].strip()
            away_franchise = row["awayteamId"].strip()
            home = resolver.get((home_franchise, season))
            away = resolver.get((away_franchise, season))
            if not home or not away:
                unresolved.add(row["hometeamId"] if not home
                               else row["awayteamId"])
                skipped["unresolved team"] += 1
                continue

            by_season[season].append({
                "match_id": game_id,
                "season": season,
                "season_label": f"{season}-{(season + 1) % 100:02d}",
                "date": (row["gameDate"] or "")[:10],
                "phase": phase,
                # The build derives regular rounds; playoff rounds come
                # from reference/playoff_series.csv.
                "round": "",
                "home_team_id": home,
                "away_team_id": away,
                "home_score": row["homeScore"].strip(),
                "away_score": row["awayScore"].strip(),
                "venue": row["arenaName"].strip(),
                "attendance": row["attendance"].strip(),
            })
            match_index[game_id] = (season, home_franchise, away_franchise)

    for season, rows in sorted(by_season.items()):
        target = out / f"matches_{season}.csv"
        with open(target, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle,
                                    fieldnames=nba_source.MATCH_COLUMNS)
            writer.writeheader()
            writer.writerows(sorted(rows, key=lambda r: (r["date"],
                                                         r["match_id"])))

    if verbose:
        print(f"  {len(match_index):,} matches across {len(by_season)} "
              f"season(s)")
        for reason, count in sorted(skipped.items(), key=lambda kv: -kv[1]):
            print(f"    skipped {count:>6,}  {reason}")
        if unresolved:
            print(f"    unresolved team ids: "
                  f"{', '.join(sorted(unresolved)[:5])}")
    return match_index


# ---------------------------------------------------------- player games

def write_player_games(source: Path, out: Path, resolver, by_name,
                       match_index, verbose: bool):
    """PlayerStatistics.csv -> player_games_<season>_<phase>.csv.

    One streaming pass, holding a file handle per (season, phase).
    Returns (players seen, first non-zero season per stat).
    """
    path = source / "PlayerStatistics.csv"
    if not path.exists():
        raise ConvertError(f"{path} does not exist")

    handles: dict[tuple, tuple] = {}
    orphans: dict[str, str] = {}
    evidence: dict[str, int] = {}
    written = 0
    skipped = defaultdict(int)
    recovered = defaultdict(int)

    def writer_for(season, phase):
        key = (season, phase)
        if key not in handles:
            target = out / f"player_games_{season}_{phase}.csv"
            handle = open(target, "w", newline="", encoding="utf-8")
            writer = csv.DictWriter(
                handle, fieldnames=nba_source.PLAYER_GAME_COLUMNS)
            writer.writeheader()
            handles[key] = (handle, writer)
        return handles[key][1]

    try:
        with open(path, encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                game_id = row["gameId"].strip()
                entry = match_index.get(game_id)
                if entry is None:
                    skipped["no matching game"] += 1
                    continue
                season, home, away = entry
                person = row["personId"].strip()
                if not person:
                    skipped["blank personId"] += 1
                    continue

                franchise = row["playerteamId"].strip()
                if not franchise:
                    # Blank on 48,585 rows -- almost all of 2021-22 and half
                    # of 2000-01 -- with opponentteamId blank on the same
                    # rows, so only the city and nickname are left to go on.
                    franchise = franchise_by_name(
                        by_name, row["playerteamCity"],
                        row["playerteamName"], season)
                    if franchise in (home, away):
                        recovered["named team, matches the fixture"] += 1
                    elif franchise:
                        skipped["named team not in this fixture"] += 1
                        continue
                    else:
                        skipped["blank team, unrecognised name"] += 1
                        continue
                team = resolver.get((franchise, season)) if franchise else None
                if not team:
                    skipped["unresolved team"] += 1
                    continue

                record = {"source_player_id": person, "match_id": game_id,
                          "team_id": team}
                for stat, column in STAT_SOURCE.items():
                    value = masked(row.get(column, ""), stat, season)
                    record[stat] = value
                    if value:
                        try:
                            if float(value) != 0.0:
                                if season < evidence.get(stat, 9999):
                                    evidence[stat] = season
                        except ValueError:
                            pass

                writer_for(season, GAME_TYPES[game_id[0]]).writerow(record)
                written += 1
                orphans.setdefault(person,
                                   f"{row['firstName']} {row['lastName']}")
    finally:
        for handle, _ in handles.values():
            handle.close()

    if verbose:
        print(f"  {written:,} player-games in {len(handles)} file(s)")
        for reason, count in sorted(recovered.items(), key=lambda kv: -kv[1]):
            print(f"    recovered blank playerteamId {count:>7,}  {reason}")
        for reason, count in sorted(skipped.items(), key=lambda kv: -kv[1]):
            print(f"    skipped {count:>9,}  {reason}")
    return orphans, evidence


def check_eras(evidence, verbose: bool) -> list:
    """Check ERAS against the export.

    Appearing later than ERAS says is normal; earlier means the mask is
    hiding real values and ERAS is wrong.
    """
    problems = []
    for stat, first in sorted(ERAS.items()):
        seen = evidence.get(stat)
        if seen is None:
            problems.append(f"{stat}: never non-zero anywhere in the export")
        elif seen < first:
            problems.append(
                f"{stat}: ERAS says {first} but a non-zero value survives "
                f"in {seen} -- the mask is hiding real data")
    if verbose:
        if problems:
            print("  ERA CHECK FAILED:")
            for line in problems:
                print(f"    {line}")
        else:
            print(f"  era check passed for {len(ERAS)} masked stat(s)")
    return problems


# --------------------------------------------------------------- players

def write_players(source: Path, out: Path, appeared, verbose: bool):
    """Players.csv -> players.csv, plus a row for anyone only in the stats.

    Without the fallback row the build's inner join drops the player and
    their games with them.
    """
    path = source / "Players.csv"
    if not path.exists():
        raise ConvertError(f"{path} does not exist")

    rows = []
    known = set()
    with open(path, encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            person = row["personId"].strip()
            if not person or person not in appeared:
                continue
            known.add(person)
            position = "".join(code for code, flag in
                               (("G", "guard"), ("F", "forward"),
                                ("C", "center"))
                               if (row.get(flag) or "").strip() == "1")
            rows.append({
                "source_player_id": person,
                "player": f"{row['firstName']} {row['lastName']}".strip(),
                "birth_year": (row["birthDate"] or "")[:4],
                "position": position,
                "height_cm": _scaled(row.get("heightInches"), 2.54),
                "weight_kg": _scaled(row.get("bodyWeightLbs"), 0.45359237),
                "birth_country": (row.get("country") or "").strip(),
            })

    missing = sorted(set(appeared) - known)
    for person in missing:
        rows.append({
            "source_player_id": person, "player": appeared[person].strip(),
            "birth_year": "", "position": "", "height_cm": "",
            "weight_kg": "", "birth_country": "",
        })

    target = out / "players.csv"
    with open(target, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=nba_source.PLAYER_COLUMNS)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: r["source_player_id"]))

    if verbose:
        print(f"  {len(rows):,} players ({len(missing):,} known only from "
              f"the box scores)")
    return len(rows)


def _scaled(value, factor):
    """A measurement converted, or blank. 0 means unrecorded."""
    text = (value or "").strip()
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        return ""
    return "" if number <= 0 else f"{number * factor:.1f}"


# ------------------------------------------------------------------ main

def convert(source: Path, out: Path, seasons=None, verbose=True):
    out.mkdir(parents=True, exist_ok=True)
    wanted = set(seasons) if seasons else None
    last_season = max(wanted) if wanted else 2025

    if verbose:
        print(f"source : {source}")
        print(f"output : {out}")
        print("teams...")
    identities = load_team_identities(source, verbose)
    resolver, by_name = write_teams(identities, out, last_season)

    if verbose:
        print("matches...")
    match_index = write_matches(source, out, resolver, wanted, verbose)
    if not match_index:
        raise ConvertError("no matches converted; check --seasons")

    if verbose:
        print("player games (one pass over the big file, this takes a "
              "minute)...")
    appeared, evidence = write_player_games(source, out, resolver, by_name,
                                            match_index, verbose)

    if verbose:
        print("era check...")
    problems = check_eras(evidence, verbose)

    if verbose:
        print("players...")
    players = write_players(source, out, appeared, verbose)

    return {"matches": len(match_index), "players": players,
            "era_problems": problems}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default=str(DEFAULT_SOURCE),
                        help="the provider export directory")
    parser.add_argument("--out", default=None,
                        help="where to write the normalised CSVs "
                             "(default data/nba/raw/csv)")
    parser.add_argument("--seasons", default=None,
                        help="start years, e.g. 2015-2025 or 1996,1999")
    parser.add_argument("--quiet", dest="verbose", action="store_false")
    args = parser.parse_args(argv)

    out = Path(args.out) if args.out else data_paths.raw_dir("nba") / "csv"
    seasons = (nba_source.parse_seasons(args.seasons)
               if args.seasons else None)

    try:
        summary = convert(Path(args.source), out, seasons, args.verbose)
    except (ConvertError, nba_source.SourceError) as error:
        print(f"convert failed: {error}", file=sys.stderr)
        return 2

    if summary["era_problems"]:
        print("\nera check failed -- ERAS disagrees with the export. "
              "Fix ERAS before building.", file=sys.stderr)
        return 1
    if args.verbose:
        print(f"\ndone. Now run:\n"
              f"    python -m nba.build_nba_db --source csv "
              f"--csv-root {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
