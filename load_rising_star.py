#!/usr/bin/env python3
"""Load and safely link FootyWire Rising Star nomination CSVs to SQLite.

Only rows resolved to one local player are trusted by the search and grid
constraints.  Name collisions, implausible career seasons and unmatched rows
remain in the table for audit but are invisible to the solver.

Rows the automatic linker cannot resolve may be corrected through a reviewed
override file (see ``rising_star_overrides.py``)::

    data/afl/reference/rising_star_name_overrides.csv

Overrides are applied after automatic resolution and only to rows that are
not already trusted, so a correction can never displace a good match.  Each
override must still resolve to exactly one real player whose game rows
support the stated club and season, so a mistake in the reference file fails
to apply rather than silently trusting the wrong person.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Iterable

TRUSTED = {"unique", "resolved"}
#: Reviewed corrections for nominations the automatic linker cannot resolve.
OVERRIDES_CSV = "rising_star_name_overrides.csv"
INTEGER_FIELDS = {
    "season", "round_number", "kicks", "handballs", "disposals", "marks",
    "goals", "behinds", "tackles", "hitouts", "frees_for", "frees_against",
    "supercoach", "afl_fantasy", "is_season_winner",
}
SOURCE_FIELDS = [
    "source_key", "season", "nomination_round", "round_number", "player",
    "player_display", "name_key", "club", "team_display", "team_slug",
    "opponent", "opponent_display", "opponent_slug", "kicks", "handballs",
    "disposals", "marks", "goals", "behinds", "tackles", "hitouts",
    "frees_for", "frees_against", "supercoach", "afl_fantasy",
    "unavailable_stats", "is_season_winner", "winner_name", "winner_team", "player_url",
    "source_url", "scraped_at",
]


def normalise_name(value: object) -> str:
    try:
        from names import normalise_name as project_normalise
    except ImportError:
        text = unicodedata.normalize("NFKD", str(value or "")).casefold()
        text = "".join(c for c in text if not unicodedata.combining(c))
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()
    return project_normalise(str(value or ""))


def _int(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def read_rows(paths: Iterable[Path]) -> list[dict]:
    rows: dict[str, dict] = {}
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(path)
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = {"source_key", "season", "player", "club"} - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"{path}: missing columns {sorted(missing)}")
            for raw in reader:
                row = {field: raw.get(field, "") for field in SOURCE_FIELDS}
                for field in INTEGER_FIELDS:
                    row[field] = _int(row.get(field))
                row["name_key"] = row.get("name_key") or normalise_name(row.get("player"))
                key = str(row.get("source_key") or "").strip()
                if not key:
                    raise ValueError(f"{path}: row has no source_key")
                rows[key] = row
    return list(rows.values())


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def loose_name_key(value: object) -> str:
    """Punctuation-insensitive key for URL slugs such as O'Meara/C-Collins."""
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def name_signature(value: object) -> tuple[str, str] | None:
    """Return first initial plus the compact remainder of the name."""
    tokens = re.findall(r"[a-z0-9]+", loose_name_key(value))
    if len(tokens) < 2:
        return None
    return tokens[0][0], "".join(tokens[1:])


def _candidate_rows(con: sqlite3.Connection, name_key: str,
                    source_player: str) -> tuple[list[tuple], str]:
    exact = con.execute(
        "SELECT player_id, player, debut_season, final_season "
        "FROM players WHERE name_key = ? ORDER BY career_games DESC",
        (name_key,),
    ).fetchall()
    if exact:
        return exact, "exact_name"

    # FootyWire player URLs omit punctuation: Che Cockatoo-Collins becomes
    # ``che-cockatoo-collins`` and O'Meara becomes ``omeara``.
    wanted = loose_name_key(source_player)
    loose = [row for row in con.execute(
        "SELECT player_id, player, debut_season, final_season "
        "FROM players ORDER BY career_games DESC"
    ).fetchall() if loose_name_key(row[1]) == wanted]
    if loose:
        return loose, "punctuation_name"
    return [], "none"


def _played_for(con: sqlite3.Connection, player_id: int, season: int, club: str) -> bool:
    return con.execute(
        "SELECT 1 FROM games WHERE player_id=? AND season=? "
        "AND (club_now=? OR club_hist=?) LIMIT 1",
        (player_id, season, club, club),
    ).fetchone() is not None


def _signature_candidates(con: sqlite3.Connection, row: dict) -> list[tuple]:
    """Conservative fallback limited to the nominated club and season."""
    season = int(row["season"])
    club = str(row.get("club") or "").strip()
    if not club:
        return []

    wanted = {
        signature for signature in (
            name_signature(row.get("player")),
            name_signature(row.get("player_display")),
        ) if signature is not None
    }
    if not wanted:
        return []

    candidates = con.execute(
        "SELECT DISTINCT p.player_id, p.player, p.debut_season, p.final_season "
        "FROM players p JOIN games g ON g.player_id=p.player_id "
        "WHERE g.season=? AND (g.club_now=? OR g.club_hist=?) "
        "ORDER BY p.career_games DESC",
        (season, club, club),
    ).fetchall()
    return [candidate for candidate in candidates
            if name_signature(candidate[1]) in wanted]


def resolve_row(con: sqlite3.Connection, row: dict) -> tuple[int | None, str, int, str, str, str]:
    season = int(row["season"])
    club = str(row.get("club") or "").strip()
    candidates, method = _candidate_rows(
        con, str(row["name_key"]), str(row.get("player") or "")
    )

    if not candidates:
        candidates = _signature_candidates(con, row)
        method = "initial_surname_season_club" if candidates else "none"

    count = len(candidates)
    if not candidates:
        return None, "unmatched", 0, "no safe player match", "", method

    active = [c for c in candidates if c[2] <= season <= c[3]]
    active_club = [c for c in active if _played_for(con, c[0], season, club)]

    if len(active_club) == 1:
        candidate = active_club[0]
        status = "unique" if method == "exact_name" and count == 1 else "resolved"
        note = {
            "exact_name": "matched exact normalised name, season and club",
            "punctuation_name": "matched punctuation-insensitive name, season and club",
            "initial_surname_season_club": (
                "matched first initial and surname within the nominated club and season"
            ),
        }.get(method, "matched name, season and club")
        return candidate[0], status, count, note, candidate[1], method

    if len(active) == 1:
        candidate = active[0]
        return candidate[0], "club_mismatch", count, (
            f"name and season match {candidate[1]}, but {club!r} does not match local game rows"
        ), candidate[1], method

    if count == 1:
        candidate = candidates[0]
        return None, "implausible", count, (
            f"only name match has career {candidate[2]}-{candidate[3]}, "
            f"outside nomination season {season}"
        ), candidate[1], method
    if len(active_club) > 1:
        return None, "ambiguous", count, (
            "multiple players share the fallback signature in the nominated club and season"
        ), "", method
    if len(active) > 1:
        return None, "ambiguous", count, "multiple players match name and season", "", method
    return None, "ambiguous", count, "multiple name matches; none fit the season", "", method


def default_overrides_path() -> Path:
    """Location of the reviewed correction file, if the layout provides one."""
    try:
        from data_paths import raw_dir
    except ImportError:
        return Path("data/afl/reference") / OVERRIDES_CSV
    return raw_dir("afl").parent / "reference" / OVERRIDES_CSV


def apply_reviewed_overrides(con: sqlite3.Connection, linked: list[dict],
                             path: str | Path | None,
                             verbose: bool = True) -> int:
    """Apply reviewed corrections to untrusted rows.  Never raises.

    The override layer is optional: a missing file, a missing module or a bad
    reference row must not break an otherwise valid import.
    """
    if path is None:
        return 0
    path = Path(path)
    if not path.exists():
        return 0
    try:
        import rising_star_overrides as overrides_module
    except ImportError:
        if verbose:
            print("  (rising_star_overrides.py not found; corrections skipped)")
        return 0
    try:
        overrides = overrides_module.load_overrides(path)
        if not overrides:
            return 0
        applied = overrides_module.apply_overrides(
            con, linked, overrides, verbose=verbose
        )
        if verbose and applied:
            print(f"  reviewed overrides applied: {applied} (from {path})")
        return applied
    except Exception as exc:  # pragma: no cover - defensive
        if verbose:
            print(f"  (reviewed overrides skipped: {type(exc).__name__}: {exc})")
        return 0


SCHEMA_SQL = """
CREATE TABLE rising_star_nominees (
    source_key TEXT PRIMARY KEY,
    season INTEGER NOT NULL,
    nomination_round TEXT,
    round_number INTEGER,
    player TEXT NOT NULL,
    player_display TEXT,
    name_key TEXT NOT NULL,
    club TEXT,
    team_display TEXT,
    team_slug TEXT,
    opponent TEXT,
    opponent_display TEXT,
    opponent_slug TEXT,
    kicks INTEGER,
    handballs INTEGER,
    disposals INTEGER,
    marks INTEGER,
    goals INTEGER,
    behinds INTEGER,
    tackles INTEGER,
    hitouts INTEGER,
    frees_for INTEGER,
    frees_against INTEGER,
    supercoach INTEGER,
    afl_fantasy INTEGER,
    unavailable_stats TEXT,
    is_season_winner INTEGER NOT NULL DEFAULT 0,
    winner_name TEXT,
    winner_team TEXT,
    player_url TEXT,
    source_url TEXT NOT NULL,
    scraped_at TEXT,
    player_id INTEGER,
    matched_player TEXT,
    match_method TEXT,
    match_status TEXT NOT NULL,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    imported_at TEXT NOT NULL
)
"""


def load_sources(db_path: str | Path, sources: Iterable[str | Path],
                 verbose: bool = True,
                 overrides: str | Path | None = "default") -> dict[str, int]:
    """Load, link and store Rising Star nominations.

    ``overrides`` accepts an explicit reference-CSV path, ``"default"`` to use
    :func:`default_overrides_path`, or ``None`` to disable corrections.
    """
    paths = [Path(path) for path in sources]
    rows = read_rows(paths)
    if not rows:
        raise ValueError("no Rising Star rows found")

    con = sqlite3.connect(str(db_path))
    try:
        if not _table_exists(con, "players") or not _table_exists(con, "games"):
            raise RuntimeError("database needs players and games tables; run build_db.py first")

        imported_at = dt.datetime.now(dt.timezone.utc).isoformat()
        linked = []
        for row in rows:
            player_id, status, candidate_count, notes, matched_player, match_method = resolve_row(con, row)
            linked.append({
                **row,
                "player_id": player_id,
                "matched_player": matched_player,
                "match_method": match_method,
                "match_status": status,
                "candidate_count": candidate_count,
                "notes": notes,
                "imported_at": imported_at,
            })

        # Reviewed corrections are auditable data, not hardcoded logic.  They
        # run after automatic resolution and only touch untrusted rows.
        overrides_path = (default_overrides_path() if overrides == "default"
                          else overrides)
        apply_reviewed_overrides(con, linked, overrides_path, verbose=verbose)

        con.execute("DROP TABLE IF EXISTS rising_star_nominees_new")
        con.execute(SCHEMA_SQL.replace(
            "CREATE TABLE rising_star_nominees",
            "CREATE TABLE rising_star_nominees_new",
            1,
        ))
        columns = SOURCE_FIELDS + [
            "player_id", "matched_player", "match_method", "match_status",
            "candidate_count", "notes", "imported_at"
        ]
        marks = ",".join("?" for _ in columns)
        con.executemany(
            f"INSERT INTO rising_star_nominees_new ({','.join(columns)}) VALUES ({marks})",
            [tuple(row.get(column) for column in columns) for row in linked],
        )
        con.execute("DROP TABLE IF EXISTS rising_star_nominees")
        con.execute("ALTER TABLE rising_star_nominees_new RENAME TO rising_star_nominees")
        for statement in [
            "CREATE INDEX ix_rs_player ON rising_star_nominees(player_id)",
            "CREATE INDEX ix_rs_season ON rising_star_nominees(season, round_number)",
            "CREATE INDEX ix_rs_club ON rising_star_nominees(club, season)",
            "CREATE INDEX ix_rs_status ON rising_star_nominees(match_status)",
            "CREATE INDEX ix_rs_name ON rising_star_nominees(name_key)",
        ]:
            con.execute(statement)
        if _table_exists(con, "meta"):
            con.execute("DELETE FROM meta WHERE key='rising_star_imported'")
            con.execute("INSERT INTO meta VALUES ('rising_star_imported', datetime('now'))")
        con.commit()

        counts = dict(con.execute(
            "SELECT match_status, COUNT(*) FROM rising_star_nominees GROUP BY match_status"
        ).fetchall())
        result = {
            "rows": len(linked),
            "trusted": sum(counts.get(status, 0) for status in TRUSTED),
            **counts,
        }
        if verbose:
            print(f"Loaded {len(linked):,} Rising Star nomination rows into {db_path}")
            for status in ("unique", "resolved", "club_mismatch", "ambiguous", "unmatched", "implausible"):
                if counts.get(status):
                    print(f"  {status:<13} {counts[status]:>5,}")
            methods = con.execute(
                "SELECT match_method, COUNT(*) FROM rising_star_nominees "
                "WHERE match_status IN ('unique','resolved') GROUP BY match_method"
            ).fetchall()
            fallback_count = sum(count for method, count in methods
                                 if method not in {"exact_name", "reviewed_override"})
            override_count = sum(count for method, count in methods
                                 if method == "reviewed_override")
            if fallback_count:
                print(f"  safe fallbacks {fallback_count:>5,}")
            if override_count:
                print(f"  reviewed overrides {override_count:>1,}")
            print(f"Trusted by search/solver: {result['trusted']:,}")
        return result
    finally:
        con.close()


def default_sources() -> list[Path]:
    try:
        from data_paths import rising_star_sources
    except ImportError:
        base = Path("data/afl/raw/footywire/rising_star")
        combined = base / "rising_star_nominees.csv"
        if combined.exists():
            return [combined]
        return sorted((base / "csv").glob("rising_star_nominees_*.csv"))
    return rising_star_sources("afl")


def default_db() -> str:
    try:
        from data_paths import sport_db
    except ImportError:      # data_paths sits beside this file; near-dead path
        from pathlib import Path
        return str(Path(__file__).resolve().parent / "gridley.db")
    return sport_db("afl", "gridley.db")


def refresh_default(db_path: str | None = None,
                    verbose: bool = True) -> dict[str, int] | None:
    sources = default_sources()
    if not sources:
        if verbose:
            print("Rising Star refresh skipped: no source CSVs found")
        return None
    return load_sources(db_path or default_db(), sources, verbose=verbose)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=default_db())
    parser.add_argument("--source", action="append", type=Path,
                        help="CSV source; repeat for multiple files")
    parser.add_argument("--report", action="store_true",
                        help="show unresolved rows after loading")
    parser.add_argument("--overrides", type=Path,
                        help="reviewed correction CSV "
                             f"(default: {default_overrides_path()})")
    parser.add_argument("--no-overrides", action="store_true",
                        help="ignore the reviewed correction file")
    args = parser.parse_args(argv)
    sources = args.source or default_sources()
    if not sources:
        print("No Rising Star CSV found. Run fetch_footywire_rising_star.py first.",
              file=sys.stderr)
        return 1
    overrides: str | Path | None = "default"
    if args.no_overrides:
        overrides = None
    elif args.overrides is not None:
        overrides = args.overrides
    result = load_sources(args.db, sources, verbose=True, overrides=overrides)
    if args.report:
        con = sqlite3.connect(args.db)
        try:
            unresolved = con.execute(
                "SELECT season, nomination_round, player, matched_player, club, "
                "match_method, match_status, notes "
                "FROM rising_star_nominees WHERE match_status NOT IN ('unique','resolved') "
                "ORDER BY season, round_number"
            ).fetchall()
            if unresolved:
                print("\nUnresolved rows:")
                for row in unresolved:
                    print("  ", row)
            else:
                print("\nAll rows resolved.")
        finally:
            con.close()
    return 0 if result.get("trusted", 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
