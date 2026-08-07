#!/usr/bin/env python3
r"""Normalize NBA and NFL award CSVs from ``wiki_sports_scraper``.

The scraper output is deliberately kept outside the repository.  Load it with::

    python -m utils.shared.load_wiki_awards --sport nba --root C:\data_lab\nlf-nba-awards
    python -m utils.shared.load_wiki_awards --sport nfl --root C:\data_lab\nlf-nba-awards

Wikipedia tables are heterogeneous.  This loader accepts only explicitly
described recipient columns, retains every unresolved name for audit, and
never guesses when more than one database player remains plausible.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import data_paths
import names
import sports


DDL = """
CREATE TABLE IF NOT EXISTS wiki_awards (
    award_key       TEXT NOT NULL,
    award_name      TEXT NOT NULL,
    season          INTEGER,
    season_label    TEXT,
    recipient       TEXT NOT NULL,
    recipient_type  TEXT NOT NULL,
    player_id,
    team             TEXT,
    position         TEXT,
    match_status     TEXT NOT NULL,
    candidate_count  INTEGER NOT NULL,
    source_file      TEXT NOT NULL,
    source_row       INTEGER NOT NULL,
    raw_row_json     TEXT NOT NULL,
    imported_at      TEXT NOT NULL
)
"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_wiki_awards_key ON wiki_awards(award_key)",
    "CREATE INDEX IF NOT EXISTS ix_wiki_awards_player ON wiki_awards(player_id)",
    "CREATE INDEX IF NOT EXISTS ix_wiki_awards_season ON wiki_awards(season)",
)

# filename -> (award key, display name, recipient field, recipient type,
#              team field, position field)
SIMPLE = {
    "nba": {
        "mvp.csv": ("nba_mvp", "Most Valuable Player", "Player", "player", "Team", "Position"),
        "rookie_of_year.csv": ("nba_rookie_of_year", "Rookie of the Year", "Player", "player", "Team", "Position"),
        "dpoy.csv": ("nba_defensive_player_of_year", "Defensive Player of the Year", "Player", "player", "Team", "Position"),
        "sixth_man.csv": ("nba_sixth_man", "Sixth Man of the Year", "Player", "player", "Team", "Position"),
        "clutch_poy.csv": ("nba_clutch_player_of_year", "Clutch Player of the Year", "Player", "player", "Team", "Position"),
        "coach_of_year.csv": ("nba_coach_of_year", "Coach of the Year", "Coach", "coach", "Team", None),
        "nba_cup_mvp.csv": ("nba_cup_mvp", "NBA Cup MVP", "Tournament MVP", "player", None, None),
        "naismith_hof.csv": ("naismith_hall_of_fame", "Naismith Hall of Fame", "Inductee", "player", None, None),
    },
    "nfl": {
        "mvp.csv": ("nfl_mvp", "AP Most Valuable Player", "Player", "player", "Team", "Position"),
        "offensive_poy.csv": ("nfl_offensive_player_of_year", "AP Offensive Player of the Year", "Player", "player", "Team", "Position"),
        "defensive_poy.csv": ("nfl_defensive_player_of_year", "AP Defensive Player of the Year", "Player", "player", "Team", "Position"),
        "rookie_of_year.csv": ("nfl_rookie_of_year", "AP Rookie of the Year", "Player", "player", "Team", "Position"),
        "coach_of_year.csv": ("nfl_coach_of_year", "AP Coach of the Year", "Coach", "coach", "Team", None),
        "assistant_coach.csv": ("nfl_assistant_coach_of_year", "AP Assistant Coach of the Year", "Coach", "coach", "Team", "Position"),
    },
}

ALIASES = {
    "lew alcindor": "Kareem Abdul-Jabbar",
    "ron artest": "Metta World Peace",
    "tom sanders": "Satch Sanders",
}

COMBINED_RECIPIENTS = {
    "brett favre barry sanders": ("Brett Favre", "Barry Sanders"),
    "peyton manning steve mcnair": ("Peyton Manning", "Steve McNair"),
}


def season_start(value) -> int | None:
    """Return the starting year from ``2024``, ``2024-25`` or ``2024–25``."""
    match = re.search(r"(?<!\d)(18|19|20)\d{2}(?!\d)", str(value or ""))
    return int(match.group(0)) if match else None


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", names.normalise_name(value)).strip("_")


def clean_recipient(value) -> str:
    """Remove Wikipedia repeat counts, tie notes and footnote markers."""
    text = str(value or "").replace("~", "").strip()
    text = re.sub(r"\s*\((?:\d+|tie)\)", "", text, flags=re.I)
    text = re.sub(r"\[[^]]+\]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _award_dir(root: str | Path, sport: str) -> Path:
    root = Path(root)
    for candidate in (root / sport / "awards", root / "awards", root):
        if candidate.is_dir() and any(candidate.glob("*.csv")):
            return candidate
    raise FileNotFoundError(f"no award CSV directory for {sport} under {root}")


def _read(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def _record(spec, raw, source, row_number, season_field="Season"):
    key, award, recipient_field, kind, team_field, position_field = spec
    recipient = clean_recipient(raw.get(recipient_field))
    if not recipient or recipient.casefold().startswith("no second team"):
        return None
    label = str(raw.get(season_field) or raw.get("Year") or "").strip()
    return {
        "award_key": key, "award_name": award,
        "season": season_start(label), "season_label": label,
        "recipient": recipient, "recipient_type": kind,
        "team": str(raw.get(team_field) or "").strip() if team_field else "",
        "position": str(raw.get(position_field) or "").strip() if position_field else "",
        "source_file": source, "source_row": row_number,
        "raw_row_json": json.dumps(raw, ensure_ascii=False, sort_keys=True),
    }


def collect(root: str | Path, sport: str) -> list[dict]:
    """Read the defensible recipient tables for one sport."""
    sport = sport.strip().lower()
    if sport not in SIMPLE:
        raise ValueError("sport must be nba or nfl")
    folder = _award_dir(root, sport)
    out = []
    for filename, spec in SIMPLE[sport].items():
        path = folder / filename
        if not path.exists():
            continue
        for number, raw in enumerate(_read(path), start=2):
            row = _record(spec, raw, filename, number,
                          "Year" if filename == "naismith_hof.csv" else "Season")
            if row:
                out.append(row)

    if sport == "nba":
        # These selection tables put one recipient in each tier per row.
        for filename, base, display in (
                ("all_defensive_team.csv", "nba_all_defensive", "All-Defensive"),
                ("all_rookie_team.csv", "nba_all_rookie", "All-Rookie")):
            path = folder / filename
            if not path.exists():
                continue
            for number, raw in enumerate(_read(path), start=2):
                for tier in ("First team", "Second team"):
                    spec = (f"{base}_{slug(tier)}", f"{display} {tier.title()}",
                            f"{tier}_Players", "player", f"{tier}_Teams", None)
                    row = _record(spec, raw, filename, number, "Season_Season")
                    if row:
                        out.append(row)
    else:
        # The scraped Comeback table contains side-by-side pre-merger NFL
        # and AFL winners.  Only its NFL half belongs in this database.
        path = folder / "comeback_poy.csv"
        if path.exists():
            spec = ("nfl_comeback_player_of_year", "AP Comeback Player of the Year",
                    "NFL_Player", "player", "NFL_Team", "NFL_Position")
            for number, raw in enumerate(_read(path), start=2):
                row = _record(spec, raw, "comeback_poy.csv", number, "NFL_Season")
                if row:
                    out.append(row)

    expanded = []
    for row in out:
        recipients = COMBINED_RECIPIENTS.get(
            names.normalise_name(row["recipient"]), (row["recipient"],))
        for recipient in recipients:
            expanded.append({**row, "recipient": recipient})

    # A source page can repeat a recipient row in nested tables. Preserve one.
    unique = {}
    for row in expanded:
        key = (row["award_key"], row["season"], names.normalise_name(row["recipient"]),
               names.normalise_name(row["team"]))
        unique.setdefault(key, row)
    return list(unique.values())


def _loose(value: str) -> str:
    value = "".join(character for character in unicodedata.normalize("NFKD", str(value))
                    if not unicodedata.combining(character))
    key = names.name_variants(value)["nopunct"]
    words = [w for w in key.split() if w not in {"jr", "sr", "ii", "iii", "iv"}]
    while len(words) > 2 and len(words[0]) == len(words[1]) == 1:
        words[:2] = [words[0] + words[1]]
    return " ".join(words)


def load(db: str | Path, sport: str, root: str | Path, verbose=True) -> dict:
    rows = collect(root, sport)
    if not rows:
        raise ValueError(f"no usable {sport.upper()} award rows found")
    schema = sports.get(sport).schema
    con = sqlite3.connect(db)
    try:
        players = con.execute(
            f"SELECT {schema.player_id}, {schema.player}, {schema.debut_season}, "
            f"{schema.final_season} FROM {schema.players}").fetchall()
        exact, loose = {}, {}
        for player_id, player, debut, final in players:
            item = (player_id, debut, final)
            exact.setdefault(names.normalise_name(player), []).append(item)
            loose.setdefault(_loose(player), []).append(item)

        def resolve(row):
            if row["recipient_type"] != "player":
                return None, "not_player", 0
            recipient = ALIASES.get(names.normalise_name(row["recipient"]),
                                    row["recipient"])
            candidates = exact.get(names.normalise_name(recipient), [])
            method = "unique"
            if not candidates:
                candidates = loose.get(_loose(recipient), [])
                method = "resolved"
            season = row["season"]
            # Induction years occur after a career; other awards should fall
            # inside it, which disambiguates same-name players safely.
            if season is not None and row["award_key"] != "naismith_hall_of_fame":
                plausible = [c for c in candidates
                             if c[1] is None or c[2] is None
                             or int(c[1]) - 1 <= season <= int(c[2]) + 1]
                if plausible:
                    candidates = plausible
            if len(candidates) == 1:
                return candidates[0][0], method, 1
            return None, "ambiguous" if candidates else "unmatched", len(candidates)

        imported_at = datetime.now(timezone.utc).isoformat()
        records, counts = [], {}
        for row in rows:
            player_id, status, candidate_count = resolve(row)
            counts[status] = counts.get(status, 0) + 1
            records.append((row["award_key"], row["award_name"], row["season"],
                            row["season_label"], row["recipient"],
                            row["recipient_type"], player_id, row["team"],
                            row["position"], status, candidate_count,
                            row["source_file"], row["source_row"],
                            row["raw_row_json"], imported_at))
        con.execute("DROP TABLE IF EXISTS wiki_awards")
        con.execute(DDL)
        con.executemany(
            "INSERT INTO wiki_awards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            records)
        for statement in INDEXES:
            con.execute(statement)
        con.commit()
    finally:
        con.close()
    if verbose:
        print(f"{sport.upper()}: loaded {len(records):,} award rows into {db}")
        for status, count in sorted(counts.items()):
            print(f"  {status:<12} {count:>6,}")
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sport", required=True, choices=("nba", "nfl"))
    parser.add_argument("--root", required=True,
                        help="scraper output root, sport folder, or awards folder")
    parser.add_argument("--db", help="database path (defaults to data/<sport>/<sport>.db)")
    parser.add_argument("--quiet", dest="verbose", action="store_false")
    args = parser.parse_args(argv)
    db = Path(args.db or data_paths.default_db(args.sport))
    if not db.exists():
        print(f"no database at {db}", file=sys.stderr)
        return 2
    try:
        load(db, args.sport, args.root, verbose=args.verbose)
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"award import failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
