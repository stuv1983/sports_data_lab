#!/usr/bin/env python3
"""Load Team of the Century selections and link them to players.

    python -m afl.load_teams_of_the_century
    python -m afl.load_teams_of_the_century --unmatched

Reads ``data/afl/raw/wikipedia_teams_of_the_century.csv`` (see
``afl/scrape_teams_of_the_century.py``) into a `team_selections` table.

Linking is deliberately conservative, and for these teams that matters more
than usual. Three of the five -- Italian, Greek and Queensland -- name the
player and nothing else, so there is no club and no career to check a
common name against. A name that matches two players in those teams stays
`ambiguous` rather than being resolved by picking the more famous one,
because "the famous one" is exactly the assumption that puts the wrong
person in a team of the century.

Where the source does give clubs, they are used: a candidate must have
played for one of them. That is what makes the AFL/VFL and Indigenous teams
resolve at a much higher rate than the other three, and the per-team report
shows that difference rather than averaging it away.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import sqlite3
import sys
from pathlib import Path

from data_paths import default_db, raw_dir
from names import name_variants, normalise_name

SOURCE_CSV = "wikipedia_teams_of_the_century.csv"
TRUSTED = ("unique", "resolved")

SCHEMA = """
CREATE TABLE IF NOT EXISTS team_selections (
    team_name       TEXT NOT NULL,
    position        TEXT,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    name            TEXT NOT NULL,
    name_key        TEXT NOT NULL,
    club            TEXT,
    role            TEXT,
    note            TEXT,
    player_id       INTEGER,
    match_status    TEXT NOT NULL,
    match_method    TEXT,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    notes           TEXT,
    source_url      TEXT,
    imported_at     TEXT NOT NULL,
    PRIMARY KEY (team_name, name_key)
)
"""
INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_toc_player ON team_selections(player_id)",
    "CREATE INDEX IF NOT EXISTS ix_toc_team ON team_selections(team_name)",
    "CREATE INDEX IF NOT EXISTS ix_toc_status ON team_selections(match_status)",
)

_SUFFIX = re.compile(r"[ ,]+(?:jn?r|sn?r|i{1,3}|iv)\.?$", re.I)
#: 'Graham 'Polly' Farmer' -- the quoted nickname is not part of the name
#: the player table uses.
_NICKNAME = re.compile(r"\s*['\"‘’]([^'\"‘’]{2,})"
                       r"['\"‘’]\s*")


def _name_forms(name: str) -> list[str]:
    """The name as written, plus the forms a player table is likely to use."""
    forms = [name]
    without_nickname = _NICKNAME.sub(" ", name).strip()
    if without_nickname != name:
        forms.append(without_nickname)
    for form in list(forms):
        stripped = _SUFFIX.sub("", form).strip()
        if stripped != form:
            forms.append(stripped)
    return forms


def _references(con: sqlite3.Connection) -> tuple[dict, dict]:
    """Name index, and player_id -> set of clubs played for."""
    index: dict[str, list] = {}
    for player_id, name, debut, final, games in con.execute(
            "SELECT player_id, player, debut_season, final_season, "
            "career_games FROM players"):
        keys = name_variants(name)
        for key in {keys["key"], keys["nopunct"]}:
            if key:
                index.setdefault(key, []).append(
                    (player_id, name, debut, final, games))

    clubs: dict[int, set] = {}
    for player_id, club in con.execute(
            "SELECT DISTINCT player_id, club_hist FROM games"):
        clubs.setdefault(player_id, set()).add(str(club or "").lower())
    return index, clubs


def _resolve(row: dict, index: dict, clubs: dict):
    """(player_id, status, method, candidate_count, notes)."""
    candidates = []
    for form in _name_forms(row["name"]):
        keys = name_variants(form)
        candidates = index.get(keys["key"]) or index.get(keys["nopunct"]) or []
        if candidates:
            break
    if not candidates:
        return None, "unmatched", "", 0, "no player with this name"
    if len(candidates) == 1:
        return candidates[0][0], "unique", "name", 1, ""

    total = len(candidates)

    listed = [c.strip().lower() for c in
              re.split(r"[,/]", str(row.get("club") or "")) if c.strip()]
    if listed:
        # The source named clubs, so require one of them. A VFL/AFL team of
        # the century entry naming 'Geelong, West Adelaide' should only
        # match a player this database has at Geelong.
        plausible = [c for c in candidates
                     if any(any(name in club for club in clubs.get(c[0], ()))
                            for name in listed)]
        if len(plausible) == 1:
            return plausible[0][0], "unique", "name+club", total, ""
        if plausible:
            candidates = plausible

    # A generational suffix is evidence, not noise. These teams are full of
    # father-and-son pairs who share a name exactly -- Ablett, Barassi,
    # Whitten, Dyer, Kennedy, Rioli -- and the source distinguishes them
    # the only way it can. Applied only when the careers do not overlap,
    # so 'Sr' and 'Jr' are unambiguous in time.
    suffix = _SUFFIX.search(row["name"])
    if suffix and len(candidates) == 2:
        first, second = sorted(candidates, key=lambda c: (c[2] or 0))
        if (first[3] or 0) <= (second[2] or 0):
            elder = suffix.group(0).strip(" ,.").lower().startswith("s")
            chosen = first if elder else second
            return (chosen[0], "resolved", "name+generation", total,
                    f"'{suffix.group(0).strip(' ,')}' → "
                    f"{chosen[1]} ({chosen[2]}-{chosen[3]})")

    # Otherwise a decisively longer career, and only when the alternative
    # is not credible. Deliberately not "pick the famous one": that is the
    # assumption that puts the wrong person in a team of the century.
    ranked = sorted(candidates, key=lambda c: -(c[4] or 0))
    if len(ranked) > 1 and (ranked[0][4] or 0) >= 5 * max(1, ranked[1][4] or 1):
        return (ranked[0][0], "resolved", "career_length", total,
                f"chosen over {len(ranked) - 1} other(s) on games played")

    return (None, "ambiguous", "", total,
            "; ".join(f"{c[1]} ({c[2]}-{c[3]}, {c[4]}g)"
                      for c in candidates[:6]))


def load(db: str, csv_path: Path, verbose: bool = True) -> dict:
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    con = sqlite3.connect(db)
    con.execute(SCHEMA)
    for statement in INDEXES:
        con.execute(statement)

    index, clubs = _references(con)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    counts: dict[str, int] = {}
    per_team: dict[str, list] = {}

    con.execute("DELETE FROM team_selections")
    for row in rows:
        player_id, status, method, n, notes = _resolve(row, index, clubs)
        counts[status] = counts.get(status, 0) + 1
        per_team.setdefault(row["team_name"], []).append(status)
        con.execute(
            """INSERT OR REPLACE INTO team_selections
               (team_name, position, sort_order, name, name_key, club, role,
                note, player_id, match_status, match_method,
                candidate_count, notes, source_url, imported_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["team_name"], row["position"], int(row["sort_order"] or 0),
             row["name"], normalise_name(row["name"]), row.get("club"),
             row.get("role"), row.get("note"), player_id, status, method,
             n, notes, row.get("source_url"), now))
    con.commit()
    con.close()

    if verbose:
        print(f"team_selections: {len(rows)} selections across "
              f"{len(per_team)} teams")
        for status, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"   {status:<12} {n:>4}")
        print()
        for team, statuses in per_team.items():
            ok = sum(1 for s in statuses if s in TRUSTED)
            print(f"   {team:<38} {ok:>3}/{len(statuses):<3} linked")
        print("\n   The Italian, Greek and Queensland sources name the "
              "player only,\n   with no club to check a shared name "
              "against, so they link less\n   often by design rather than "
              "by failure.")
    return {"rows": len(rows), "statuses": counts}


def show_unmatched(db: str, limit: int = 40) -> None:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    print(f"{'team':<34}{'name':<24}{'status':<12}notes")
    for team, name, status, notes in con.execute(
            "SELECT team_name, name, match_status, notes FROM team_selections "
            "WHERE match_status NOT IN ('unique','resolved') "
            "ORDER BY team_name, name LIMIT ?", (limit,)):
        print(f"{team[:33]:<34}{name[:23]:<24}{status:<12}{(notes or '')[:44]}")
    con.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None)
    ap.add_argument("--csv", default=None)
    ap.add_argument("--unmatched", action="store_true")
    args = ap.parse_args(argv)

    db = args.db or default_db("afl")
    if args.unmatched:
        show_unmatched(db)
        return 0
    path = Path(args.csv) if args.csv else raw_dir("afl") / SOURCE_CSV
    if not path.exists():
        sys.exit(f"{path} not found -- run afl/scrape_teams_of_the_century.py")
    load(db, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
