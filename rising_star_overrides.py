#!/usr/bin/env python3
"""Apply reviewed Rising Star nominee corrections from a reference CSV.

This keeps historical corrections out of Python and in an auditable file
(``data/afl/reference/rising_star_name_overrides.csv``).  An override is
applied to a nominee row only when it identifies exactly one real local
player, so a typo in the reference file can never silently trust the wrong
person -- it simply fails to apply and the row stays untrusted.

Reference columns (extra columns are ignored)::

    source_name        nominee name as it appears in the source CSV
    source_club        source club string (may be a leaked template token);
                       blank matches any club for that name+season
    season             nomination season
    resolved_player    the correct player name (for the audit trail)
    resolved_club      the correct club (must match the player's game rows)
    resolved_player_id optional explicit players.player_id (most robust key)
    reason             free-text justification
    source_url         provenance
    reviewed_at        date the correction was reviewed

Only rows whose importer status is *not already trusted* are considered, so
overrides never override a good automatic match.
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Iterable

try:
    from names import normalise_name
except ImportError:  # pragma: no cover - standalone fallback
    import re
    import unicodedata

    def normalise_name(value: object) -> str:
        text = unicodedata.normalize("NFKD", str(value or "")).casefold()
        text = "".join(c for c in text if not unicodedata.combining(c))
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()

TRUSTED = {"unique", "resolved"}


def load_overrides(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = []
        for raw in csv.DictReader(handle):
            if not (raw.get("source_name") and raw.get("season")):
                continue
            rows.append({
                "source_name": raw["source_name"].strip(),
                "source_name_key": normalise_name(raw["source_name"]),
                "source_club": (raw.get("source_club") or "").strip(),
                "season": int(str(raw["season"]).strip()),
                "resolved_player": (raw.get("resolved_player") or "").strip(),
                "resolved_club": (raw.get("resolved_club") or "").strip(),
                "resolved_player_id": _int_or_none(raw.get("resolved_player_id")),
                "reason": (raw.get("reason") or "").strip(),
            })
    return rows


def _int_or_none(value: object) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def _resolve_player_id(con: sqlite3.Connection, override: dict) -> tuple[int | None, str]:
    """Return (player_id, matched_player) if the override names exactly one
    real player consistent with the game data, else (None, reason)."""
    season = override["season"]
    club = override["resolved_club"]
    pid = override["resolved_player_id"]

    if pid is not None:
        row = con.execute(
            "SELECT player_id, player FROM players WHERE player_id=?", (pid,)
        ).fetchone()
        if not row:
            return None, f"resolved_player_id {pid} not in players"
        # Verify the player actually played the resolved club that season.
        if club and not _played_for(con, pid, season, club):
            return None, (f"player {pid} has no {club!r} game rows in {season}")
        return pid, row[1]

    key = normalise_name(override["resolved_player"])
    candidates = con.execute(
        "SELECT player_id, player FROM players WHERE name_key=?", (key,)
    ).fetchall()
    if club:
        candidates = [c for c in candidates if _played_for(con, c[0], season, club)]
    if len(candidates) == 1:
        return candidates[0][0], candidates[0][1]
    if not candidates:
        return None, "override resolves to no player matching club/season"
    return None, "override resolves to multiple players; add resolved_player_id"


def _played_for(con: sqlite3.Connection, player_id: int, season: int, club: str) -> bool:
    return con.execute(
        "SELECT 1 FROM games WHERE player_id=? AND season=? "
        "AND (club_now=? OR club_hist=?) LIMIT 1",
        (player_id, season, club, club),
    ).fetchone() is not None


def _is_template_leak(value: str) -> bool:
    return any(ch in value for ch in "${}")


def _matches(override: dict, row: dict) -> bool:
    if override["season"] != int(row["season"]):
        return False
    if override["source_name_key"] != normalise_name(row.get("player")):
        return False
    src_club = override["source_club"]
    row_club = str(row.get("club") or "").strip()
    # A blank override club, an exact club match, or a leaked template token in
    # either side all count as a match: the club field is exactly what a leak
    # corrupts, so it must not be required to line up character-for-character.
    if not src_club or src_club == row_club:
        return True
    return _is_template_leak(src_club) or _is_template_leak(row_club)


def apply_overrides(con: sqlite3.Connection, linked: list[dict],
                    overrides: Iterable[dict], verbose: bool = True) -> int:
    """Mutate ``linked`` rows in place; return the number applied."""
    overrides = list(overrides)
    applied = 0
    for row in linked:
        if row.get("match_status") in TRUSTED:
            continue
        override = next((o for o in overrides if _matches(o, row)), None)
        if override is None:
            continue
        pid, info = _resolve_player_id(con, override)
        if pid is None:
            if verbose:
                print(f"  override SKIPPED for {row.get('player')!r} "
                      f"{row.get('season')}: {info}")
            continue
        row["player_id"] = pid
        row["matched_player"] = info
        row["match_method"] = "reviewed_override"
        row["match_status"] = "resolved"
        row["notes"] = f"reviewed override: {override['reason']}"[:500]
        applied += 1
        if verbose:
            print(f"  override applied: {row.get('player')!r} {row.get('season')} "
                  f"-> {info} ({override['resolved_club']})")
    return applied
