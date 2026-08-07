#!/usr/bin/env python3
"""Parse explicitly cached AFL Tables profiles missing from fitzRoy data.

This is a small, auditable overlay rather than a profile crawler.  Each HTML
file is cached locally, carries AFL Tables' player ID, and every appearance is
matched to the independently cached chronological match-score list.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

import pandas as pd
from bs4 import BeautifulSoup

from .build_db import CLUB_LINEAGE
from .load_match_scores import SourceMatch, read_source

BASE_PROFILE_URL = "https://afltables.com/afl/stats/players"
STAT_COLUMNS = {
    "KI": "kicks", "MK": "marks", "HB": "handballs",
    "DI": "disposals", "GL": "goals", "BH": "behinds",
    "HO": "hitouts", "TK": "tackles", "RB": "rebounds",
    "IF": "inside50s", "CL": "clearances", "CG": "clangers",
    "FF": "frees_for", "FA": "frees_against", "BR": "brownlow",
    "CP": "contested", "UP": "uncontested", "CM": "contested_marks",
    "MI": "marks_i50", "1%": "one_percenters", "BO": "bounces",
    "GA": "goal_assists",
}


def _profile_metadata(path: Path) -> tuple[int, str, str, str]:
    text = path.read_text(encoding="windows-1252", errors="replace")
    soup = BeautifulSoup(text, "html.parser")
    heading = soup.find("h1")
    player = heading.get_text(" ", strip=True) if heading else ""
    player_id = re.search(r"document\.write\(r\[(\d+)\]\)", text)
    born = re.search(r"<b>Born:</b>\s*([^<(]+)", text, re.I)
    if not player or not player_id or not born:
        raise ValueError(f"incomplete player metadata in {path}")
    dob = datetime.strptime(born.group(1).strip(), "%d-%b-%Y").date().isoformat()
    letter = path.stem[0].upper()
    url = f"{BASE_PROFILE_URL}/{letter}/{path.stem}.html"
    return int(player_id.group(1)), player, dob, url


def cached_profile_players(raw_dir: Path) -> list[tuple[int, str, str]]:
    """Return identity rows suitable for the A-Z player index audit."""
    if not raw_dir.is_dir():
        return []
    return [(pid, player, url) for pid, player, _dob, url in
            (_profile_metadata(path) for path in sorted(raw_dir.glob("*.html")))]


def _match_lookup(matches: list[SourceMatch]) -> dict[tuple, SourceMatch]:
    lookup: dict[tuple, SourceMatch] = {}
    for match in matches:
        clubs = frozenset((match.home_team, match.away_team))
        key = (match.season, str(match.round), clubs, match.match_date)
        if key in lookup:
            raise ValueError(f"ambiguous match identity {key}")
        lookup[key] = match
    return lookup


def _profile_game_dates(path: Path) -> dict[tuple[str, int], str]:
    """Read exact dates encoded in detailed-table match links."""
    soup = BeautifulSoup(
        path.read_text(encoding="windows-1252", errors="replace"), "html.parser")
    dates: dict[tuple[str, int], str] = {}
    for table in soup.find_all("table"):
        heading_cell = table.find("th", colspan=True)
        if heading_cell is None:
            continue
        heading = heading_cell.get_text(" ", strip=True)
        if not re.fullmatch(r".+?\s+-\s+\d{4}", heading):
            continue
        body = table.find("tbody")
        for row in body.find_all("tr", recursive=False) if body else []:
            cells = row.find_all("td", recursive=False)
            if not cells:
                continue
            try:
                game_no = int(cells[0].get_text(" ", strip=True))
            except ValueError:
                continue
            anchor = row.find("a", href=True)
            found = re.search(r"(\d{8})\.html$", anchor["href"] if anchor else "")
            if not found:
                raise ValueError(f"missing match date link in {path}: game {game_no}")
            date = datetime.strptime(found.group(1), "%Y%m%d").date().isoformat()
            dates[(heading, game_no)] = date
    return dates


def parse_profile(path: Path, matches: list[SourceMatch]) -> pd.DataFrame:
    """Parse one cached profile and enrich its games from the score list."""
    player_id, player, dob, _url = _profile_metadata(path)
    lookup = _match_lookup(matches)
    game_dates = _profile_game_dates(path)
    rows: list[dict] = []
    for table in pd.read_html(path):
        if not isinstance(table.columns, pd.MultiIndex):
            continue
        detail = [str(value) for value in table.columns.get_level_values(-1)]
        if "Gm" not in detail or "Opponent" not in detail or "Rd" not in detail:
            continue
        heading = str(table.columns.get_level_values(0)[0])
        header = re.fullmatch(r"(.+?)\s+-\s+(\d{4})", heading)
        if not header:
            continue
        club, season = header.group(1).strip(), int(header.group(2))
        table = table.copy()
        table.columns = detail
        table = table[pd.to_numeric(table["Gm"], errors="coerce").notna()]
        # A blank is zero only where that statistic exists in this season's
        # table. This preserves genuinely unavailable historical fields.
        numeric = {code: pd.to_numeric(table[code], errors="coerce")
                   for code in STAT_COLUMNS if code in table}
        available = {code: values.notna().any() for code, values in numeric.items()}
        for index, source in table.iterrows():
            game_no = int(float(source["Gm"]))
            opponent = str(source["Opponent"]).strip()
            round_name = str(source["Rd"]).strip()
            if round_name.endswith(".0"):
                round_name = round_name[:-2]
            date = game_dates.get((heading, game_no))
            key = (season, round_name, frozenset((club, opponent)), date)
            match = lookup.get(key)
            if match is None:
                raise ValueError(f"no score-list match for {player}, {key}")
            is_home = club == match.home_team
            if not is_home and club != match.away_team:
                raise ValueError(f"club mismatch for {player}, {key}")
            points_for = match.home_score if is_home else match.away_score
            points_against = match.away_score if is_home else match.home_score
            result = "W" if points_for > points_against else (
                "L" if points_for < points_against else "D")
            claimed = str(source.get("R", result)).strip().upper()
            if claimed and claimed != result:
                raise ValueError(f"result mismatch for {player}, {key}")
            row = {
                "player_id": player_id, "player": player, "season": season,
                "round": round_name, "date": match.match_date,
                "venue": match.venue, "club_hist": club,
                "club_now": CLUB_LINEAGE.get(club, club),
                "career_game_no": game_no, "dob": dob,
                "birth_est": pd.Timestamp(dob),
                "birth_year_est": int(dob[:4]), "opponent": opponent,
                "is_home": int(is_home), "result": result,
                "points_for": points_for, "points_against": points_against,
                "is_final": int(round_name.upper() in {"EF", "QF", "SF", "PF", "GF"}),
            }
            for code, column in STAT_COLUMNS.items():
                value = numeric[code].loc[index] if code in numeric else pd.NA
                row[column] = (0 if pd.isna(value) and available.get(code)
                               else value)
            rows.append(row)
    if not rows:
        raise ValueError(f"no detailed player-game tables in {path}")
    output = pd.DataFrame(rows).sort_values("career_game_no")
    expected = list(range(1, len(output) + 1))
    if output["career_game_no"].tolist() != expected:
        raise ValueError(f"non-contiguous career game numbers in {path}")
    return output


def load_cached_profiles(raw_dir: Path, score_path: Path) -> pd.DataFrame:
    """Load every deliberately cached profile, failing closed on conflicts."""
    if not raw_dir.is_dir():
        return pd.DataFrame()
    paths = sorted(raw_dir.glob("*.html"))
    if not paths:
        return pd.DataFrame()
    matches = read_source(score_path)
    frames = [parse_profile(path, matches) for path in paths]
    output = pd.concat(frames, ignore_index=True)
    duplicated = output.duplicated(["player_id", "date"], keep=False)
    if duplicated.any():
        raise ValueError("cached profiles contain duplicate player-date rows")
    return output
