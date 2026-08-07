#!/usr/bin/env python3
"""Import and conservatively link Wikipedia family-draft records.

The source contains two related datasets:

* AFL father-son selections; and
* AFLW father-daughter selections.

Both source people are linked independently against the AFL ``players`` table.
AFLW children are intentionally marked ``out_of_scope`` because this database
contains men's VFL/AFL players, while their fathers can still be linked.
Ambiguous and unmatched names are retained for audit and excluded from all
constraints.

Examples:
    python -m utils.afl.load_family_draft --inspect
    python -m utils.afl.load_family_draft --db gridley.db
    python -m utils.afl.load_family_draft --db gridley.db --report --details
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import hashlib
import os
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

try:
    from data_paths import default_db, family_draft_sources
except ImportError:  # bundle-only validation before installation
    def default_db(sport_key: str) -> str:
        root = Path(__file__).resolve().parents[2]
        modern = root / "data" / sport_key / f"{sport_key}.db"
        return str(modern if modern.exists() else root / "gridley.db")

    def family_draft_sources(sport_key: str = "afl") -> list[Path]:
        base = Path("data") / sport_key / "raw"
        canonical = base / "wikipedia_family_draft.csv"
        if canonical.exists():
            return [canonical]
        fallback = base / "family_draft.csv"
        return [fallback] if fallback.exists() else []

REQUIRED_COLUMNS = {
    "competition", "rule", "year", "drafted_player", "club", "father"
}
TRUSTED_STATUSES = {"unique", "resolved"}
LINK_STATUSES = ("unique", "resolved", "ambiguous", "unmatched", "out_of_scope")

# Source/database display-name differences reviewed for this dataset.  The
# aliases only nominate a name key; temporal and club evidence still decides
# whether the link is safe.
NAME_ALIASES = {
    "erniehug": "Ernie Hug",
    "garyablett": "Gary Ablett",
    "mauricerioli": "Maurice Rioli",
    "alwyndavey": "Alwyn Davey",
    "billybrownless": "Bill Brownless",
}

_SUFFIX_RE = re.compile(r"\b(?:jr|jnr|junior|sr|snr|senior|ii|iii|iv)\.?\s*$", re.I)


def clean_text(value: object) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip()


def _ascii_words(value: object) -> list[str]:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("’", "'").replace("`", "'")
    text = _SUFFIX_RE.sub(" ", text)
    return re.findall(r"[A-Za-z0-9]+", text.lower())


def identity_key(value: object) -> str:
    """Person key tolerant of punctuation and generational suffixes."""
    return "".join(_ascii_words(value))


def relaxed_identity_key(value: object) -> str:
    """Also ignore single-letter middle initials when exact matching fails."""
    words = _ascii_words(value)
    if len(words) > 2:
        words = [word for i, word in enumerate(words)
                 if not (0 < i < len(words) - 1 and len(word) == 1)]
    return "".join(words)


def text_key(value: object) -> str:
    return " ".join(_ascii_words(value))


def optional_int(value: object) -> int | None:
    text = clean_text(value).replace(",", "")
    if not text:
        return None
    match = re.search(r"-?\d+", text)
    return int(match.group(0)) if match else None


def bool_int(value: object) -> int:
    return int(clean_text(value).casefold() in {"1", "true", "yes", "y", "^", "*"})


def _source_row_id(row: dict) -> str:
    parts = (
        row["competition"], row["rule"], str(row["draft_year"]),
        identity_key(row["drafted_player"]), text_key(row["club"]),
        identity_key(row["father"]), row.get("selection_raw", ""),
        str(row.get("source_revision_id") or ""), row.get("source_url", ""),
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def read_csvs(paths: list[str | Path]) -> list[dict]:
    """Read canonical scraper CSVs and collapse duplicate copies."""
    rows: list[dict] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = Path(raw_path)
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise ValueError(f"{path}: no header row")
            missing = REQUIRED_COLUMNS - set(reader.fieldnames)
            if missing:
                raise ValueError(
                    f"{path}: missing required columns: {', '.join(sorted(missing))}"
                )
            for line_no, source in enumerate(reader, start=2):
                competition = clean_text(source.get("competition")).upper()
                rule = clean_text(source.get("rule")).lower()
                draft_year = optional_int(source.get("year"))
                drafted_player = clean_text(source.get("drafted_player"))
                club = clean_text(source.get("club"))
                father = clean_text(source.get("father"))
                if competition not in {"AFL", "AFLW"}:
                    raise ValueError(
                        f"{path}:{line_no}: unsupported competition {competition!r}"
                    )
                expected_rule = "father-son" if competition == "AFL" else "father-daughter"
                if rule != expected_rule:
                    raise ValueError(
                        f"{path}:{line_no}: {competition} row has rule {rule!r}"
                    )
                if draft_year is None or not drafted_player or not club or not father:
                    raise ValueError(
                        f"{path}:{line_no}: year/player/club/father cannot be blank"
                    )

                row = {
                    "competition": competition,
                    "rule": rule,
                    "draft_year": draft_year,
                    "drafted_player": drafted_player,
                    "drafted_player_wikipedia_url": clean_text(
                        source.get("drafted_player_wikipedia_url")
                    ),
                    "club": club,
                    "club_wikipedia_url": clean_text(source.get("club_wikipedia_url")),
                    "father": father,
                    "father_wikipedia_url": clean_text(
                        source.get("father_wikipedia_url")
                    ),
                    "selection_raw": clean_text(source.get("selection_raw")),
                    "selection_pick": optional_int(source.get("selection_pick")),
                    "selection_note": clean_text(source.get("selection_note")),
                    "games_played": optional_int(source.get("games_played")),
                    "father_games_raw": clean_text(source.get("father_games_raw")),
                    "father_games_played": optional_int(
                        source.get("father_games_played")
                    ),
                    "father_games_note": clean_text(source.get("father_games_note")),
                    "current_player": bool_int(source.get("current_player")),
                    "changed_team": bool_int(source.get("changed_team")),
                    "status_marker": clean_text(source.get("status_marker")),
                    "source_url": clean_text(source.get("source_url")),
                    "source_revision_id": optional_int(
                        source.get("source_revision_id")
                    ),
                    "scraped_at_utc": clean_text(source.get("scraped_at_utc")),
                    "source_name": path.name,
                }
                row["source_row_id"] = _source_row_id(row)
                if row["source_row_id"] in seen:
                    continue
                seen.add(row["source_row_id"])
                rows.append(row)
    return rows


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def load_reference_maps(con: sqlite3.Connection):
    required = {
        "player_id", "player", "debut_season", "final_season",
        "career_games", "clubs_hist", "clubs_now",
    }
    missing = required - table_columns(con, "players")
    if missing:
        raise RuntimeError(f"players table lacks {sorted(missing)}")

    exact: dict[str, set[int]] = defaultdict(set)
    relaxed: dict[str, set[int]] = defaultdict(set)
    player_names: dict[int, str] = {}
    spans: dict[int, tuple[int, int]] = {}
    career_games: dict[int, int] = {}
    player_clubs: dict[int, set[str]] = defaultdict(set)
    club_games: dict[int, Counter[str]] = defaultdict(Counter)

    for pid, name, debut, final, games, hist, current in con.execute("""
        SELECT player_id, player, debut_season, final_season, career_games,
               clubs_hist, clubs_now
        FROM players
    """):
        pid = int(pid)
        exact[identity_key(name)].add(pid)
        relaxed[relaxed_identity_key(name)].add(pid)
        player_names[pid] = str(name)
        if debut is not None and final is not None:
            spans[pid] = (int(debut), int(final))
        if games is not None:
            career_games[pid] = int(games)
        for value in (hist, current):
            for club in str(value or "").split("|"):
                if club:
                    player_clubs[pid].add(text_key(club))

    # Games is the authoritative club membership evidence.  Keep players-table
    # paths as a fallback for zero-row fixtures and older database builds.
    if {"player_id", "club_now", "club_hist"} <= table_columns(con, "games"):
        # One row is one senior appearance. Count each appearance once for
        # every distinct historical/current club identity represented by it.
        # This supports source values such as Brian Walsh's 64 Carlton games
        # even though his whole VFL career total is 115.
        for pid, current, historical in con.execute(
            "SELECT player_id, club_now, club_hist FROM games"
        ):
            pid = int(pid)
            clubs = {text_key(club) for club in (current, historical) if club}
            for club in clubs:
                player_clubs[pid].add(club)
                club_games[pid][club] += 1

    return (exact, relaxed, player_names, spans, career_games,
            player_clubs, club_games)


def _candidate_set(name: str, refs):
    exact, relaxed = refs[0], refs[1]
    key = identity_key(name)
    alias = NAME_ALIASES.get(key)
    if alias:
        aliased = set(exact.get(identity_key(alias), set()))
        if aliased:
            return aliased, "reviewed name alias"
    direct = set(exact.get(key, set()))
    if direct:
        return direct, "exact normalised identity"
    loose = set(relaxed.get(relaxed_identity_key(name), set()))
    return loose, "identity with middle initials ignored" if loose else "identity not found"


def _format_candidates(candidates: list[int], refs) -> str:
    names = refs[2]
    return ", ".join(names[pid] for pid in candidates[:8])


def _resolve_child(row: dict, refs):
    if row["competition"] != "AFL":
        return None, "out_of_scope", [], "AFLW child is outside the men's AFL players table"

    # ``players`` is built from senior game rows. A retired/non-current source
    # record with an explicit zero-game total cannot honestly resolve to any
    # player_id; a same-name match would be a different AFL player. Current
    # players are exempt because the source count can lag a newer local season.
    if row.get("games_played") == 0 and not row.get("current_player"):
        return (None, "unmatched", [],
                "source records zero senior AFL games; no player-game identity exists")

    candidates, method = _candidate_set(row["drafted_player"], refs)
    spans, player_clubs = refs[3], refs[5]
    year = row["draft_year"]
    club = text_key(row["club"])

    plausible = {
        pid for pid in candidates
        if pid in spans and year - 2 <= spans[pid][0] <= year + 12
        and spans[pid][1] >= year
    }
    same_club = {pid for pid in plausible if club in player_clubs.get(pid, set())}

    if len(same_club) == 1:
        pid = next(iter(same_club))
        status = "unique" if method == "exact normalised identity" and len(candidates) == 1 else "resolved"
        return pid, status, sorted(same_club), method + " + draft-era club career"
    if len(same_club) > 1:
        return None, "ambiguous", sorted(same_club), method + ": multiple draft-era club matches"
    if len(plausible) == 1:
        pid = next(iter(plausible))
        status = "unique" if method == "exact normalised identity" and len(candidates) == 1 else "resolved"
        return pid, status, sorted(plausible), method + " + draft-era career window"
    if len(plausible) > 1:
        return None, "ambiguous", sorted(plausible), method + ": multiple draft-era identities"

    if candidates:
        return None, "unmatched", sorted(candidates), method + ": name found but draft-era evidence does not match"
    return None, "unmatched", [], method


def _resolve_father(row: dict, refs):
    candidates, method = _candidate_set(row["father"], refs)
    spans, career_games, club_games = refs[3], refs[4], refs[6]
    year = row["draft_year"]

    # A father need not have played for the drafting club.  The defensible
    # evidence is generational: an AFL career beginning well before the child
    # was drafted and normally complete by then.  A five-year final-season
    # tolerance covers unusual late careers without allowing the child record
    # to resolve back to itself.
    plausible = {
        pid for pid in candidates
        if pid in spans and spans[pid][0] <= year - 12 and spans[pid][1] <= year + 5
    }

    if len(plausible) == 1:
        pid = next(iter(plausible))
        status = "unique" if method == "exact normalised identity" and len(candidates) == 1 else "resolved"
        return pid, status, sorted(plausible), method + " + parent-generation career window"

    # Wikipedia publishes the father's game total.  It is strong
    # disambiguation evidence when two or more same-name AFL identities pass
    # the generation window.  Use it only when exactly one candidate matches;
    # state-league totals and stale source values therefore cannot force a
    # questionable link.
    source_games = row.get("father_games_played")
    if len(plausible) > 1 and source_games is not None:
        source_games = int(source_games)

        # The source value is generally the games establishing eligibility
        # for the drafting club, not necessarily the father's career total.
        # Prefer an exact per-club match when it identifies one candidate.
        source_club = text_key(row.get("club"))
        club_game_matches = {
            pid for pid in plausible
            if source_club
            and club_games.get(pid, {}).get(source_club) == source_games
        }
        if len(club_game_matches) == 1:
            pid = next(iter(club_game_matches))
            return (pid, "resolved", sorted(club_game_matches),
                    method + " + parent-generation career window "
                    "+ source qualifying-club game total")
        if len(club_game_matches) > 1:
            return (None, "ambiguous", sorted(club_game_matches),
                    method + ": multiple parent-generation identities "
                    "share the source qualifying-club game total")

        # Retain the previous whole-career fallback for one-club fathers and
        # records where the source count is a career total.
        game_matches = {
            pid for pid in plausible
            if career_games.get(pid) == source_games
        }
        if len(game_matches) == 1:
            pid = next(iter(game_matches))
            return (pid, "resolved", sorted(game_matches),
                    method + " + parent-generation career window "
                    "+ source father-game total")
        if len(game_matches) > 1:
            return (None, "ambiguous", sorted(game_matches),
                    method + ": multiple parent-generation identities "
                    "share the source father-game total")

    if len(plausible) > 1:
        return None, "ambiguous", sorted(plausible), method + ": multiple parent-generation identities"
    if candidates:
        return None, "unmatched", sorted(candidates), method + ": name found but parent-generation evidence does not match"
    return None, "unmatched", [], method


FAMILY_COLUMNS_SQL = """
    relationship_id INTEGER PRIMARY KEY,
    source_row_id TEXT NOT NULL UNIQUE,
    competition TEXT NOT NULL,
    rule TEXT NOT NULL,
    draft_year INTEGER NOT NULL,
    drafted_player TEXT NOT NULL,
    drafted_player_wikipedia_url TEXT NOT NULL DEFAULT '',
    club TEXT NOT NULL,
    club_wikipedia_url TEXT NOT NULL DEFAULT '',
    father TEXT NOT NULL,
    father_wikipedia_url TEXT NOT NULL DEFAULT '',
    selection_raw TEXT NOT NULL DEFAULT '',
    selection_pick INTEGER,
    selection_note TEXT NOT NULL DEFAULT '',
    games_played INTEGER,
    father_games_raw TEXT NOT NULL DEFAULT '',
    father_games_played INTEGER,
    father_games_note TEXT NOT NULL DEFAULT '',
    current_player INTEGER NOT NULL DEFAULT 0,
    changed_team INTEGER NOT NULL DEFAULT 0,
    status_marker TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    source_revision_id INTEGER,
    scraped_at_utc TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL,
    drafted_player_id INTEGER,
    drafted_player_match_status TEXT NOT NULL,
    drafted_player_candidate_count INTEGER NOT NULL,
    drafted_player_notes TEXT,
    father_player_id INTEGER,
    father_match_status TEXT NOT NULL,
    father_candidate_count INTEGER NOT NULL,
    father_notes TEXT,
    imported_at TEXT NOT NULL
"""


def _prepare_stage(con: sqlite3.Connection) -> None:
    con.execute("DROP TABLE IF EXISTS family_draft_import")
    con.execute(f"CREATE TABLE family_draft_import ({FAMILY_COLUMNS_SQL})")


def _publish_stage(con: sqlite3.Connection) -> None:
    con.execute("DROP TABLE IF EXISTS family_draft")
    con.execute("ALTER TABLE family_draft_import RENAME TO family_draft")
    for statement in (
        "CREATE INDEX idx_family_draft_child "
        "ON family_draft(drafted_player_id, drafted_player_match_status)",
        "CREATE INDEX idx_family_draft_father "
        "ON family_draft(father_player_id, father_match_status)",
        "CREATE INDEX idx_family_draft_club_year "
        "ON family_draft(club, draft_year)",
        "CREATE INDEX idx_family_draft_rule "
        "ON family_draft(competition, rule)",
    ):
        con.execute(statement)


def import_rows(con: sqlite3.Connection, rows: list[dict]) -> Counter:
    """Resolve both people and atomically replace ``family_draft``."""
    refs = load_reference_maps(con)
    player_names = refs[2]
    imported_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    totals: Counter = Counter()

    try:
        con.execute("SAVEPOINT family_draft_import")
        _prepare_stage(con)
        for row in rows:
            child_id, child_status, child_candidates, child_notes = _resolve_child(row, refs)
            father_id, father_status, father_candidates, father_notes = _resolve_father(row, refs)
            if child_id is not None:
                child_notes += f" -> {player_names[child_id]}"
            elif child_candidates:
                child_notes += ": " + _format_candidates(child_candidates, refs)
            if father_id is not None:
                father_notes += f" -> {player_names[father_id]}"
            elif father_candidates:
                father_notes += ": " + _format_candidates(father_candidates, refs)

            con.execute("""
                INSERT INTO family_draft_import (
                    source_row_id, competition, rule, draft_year,
                    drafted_player, drafted_player_wikipedia_url,
                    club, club_wikipedia_url, father, father_wikipedia_url,
                    selection_raw, selection_pick, selection_note, games_played,
                    father_games_raw, father_games_played, father_games_note,
                    current_player, changed_team, status_marker, source_url,
                    source_revision_id, scraped_at_utc, source_name,
                    drafted_player_id, drafted_player_match_status,
                    drafted_player_candidate_count, drafted_player_notes,
                    father_player_id, father_match_status,
                    father_candidate_count, father_notes, imported_at
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
            """, (
                row["source_row_id"], row["competition"], row["rule"],
                row["draft_year"], row["drafted_player"],
                row["drafted_player_wikipedia_url"], row["club"],
                row["club_wikipedia_url"], row["father"],
                row["father_wikipedia_url"], row["selection_raw"],
                row["selection_pick"], row["selection_note"],
                row["games_played"], row["father_games_raw"],
                row["father_games_played"], row["father_games_note"],
                row["current_player"], row["changed_team"],
                row["status_marker"], row["source_url"],
                row["source_revision_id"], row["scraped_at_utc"],
                row["source_name"], child_id, child_status,
                len(child_candidates), child_notes, father_id, father_status,
                len(father_candidates), father_notes, imported_at,
            ))
            totals[f"child_{child_status}"] += 1
            totals[f"father_{father_status}"] += 1

        _publish_stage(con)
        con.execute("RELEASE SAVEPOINT family_draft_import")
    except Exception:
        con.execute("ROLLBACK TO SAVEPOINT family_draft_import")
        con.execute("RELEASE SAVEPOINT family_draft_import")
        raise
    return totals


def inspect(rows: list[dict]) -> None:
    print(f"Rows: {len(rows):,}")
    for competition in ("AFL", "AFLW"):
        subset = [row for row in rows if row["competition"] == competition]
        if subset:
            years = [row["draft_year"] for row in subset]
            print(f"  {competition}: {len(subset):,} rows, {min(years)}-{max(years)}")
    print(f"Drafted people: {len({identity_key(row['drafted_player']) for row in rows}):,}")
    print(f"Fathers: {len({identity_key(row['father']) for row in rows}):,}")


def report(con: sqlite3.Connection, details: bool = False) -> None:
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='family_draft'"
    ).fetchone()
    if not exists:
        print("family_draft table does not exist")
        return

    total = con.execute("SELECT COUNT(*) FROM family_draft").fetchone()[0]
    print(f"Family-draft link report ({total:,} source rows)")
    print("  Drafted-player links:")
    for status, count in con.execute("""
        SELECT drafted_player_match_status, COUNT(*) FROM family_draft
        GROUP BY drafted_player_match_status ORDER BY COUNT(*) DESC
    """):
        print(f"    {status:<16} {count:>6,}")
    print("  Father links:")
    for status, count in con.execute("""
        SELECT father_match_status, COUNT(*) FROM family_draft
        GROUP BY father_match_status ORDER BY COUNT(*) DESC
    """):
        print(f"    {status:<16} {count:>6,}")

    trusted_pairs = con.execute("""
        SELECT COUNT(*) FROM family_draft
        WHERE competition='AFL'
          AND drafted_player_match_status IN ('unique','resolved')
          AND father_match_status IN ('unique','resolved')
          AND drafted_player_id IS NOT NULL AND father_player_id IS NOT NULL
    """).fetchone()[0]
    print(f"  {'trusted AFL pairs':<20} {trusted_pairs:>6,}")

    if not details:
        return
    unresolved = con.execute("""
        SELECT competition, draft_year, drafted_player, father,
               drafted_player_match_status, father_match_status,
               drafted_player_notes, father_notes
        FROM family_draft
        WHERE (competition='AFL' AND drafted_player_match_status NOT IN ('unique','resolved'))
           OR father_match_status NOT IN ('unique','resolved')
        ORDER BY competition, draft_year, drafted_player
    """).fetchall()
    print(f"\nRows needing review: {len(unresolved):,}")
    for comp, year, child, father, cs, fs, cn, fn in unresolved:
        print(f"  {comp} {year} | {child} <- {father}")
        if comp == "AFL" and cs not in TRUSTED_STATUSES:
            print(f"    child  {cs}: {cn}")
        if fs not in TRUSTED_STATUSES:
            print(f"    father {fs}: {fn}")


def suggest(con: sqlite3.Connection, limit: int = 5) -> None:
    """Show close database names for unmatched AFL people without linking."""
    player_rows = con.execute(
        "SELECT player, debut_season, final_season FROM players"
    ).fetchall()
    unresolved = con.execute("""
        SELECT 'child', draft_year, drafted_player FROM family_draft
        WHERE competition='AFL' AND drafted_player_match_status='unmatched'
        UNION ALL
        SELECT 'father', draft_year, father FROM family_draft
        WHERE father_match_status='unmatched'
        ORDER BY 1, 2, 3
    """).fetchall()
    for role, year, source_name in unresolved:
        key = relaxed_identity_key(source_name)
        candidates = []
        for name, debut, final in player_rows:
            score = difflib.SequenceMatcher(
                None, key, relaxed_identity_key(name)
            ).ratio()
            if score >= 0.65:
                candidates.append((score, name, debut, final))
        candidates.sort(reverse=True)
        text = ", ".join(
            f"{name} ({debut}-{final}, {score:.2f})"
            for score, name, debut, final in candidates[:limit]
        ) or "no close names"
        print(f"{role:<6} {year} | {source_name} -> {text}")


def _default_sources() -> list[Path]:
    paths = family_draft_sources("afl")
    if not paths:
        raise FileNotFoundError(
            "no family-draft CSV found; run afl/scrape_wikipedia_family_draft.py"
        )
    return paths


def refresh_default(db_path: str | None = None, verbose: bool = True):
    rows = read_csvs(_default_sources())
    con = sqlite3.connect(db_path or default_db("afl"))
    try:
        totals = import_rows(con, rows)
    finally:
        con.close()
    if verbose:
        print(f"Imported {len(rows):,} family-draft rows")
        for side in ("child", "father"):
            print(f"  {side} links:")
            for status in LINK_STATUSES:
                count = totals[f"{side}_{status}"]
                if count:
                    print(f"    {status:<16} {count:>6,}")
    return totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", nargs="*", help="family-draft CSV files")
    parser.add_argument("--db", default=default_db("afl"))
    parser.add_argument("--inspect", action="store_true", help="validate CSV only")
    parser.add_argument("--report", action="store_true", help="show current link counts")
    parser.add_argument("--details", action="store_true", help="show unresolved rows")
    parser.add_argument("--suggest", action="store_true", help="suggest close names")
    args = parser.parse_args(argv)

    try:
        if args.report or args.suggest:
            con = sqlite3.connect(args.db)
            try:
                if args.report:
                    report(con, details=args.details)
                if args.suggest:
                    suggest(con)
            finally:
                con.close()
            return 0

        paths = [Path(path) for path in args.csv] or _default_sources()
        rows = read_csvs(paths)
        if args.inspect:
            inspect(rows)
            return 0
        if not os.path.exists(args.db):
            raise FileNotFoundError(f"database not found: {args.db}")

        con = sqlite3.connect(args.db)
        try:
            totals = import_rows(con, rows)
        finally:
            con.close()
        print(f"Imported {len(rows):,} family-draft rows into {args.db}")
        for side in ("child", "father"):
            print(f"  {side} links:")
            for status in LINK_STATUSES:
                print(f"    {status:<16} {totals[f'{side}_{status}']:>6,}")
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
