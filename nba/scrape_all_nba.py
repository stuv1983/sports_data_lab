#!/usr/bin/env python3
"""
nba/scrape_all_nba.py -- All-NBA / All-ABA / All-BAA selections.

    python -m nba.scrape_all_nba                 # fetch (cached) and load
    python -m nba.scrape_all_nba --refresh       # re-fetch the page
    python -m nba.scrape_all_nba --csv-only      # write the CSV, load nothing

One page holds every selection ever made, so this is a single request and
the response is cached. Sports Reference clears personal, non-commercial
research only -- see nba/nba_source_bbr.py for the same conditions.

The page names the player and the tier but not the team. The team comes
from `player_seasons` instead, which is what makes "All-NBA in a season
with this club" answerable: the database already knows who a player turned
out for in the season they were selected.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

import data_paths
import names

URL = "https://www.basketball-reference.com/awards/all_league.html"
USER_AGENT = ("SportsDataLab/1.2 (personal NBA research; "
              "cached low-volume requests)")
CACHE = data_paths.cache_dir("nba", "awards") / "all_league.html"
CSV_PATH = data_paths.reference_dir("nba") / "all_nba.csv"

TABLE_ID = "awards_all_league"
FIELDS = ("season", "season_label", "league", "tier", "player_name",
          "player_ref", "position")


class ScrapeError(RuntimeError):
    pass


def fetch(refresh=False, delay=3.0, verbose=True):
    """The awards page, from cache unless `refresh`."""
    if CACHE.exists() and not refresh:
        if verbose:
            print(f"  cached: {CACHE}")
        return CACHE.read_text(encoding="utf-8")

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(URL, headers={"User-Agent": USER_AGENT})
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                html = response.read().decode("utf-8", "replace")
            CACHE.write_text(html, encoding="utf-8")
            if verbose:
                print(f"  fetched {len(html):,} bytes -> {CACHE}")
            return html
        except urllib.error.HTTPError as error:
            if error.code == 429:
                wait = float(error.headers.get("Retry-After") or delay * attempt)
                time.sleep(wait)
                continue
            raise ScrapeError(f"{URL}: HTTP {error.code}") from error
        except OSError as error:
            if attempt == 3:
                raise ScrapeError(f"{URL}: {error}") from error
            time.sleep(delay * attempt)
    raise ScrapeError(f"{URL}: gave up after 3 attempts")


def _text(fragment):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", fragment)).strip()


def _cell(row, stat):
    match = re.search(r'data-stat="' + stat + r'"[^>]*>(.*?)</t[hd]>',
                      row, re.S)
    return _text(match.group(1)) if match else None


def season_start(label):
    """'1946-47' -> 1946."""
    match = re.match(r"^(\d{4})-\d{2}$", label.strip())
    return int(match.group(1)) if match else None


def parse(html):
    """The awards table -> one row per selection."""
    start = html.find(f'id="{TABLE_ID}"')
    if start < 0:
        raise ScrapeError(f"no #{TABLE_ID} table -- the page layout changed")

    rows = []
    for raw in re.findall(r"<tr[^>]*>(.*?)</tr>", html[start:], re.S):
        label = _cell(raw, "season")
        league = _cell(raw, "lg_id")
        tier = _cell(raw, "all_team")
        season = season_start(label) if label else None
        if season is None or not league or not tier:
            continue

        for cell in re.findall(r"<td[^>]*>(.*?)</td>", raw, re.S):
            link = re.search(r"/players/\w/(\w+)\.html'>(.*?)</a>(.*)$",
                             cell, re.S)
            if not link:
                continue
            rows.append({
                "season": season,
                "season_label": label,
                "league": league,
                "tier": tier,
                "player_name": _text(link.group(2)),
                "player_ref": link.group(1),
                "position": _text(link.group(3)),
            })

    if not rows:
        raise ScrapeError("table found but no selections parsed")
    return rows


def write_csv(rows, path=None, verbose=True):
    path = Path(path) if path else CSV_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: (-r["season"],
                                                     r["league"], r["tier"],
                                                     r["player_name"])))
    if verbose:
        seasons = {r["season"] for r in rows}
        leagues = sorted({r["league"] for r in rows})
        print(f"  {len(rows):,} selections, {min(seasons)}-{max(seasons)}, "
              f"{'/'.join(leagues)} -> {path}")
    return path


# ------------------------------------------------------------------ load

def fold(name):
    """Match key that survives an accent. BBR writes Jokić, the box scores
    Jokic, and a plain lower() makes those two different players."""
    stripped = "".join(ch for ch in unicodedata.normalize("NFKD", name)
                       if not unicodedata.combining(ch))
    return names.normalise_name(stripped)


SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _parts(name):
    words = [w for w in re.split(r"[^a-z0-9]+", fold(name)) if w]
    return [w for w in words if w not in SUFFIXES]


def simplify(name):
    """Full name with suffixes, punctuation, spacing and middle initials
    gone: 'Jimmy Butler III', 'World B. Free' and 'Jo Jo White' each have
    to meet the box scores' own spelling."""
    words = _parts(name)
    if len(words) > 2:
        words = [words[0]] + [w for w in words[1:-1] if len(w) > 1] + [words[-1]]
    return "".join(words)


def surname(name):
    words = _parts(name)
    return words[-1] if words else ""


def forename(name):
    words = _parts(name)
    return words[0] if words else ""


def _compatible(a, b):
    """First names that could be the same person -- Lou/Louie, Mike/Michael.
    Not nicknames like Tiny for Nate, which only the season can settle."""
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


DDL = """
CREATE TABLE IF NOT EXISTS nba_all_nba (
    player_id    INTEGER,
    season       INTEGER NOT NULL,
    season_label TEXT,
    league       TEXT NOT NULL,
    tier         TEXT NOT NULL,
    player_name  TEXT NOT NULL,
    player_ref   TEXT,
    position     TEXT,
    match_status TEXT NOT NULL
)
"""
INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_all_nba_player ON nba_all_nba(player_id)",
    "CREATE INDEX IF NOT EXISTS ix_all_nba_season ON nba_all_nba(season)",
)


def load(db, rows, verbose=True):
    """Write `nba_all_nba`, resolving each selection to a player_id.

    Layered, strictest first: the exact name, then the name with suffixes
    and middle initials removed, then the surname alone. Each layer is
    tried against the players who appeared in that selection's season
    before it is tried against the whole database, so 'Archibald' in 1972
    is Nate Archibald without the nickname ever being understood.

    The whole-database fallbacks are what link the ABA years, which have no
    games in this build to check a season against. Anything still ambiguous
    gets a NULL player_id and a status: a wrong link is worse than a gap.
    """
    con = sqlite3.connect(db)
    try:
        con.executescript(DDL)
        con.execute("DELETE FROM nba_all_nba")

        exact, loose, by_surname, forenames = {}, {}, {}, {}
        for player_id, player in con.execute(
                "SELECT player_id, player FROM players"):
            exact.setdefault(fold(player), []).append(player_id)
            loose.setdefault(simplify(player), []).append(player_id)
            by_surname.setdefault(surname(player), []).append(player_id)
            forenames[player_id] = forename(player)

        seasons = set()
        for player_id, season in con.execute(
                "SELECT DISTINCT player_id, season FROM player_seasons"):
            seasons.add((player_id, season))

        def resolve(row):
            name = row["player_name"]
            # An ABA season cannot be checked against seasons this build
            # does not have. Matching an All-ABA pick to whoever happened
            # to play in the NBA that year is how Charles Williams became
            # Arthur Williams.
            checkable = row["league"] != "ABA"

            def only(ids):
                if len(ids) == 1:
                    return ids[0]
                fits = [p for p in ids
                        if _compatible(forenames[p], forename(name))]
                return fits[0] if len(fits) == 1 else None

            tiers = (("exact name", exact.get(fold(name), [])),
                     ("simplified name", loose.get(simplify(name), [])),
                     ("surname", by_surname.get(surname(name), [])))

            if checkable:
                for label, ids in tiers:
                    found = only([p for p in ids
                                  if (p, row["season"]) in seasons])
                    if found:
                        return found, f"{label}, in season"

            # Whole-database fallbacks, which are what link an ABA pick who
            # also played in the NBA. Surname alone is deliberately not one
            # of them: it turned Red Robbins into a player who debuted in
            # 2024.
            for label, ids in tiers[:2]:
                found = only(ids)
                if found:
                    return found, label

            return None, ("ambiguous name"
                          if by_surname.get(surname(name)) else "no such player")

        records, counts = [], {}
        for row in rows:
            player_id, status = resolve(row)
            counts[status] = counts.get(status, 0) + 1
            records.append((player_id, row["season"], row["season_label"],
                            row["league"], row["tier"], row["player_name"],
                            row["player_ref"], row["position"], status))

        con.executemany(
            "INSERT INTO nba_all_nba (player_id, season, season_label, "
            "league, tier, player_name, player_ref, position, match_status) "
            "VALUES (?,?,?,?,?,?,?,?,?)", records)
        for statement in INDEXES:
            con.execute(statement)
        con.commit()
    finally:
        con.close()

    if verbose:
        linked = sum(v for k, v in counts.items()
                     if k not in ("no such player", "ambiguous name"))
        print(f"  {linked:,} of {len(records):,} selections linked")
        for status, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"    {count:>5,}  {status}")
    return counts


def unresolved(db, limit=25):
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT season, league, tier, player_name, match_status "
            "FROM nba_all_nba WHERE player_id IS NULL "
            "ORDER BY season DESC LIMIT ?", (limit,)).fetchall()
    finally:
        con.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--db", default=data_paths.default_db("nba"))
    parser.add_argument("--refresh", action="store_true",
                        help="re-fetch instead of using the cached page")
    parser.add_argument("--csv-only", action="store_true",
                        help="write the reference CSV and load nothing")
    parser.add_argument("--quiet", dest="verbose", action="store_false")
    args = parser.parse_args(argv)

    try:
        print("fetching...")
        html = fetch(refresh=args.refresh, verbose=args.verbose)
        print("parsing...")
        rows = parse(html)
        write_csv(rows, verbose=args.verbose)
        if args.csv_only:
            return 0
        if not Path(args.db).exists():
            print(f"no database at {args.db}; CSV written, nothing loaded",
                  file=sys.stderr)
            return 1
        print("loading...")
        load(args.db, rows, verbose=args.verbose)
    except ScrapeError as error:
        print(f"all-nba scrape failed: {error}", file=sys.stderr)
        return 2

    for row in unresolved(args.db, limit=15):
        print(f"    unresolved {row[0]} {row[1]} {row[2]:<4} "
              f"{row[3]} ({row[4]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
