#!/usr/bin/env python3
"""Parse AFL Tables "All Games - By Season" club pages.

One row per match *observation*. Every match appears on two club pages, so
these rows are source observations, not matches: deduplication happens at load
time against the existing ``matches`` table.

What the source actually gives us
---------------------------------
Each row carries the round, the club's position (H/A/F), the opponent, four
cumulative goal.behind scores for each side, final points, result, margin, the
running W-D-L, venue, crowd, the exact date and time, and a link to the AFL
Tables game page.

The game link is the strongest shared identifier between the two club pages,
but it does **not** encode orientation: the key is
``{lower_team_code}{higher_team_code}{YYYY}{MMDD}``, with the two team codes in
ascending numeric order. It is a canonical match key and nothing more. Home and
away come from the ``T`` column, and ``T=F`` marks a final with no orientation
at all - those must be oriented from the existing match database, never guessed.

Scoring is cumulative. ``3.3 5.6 7.12 9.17`` means 3.3 at quarter time and 9.17
at full time, not 3.3 in the first quarter. The cumulative values are kept as
the authoritative source fields; per-quarter scoring is derived.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
import re
import sys

try:
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency. Run: python -m pip install beautifulsoup4") from exc

FINALS_ROUNDS = {"EF", "QF", "SF", "PF", "GF"}
EXPECTED_HEADERS = ["Rnd", "T", "Opponent", "Scoring", "F", "Scoring", "A",
                    "R", "M", "W-D-L", "Venue", "Crowd", "Date"]

# AFL Tables' own club labels. These already match the historical club labels
# used by games.club_hist, so no renaming is applied here beyond whitespace.
# Brisbane Bears, Fitzroy and University stay distinct identities.
SOURCE_CLUB_LABELS = {
    "Adelaide", "Brisbane Bears", "Brisbane Lions", "Carlton", "Collingwood",
    "Essendon", "Fitzroy", "Footscray", "Fremantle", "Geelong", "Gold Coast",
    "Greater Western Sydney", "Hawthorn", "Kangaroos", "Melbourne",
    "North Melbourne", "Port Adelaide", "Richmond", "South Melbourne",
    "St Kilda", "Sydney", "University", "West Coast", "Western Bulldogs",
    "University of Melbourne",
}

_SCORE_RE = re.compile(r"^(\d+)\.(\d+)$")
_KEY_RE = re.compile(r"^(\d{2})(\d{2})(\d{4})(\d{2})(\d{2})$")
_WDL_RE = re.compile(r"^(\d+)-(\d+)-(\d+)$")


class ParseError(ValueError):
    """Raised when a page or row cannot be parsed safely."""


def clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def score_to_points(score: str) -> tuple[int, int, int]:
    """'9.17' -> (goals, behinds, points)."""
    match = _SCORE_RE.match(clean_text(score))
    if not match:
        raise ParseError(f"unparseable score {score!r}")
    goals, behinds = int(match.group(1)), int(match.group(2))
    return goals, behinds, goals * 6 + behinds


def parse_scoring(raw: str) -> list[tuple[int, int, int]]:
    """'3.3 5.6 7.12 9.17' -> four cumulative (goals, behinds, points)."""
    parts = clean_text(raw).split()
    if len(parts) != 4:
        raise ParseError(f"expected 4 cumulative scores, got {raw!r}")
    return [score_to_points(part) for part in parts]


def quarter_only(cumulative: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """Cumulative quarters -> scoring within each quarter."""
    out = []
    prev = (0, 0, 0)
    for goals, behinds, points in cumulative:
        out.append((goals - prev[0], behinds - prev[1], points - prev[2]))
        prev = (goals, behinds, points)
    return out


def parse_game_key(href: str) -> tuple[str, str, tuple[str, str], str]:
    """Return (url, key, (team_code_a, team_code_b), YYYY-MM-DD) from a game link.

    The two team codes are in ascending order in the source, so they identify
    the pair of clubs but not which side was at home.
    """
    url = clean_text(href)
    key = url.rsplit("/", 1)[-1].split(".")[0]
    match = _KEY_RE.match(key)
    if not match:
        raise ParseError(f"unrecognised game key {key!r} in {url!r}")
    a, b, year, month, day = match.groups()
    return url, key, (a, b), f"{year}-{month}-{day}"


def parse_date_text(text: str) -> tuple[str | None, str | None, str | None]:
    """'Thu 12-Mar-2026 7:30 PM' -> ('2026-03-12', '19:30', ISO datetime).

    Rows without a time are valid and keep a NULL time.
    """
    cleaned = clean_text(text)
    body = re.sub(r"^[A-Za-z]{3},?\s+", "", cleaned)
    date_match = re.match(r"(\d{1,2}-[A-Za-z]{3}-\d{4})", body)
    if not date_match:
        return None, None, None
    date = datetime.strptime(date_match.group(1), "%d-%b-%Y").date()
    time_match = re.search(r"(\d{1,2}:\d{2})\s*([AP]M)?", body)
    if not time_match:
        return date.isoformat(), None, None
    hhmm, meridiem = time_match.group(1), time_match.group(2)
    fmt = "%I:%M %p" if meridiem else "%H:%M"
    stamp = f"{hhmm} {meridiem}".strip()
    parsed = datetime.strptime(stamp, fmt).time()
    return (date.isoformat(), parsed.strftime("%H:%M"),
            f"{date.isoformat()}T{parsed.strftime('%H:%M')}")


def parse_attendance(text: str) -> int | None:
    """Blank crowds stay NULL. Zero is never substituted."""
    cleaned = clean_text(text).replace(",", "")
    if not cleaned or cleaned in {"-", "--"}:
        return None
    if not cleaned.isdigit():
        raise ParseError(f"unparseable crowd {text!r}")
    return int(cleaned)


@dataclass
class MatchObservation:
    source_club_id: str
    source_club_label: str
    season: int
    round: str
    is_final: int
    team_position: str            # H, A or F
    opponent_raw: str
    scoring_for_raw: str
    scoring_against_raw: str
    points_for: int
    points_against: int
    result: str
    margin: int
    season_wins_after: int | None
    season_draws_after: int | None
    season_losses_after: int | None
    venue_raw: str
    attendance: int | None
    date_text: str
    match_date: str | None
    match_time: str | None
    match_datetime: str | None
    source_game_url: str
    source_game_key: str
    team_code_low: str
    team_code_high: str
    home_team_raw: str | None
    away_team_raw: str | None
    cumulative_for: list = field(default_factory=list)
    cumulative_against: list = field(default_factory=list)

    def flat(self) -> dict:
        """Row shaped for the club_match_sources table."""
        row = {k: v for k, v in asdict(self).items()
               if k not in {"cumulative_for", "cumulative_against"}}
        for side, quarters in (("for", self.cumulative_for),
                               ("against", self.cumulative_against)):
            for index, (goals, behinds, points) in enumerate(quarters, start=1):
                row[f"q{index}_{side}_goals"] = goals
                row[f"q{index}_{side}_behinds"] = behinds
                row[f"q{index}_{side}_points"] = points
        return row


def _validate_row(obs: MatchObservation) -> None:
    if obs.cumulative_for[-1][2] != obs.points_for:
        raise ParseError(
            f"{obs.season} {obs.round}: final cumulative score "
            f"{obs.cumulative_for[-1][2]} != points for {obs.points_for}")
    if obs.cumulative_against[-1][2] != obs.points_against:
        raise ParseError(
            f"{obs.season} {obs.round}: final cumulative score "
            f"{obs.cumulative_against[-1][2]} != points against "
            f"{obs.points_against}")
    if obs.points_for - obs.points_against != obs.margin:
        raise ParseError(f"{obs.season} {obs.round}: margin disagrees with scores")
    expected = "W" if obs.margin > 0 else ("L" if obs.margin < 0 else "D")
    if obs.result != expected:
        raise ParseError(
            f"{obs.season} {obs.round}: result {obs.result!r} disagrees with "
            f"margin {obs.margin}")
    for quarters in (obs.cumulative_for, obs.cumulative_against):
        for earlier, later in zip(quarters, quarters[1:]):
            if later[0] < earlier[0] or later[1] < earlier[1]:
                raise ParseError(
                    f"{obs.season} {obs.round}: cumulative scoring goes backwards")


def _season_from_caption(table) -> int | None:
    head = table.find("th")
    if not head:
        return None
    match = re.search(r"\b(18|19|20)\d{2}\b", clean_text(head.get_text()))
    return int(match.group(0)) if match else None


def clean_club_heading(raw_label: str) -> str:
    """AFL Tables titles some pages with both historical names joined by '/'
    (Sydney's is literally "South Melbourne/Sydney"). Matching never depends
    on this text -- linking uses the manifest's club_now identity instead --
    but the last segment is the more recognisable name for display."""
    parts = [part.strip() for part in raw_label.split("/") if part.strip()]
    return parts[-1] if parts else raw_label


def parse_all_games(source: Path | str, source_club_id: str,
                    *, strict: bool = True) -> tuple[list[MatchObservation], list[str]]:
    """Parse a cached All Games page.

    Returns (observations, errors). With strict=True the first bad row raises;
    otherwise problems are collected so a report can show every one at once.
    """
    if isinstance(source, Path):
        html = source.read_bytes().decode("windows-1252", errors="replace")
    else:
        html = source
    soup = BeautifulSoup(html, "html.parser")

    heading = soup.find("h1")
    heading_raw = clean_text(heading.get_text()).split(" - ")[0] if heading else ""
    club_label = clean_club_heading(heading_raw)

    observations: list[MatchObservation] = []
    errors: list[str] = []
    tables = soup.find_all("table")
    if not tables:
        raise ParseError("no season tables found - is this an All Games page?")

    for table in tables:
        season = _season_from_caption(table)
        if season is None:
            continue
        body = table.find("tbody") or table
        for tr in body.find_all("tr", recursive=False):
            cells = tr.find_all(["td", "th"], recursive=False)
            if not cells or cells[0].name == "th":
                continue
            values = [clean_text(cell.get_text(" ", strip=True)) for cell in cells]
            if len(values) != len(EXPECTED_HEADERS):
                errors.append(
                    f"{source_club_id} {season}: expected "
                    f"{len(EXPECTED_HEADERS)} cells, got {len(values)}")
                continue
            try:
                observations.append(
                    _build_observation(cells, values, season, source_club_id,
                                       club_label))
            except ParseError as exc:
                if strict:
                    raise
                errors.append(f"{source_club_id} {season}: {exc}")
    return observations, errors


def _build_observation(cells, values, season, source_club_id, club_label):
    (rnd, position, opponent, scoring_for, points_for, scoring_against,
     points_against, result, margin, wdl, venue, crowd, date_text) = values

    link = cells[0].find("a", href=True)
    if not link:
        raise ParseError(f"{rnd}: no game link")
    url, key, (code_a, code_b), key_date = parse_game_key(link["href"])

    position = position.upper()
    if position not in {"H", "A", "F"}:
        raise ParseError(f"{rnd}: unexpected position {position!r}")

    match_date, match_time, match_datetime = parse_date_text(date_text)
    if match_date and match_date != key_date:
        raise ParseError(f"{rnd}: game key date {key_date} != row date {match_date}")

    wdl_match = _WDL_RE.match(wdl)
    wins, draws, losses = (
        tuple(int(part) for part in wdl_match.groups()) if wdl_match
        else (None, None, None))

    home = away = None
    if position == "H":
        home, away = club_label, opponent
    elif position == "A":
        home, away = opponent, club_label

    obs = MatchObservation(
        source_club_id=source_club_id,
        source_club_label=club_label,
        season=season,
        round=rnd,
        is_final=int(position == "F" or rnd.upper() in FINALS_ROUNDS),
        team_position=position,
        opponent_raw=opponent,
        scoring_for_raw=scoring_for,
        scoring_against_raw=scoring_against,
        points_for=int(points_for),
        points_against=int(points_against),
        result=result.upper(),
        margin=int(margin),
        season_wins_after=wins,
        season_draws_after=draws,
        season_losses_after=losses,
        venue_raw=venue,
        attendance=parse_attendance(crowd),
        date_text=date_text,
        match_date=match_date or key_date,
        match_time=match_time,
        match_datetime=match_datetime,
        source_game_url=url,
        source_game_key=key,
        team_code_low=code_a,
        team_code_high=code_b,
        home_team_raw=home,
        away_team_raw=away,
        cumulative_for=parse_scoring(scoring_for),
        cumulative_against=parse_scoring(scoring_against),
    )
    _validate_row(obs)
    return obs


def parse_season_footers(source: Path | str) -> dict[int, dict]:
    """Each season table carries a Totals footer: points for and against, the
    P/W/D/L summary and the aggregate crowd. Free integrity check on parsing."""
    if isinstance(source, Path):
        html = source.read_bytes().decode("windows-1252", errors="replace")
    else:
        html = source
    soup = BeautifulSoup(html, "html.parser")
    footers: dict[int, dict] = {}
    for table in soup.find_all("table"):
        season = _season_from_caption(table)
        foot = table.find("tfoot")
        if season is None or foot is None:
            continue
        row = foot.find("tr")
        if row is None:
            continue
        values = [clean_text(cell.get_text(" ", strip=True))
                  for cell in row.find_all(["td", "th"])]
        summary = next((value for value in values if value.startswith("P:")), "")
        played = re.search(r"P:(\d+)", summary)
        wins = re.search(r"W:(\d+)", summary)
        draws = re.search(r"D:(\d+)", summary)
        losses = re.search(r"L:(\d+)", summary)
        numbers = [value for value in values if value.isdigit()]
        footers[season] = {
            "played": int(played.group(1)) if played else None,
            "wins": int(wins.group(1)) if wins else None,
            "draws": int(draws.group(1)) if draws else None,
            "losses": int(losses.group(1)) if losses else None,
            "points_for": int(numbers[0]) if len(numbers) > 0 else None,
            "points_against": int(numbers[1]) if len(numbers) > 1 else None,
            "total_crowd": int(numbers[2]) if len(numbers) > 2 else None,
        }
    return footers


def check_against_footers(observations: list[MatchObservation],
                          footers: dict[int, dict]) -> list[str]:
    """Compare parsed rows with each season's own totals."""
    problems: list[str] = []
    by_season: dict[int, list[MatchObservation]] = {}
    for obs in observations:
        by_season.setdefault(obs.season, []).append(obs)
    for season, expected in footers.items():
        rows = by_season.get(season, [])
        if expected["played"] is not None and expected["played"] != len(rows):
            problems.append(
                f"{season}: footer says {expected['played']} games, "
                f"parsed {len(rows)}")
        for label, key in (("W", "wins"), ("D", "draws"), ("L", "losses")):
            if expected[key] is None:
                continue
            actual = sum(obs.result == label for obs in rows)
            if actual != expected[key]:
                problems.append(
                    f"{season}: footer {label}={expected[key]}, parsed {actual}")
        for key, field in (("points_for", "points_for"),
                           ("points_against", "points_against")):
            if expected[key] is None:
                continue
            actual = sum(getattr(obs, field) for obs in rows)
            if actual != expected[key]:
                problems.append(
                    f"{season}: footer {key}={expected[key]}, parsed {actual}")
        if expected["total_crowd"] is not None:
            actual = sum(obs.attendance or 0 for obs in rows)
            if actual != expected["total_crowd"]:
                problems.append(
                    f"{season}: footer crowd={expected['total_crowd']:,}, "
                    f"parsed {actual:,}")
    return problems


def summarise(observations: list[MatchObservation]) -> dict:
    seasons = sorted({obs.season for obs in observations})
    return {
        "matches": len(observations),
        "seasons": len(seasons),
        "season_from": seasons[0] if seasons else None,
        "season_to": seasons[-1] if seasons else None,
        "finals": sum(obs.is_final for obs in observations),
        "home": sum(obs.team_position == "H" for obs in observations),
        "away": sum(obs.team_position == "A" for obs in observations),
        "neutral_finals": sum(obs.team_position == "F" for obs in observations),
        "attendance_known": sum(obs.attendance is not None for obs in observations),
        "attendance_blank": sum(obs.attendance is None for obs in observations),
        "unique_game_keys": len({obs.source_game_key for obs in observations}),
        "opponents": len({obs.opponent_raw for obs in observations}),
        "venues": len({obs.venue_raw for obs in observations}),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path", type=Path, help="cached All Games HTML file")
    ap.add_argument("--club", default="unknown", help="source club id")
    ap.add_argument("--lenient", action="store_true",
                    help="collect row errors instead of stopping at the first")
    ap.add_argument("--sample", type=int, default=0,
                    help="print N parsed rows as JSON")
    args = ap.parse_args(argv)

    observations, errors = parse_all_games(args.path, args.club,
                                           strict=not args.lenient)
    for key, value in summarise(observations).items():
        print(f"{key:20} {value}")
    if errors:
        print(f"\n{len(errors)} row error(s):")
        for line in errors[:25]:
            print(f"  {line}")
    for obs in observations[:args.sample]:
        print(json.dumps(obs.flat(), indent=2, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
