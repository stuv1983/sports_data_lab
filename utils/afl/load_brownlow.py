#!/usr/bin/env python3
"""Load and link the yearly AFL Tables Brownlow CSVs into the AFL database."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

from names import normalise_name


TRUSTED = {"unique", "resolved"}

# AFL Tables team-page slugs -> historical club names used by games.club_hist.
TEAM_SLUG_CLUBS = {
    "adelaide": ("Adelaide",),
    "brisbaneb": ("Brisbane Bears",),
    "brisbanel": ("Brisbane Lions",),
    "carlton": ("Carlton",),
    "collingwood": ("Collingwood",),
    "essendon": ("Essendon",),
    "fitzroy": ("Fitzroy",),
    "fremantle": ("Fremantle",),
    "geelong": ("Geelong",),
    "goldcoast": ("Gold Coast",),
    "gws": ("Greater Western Sydney", "GWS"),
    "hawthorn": ("Hawthorn",),
    "kangaroos": ("North Melbourne", "Kangaroos"),
    "melbourne": ("Melbourne",),
    "padelaide": ("Port Adelaide",),
    "richmond": ("Richmond",),
    "stkilda": ("St Kilda",),
    "swans": ("South Melbourne", "Sydney"),
    "university": ("University",),
    "westcoast": ("West Coast",),
    # The typo is in AFL Tables' long-standing URL.
    "bullldogs": ("Footscray", "Western Bulldogs"),
}


RESULT_SCHEMA = """
CREATE TABLE brownlow_results (
    result_id TEXT PRIMARY KEY,
    season INTEGER NOT NULL,
    player_source TEXT NOT NULL,
    player TEXT NOT NULL,
    name_key TEXT NOT NULL,
    team_source TEXT,
    clubs TEXT,
    votes INTEGER NOT NULL,
    vote_rank INTEGER NOT NULL,
    eligible_rank INTEGER,
    ineligible INTEGER NOT NULL DEFAULT 0,
    winner INTEGER NOT NULL DEFAULT 0,
    games INTEGER,
    three_vote_games INTEGER,
    two_vote_games INTEGER,
    one_vote_games INTEGER,
    polling_games INTEGER,
    player_url TEXT,
    team_url TEXT,
    source_url TEXT NOT NULL,
    player_id INTEGER,
    matched_player TEXT,
    match_method TEXT,
    match_status TEXT NOT NULL,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    imported_at TEXT NOT NULL
)
"""

ROUND_SCHEMA = """
CREATE TABLE brownlow_round_votes (
    result_id TEXT NOT NULL,
    season INTEGER NOT NULL,
    round_number INTEGER NOT NULL,
    played INTEGER NOT NULL,
    votes INTEGER,
    PRIMARY KEY (result_id, round_number)
)
"""


def _int(value) -> int | None:
    text = str(value or "").strip()
    return int(text) if text else None


def _bool(value) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes"}


def display_name(source_name: str) -> str:
    """Convert AFL Tables' ``Surname, Given`` display to the local order."""
    parts = [part.strip() for part in str(source_name).split(",", 1)]
    return f"{parts[1]} {parts[0]}" if len(parts) == 2 else parts[0]


def _team_clubs(team_url: str) -> tuple[str, ...]:
    clubs: list[str] = []
    for url in str(team_url or "").split("|"):
        slug = Path(urlparse(url).path).stem.removesuffix("_totals")
        clubs.extend(TEAM_SLUG_CLUBS.get(slug, ()))
    return tuple(dict.fromkeys(clubs))


def _signature(value: str) -> tuple[str, str] | None:
    tokens = re.findall(r"[a-z0-9]+", normalise_name(value))
    if len(tokens) < 2:
        return None
    return tokens[0][0], tokens[-1]


def _one_edit_apart(left: str, right: str) -> bool:
    """True for one insertion, deletion or substitution; no broad fuzzing."""
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) > len(right):
        left, right = right, left
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right)) <= 1
    i = j = differences = 0
    while i < len(left) and j < len(right):
        if left[i] == right[j]:
            i += 1
            j += 1
        else:
            differences += 1
            j += 1
            if differences > 1:
                return False
    return True


def read_rows(paths: list[Path]) -> list[dict]:
    rows_by_id: dict[str, dict] = {}
    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"season", "player", "team", "votes", "player_url", "source_url"}
            missing = required - set(reader.fieldnames or ())
            if missing:
                raise ValueError(f"{path}: missing columns {sorted(missing)}")
            for source_row, raw in enumerate(reader, 1):
                season = _int(raw.get("season"))
                if season != int(path.stem):
                    raise ValueError(f"{path}: row {source_row} has season {season}")
                player = display_name(raw["player"])
                player_url = raw.get("player_url", "").strip()
                result_key = Path(urlparse(player_url).path).stem or str(source_row)
                row = {
                    **raw,
                    "result_id": f"{season}:{result_key}",
                    "season": season,
                    "player_source": raw["player"].strip(),
                    "player": player,
                    "name_key": normalise_name(player),
                    "team_source": raw.get("team", "").strip(),
                    "clubs": "|".join(_team_clubs(raw.get("team_url", ""))),
                    "votes": _int(raw.get("votes")) or 0,
                    "ineligible": int(_bool(raw.get("ineligible"))),
                    "winner": int(_bool(raw.get("winner"))),
                    "games": _int(raw.get("games")),
                    "three_vote_games": _int(raw.get("three_vote_games")),
                    "two_vote_games": _int(raw.get("two_vote_games")),
                    "one_vote_games": _int(raw.get("one_vote_games")),
                    "polling_games": _int(raw.get("polling_games")),
                    "player_url": player_url,
                    "team_url": raw.get("team_url", "").strip(),
                    "source_url": raw["source_url"].strip(),
                }
                previous = rows_by_id.get(row["result_id"])
                if previous is None:
                    rows_by_id[row["result_id"]] = row
                    continue

                # AFL Tables carries one known duplicated 1959 Bill
                # Stephenson row under the same player URL. It is one player
                # season split into two vote totals, not two identities.
                previous["votes"] += row["votes"]
                previous["winner"] = max(previous["winner"], row["winner"])
                previous["ineligible"] = max(
                    previous["ineligible"], row["ineligible"])
                game_values = [value for value in
                               (previous["games"], row["games"])
                               if value is not None]
                previous["games"] = max(game_values) if game_values else None
                for field in ("three_vote_games", "two_vote_games",
                              "one_vote_games", "polling_games"):
                    values = [value for value in (previous[field], row[field])
                              if value is not None]
                    previous[field] = sum(values) if values else None
                for field in ("team_source", "clubs", "team_url"):
                    values = [value for item in (previous[field], row[field])
                              for value in str(item or "").split("|") if value]
                    previous[field] = "|".join(dict.fromkeys(values))
    rows = list(rows_by_id.values())
    if not rows:
        raise ValueError("no Brownlow result rows found")
    _add_ranks(rows)
    return rows


def _add_ranks(rows: list[dict]) -> None:
    by_season: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        by_season[row["season"]].append(row)
    for season_rows in by_season.values():
        ordered = sorted(season_rows, key=lambda row: -row["votes"])
        previous = None
        for position, row in enumerate(ordered, 1):
            if row["votes"] != previous:
                vote_rank = position
                previous = row["votes"]
            row["vote_rank"] = vote_rank
        eligible = [row for row in ordered if not row["ineligible"]]
        previous = None
        for position, row in enumerate(eligible, 1):
            if row["votes"] != previous:
                eligible_rank = position
                previous = row["votes"]
            row["eligible_rank"] = eligible_rank
        for row in ordered:
            row.setdefault("eligible_rank", None)


def _link_indexes(con) -> tuple[dict[str, list[tuple]], dict[tuple[int, str], list[tuple]]]:
    """Read linkage evidence once; 16k rows must not issue 32k SQL queries."""
    by_name: dict[str, list[tuple]] = defaultdict(list)
    for candidate in con.execute(
        "SELECT player_id, player, name_key, debut_season, final_season FROM players"
    ):
        by_name[candidate[2]].append(candidate)
    by_season_club: dict[tuple[int, str], list[tuple]] = defaultdict(list)
    for season, club, *candidate in con.execute(
        """SELECT DISTINCT g.season, g.club_hist, p.player_id, p.player,
                           p.name_key, p.debut_season, p.final_season
              FROM games g JOIN players p ON p.player_id=g.player_id"""
    ):
        by_season_club[(season, club)].append(tuple(candidate))
    return by_name, by_season_club


def _season_club_candidates(con, season: int, clubs: tuple[str, ...],
                            by_season_club=None) -> list[tuple]:
    if not clubs:
        return []
    if by_season_club is not None:
        found: dict[int, tuple] = {}
        for club in clubs:
            for candidate in by_season_club.get((season, club), ()):
                found[candidate[0]] = candidate
        return list(found.values())
    marks = ",".join("?" for _ in clubs)
    return con.execute(
        f"""SELECT DISTINCT p.player_id, p.player, p.name_key,
                           p.debut_season, p.final_season
              FROM players p JOIN games g ON g.player_id=p.player_id
             WHERE g.season=? AND g.club_hist IN ({marks})""",
        (season, *clubs),
    ).fetchall()


def resolve_row(con, row: dict, indexes=None) -> tuple[int | None, str, int, str, str, str]:
    season = row["season"]
    clubs = tuple(filter(None, str(row.get("clubs") or "").split("|")))
    by_name, by_season_club = indexes if indexes is not None else (None, None)
    exact = (list(by_name.get(row["name_key"], ())) if by_name is not None
             else con.execute(
                 "SELECT player_id, player, name_key, debut_season, final_season "
                 "FROM players WHERE name_key=?", (row["name_key"],)
             ).fetchall())
    active = [candidate for candidate in exact
              if candidate[3] <= season <= candidate[4]]
    club_candidates = _season_club_candidates(
        con, season, clubs, by_season_club=by_season_club)
    club_ids = {candidate[0] for candidate in club_candidates}
    matched = [candidate for candidate in active if candidate[0] in club_ids]
    if len(matched) == 1:
        candidate = matched[0]
        status = "unique" if len(exact) == 1 else "resolved"
        return (candidate[0], status, len(exact), candidate[1],
                "exact_name_season_club", "matched name, season and club")

    wanted = _signature(row["player"])
    signature_matches = [candidate for candidate in club_candidates
                         if wanted and _signature(candidate[1]) == wanted]
    if len(signature_matches) == 1:
        candidate = signature_matches[0]
        return (candidate[0], "resolved", len(signature_matches), candidate[1],
                "initial_surname_season_club",
                "matched first initial and surname within season and club")

    fuzzy = []
    if wanted:
        for candidate in club_candidates:
            signature = _signature(candidate[1])
            if (signature and signature[0] == wanted[0]
                    and _one_edit_apart(signature[1], wanted[1])):
                fuzzy.append(candidate)
    if len(fuzzy) == 1:
        candidate = fuzzy[0]
        return (candidate[0], "resolved", len(fuzzy), candidate[1],
                "one_edit_surname_season_club",
                "matched one-character surname variant within season and club")

    candidates = matched or signature_matches or fuzzy or active
    if len(candidates) > 1:
        return None, "ambiguous", len(candidates), "", "none", "multiple safe candidates"
    if exact:
        return None, "implausible", len(exact), "", "none", "name match failed season/club check"
    return None, "unmatched", 0, "", "none", "no safe player match"


RESULT_FIELDS = [
    "result_id", "season", "player_source", "player", "name_key",
    "team_source", "clubs", "votes", "vote_rank", "eligible_rank",
    "ineligible", "winner", "games", "three_vote_games", "two_vote_games",
    "one_vote_games", "polling_games", "player_url", "team_url", "source_url",
    "player_id", "matched_player", "match_method", "match_status",
    "candidate_count", "notes", "imported_at",
]


def load_sources(db_path: str | Path, sources: list[str | Path],
                 verbose: bool = True) -> dict[str, int]:
    paths = [Path(path) for path in sources]
    rows = read_rows(paths)
    con = sqlite3.connect(str(db_path))
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='players'").fetchone():
            raise RuntimeError("database needs players and games tables; run afl.build_db first")
        imported_at = dt.datetime.now(dt.timezone.utc).isoformat()
        indexes = _link_indexes(con)
        linked = []
        for row in rows:
            player_id, status, count, matched, method, notes = resolve_row(
                con, row, indexes=indexes)
            linked.append({**row, "player_id": player_id, "matched_player": matched,
                           "match_method": method, "match_status": status,
                           "candidate_count": count, "notes": notes,
                           "imported_at": imported_at})

        con.execute("DROP TABLE IF EXISTS brownlow_results_new")
        con.execute("DROP TABLE IF EXISTS brownlow_round_votes_new")
        con.execute(RESULT_SCHEMA.replace(
            "CREATE TABLE brownlow_results", "CREATE TABLE brownlow_results_new", 1))
        con.execute(ROUND_SCHEMA.replace(
            "CREATE TABLE brownlow_round_votes", "CREATE TABLE brownlow_round_votes_new", 1))
        marks = ",".join("?" for _ in RESULT_FIELDS)
        con.executemany(
            f"INSERT INTO brownlow_results_new ({','.join(RESULT_FIELDS)}) VALUES ({marks})",
            [tuple(row.get(field) for field in RESULT_FIELDS) for row in linked],
        )
        round_rows = []
        for row in linked:
            for field, value in row.items():
                match = re.fullmatch(r"round_(\d+)", field)
                if not match:
                    continue
                played = str(value or "").strip() != ""
                round_rows.append((row["result_id"], row["season"], int(match.group(1)),
                                   int(played), _int(value) if played else None))
        con.executemany(
            "INSERT INTO brownlow_round_votes_new VALUES (?,?,?,?,?)", round_rows)

        con.execute("DROP TABLE IF EXISTS brownlow_round_votes")
        con.execute("DROP TABLE IF EXISTS brownlow_results")
        con.execute("ALTER TABLE brownlow_results_new RENAME TO brownlow_results")
        con.execute("ALTER TABLE brownlow_round_votes_new RENAME TO brownlow_round_votes")
        for statement in (
            "CREATE INDEX ix_bm_player ON brownlow_results(player_id)",
            "CREATE INDEX ix_bm_season_rank ON brownlow_results(season, eligible_rank)",
            "CREATE INDEX ix_bm_votes ON brownlow_results(votes)",
            "CREATE INDEX ix_bm_status ON brownlow_results(match_status)",
            "CREATE INDEX ix_bm_round_season ON brownlow_round_votes(season, round_number)",
        ):
            con.execute(statement)
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():
            con.execute("DELETE FROM meta WHERE key='brownlow_imported'")
            con.execute("INSERT INTO meta VALUES ('brownlow_imported', datetime('now'))")
        con.commit()
        counts = dict(con.execute(
            "SELECT match_status, COUNT(*) FROM brownlow_results GROUP BY match_status"))
        result = {"rows": len(linked), "round_rows": len(round_rows),
                  "trusted": sum(counts.get(status, 0) for status in TRUSTED), **counts}
        if verbose:
            print(f"Loaded {len(linked):,} Brownlow results and {len(round_rows):,} round rows into {db_path}")
            for status in ("unique", "resolved", "ambiguous", "unmatched", "implausible"):
                if counts.get(status):
                    print(f"  {status:<10} {counts[status]:>6,}")
            print(f"Trusted by search/solver: {result['trusted']:,}")
        return result
    finally:
        con.close()


def default_sources() -> list[Path]:
    from data_paths import brownlow_sources
    return brownlow_sources("afl")


def default_db() -> str:
    from data_paths import default_db as resolve_db
    return resolve_db("afl")


def refresh_default(db_path: str | None = None, verbose: bool = True):
    sources = default_sources()
    if not sources:
        if verbose:
            print("Brownlow refresh skipped: no yearly CSVs found")
        return None
    return load_sources(db_path or default_db(), sources, verbose=verbose)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=default_db())
    parser.add_argument("--source", action="append", type=Path)
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args(argv)
    sources = args.source or default_sources()
    if not sources:
        parser.error("no Brownlow CSVs found in data/afl/bm")
    result = load_sources(args.db, sources)
    if args.report:
        con = sqlite3.connect(args.db)
        try:
            for row in con.execute(
                "SELECT season, player_source, team_source, match_status, notes "
                "FROM brownlow_results WHERE match_status NOT IN ('unique','resolved') "
                "ORDER BY season, player_source"):
                print(row)
        finally:
            con.close()
    return 0 if result["trusted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
