#!/usr/bin/env python3
"""Hand-entered Rising Star nominations, votes and suspensions.

    python -m utils.afl.rising_star_manual --list
    python -m utils.afl.rising_star_manual --nominate 2026 23 "Jack Smith" Carlton
    python -m utils.afl.rising_star_manual --ineligible 2026 "Ty Gallop"
    python -m utils.afl.rising_star_manual --votes 2026 "Murphy Reid" 45
    python -m utils.afl.rising_star_manual --remove <key>

Writes ``data/afl/reference/rising_star_manual.csv``, which
``utils/afl/load_rising_star.py`` reads as a third source alongside
FootyWire and Wikipedia.

Why a file and not an UPDATE
----------------------------
``load_rising_star.load_sources`` rebuilds ``rising_star_nominees`` from
its sources every time it runs, and ``afl/build_db.py`` reassigns player
ids on every full rebuild. An edit written straight into the table would
therefore survive until the next Monday scan at the latest, and an edit
keyed to a player id would eventually point at somebody else. Recording
the edit as a *source* means it is re-applied on every load, by name, for
as long as the file says so -- the same reasoning as the hand-entered
rounds in ``utils/afl/load_round_csv.py``.

Two kinds of edit, one mechanism
--------------------------------
A **nomination** is a row nobody else published, so it is the only row for
its round and stands on its own.

A **suspension** or a **vote count** is an annotation on a nomination that
FootyWire or Wikipedia already publishes. The ``admin`` source ranks last
precisely so these lose the row -- keeping the published match statistics
-- while ``MERGED_FIELDS`` carries the flag or the votes onto the row that
won. See SOURCE_PRECEDENCE in ``utils/afl/load_rising_star.py``.

Both are the same shape on disk, so an annotation for a nomination that no
source has published yet simply becomes the nomination.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

MANUAL_CSV = "rising_star_manual.csv"
SOURCE_NAME = "admin"
DEFAULT_REASON = "Ineligible to win the Rising Star due to suspension."

FIELDS = [
    "source_key", "season", "nomination_round", "round_number", "player",
    "player_display", "name_key", "club", "ineligible", "ineligible_reason",
    "votes", "is_season_winner", "source_url", "source", "edited_by",
    "edited_at", "note",
]


def normalise_name(value: object) -> str:
    try:
        from names import normalise_name as project_normalise
    except ImportError:
        text = unicodedata.normalize("NFKD", str(value or "")).casefold()
        text = "".join(c for c in text if not unicodedata.combining(c))
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()
    return project_normalise(str(value or ""))


def like_contains(value: object) -> str:
    """A LIKE pattern matching `value` anywhere, with wildcards escaped.

    Defined here rather than imported from `names` because this module also
    runs standalone; kept to the same behaviour. Queries using it must add
    ``ESCAPE '\\'``.
    """
    text = str(value or "")
    text = (text.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_"))
    return f"%{text}%"


def default_path() -> Path:
    try:
        from data_paths import reference_dir
    except ImportError:
        return Path("data/afl/reference") / MANUAL_CSV
    return reference_dir("afl") / MANUAL_CSV


def entry_key(season: int, name_key: str) -> str:
    """One entry per player per season, whatever it says about them.

    Keyed on the person rather than the round because the three edits are
    facts about one nomination: nominating a player, recording that they
    were later suspended, and recording their vote count all belong on the
    same row. Keying on the round would let a suspension entered without a
    round number become a second, contentless nomination.
    """
    return hashlib.sha256(
        f"admin|{int(season)}|{name_key}".encode("utf-8")).hexdigest()[:24]


def read_entries(path: str | Path | None = None) -> list[dict]:
    path = Path(path or default_path())
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_entries(entries: list[dict], path: str | Path | None = None) -> Path:
    path = Path(path or default_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(entries, key=lambda row: (
        int(row.get("season") or 0),
        int(row.get("round_number") or 0),
        str(row.get("player") or ""),
    ))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(ordered)
    return path


def upsert(season: int, player: str, *, club: str = "",
           round_number: int | None = None, ineligible: bool | None = None,
           reason: str | None = None, votes: int | None = None,
           winner: bool | None = None, note: str = "",
           edited_by: str = "admin", path: str | Path | None = None) -> dict:
    """Add or amend one player's entry for one season.

    Only the fields passed are changed, so marking an existing entry
    ineligible cannot blank the round it was nominated in.
    """
    season = int(season)
    player = str(player).strip()
    if not player:
        raise ValueError("a player name is required")
    name_key = normalise_name(player)
    if not name_key:
        raise ValueError(f"{player!r} does not normalise to a usable name")

    path = Path(path or default_path())
    entries = read_entries(path)
    key = entry_key(season, name_key)
    entry = next((row for row in entries if row.get("source_key") == key), None)
    if entry is None:
        entry = {field: "" for field in FIELDS}
        entry.update({"source_key": key, "season": season, "player": player,
                      "player_display": player, "name_key": name_key,
                      "ineligible": 0, "is_season_winner": 0})
        entries.append(entry)

    if club:
        entry["club"] = str(club).strip()
    if round_number is not None:
        entry["round_number"] = int(round_number)
        entry["nomination_round"] = str(int(round_number))
    if ineligible is not None:
        entry["ineligible"] = int(bool(ineligible))
        entry["ineligible_reason"] = (
            (reason or DEFAULT_REASON) if ineligible else "")
    elif reason is not None:
        entry["ineligible_reason"] = reason
    if votes is not None:
        entry["votes"] = int(votes)
    if winner is not None:
        entry["is_season_winner"] = int(bool(winner))
    if note:
        entry["note"] = note
    entry["source"] = SOURCE_NAME
    entry["source_url"] = f"admin://rising_star/{season}/{name_key}"
    entry["edited_by"] = edited_by
    entry["edited_at"] = dt.datetime.now(dt.timezone.utc).isoformat()

    write_entries(entries, path)
    return entry


def remove(source_key: str, path: str | Path | None = None) -> bool:
    path = Path(path or default_path())
    entries = read_entries(path)
    kept = [row for row in entries if row.get("source_key") != source_key]
    if len(kept) == len(entries):
        return False
    write_entries(kept, path)
    return True


# ------------------------------------------------------------- lookups

def search_players(con: sqlite3.Connection, term: str,
                   limit: int = 25) -> list[dict]:
    """Players whose name matches, with the context needed to tell them apart.

    Career span and clubs are returned because a name is not an identity:
    two Bailey Williamses played in 2026, for West Coast and the Western
    Bulldogs, and picking the wrong one attaches the nomination to a real
    but different person.
    """
    term = str(term or "").strip()
    if len(term) < 2:
        return []
    pattern = like_contains(normalise_name(term))
    rows = con.execute(
        "SELECT player_id, player, debut_season, final_season, career_games, "
        "COALESCE(clubs_now, clubs_hist, '') FROM players "
        "WHERE name_key LIKE ? ESCAPE '\\' "
        "OR LOWER(player) LIKE LOWER(?) ESCAPE '\\' "
        "ORDER BY career_games DESC LIMIT ?",
        (pattern, like_contains(term), limit),
    ).fetchall()
    return [{
        "player_id": row[0], "player": row[1], "debut_season": row[2],
        "final_season": row[3], "career_games": row[4], "clubs": row[5],
        "label": (f"{row[1]} ({row[2]}-{row[3]}, {row[4]:,} games"
                  + (f", {row[5]}" if row[5] else "") + ")"),
    } for row in rows]


def nominations_for(con: sqlite3.Connection, season: int | None = None,
                    term: str = "") -> list[dict]:
    """Nominations already in the database, for annotating one of them."""
    if not _table_exists(con, "rising_star_nominees"):
        return []
    where, params = ["1=1"], []
    if season is not None:
        where.append("season = ?")
        params.append(int(season))
    if term.strip():
        where.append("(name_key LIKE ? ESCAPE '\\' "
                     "OR LOWER(player) LIKE LOWER(?) ESCAPE '\\')")
        params.extend([like_contains(normalise_name(term)),
                       like_contains(term.strip())])
    rows = con.execute(
        "SELECT season, round_number, player, club, source, ineligible, "
        "votes, player_id, match_status FROM rising_star_nominees "
        f"WHERE {' AND '.join(where)} ORDER BY season DESC, round_number",
        params,
    ).fetchall()
    return [{
        "season": row[0], "round_number": row[1], "player": row[2],
        "club": row[3], "source": row[4], "ineligible": bool(row[5]),
        "votes": row[6], "player_id": row[7], "match_status": row[8],
    } for row in rows]


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--path", type=Path, default=None)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--nominate", nargs=4,
                        metavar=("SEASON", "ROUND", "PLAYER", "CLUB"))
    parser.add_argument("--ineligible", nargs=2, metavar=("SEASON", "PLAYER"))
    parser.add_argument("--votes", nargs=3,
                        metavar=("SEASON", "PLAYER", "VOTES"))
    parser.add_argument("--remove", metavar="KEY")
    parser.add_argument("--by", default="cli")
    args = parser.parse_args(argv)

    if args.nominate:
        season, round_number, player, club = args.nominate
        entry = upsert(int(season), player, club=club,
                       round_number=int(round_number), edited_by=args.by,
                       path=args.path)
        print(f"nominated {entry['player']} ({entry['club']}), round "
              f"{entry['round_number']}, {entry['season']}")
    if args.ineligible:
        season, player = args.ineligible
        entry = upsert(int(season), player, ineligible=True,
                       edited_by=args.by, path=args.path)
        print(f"{entry['player']} marked ineligible for {entry['season']}")
    if args.votes:
        season, player, votes = args.votes
        entry = upsert(int(season), player, votes=int(votes),
                       edited_by=args.by, path=args.path)
        print(f"{entry['player']}: {entry['votes']} votes in {entry['season']}")
    if args.remove:
        print("removed" if remove(args.remove, args.path) else "no such entry")
    if args.list or not any(
            (args.nominate, args.ineligible, args.votes, args.remove)):
        entries = read_entries(args.path)
        if not entries:
            print(f"No hand-entered rows in {args.path or default_path()}")
            return 0
        print(f"{len(entries)} hand-entered row(s):")
        for row in entries:
            flags = []
            if str(row.get("ineligible")) == "1":
                flags.append("ineligible")
            if str(row.get("votes") or "").strip():
                flags.append(f"{row['votes']} votes")
            print(f"  {row['season']} round {row.get('round_number') or '?'}: "
                  f"{row['player']} ({row.get('club') or 'club unknown'})"
                  + (f"  [{', '.join(flags)}]" if flags else "")
                  + f"  {row['source_key']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
