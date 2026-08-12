#!/usr/bin/env python3
"""Load a hand-entered AFL Tables round into the AFL database.

Why this exists
---------------
``afl/build_db.py`` deliberately does not scrape afltables.com -- its
robots.txt disallows automated clients on the stats paths -- so the game
data arrives through the cached fitzRoy dataset instead. That dataset lags
the live season by a round or two. This loader closes the gap for a round
that has been played but not yet published, from the one source that needs
no crawler: the AFL Tables match pages as a person copy-pastes them into a
spreadsheet.

Input layout
------------
One directory per round, holding:

* a **round summary** -- the AFL Tables season page rows for that round, two
  lines per match::

      Western Bulldogs,2.3   5.6  10.9 11.11,77,"Thu 06-Aug-2026 7:30 PM Att: 25,052 Venue: Docklands"
      North Melbourne,6.0  10.2  12.7 15.10,100,North Melbourne won by 23 pts [ Match stats ]

  The first line is the home side and carries the fixture; the second is the
  away side and carries the result. Quarter scores are cumulative, as AFL
  Tables shows them.

* one **game file** per match -- the match page's four tables in order: both
  sides' ``Match Statistics``, then both sides' ``Player Details``.

Files are paired to fixtures by the club names in their ``... Match
Statistics`` headings, never by filename, so a misnamed or duplicated file
cannot load a match twice or attach stats to the wrong fixture. Anything in
the directory that is not a round summary or a recognised game file is
ignored and reported.

Durability
----------
``afl/build_db.py`` writes ``games`` and ``players`` with
``to_sql(if_exists="replace")``, so a rebuild drops anything hand-entered.
The parsed rows are therefore kept in ``manual_round_games``, which survives
a rebuild, and the fixture side is written to ``club_match_sources`` -- the
same table the All Games scrape feeds. A rebuild is followed by
``--apply-only``, which re-applies both from the stored rows without
re-reading any CSV. ``afl/build_db.py`` calls that itself, so a scheduled
refresh does not quietly lose a round.

When fitzRoy does publish the round, the rebuilt ``games`` rows are the
authority: ``--apply-only`` skips any match the rebuild already produced,
and ``--forget`` drops the stored rows once they are redundant.

Usage:
    python -m utils.afl.load_round_csv --dir <folder> --season 2026 --round 23 --dry-run
    python -m utils.afl.load_round_csv --dir <folder> --season 2026 --round 23
    python -m utils.afl.load_round_csv --apply-only        # after a rebuild
    python -m utils.afl.load_round_csv --forget 2026 23    # once fitzRoy has it
    python -m utils.afl.load_round_csv --remove 2026 23    # undo the load

``--forget`` and ``--remove`` are not the same thing and the difference
matters. ``--forget`` is for a round that succeeded: AFL Tables has since
published it, the rebuilt ``games`` rows are the authority, and only the
stored copy is redundant -- so ``games`` and ``matches`` are deliberately
left alone. ``--remove`` is for a round that should not be there at all,
and takes the whole thing back out.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_paths import default_db                      # noqa: E402
from names import normalise_name                       # noqa: E402
from utils.afl import load_club_all_games              # noqa: E402
from utils.afl.club_sources import ALL_GAMES_BY_ID     # noqa: E402

DEFAULT_DB = Path(default_db("afl"))

#: Where the last folder loaded from is remembered. The CSVs are written by
#: hand and live wherever suits -- a OneDrive desktop folder, a memory stick --
#: so the location is a setting rather than a fixed path under data/. Both the
#: CLI (`--dir` omitted) and the browse window read it, so choosing a folder in
#: one carries to the other. Under data/, which is gitignored: it is one
#: person's local path, not a fact about the project.
SETTINGS = PROJECT_ROOT / "data" / "app" / "round_loader.json"

#: AFL Tables' numeric club codes, which form the game key {low}{high}{date}.
#: Only the clubs that can appear in a current-season round are listed; a
#: historical club would need its code added before this loader could carry it.
CLUB_CODES = {
    "adelaide": "01", "brisbane_lions": "19", "carlton": "03",
    "collingwood": "04", "essendon": "05", "fremantle": "08",
    "geelong": "09", "gold_coast": "20", "gws": "21", "hawthorn": "10",
    "melbourne": "11", "north_melbourne": "12", "port_adelaide": "13",
    "richmond": "14", "st_kilda": "15", "sydney": "16", "west_coast": "18",
    "western_bulldogs": "07",
}

#: Match Statistics header abbreviation -> games column. `%P` (percent of game
#: played) is read and discarded: games has nowhere to put it.
STAT_COLUMNS = {
    "KI": "kicks", "MK": "marks", "HB": "handballs", "DI": "disposals",
    "GL": "goals", "BH": "behinds", "HO": "hitouts", "TK": "tackles",
    "RB": "rebounds", "IF": "inside50s", "CL": "clearances", "CG": "clangers",
    "FF": "frees_for", "FA": "frees_against", "BR": "brownlow",
    "CP": "contested", "UP": "uncontested", "CM": "contested_marks",
    "MI": "marks_i50", "1%": "one_percenters", "BO": "bounces",
    "GA": "goal_assists",
}

#: A blank counting stat on an AFL Tables match page means zero. Brownlow is
#: the exception: votes are not published until the season's count, so a blank
#: BR is "not yet known", which is NULL -- the same thing afl/build_db.py
#: writes for a round whose votes have not been read.
NULL_WHEN_BLANK = {"brownlow"}

GAME_COLUMNS = [
    "player_id", "player", "season", "round", "date", "venue",
    "club_hist", "club_now", "career_game_no", "dob", "birth_est",
    "birth_year_est",
] + list(STAT_COLUMNS.values()) + [
    "opponent", "is_home", "result", "points_for", "points_against",
    "is_final", "match_id", "match_event",
]

STORE_COLUMNS = [
    "season", "round", "match_date", "venue", "club_hist", "club_now",
    "opponent", "is_home", "result", "points_for", "points_against",
    "is_final", "jumper", "source_name", "player_id", "player",
    "career_game_no", "age_text", "career_games_text", "career_goals_text",
] + list(STAT_COLUMNS.values()) + ["source_file", "imported_at"]

HEADER_RE = re.compile(r"^(?P<club>.+?)\s+Match Statistics\b")
DETAILS_RE = re.compile(r"^(?P<club>.+?)\s+Player Details\b")
FIXTURE_RE = re.compile(
    r"(?P<dow>\w{3})\s+(?P<date>\d{1,2}-\w{3}-\d{4})"
    r"(?:\s+(?P<time>\d{1,2}:\d{2}\s*[AP]M))?"
    r"(?:\s*\([^)]*\))?"
    r"(?:\s+Att:\s*(?P<att>[\d,]+))?"
    r"(?:\s+Venue:\s*(?P<venue>.+?))?\s*$")
RESULT_RE = re.compile(r"^(?P<winner>.+?)\s+won by\s+(?P<margin>\d+)\s+pts")
DRAWN_RE = re.compile(r"\bmatch drawn\b|\bdrew\b", re.I)
AGE_RE = re.compile(r"(?P<years>\d+)y\s*(?P<days>\d+)d")
CAREER_RE = re.compile(r"^\s*(?P<games>\d+)")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LoadError(RuntimeError):
    """A problem in the source files that must be fixed before loading."""


# --------------------------------------------------------------------------
# what the load noticed


#: Line prefixes for the three kinds of finding. They are part of the output
#: rather than decoration: utils/afl/load_round_gui.py colours a line by the
#: marker it starts with, and reading them back is how the window knows which
#: findings still want a person's attention.
FIXED = "[fixed]"
NOTE = "[note]"
NOT_FIXED = "[not fixed]"

#: Widest marker, so the text after them lines up in a fixed-width log.
_MARKER_WIDTH = max(len(m) for m in (FIXED, NOTE, NOT_FIXED))

#: Character column a finding's text starts at, after two spaces of indent,
#: the marker and two more spaces. The window hangs wrapped lines here.
MARKER_COLUMN = 2 + _MARKER_WIDTH + 2


@dataclass
class Finding:
    status: str      # FIXED, NOTE or NOT_FIXED
    topic: str
    detail: str

    def line(self) -> str:
        return f"  {self.status:<{_MARKER_WIDTH}}  {self.topic}: {self.detail}"


class Findings:
    """Everything the load decided, resolved or refused, in one place.

    A load makes several judgement calls that used to leave no trace beyond a
    count -- which of two identically named players a row meant, which of two
    identical files was used, that a squad's behinds fall short of the
    summary because rushed behinds belong to nobody. Each is recorded here
    with whether it was settled or still wants a person, so the report says
    what happened rather than only how many of each there were.
    """

    def __init__(self):
        self.items: list[Finding] = []

    def fixed(self, topic: str, detail: str) -> None:
        """Noticed and resolved; nothing for anyone to do."""
        self.items.append(Finding(FIXED, topic, detail))

    def note(self, topic: str, detail: str) -> None:
        """Worth knowing, but nothing was wrong and nothing was decided."""
        self.items.append(Finding(NOTE, topic, detail))

    def not_fixed(self, topic: str, detail: str) -> None:
        """Stopped the load. Only a person can settle it."""
        self.items.append(Finding(NOT_FIXED, topic, detail))

    def of(self, status: str) -> list[Finding]:
        return [item for item in self.items if item.status == status]

    def report(self, heading: str = "Findings") -> None:
        if not self.items:
            print(f"\n{heading}: nothing to report.")
            return
        print(f"\n{heading}")
        for status in (NOT_FIXED, FIXED, NOTE):
            for item in self.of(status):
                print(item.line())
        counts = ", ".join(
            f"{len(self.of(status))} {label}"
            for status, label in ((NOT_FIXED, "not fixed"), (FIXED, "fixed"),
                                  (NOTE, "noted"))
            if self.of(status))
        print(f"  -- {counts}")


# --------------------------------------------------------------------------
# remembering where the CSVs live


def remembered_dir() -> Path | None:
    """The folder last loaded from, if it is still there."""
    try:
        with SETTINGS.open(encoding="utf-8") as handle:
            stored = json.load(handle).get("dir")
    except (OSError, ValueError, AttributeError):
        return None
    if not stored:
        return None
    folder = Path(stored)
    return folder if folder.is_dir() else None


def remember_dir(folder: Path) -> None:
    """Record a folder as the default for next time. Never fatal."""
    try:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        with SETTINGS.open("w", encoding="utf-8") as handle:
            json.dump({"dir": str(folder)}, handle, indent=2)
    except OSError as error:
        print(f"warning: could not remember {folder} ({error})",
              file=sys.stderr)


def guess_round(*texts: str) -> str | None:
    """Pull a round number out of a folder or summary name, e.g. "rd23"."""
    for text in texts:
        found = re.search(r"(?:^|[^a-z0-9])r(?:ou)?n?d?\s*0*(\d{1,2})",
                          str(text), re.I)
        if found:
            return found.group(1)
    return None


# --------------------------------------------------------------------------
# parse


@dataclass
class Side:
    club: str
    quarters: list[tuple[int, int]]      # cumulative (goals, behinds)
    score: int


@dataclass
class Fixture:
    home: Side
    away: Side
    match_date: str                      # ISO
    match_time: str | None               # HH:MM
    attendance: int | None
    venue: str | None
    winner: str | None
    margin: int | None
    source_line: int

    @property
    def clubs(self) -> frozenset[str]:
        return frozenset({self.home.club, self.away.club})


@dataclass
class PlayerLine:
    jumper: str
    source_name: str
    stats: dict[str, float | None]
    age_text: str | None = None
    career_games_text: str | None = None
    career_goals_text: str | None = None


@dataclass
class GameFile:
    path: Path
    clubs: list[str] = field(default_factory=list)
    players: dict[str, list[PlayerLine]] = field(default_factory=dict)


def parse_score(text: str) -> tuple[list[tuple[int, int]], int]:
    """"2.3   5.6  10.9 11.11" -> [(2,3),(5,6),(10,9),(11,11)], 77."""
    parts = text.split()
    quarters = []
    for part in parts:
        goals, _, behinds = part.partition(".")
        try:
            quarters.append((int(goals), int(behinds)))
        except ValueError as error:
            raise LoadError(f"unreadable quarter score {part!r}") from error
    if not quarters:
        raise LoadError(f"no quarter scores in {text!r}")
    goals, behinds = quarters[-1]
    return quarters, goals * 6 + behinds


def parse_fixture_text(text: str) -> dict:
    match = FIXTURE_RE.search(text.strip())
    if not match:
        raise LoadError(f"unreadable fixture line {text!r}")
    when = datetime.strptime(match.group("date"), "%d-%b-%Y").date()
    time_text = match.group("time")
    match_time = None
    if time_text:
        parsed = datetime.strptime(re.sub(r"\s+", " ", time_text.strip()),
                                   "%I:%M %p")
        match_time = parsed.strftime("%H:%M")
    attendance = match.group("att")
    return {
        "match_date": when.isoformat(),
        "match_time": match_time,
        "attendance": int(attendance.replace(",", "")) if attendance else None,
        "venue": (match.group("venue") or "").strip() or None,
    }


def parse_round_summary(path: Path) -> list[Fixture]:
    """Read the season-page rows for one round into fixtures."""
    fixtures: list[Fixture] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [(number, row)
                for number, row in enumerate(csv.reader(handle), start=1)]

    pending: list[tuple[int, list[str]]] = []
    for number, row in rows:
        if not any(cell.strip() for cell in row):
            continue
        if len(row) < 3:
            raise LoadError(f"{path.name} line {number}: expected at least "
                            f"three columns, got {len(row)}")
        pending.append((number, row))
        if len(pending) == 2:
            fixtures.append(_fixture_from_pair(path, pending))
            pending = []
    if pending:
        raise LoadError(f"{path.name}: {len(pending)} leftover line(s) -- the "
                        f"summary must hold two lines per match")
    if not fixtures:
        raise LoadError(f"{path.name}: no fixtures found")
    return fixtures


def _fixture_from_pair(path: Path, pair: list[tuple[int, list[str]]]) -> Fixture:
    (home_line, home_row), (_, away_row) = pair
    home_q, home_score = parse_score(home_row[1])
    away_q, away_score = parse_score(away_row[1])
    for row, computed, label in ((home_row, home_score, "home"),
                                 (away_row, away_score, "away")):
        stated = row[2].strip()
        if stated and int(stated) != computed:
            raise LoadError(
                f"{path.name} line {home_line}: {label} score {stated} does "
                f"not match the quarter scores ({computed})")

    details = parse_fixture_text(home_row[3] if len(home_row) > 3 else "")
    outcome = (away_row[3] if len(away_row) > 3 else "").strip()

    winner = margin = None
    result = RESULT_RE.match(outcome)
    if result:
        winner = result.group("winner").strip()
        margin = int(result.group("margin"))
    elif not DRAWN_RE.search(outcome) and home_score != away_score:
        raise LoadError(f"{path.name} line {home_line}: unreadable result "
                        f"{outcome!r}")

    home = Side(home_row[0].strip(), home_q, home_score)
    away = Side(away_row[0].strip(), away_q, away_score)

    if winner is not None:
        expected = home.club if home_score > away_score else away.club
        if winner != expected:
            raise LoadError(
                f"{path.name} line {home_line}: result says {winner!r} won "
                f"but the scores say {expected!r}")
        if margin != abs(home_score - away_score):
            raise LoadError(
                f"{path.name} line {home_line}: margin {margin} does not "
                f"match the scores ({abs(home_score - away_score)})")

    return Fixture(home=home, away=away, winner=winner, margin=margin,
                   source_line=home_line, **details)


def _stat_values(header: list[str], row: list[str]) -> dict[str, float | None]:
    values: dict[str, float | None] = {}
    for index, abbreviation in enumerate(header):
        column = STAT_COLUMNS.get(abbreviation.strip())
        if column is None:
            continue
        raw = row[index].strip() if index < len(row) else ""
        if not raw:
            values[column] = None if column in NULL_WHEN_BLANK else 0.0
        else:
            values[column] = float(raw)
    missing = set(STAT_COLUMNS.values()) - set(values)
    if missing:
        raise LoadError(f"match statistics header is missing "
                        f"{', '.join(sorted(missing))}")
    return values


def parse_game_file(path: Path) -> GameFile | None:
    """Read one match page. Returns None if the file is not a game file."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    if not any(row and HEADER_RE.match(row[0].strip()) for row in rows):
        return None

    game = GameFile(path=path)
    section = None            # "stats" | "details" | None
    club = None
    header: list[str] = []
    details: dict[str, dict[str, PlayerLine]] = {}

    for row in rows:
        first = row[0].strip() if row else ""
        if not first:
            section = None
            continue

        stats_header = HEADER_RE.match(first)
        details_header = DETAILS_RE.match(first)
        if stats_header:
            club = stats_header.group("club").strip()
            section, header = "stats", []
            if club not in game.players:
                game.clubs.append(club)
                game.players[club] = []
            continue
        if details_header:
            club = details_header.group("club").strip()
            section, header = "details", []
            details.setdefault(club, {})
            continue
        if section is None:
            continue
        if first == "#":
            header = [cell.strip() for cell in row]
            continue
        if first in {"Rushed", "Totals", "Opposition"} or not first.isdigit():
            # Rushed behinds, team totals and the coach row ("C") are not
            # player-games. They are read for validation, not loaded.
            continue

        name = row[1].strip() if len(row) > 1 else ""
        if not name:
            raise LoadError(f"{path.name}: a row has a number but no name")
        if section == "stats":
            game.players[club].append(
                PlayerLine(jumper=first, source_name=name,
                           stats=_stat_values(header, row)))
        else:
            details[club][normalise_name(name)] = PlayerLine(
                jumper=first, source_name=name, stats={},
                age_text=(row[2].strip() if len(row) > 2 else None),
                career_games_text=(row[3].strip() if len(row) > 3 else None),
                career_goals_text=(row[4].strip() if len(row) > 4 else None))

    if len(game.clubs) != 2:
        raise LoadError(f"{path.name}: expected two Match Statistics tables, "
                        f"found {len(game.clubs)}")

    for club, lines in game.players.items():
        by_name = details.get(club, {})
        for line in lines:
            extra = by_name.get(normalise_name(line.source_name))
            if extra is not None:
                line.age_text = extra.age_text
                line.career_games_text = extra.career_games_text
                line.career_goals_text = extra.career_goals_text
    return game


def read_directory(folder: Path, summary_name: str | None
                   ) -> tuple[list[Fixture], list[GameFile], list[Path]]:
    """Split a folder into the round summary, its game files and the rest."""
    candidates = sorted(p for p in folder.glob("*.csv") if p.is_file())
    if not candidates:
        raise LoadError(f"{folder}: no .csv files")

    # A game file identifies itself by its "<club> Match Statistics" heading,
    # so the split needs no filename convention -- which is the point, given
    # that a hand-assembled folder tends to collect misnamed and duplicated
    # copies.
    games: list[GameFile] = []
    summaries: list[Path] = []
    for path in candidates:
        game = parse_game_file(path)
        if game is None:
            summaries.append(path)
        else:
            games.append(game)

    if summary_name:
        chosen = [p for p in summaries if p.name.lower() == summary_name.lower()]
        if not chosen:
            raise LoadError(f"{folder}: no summary file named {summary_name!r}")
        summary = chosen[0]
    elif len(summaries) == 1:
        summary = summaries[0]
    else:
        raise LoadError(
            f"{folder}: expected exactly one round summary, found "
            f"{len(summaries)} ({', '.join(p.name for p in summaries)}). "
            f"Name the right one with --summary.")

    fixtures = parse_round_summary(summary)
    others = [p for p in summaries if p != summary]
    return fixtures, games, others


def pair_games(fixtures: list[Fixture], games: list[GameFile]
               ) -> tuple[dict, list[GameFile], list[tuple[Path, Path]]]:
    """Match each game file to its fixture by the two club names it names."""
    wanted = {fixture.clubs: fixture for fixture in fixtures}
    paired: dict[frozenset[str], GameFile] = {}
    unused: list[GameFile] = []
    duplicates: list[tuple[Path, Path]] = []

    for game in games:
        key = frozenset(game.clubs)
        if key not in wanted:
            unused.append(game)
            continue
        if key in paired:
            first, second = paired[key].path.name, game.path.name
            if _same_content(paired[key], game):
                # A duplicate copy of a file already paired, usually a stale
                # rename. Identical content, so it is dropped rather than
                # treated as a conflict -- but say which file was used, or a
                # misnamed copy looks like the one that loaded.
                duplicates.append((game.path, paired[key].path))
                continue
            raise LoadError(
                f"two different files both claim {' v '.join(sorted(key))}: "
                f"{first} and {second}")
        paired[key] = game

    missing = [f for f in fixtures if f.clubs not in paired]
    if missing:
        raise LoadError("no game file for: " + "; ".join(
            f"{f.home.club} v {f.away.club}" for f in missing))
    return paired, unused, duplicates


def _same_content(first: GameFile, second: GameFile) -> bool:
    def shape(game: GameFile):
        return {club: [(line.jumper, line.source_name, tuple(sorted(
            line.stats.items(), key=lambda item: item[0])))
            for line in lines]
            for club, lines in game.players.items()}
    return shape(first) == shape(second)


def describe_fixture_gaps(fixture: Fixture, found: Findings) -> None:
    """Fields the season page sometimes leaves out. Absent, not wrong."""
    missing = [label for label, value in (("venue", fixture.venue),
                                          ("crowd", fixture.attendance),
                                          ("start time", fixture.match_time))
               if value is None]
    if missing:
        found.note("fixture detail",
                   f"{fixture.home.club} v {fixture.away.club}: no "
                   f"{', '.join(missing)} in the summary")


def describe_rushed_behinds(fixtures: list[Fixture], paired: dict,
                            found: Findings) -> None:
    """Behinds credited to no player, which is what a rushed behind is.

    check_against_summary already refuses an *excess*. A shortfall is normal
    and is the one quantity in the round that no player row accounts for, so
    the total is reported rather than passed over. One line for the round, not
    one per club: every side has some, and eighteen lines saying so would bury
    the findings that need reading.
    """
    rushed = total = 0
    for fixture in fixtures:
        game = paired[fixture.clubs]
        for side in (fixture.home, fixture.away):
            behinds = sum(line.stats.get("behinds") or 0
                          for line in game.players[side.club])
            rushed += side.quarters[-1][1] - int(behinds)
            total += side.quarters[-1][1]
    if rushed:
        found.note("rushed behinds",
                   f"{rushed} of the round's {total} behinds belong to no "
                   f"player, as rushed behinds do")


def describe_identities(roster: Roster, resolved: dict,
                        found: Findings) -> None:
    """Say which player a shared name was taken to mean, and on what grounds.

    Recording only "14 resolved by era" leaves the actual decisions invisible,
    and these are the decisions most worth being able to check by eye: the
    round is full of names belonging to more than one player.
    """
    def sharing(name: str, display: str) -> int:
        return len(roster.indexed.get(normalise_name(name), set())
                   or roster.named.get(normalise_name(display), set()))

    by_era = 0
    for (club, name), (player_id, display, how) in sorted(resolved.items()):
        if how == "debut":
            found.fixed("debut", f"{display} ({club}) matches no player on "
                                 f"record; treated as a first game")
        elif how == "ambiguous":
            found.not_fixed(
                "ambiguous name",
                f"{name} ({club}) could be any of {sharing(name, display)} "
                f"players and neither the season nor the club separates them")
        elif how.endswith("+club"):
            # Two people of the same name playing in the same season: the
            # only thing between them is which club the row was pasted under,
            # so each of these is worth showing individually.
            found.fixed(
                "shared name, both current",
                f"{name} plays for two clubs this season; the {club} row was "
                f"read as player_id {player_id}")
        elif how.endswith("+era"):
            by_era += 1
    if by_era:
        found.note("shared name, different eras",
                   f"{by_era} names also belong to a player from another era; "
                   f"the season being loaded ruled those out")


def check_career_totals(con: sqlite3.Connection, season: int, round_name: str,
                        paired: dict, resolved: dict, *,
                        before: str | None = None) -> list[str]:
    """Every player's stated career games must be one more than we hold.

    AFL Tables counts the match being read, so a player with 174 games in the
    database is listed at 175 and a debutant at 1. That makes the Player
    Details column an independent check on the identity decision, catching
    both ways a name can go wrong:

    * a debut that is really a failed match -- the source says 175 career
      games and we hold none, so a new player_id would split a real career
      in two;
    * a match to the wrong namesake -- we hold 48 games for an Archie Roberts
      who last played in 1937 and the source says 46.

    It also catches a game missing from the database underneath, which is
    the same evidence read from the other direction.

    `before` is the round's first match date, and games are counted up to
    it. Without one the count falls back to every game that is not this
    round, which is the same number only while rounds arrive in order --
    and they need not. A round played later can already be in the database
    by hand while an earlier one is still missing upstream, and then every
    player who appears in both is reported as one game ahead of himself:
    the later game is counted as an earlier one, 366 names are refused at
    once, and the round that is actually correct will not load.
    """
    counted = {}
    if before:
        rows = con.execute(
            "SELECT player_id, COUNT(*) FROM games WHERE date < ? "
            "GROUP BY player_id", (before,))
    else:
        rows = con.execute(
            "SELECT player_id, COUNT(*) FROM games "
            "WHERE NOT (season = ? AND round = ?) GROUP BY player_id",
            (season, str(round_name)))
    for player_id, total in rows:
        counted[player_id] = total

    notes = []
    for game in paired.values():
        for club, lines in game.players.items():
            for line in lines:
                if not line.career_games_text:
                    continue
                found = CAREER_RE.match(line.career_games_text)
                if not found:
                    continue
                stated = int(found.group("games"))
                player_id = resolved[(club, line.source_name)][0]
                held = counted.get(player_id, 0) if player_id else 0
                if stated == held + 1:
                    continue
                notes.append(
                    f"{line.source_name} ({club}): AFL Tables says this is "
                    f"career game {stated}, the database holds {held} "
                    f"earlier game(s)"
                    + (" and no player of that name" if not player_id else ""))
    return notes


def check_against_summary(fixture: Fixture, game: GameFile) -> list[str]:
    """Do the player rows add up to the score the summary states?"""
    notes = []
    for side in (fixture.home, fixture.away):
        lines = game.players[side.club]
        goals = sum(line.stats.get("goals") or 0 for line in lines)
        behinds = sum(line.stats.get("behinds") or 0 for line in lines)
        stated_goals, stated_behinds = side.quarters[-1]
        if int(goals) != stated_goals:
            notes.append(
                f"{side.club}: players kicked {int(goals)} goals, summary "
                f"says {stated_goals}")
        # Rushed behinds are on the team line, not any player, so the player
        # behinds legitimately fall short. Only an excess is a real problem.
        if int(behinds) > stated_behinds:
            notes.append(
                f"{side.club}: players recorded {int(behinds)} behinds, more "
                f"than the summary's {stated_behinds}")
    return notes


# --------------------------------------------------------------------------
# identity


def date_minus_age(when: str, age_text: str | None) -> str | None:
    """Invert AFL Tables' "28y 303d" against the match date to get a DOB."""
    if not age_text:
        return None
    match = AGE_RE.search(age_text)
    if not match:
        return None
    played = date.fromisoformat(when) - timedelta(days=int(match.group("days")))
    year = played.year - int(match.group("years"))
    try:
        born = played.replace(year=year)
    except ValueError:                      # 29 February
        born = played.replace(year=year, day=28)
    return born.isoformat()


@dataclass
class Roster:
    """Everything needed to turn a pasted name into the right player_id.

    A name is not a key. 460 names in afltables_player_index belong to more
    than one player -- there are two Bailey Williamses playing right now, one
    at the Western Bulldogs and one at West Coast -- so a lookup that takes
    the first match will happily give one player both of their games. Every
    lookup here therefore returns a set, and a set larger than one is settled
    by the club the row was pasted under, or not at all.
    """
    indexed: dict[str, set[int]]
    named: dict[str, set[int]]
    clubs: dict[int, set[str]]
    span: dict[int, tuple[int | None, int | None]]


def player_lookup(con: sqlite3.Connection) -> Roster:
    indexed: dict[str, set[int]] = {}
    for name, pid in con.execute(
            "SELECT source_name, player_id FROM afltables_player_index "
            "WHERE player_id IS NOT NULL"):
        indexed.setdefault(normalise_name(name), set()).add(pid)

    named: dict[str, set[int]] = {}
    clubs: dict[int, set[str]] = {}
    span: dict[int, tuple[int | None, int | None]] = {}
    for pid, name, hist, now, debut, final in con.execute(
            "SELECT player_id, player, clubs_hist, clubs_now, debut_season, "
            "final_season FROM players"):
        named.setdefault(normalise_name(name), set()).add(pid)
        clubs[pid] = {part.strip() for part in
                      (str(hist or "") + "|" + str(now or "")).split("|")
                      if part.strip()}
        span[pid] = (debut, final)
    return Roster(indexed=indexed, named=named, clubs=clubs, span=span)


def flip_name(source_name: str) -> str:
    """"Ah Chee, Callum" -> "Callum Ah Chee"."""
    surname, _, given = source_name.partition(",")
    return f"{given.strip()} {surname.strip()}".strip() if given else source_name.strip()


def resolve(roster: Roster, source_name: str, club_hist: str, club_now: str,
            season: int) -> tuple[int | None, str, str]:
    """-> (player_id or None, display name, how it was resolved).

    The audited index is tried first and the players roster second; either can
    return several people sharing the name. Two filters then narrow the set,
    in order:

    * **era** -- a 2026 match cannot have been played by someone who last
      played in 1937. This separates a modern player from a namesake decades
      earlier, which the club cannot: there are two Archie Robertses and both
      played for Essendon.
    * **club** -- the club the row was pasted under. This separates
      contemporaries, such as the two Bailey Williamses currently playing, one
      at the Western Bulldogs and one at West Coast.

    A name that survives both filters with more than one candidate is reported
    as ambiguous rather than guessed. Picking one would silently credit a
    stranger with someone else's game, and quietly extend a dead man's career.
    """
    display = flip_name(source_name)
    for how, candidates in (
            ("index", roster.indexed.get(normalise_name(source_name), set())),
            ("roster", roster.named.get(normalise_name(display), set()))):
        if not candidates:
            continue
        if len(candidates) == 1:
            return next(iter(candidates)), display, how

        why = how
        active = {pid for pid in candidates if _plausible(roster, pid, season)}
        if len(active) == 1:
            return next(iter(active)), display, f"{how}+era"
        if active:
            candidates, why = active, f"{how}+era"

        narrowed = {pid for pid in candidates
                    if roster.clubs.get(pid, set()) & {club_hist, club_now}}
        if len(narrowed) == 1:
            return next(iter(narrowed)), display, f"{why}+club"
        return None, display, "ambiguous"
    return None, display, "debut"


def _plausible(roster: Roster, player_id: int, season: int) -> bool:
    """Could this player have played in `season`?

    The window is open at the top by one season: a player whose final_season
    is the season being loaded is mid-career, and so is one whose last
    recorded game is the round before this one.
    """
    debut, final = roster.span.get(player_id, (None, None))
    if debut is not None and debut > season:
        return False
    if final is not None and final < season - 1:
        return False
    return True


# --------------------------------------------------------------------------
# schema


def create_schema(con: sqlite3.Connection) -> None:
    """The durable store, plus whatever the All Games pipeline needs."""
    numeric = set(STAT_COLUMNS.values()) | {
        "season", "is_home", "points_for", "points_against", "is_final",
        "player_id", "career_game_no"}
    defs = ",\n            ".join(
        f"{name} {'REAL' if name in STAT_COLUMNS.values() else 'INTEGER'}"
        if name in numeric else f"{name} TEXT"
        for name in STORE_COLUMNS)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS manual_round_games (
            {defs},
            PRIMARY KEY (season, round, club_hist, source_name)
        )""")
    for statement in (
        "CREATE INDEX IF NOT EXISTS ix_mrg_round "
        "ON manual_round_games(season, round)",
        "CREATE INDEX IF NOT EXISTS ix_mrg_player "
        "ON manual_round_games(player_id)",
    ):
        con.execute(statement)
    load_club_all_games.create_schema(con)
    con.commit()


# --------------------------------------------------------------------------
# store


def club_id_for(name: str) -> str:
    for club_id, club in ALL_GAMES_BY_ID.items():
        if club.name == name:
            return club_id
    raise LoadError(f"unknown club {name!r} -- it is not in club_sources.py")


def game_key(home: str, away: str, match_date: str) -> tuple[str, str, str]:
    """AFL Tables' order-independent key: {low}{high}{YYYYMMDD}."""
    codes = sorted(CLUB_CODES[club_id_for(club)] for club in (home, away))
    if len(set(codes)) != 2:
        raise LoadError(f"{home} v {away}: could not tell the clubs apart")
    stamp = match_date.replace("-", "")
    return codes[0], codes[1], f"{codes[0]}{codes[1]}{stamp}"


def store(con: sqlite3.Connection, season: int, round_name: str,
          fixtures: list[Fixture], paired: dict, resolved: dict) -> dict:
    """Write the parsed round to the two tables that survive a rebuild."""
    now = utc_now()
    stats = {"player_rows": 0, "fixtures": 0}
    player_rows = []
    source_rows = []

    for fixture in fixtures:
        game = paired[fixture.clubs]
        stats["fixtures"] += 1
        for side, other in ((fixture.home, fixture.away),
                            (fixture.away, fixture.home)):
            is_home = side is fixture.home
            result = ("D" if side.score == other.score else
                      "W" if side.score > other.score else "L")
            club_now = ALL_GAMES_BY_ID[club_id_for(side.club)].db_club_now
            for line in game.players[side.club]:
                pid, display, _how = resolved[(side.club, line.source_name)]
                career = None
                if line.career_games_text:
                    found = CAREER_RE.match(line.career_games_text)
                    career = int(found.group("games")) if found else None
                values = {
                    "season": season, "round": round_name,
                    "match_date": fixture.match_date, "venue": fixture.venue,
                    "club_hist": side.club, "club_now": club_now,
                    "opponent": other.club, "is_home": int(is_home),
                    "result": result, "points_for": side.score,
                    "points_against": other.score, "is_final": 0,
                    "jumper": line.jumper, "source_name": line.source_name,
                    "player_id": pid, "player": display,
                    "career_game_no": career, "age_text": line.age_text,
                    "career_games_text": line.career_games_text,
                    "career_goals_text": line.career_goals_text,
                    "source_file": game.path.name, "imported_at": now,
                }
                values.update(line.stats)
                player_rows.append(tuple(values[name] for name in STORE_COLUMNS))
                stats["player_rows"] += 1

        source_rows.extend(_source_rows(season, round_name, fixture, now))

    con.executemany(
        f"INSERT OR REPLACE INTO manual_round_games "
        f"({', '.join(STORE_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in STORE_COLUMNS)})", player_rows)

    names = load_club_all_games.SOURCE_COLUMNS + ["imported_at"]
    payload = [tuple(row[name] for name in names) for row in source_rows]
    # Both tables, always: club_match_sources is the live view the club
    # pages read, and manual_round_fixtures is the copy that survives the
    # rebuild which clears that view. Writing only the first is what let a
    # round vanish from the app while its player rows stayed put.
    for table in ("club_match_sources",
                  load_club_all_games.MANUAL_SOURCE_TABLE):
        con.executemany(
            f"INSERT OR REPLACE INTO {table} ({', '.join(names)}) "
            f"VALUES ({', '.join('?' for _ in names)})", payload)
    con.commit()
    return stats


def _source_rows(season: int, round_name: str, fixture: Fixture,
                 now: str) -> list[dict]:
    """The match as two rows, one per club, in the All Games scrape's shape.

    The table's grain is one row per club per match -- its primary key says
    so, and afl/club_history.py reads a club's history straight out of it. A
    single row per match would therefore drop the fixture from the away
    club's history entirely, so both sides are written, each from its own
    point of view.

    ``source_game_url`` is left NULL: the key is the real AFL Tables game key,
    derived from the two club codes and the date, but no page was fetched and
    the row must not claim one was. When the All Games scrape does reach this
    round it writes the same two keys, and its rows replace these.
    """
    home, away = fixture.home, fixture.away
    low, high, key = game_key(home.club, away.club, fixture.match_date)
    shared = {
        "season": season,
        "round": f"R{round_name}" if str(round_name).isdigit() else round_name,
        "is_final": 0,
        "venue_raw": fixture.venue,
        "attendance": fixture.attendance,
        "date_text": fixture.match_date,
        "match_date": fixture.match_date,
        "match_time": fixture.match_time,
        "match_datetime": (f"{fixture.match_date}T{fixture.match_time}"
                           if fixture.match_time else None),
        "source_game_url": None,
        "source_game_key": key,
        "team_code_low": low,
        "team_code_high": high,
        "home_team_raw": home.club,
        "away_team_raw": away.club,
        "imported_at": now,
    }

    rows = []
    for side, other, position in ((home, away, "H"), (away, home, "A")):
        row = {name: None for name in load_club_all_games.SOURCE_COLUMNS}
        row.update(shared)
        row.update({
            "source_club_id": club_id_for(side.club),
            "source_club_label": side.club,
            "team_position": position,
            "opponent_raw": other.club,
            "scoring_for_raw": " ".join(f"{g}.{b}" for g, b in side.quarters),
            "scoring_against_raw": " ".join(f"{g}.{b}"
                                            for g, b in other.quarters),
            "points_for": side.score,
            "points_against": other.score,
            "result": ("D" if side.score == other.score else
                       "W" if side.score > other.score else "L"),
            "margin": side.score - other.score,
        })
        for label, quarters in (("for", side.quarters),
                                ("against", other.quarters)):
            for index, (goals, behinds) in enumerate(quarters, start=1):
                row[f"q{index}_{label}_goals"] = goals
                row[f"q{index}_{label}_behinds"] = behinds
                row[f"q{index}_{label}_points"] = goals * 6 + behinds
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# apply


def stored_rounds(con: sqlite3.Connection) -> list[tuple[int, str]]:
    if not table_exists(con, "manual_round_games"):
        return []
    return [(int(season), str(name)) for season, name in con.execute(
        "SELECT DISTINCT season, round FROM manual_round_games "
        "ORDER BY season, CAST(round AS INTEGER), round")]


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def upstream_has(con: sqlite3.Connection, season: int, round_name: str) -> bool:
    """Has a rebuild produced this round from the upstream dataset?

    Only meaningful on the --apply-only path, which runs straight after a
    rebuild. A rebuild replaces `games` wholesale, so at that moment there are
    none of our rows left and any row for the round means fitzRoy has
    published it and is now the better source. An explicit CSV load does not
    ask this question: the rows it finds may well be its own from last time,
    and the user asking to load a round is intent enough.
    """
    return con.execute(
        "SELECT 1 FROM games WHERE season = ? AND round = ? LIMIT 1",
        (season, str(round_name))).fetchone() is not None


def _birth_columns(con: sqlite3.Connection, player_id: int) -> tuple:
    """Carry a player's birth columns forward onto their new game row.

    The three are looked up independently: a player can have birth_est on
    every row and dob on none, so a single query filtered on dob would drop
    the estimate as well. A debutant has no earlier row at all and falls back
    to the players row created for them, which carries an exact date.
    """
    row = con.execute(
        "SELECT (SELECT dob FROM games WHERE player_id = ?1 "
        "        AND dob IS NOT NULL ORDER BY date DESC LIMIT 1), "
        "       (SELECT birth_est FROM games WHERE player_id = ?1 "
        "        AND birth_est IS NOT NULL ORDER BY date DESC LIMIT 1), "
        "       (SELECT birth_year_est FROM games WHERE player_id = ?1 "
        "        AND birth_year_est IS NOT NULL ORDER BY date DESC LIMIT 1)",
        (player_id,)).fetchone()
    if row and row[0] is not None:
        return tuple(row)

    fallback = con.execute(
        "SELECT dob, birth_year FROM players WHERE player_id = ?",
        (player_id,)).fetchone()
    if not fallback or not fallback[0]:
        return tuple(row) if row else (None, None, None)
    try:
        born = datetime.strptime(fallback[0], "%d-%b-%Y").date()
    except ValueError:
        return tuple(row) if row else (None, None, None)
    return (fallback[0], born.isoformat(), born.year)


def create_debutants(con: sqlite3.Connection, season: int,
                     round_name: str) -> list[tuple[int, str]]:
    """Give every debutant a player_id and a players row.

    A debutant has no history to inherit, so the row is built from what the
    Player Details table states: an exact date of birth (the match date less
    the stated age) and the club played for. The obscurity columns are left
    NULL for utils/shared/recompute_obscurity.py, which is the only thing
    that should ever write them.

    Two cases arrive here. On a first load the stored row has no player_id
    and one is minted. After a rebuild it has the id we minted last time but
    the players row itself was replaced away, so the row is recreated *under
    that same id* -- a new one would leave the games rows pointing at a
    player who does not exist, and would break any saved reference to them.
    """
    pending = con.execute(
        "SELECT DISTINCT g.player_id, g.source_name, g.player, g.club_hist, "
        "g.club_now, g.match_date, g.age_text, g.career_goals_text "
        "FROM manual_round_games g "
        "LEFT JOIN players p ON p.player_id = g.player_id "
        "WHERE g.season = ? AND g.round = ? AND p.player_id IS NULL "
        "ORDER BY g.source_name", (season, round_name)).fetchall()
    if not pending:
        return []

    next_id = (con.execute(
        "SELECT MAX(player_id) FROM players").fetchone()[0] or 0) + 1
    # Insert only the columns this players table actually has. The replay
    # runs at two very different moments: over the live database (which
    # carries every later loader's columns, club_path_* included) and
    # inside a rebuild, straight after the core build -- where those
    # columns do not exist yet and naming them aborted the whole replay,
    # losing the round. The later loaders derive their own columns for
    # every player, debutants included, so omitting them here is not a
    # gap.
    present = {row[1] for row in con.execute("PRAGMA table_info(players)")}
    created = []
    for (stored_id, source_name, display, club_hist, club_now, match_date,
         age_text, goals_text) in pending:
        player_id = stored_id if stored_id is not None else next_id
        if stored_id is None:
            next_id += 1
        born = date_minus_age(match_date, age_text)
        goals = 0.0
        if goals_text:
            found = CAREER_RE.match(goals_text)
            goals = float(found.group("games")) if found else 0.0
        year = int(born[:4]) if born else None
        values = {
            "player_id": player_id, "player": display,
            "dob": _afltables_dob(born),
            "birth_year": year, "birth_year_min": year,
            "birth_year_max": year,
            "debut_season": season, "final_season": season,
            "career_games": 0, "career_goals": goals,
            "career_brownlow": None, "finals_played": 0,
            "clubs_hist": club_hist, "clubs_now": club_now, "n_clubs": 1,
            "name_key": normalise_name(display),
            "club_path_hist": club_hist, "club_path_now": club_now,
        }
        columns = [column for column in values if column in present]
        con.execute(
            f"INSERT INTO players ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            [values[column] for column in columns])
        con.execute(
            "UPDATE manual_round_games SET player_id = ? "
            "WHERE season = ? AND round = ? AND source_name = ?",
            (player_id, season, round_name, source_name))
        created.append((player_id, display))
    con.commit()
    return created


def _afltables_dob(iso: str | None) -> str | None:
    """ISO date -> the players.dob spelling, "9-Jan-2004"."""
    if not iso:
        return None
    born = date.fromisoformat(iso)
    return f"{born.day}-{born.strftime('%b-%Y')}"


def apply_games(con: sqlite3.Connection, season: int, round_name: str) -> int:
    """Write the stored round into `games`, replacing any earlier attempt.

    The INSERT names only the columns this games table actually has. The
    replay runs at two very different moments: over the live database,
    whose games table carries every later step's columns (match_id from
    derive_matches, match_event from the marquee tagger), and inside a
    rebuild straight after the core build, where match_event does not
    exist yet and naming it aborted the whole replay. A column a later
    step owns is simply left for that step.
    """
    con.execute("DELETE FROM games WHERE season = ? AND round = ?",
                (season, round_name))
    rows = con.execute(
        "SELECT player_id, player, season, round, match_date, venue, "
        "club_hist, club_now, career_game_no, "
        + ", ".join(STAT_COLUMNS.values()) +
        ", opponent, is_home, result, points_for, points_against, is_final "
        "FROM manual_round_games WHERE season = ? AND round = ? "
        "AND player_id IS NOT NULL", (season, round_name)).fetchall()

    birth_cache: dict[int, tuple] = {}
    payload = []
    for row in rows:
        player_id = row[0]
        if player_id not in birth_cache:
            birth_cache[player_id] = _birth_columns(con, player_id)
        payload.append(tuple(list(row[:9]) + list(birth_cache[player_id])
                             + list(row[9:]) + [None, None]))

    present = {row[1] for row in con.execute("PRAGMA table_info(games)")}
    columns = [c for c in GAME_COLUMNS if c in present]
    index_of = {c: i for i, c in enumerate(GAME_COLUMNS)}
    projected = [tuple(full[index_of[c]] for c in columns)
                 for full in payload]
    con.executemany(
        f"INSERT INTO games ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})", projected)
    con.commit()
    return len(projected)


def refresh_player_totals(con: sqlite3.Connection, season: int,
                          round_name: str) -> int:
    """Bring `players` into line with the rows just added.

    afl/build_db.py derives these from the whole games table. Adding a round
    underneath it would otherwise leave career_games one behind, and the
    Player Search summary disagreeing with the game list below it.
    """
    updated = con.execute(
        """UPDATE players SET
             career_games = (SELECT COUNT(*) FROM games g
                             WHERE g.player_id = players.player_id),
             career_goals = (SELECT COALESCE(SUM(g.goals), 0) FROM games g
                             WHERE g.player_id = players.player_id),
             final_season = (SELECT MAX(g.season) FROM games g
                             WHERE g.player_id = players.player_id)
           WHERE player_id IN (SELECT player_id FROM manual_round_games
                               WHERE season = ? AND round = ?
                                 AND player_id IS NOT NULL)""",
        (season, round_name)).rowcount
    con.commit()
    return updated


#: The phases a load moves through, in order.
#:
#: Named here rather than inside `load` so a caller can size a progress bar
#: before starting, and so the admin page and the desktop window describe
#: the same run in the same words.
#:
#: The phases are nothing like equal. Measured on the 2026 database, the
#: first four take a tenth of a second between them, deriving matches
#: takes about six, and rebuilding the ladder and club paths -- which
#: walks all 694,000 games rows in player order -- takes about twenty-two
#: on its own. A bar that sits on the last phase for most of the run is
#: therefore working, and the label is what says so.
LOAD_PHASES = (
    "Reading the round folder",
    "Checking scores against the summary",
    "Identifying players",
    "Checking career totals",
    "Storing the round",
    "Writing the games rows",
    "Deriving matches",
    "Restoring crowds and quarter scores",
    "Rebuilding the ladder and club paths",
)


def _reporter(callback):
    """Wrap a progress callback so `load` can just name the phase it is on.

    Always returns something callable, so the phases are announced the same
    way whether or not anybody is listening.
    """
    def report(label: str) -> None:
        if callback is None:
            return
        try:
            step = LOAD_PHASES.index(label) + 1
        except ValueError:
            step = 0
        try:
            callback(step, len(LOAD_PHASES), label)
        except Exception:
            # A progress display is never worth failing a load over.
            pass
    return report


def apply_stored(con: sqlite3.Connection, db_path: Path, season: int,
                 round_name: str, *, defer_to_upstream: bool = False,
                 report=None) -> dict:
    """Stored rows -> games, players, matches, match_details."""
    from afl import derive_matches

    report = report or _reporter(None)
    if defer_to_upstream and upstream_has(con, season, round_name):
        print(f"  round {round_name} {season}: already in games from the "
              f"upstream build; not re-applied")
        return {"skipped": True}

    report("Writing the games rows")
    created = create_debutants(con, season, round_name)
    for player_id, name in created:
        print(f"  new player {player_id}  {name}")
    written = apply_games(con, season, round_name)
    print(f"  wrote {written:,} rows into games")

    con.commit()
    report("Deriving matches")
    derive_matches.run(str(db_path))

    # Only where the club-page layer exists. Inside a rebuild the replay
    # runs before that layer has loaded, and its own loader re-links and
    # re-mirrors everything when it does -- skipping here loses nothing.
    if table_exists(con, "club_match_sources"):
        report("Restoring crowds and quarter scores")
        # A replay after a rebuild finds the fixture rows gone: the store
        # survived, the live view did not. Put them back before linking,
        # so the round reappears in the club pages and its crowds and
        # quarter scores reach `matches` exactly as on a first load.
        restored = load_club_all_games.restore_manual_sources(con)
        if restored:
            print(f"  restored {restored:,} fixture rows into "
                  f"club_match_sources")
        counts = load_club_all_games.link_sources(con)
        print("  link: " + ", ".join(
            f"{k}={v}" for k, v in counts.items() if v))
        stats = load_club_all_games.apply_details(con)
        print("  details: " + ", ".join(
            f"{k}={v}" for k, v in stats.items() if v))
        load_club_all_games.mirror_onto_matches(con)
    else:
        print("  crowds and quarter scores left for the club-page loader "
              "(club_match_sources not loaded yet)")
    touched = refresh_player_totals(con, season, round_name)
    print(f"  refreshed career totals for {touched:,} players")
    return {"skipped": False, "games": written, "created": created}


def report_round(con: sqlite3.Connection, season: int, round_name: str) -> None:
    total, clubs, days = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT club_hist), COUNT(DISTINCT date) "
        "FROM games WHERE season = ? AND round = ?",
        (season, round_name)).fetchone()
    linked = con.execute(
        "SELECT COUNT(*) FROM games WHERE season = ? AND round = ? "
        "AND match_id IS NOT NULL", (season, round_name)).fetchone()[0]
    detail = con.execute(
        "SELECT COUNT(*) FROM match_details d JOIN matches m USING(match_id) "
        "WHERE m.season = ? AND m.round = ?", (season, round_name)).fetchone()[0]
    quarters, attendance = con.execute(
        "SELECT COUNT(*) FILTER (WHERE home_q1 IS NOT NULL), "
        "COUNT(*) FILTER (WHERE attendance IS NOT NULL) "
        "FROM matches WHERE season = ? AND round = ?",
        (season, round_name)).fetchone()
    print(f"\nRound {round_name}, {season}:")
    print(f"  games          {total:,} rows, {clubs} clubs, {days} match days")
    print(f"  match_id       {linked:,} of {total:,} linked")
    print(f"  match_details  {detail} matches")
    print(f"  matches        {quarters} with quarter scores, "
          f"{attendance} with attendance")


# --------------------------------------------------------------------------
# entry points


def load(db_path: Path, folder: Path, season: int, round_name: str, *,
         summary: str | None = None, dry_run: bool = False,
         on_progress=None) -> int:
    """Read a round from CSVs and apply it.

    `on_progress(step, total, label)` is called as each phase begins, for a
    caller that has somewhere to show it. The admin page writes them into
    the job's status file so the browser can follow a load it did not
    start; the CLI passes nothing and prints as it always has.
    """
    report = _reporter(on_progress)

    report("Reading the round folder")
    fixtures, games, ignored = read_directory(folder, summary)
    paired, unused, duplicates = pair_games(fixtures, games)

    # Remembered once the folder has proved to hold a round, including on a
    # dry run: checking a folder is how you say "this is where they live now".
    remember_dir(folder)

    print(f"{folder}")
    print(f"  {len(fixtures)} fixtures, {len(paired)} game files paired")

    found = Findings()
    for path in ignored + [game.path for game in unused]:
        found.fixed("file ignored", f"{path.name} is not part of this round")
    for copy, used in duplicates:
        found.fixed("duplicate file",
                    f"{copy.name} is identical to {used.name}; used {used.name}")
    for fixture in fixtures:
        describe_fixture_gaps(fixture, found)

    report("Checking scores against the summary")
    notes = []
    for fixture in fixtures:
        notes.extend(check_against_summary(fixture, paired[fixture.clubs]))
    if notes:
        for note in notes:
            found.not_fixed("score mismatch", note)
        found.report()
        raise LoadError("player stats disagree with the round summary")
    print("  score checks: player goals agree with every quarter total")
    describe_rushed_behinds(fixtures, paired, found)

    con = sqlite3.connect(db_path)
    try:
        report("Identifying players")
        roster = player_lookup(con)
        resolved: dict[tuple[str, str], tuple] = {}
        how: dict[str, int] = {}
        for fixture in fixtures:
            game = paired[fixture.clubs]
            for club, lines in game.players.items():
                club_now = ALL_GAMES_BY_ID[club_id_for(club)].db_club_now
                for line in lines:
                    answer = resolve(roster, line.source_name, club, club_now,
                                     season)
                    resolved[(club, line.source_name)] = answer
                    how[answer[2]] = how.get(answer[2], 0) + 1
        print("  players: " + ", ".join(
            f"{count} {kind}" for kind, count in sorted(how.items())))
        describe_identities(roster, resolved, found)

        if how.get("ambiguous"):
            found.report()
            raise LoadError("a name matches more than one player and the club "
                            "does not separate them; fix the mapping in "
                            "afltables_player_index before loading")

        report("Checking career totals")
        # The first day of the round is the line between "already played"
        # and "this game or later", and it is the only one that holds when
        # the database already carries a round played after this one.
        first_day = min((fixture.match_date for fixture in fixtures
                         if fixture.match_date), default=None)
        careers = check_career_totals(con, season, round_name, paired,
                                      resolved, before=first_day)
        if careers:
            for note in careers:
                found.not_fixed("career total", note)
            found.report()
            raise LoadError(
                "the stated career games do not follow on from the database; "
                "a name has matched the wrong player, or a game is missing "
                "underneath this round")
        print("  career checks: every total follows on from the database")

        if dry_run:
            found.report("Findings (nothing was written)")
            print("\n--dry-run: nothing written")
            return 0

        report("Storing the round")
        create_schema(con)
        stats = store(con, season, round_name, fixtures, paired, resolved)
        print(f"\nStored {stats['player_rows']:,} player rows and "
              f"{stats['fixtures']} fixtures")
        outcome = apply_stored(con, db_path, season, round_name,
                               report=report)
        for player_id, name in outcome.get("created", []):
            found.fixed("new player",
                        f"{name} debuted; created as player_id {player_id}")

        # The ladder and the club paths are derived from `games`, so a round
        # added underneath them leaves both a round behind. afl/build_db.py
        # runs this itself after its own rebuild -- and after the --apply-only
        # hook -- so only the explicit load has to ask for it.
        con.commit()
        print()
        report("Rebuilding the ladder and club paths")
        from utils.shared import repair_database
        repair_database.run(str(db_path))

        if outcome.get("created"):
            found.note("obscurity",
                       "new players are unscored until you run "
                       "`python -m utils.shared.recompute_obscurity "
                       "--sport afl`")
        report_round(con, season, round_name)
        found.report("Findings (round written)")
    finally:
        con.close()
    return 0


def apply_only(db_path: Path, *, force: bool = False) -> int:
    """Re-apply every stored round. Called by afl/build_db.py after a rebuild."""
    con = sqlite3.connect(db_path)
    try:
        rounds = stored_rounds(con)
        if not rounds:
            return 0
        print(f"Re-applying {len(rounds)} hand-entered round(s)")
        for season, round_name in rounds:
            print(f" round {round_name} {season}:")
            apply_stored(con, db_path, season, round_name,
                         defer_to_upstream=not force)
    finally:
        con.close()
    return 0


def forget(db_path: Path, season: int, round_name: str) -> int:
    """Drop a stored round, once the upstream dataset carries it.

    The club_match_sources rows are only dropped if they are still the
    hand-entered ones -- a NULL source_game_url is what marks them. Once the
    All Games scrape has replaced a row with a real fetched page, that row is
    the scrape's and is left alone.
    """
    con = sqlite3.connect(db_path)
    try:
        if not table_exists(con, "manual_round_games"):
            print("Nothing stored.")
            return 0
        source_round = (f"R{round_name}" if str(round_name).isdigit()
                        else round_name)
        removed = con.execute(
            "DELETE FROM manual_round_games WHERE season = ? AND round = ?",
            (season, str(round_name))).rowcount
        sources = con.execute(
            "DELETE FROM club_match_sources WHERE season = ? AND round = ? "
            "AND source_game_url IS NULL", (season, source_round)).rowcount
        # The durable copy goes with them, or the next rebuild would put
        # the forgotten round straight back.
        if table_exists(con, load_club_all_games.MANUAL_SOURCE_TABLE):
            con.execute(
                f"DELETE FROM {load_club_all_games.MANUAL_SOURCE_TABLE} "
                f"WHERE season = ? AND round = ?", (season, source_round))
        con.commit()
        print(f"Dropped {removed:,} stored player rows and {sources} fixture "
              f"rows for round {round_name}, {season}.")
    finally:
        con.close()
    return 0


def _backup(db_path: Path, keep: int = 5) -> Path | None:
    """Copy the database aside before an irreversible change.

    Same folder and naming as database_updates.py's promote step, so the
    two kinds of backup rotate together and a reader does not have to know
    which one made a given file.
    """
    import shutil

    if keep <= 0 or not db_path.exists():
        return None
    folder = db_path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    backup = folder / f"{db_path.stem}-{stamp}{db_path.suffix}"
    shutil.copy2(db_path, backup)
    stored = sorted(folder.glob(f"{db_path.stem}-*{db_path.suffix}"),
                    key=lambda item: item.stat().st_mtime_ns, reverse=True)
    for stale in stored[keep:]:
        stale.unlink(missing_ok=True)
    return backup


def _round_footprint(con: sqlite3.Connection, season: int,
                     round_name: str) -> dict:
    """Everything a hand-entered round put in the database."""
    source_round = (f"R{round_name}" if str(round_name).isdigit()
                    else round_name)
    counts = {}
    for label, sql, params in (
        ("games", "SELECT COUNT(*) FROM games WHERE season=? AND round=?",
         (season, str(round_name))),
        ("manual_round_games",
         "SELECT COUNT(*) FROM manual_round_games WHERE season=? AND round=?",
         (season, str(round_name))),
        ("matches", "SELECT COUNT(*) FROM matches WHERE season=? AND round=?",
         (season, str(round_name))),
        ("club_match_sources",
         "SELECT COUNT(*) FROM club_match_sources WHERE season=? AND round=? "
         "AND source_game_url IS NULL", (season, source_round)),
    ):
        if not table_exists(con, label):
            counts[label] = 0
            continue
        counts[label] = con.execute(sql, params).fetchone()[0]
    counts["match_details"] = con.execute(
        "SELECT COUNT(*) FROM match_details WHERE match_id IN "
        "(SELECT match_id FROM matches WHERE season=? AND round=?)",
        (season, str(round_name))).fetchone()[0] if table_exists(
            con, "match_details") else 0
    return counts


def _upstream_owns(con: sqlite3.Connection, season: int,
                   round_name: str) -> str | None:
    """Why this round is not safe to remove, or None when it is.

    Removal is for a round that exists only because somebody typed it in.
    Once AFL Tables carries the round, the rows are the scrape's and this
    would be deleting real data to no purpose -- `--apply-only` already
    defers to the rebuild and `--forget` already drops the stored copy.
    """
    source_round = (f"R{round_name}" if str(round_name).isdigit()
                    else round_name)
    if table_exists(con, "afltables_match_scores"):
        audited = con.execute(
            "SELECT COUNT(*) FROM afltables_match_scores "
            "WHERE season=? AND round=?",
            (season, str(round_name))).fetchone()[0]
        if audited:
            return (f"afltables_match_scores carries {audited} match(es) for "
                    f"round {round_name}, {season}: AFL Tables has published "
                    "this round, so it is no longer hand-entered data")
    if table_exists(con, "club_match_sources"):
        fetched = con.execute(
            "SELECT COUNT(*) FROM club_match_sources WHERE season=? AND "
            "round=? AND source_game_url IS NOT NULL",
            (season, source_round)).fetchone()[0]
        if fetched:
            return (f"club_match_sources holds {fetched} fetched fixture "
                    f"row(s) for round {round_name}, {season}: the All Games "
                    "scrape has replaced the hand-entered ones")
    return None


def remove(db_path: Path, season: int, round_name: str, *,
           dry_run: bool = False, keep_backups: int = 5,
           force: bool = False) -> int:
    """Undo a hand-entered round completely -- the inverse of a load.

    `--forget` is the other half of the story and does something quite
    different: it drops the stored rows once AFL Tables carries the round,
    deliberately leaving `games` and `matches` alone because by then they
    are the rebuild's. This removes the round itself, for a round that
    should never have been entered.

    The order matters, and it is the load's order run backwards:

      1. `games`, `manual_round_games` and the hand-entered fixture rows go.
      2. `afl.derive_matches` rebuilds `matches` from what is left, which
         is what actually drops the round's matches -- and, because it
         replaces the whole table, also blanks every match's crowd and
         quarter scores.
      3. `load_club_all_games` puts those back from `club_match_sources`,
         exactly as `apply_stored` does after a load. Skipping this step
         is the one way to turn removing nine matches into silently
         emptying seventeen thousand.
      4. Orphaned `match_details` rows go. They are keyed on match_id and
         `derive_matches` hands a freed id to the next new match, so a
         stale row left here would later attach itself to a different
         match entirely.
      5. Career totals are recomputed for everyone who played, and any
         player the load invented is dropped.
      6. `repair_database` rebuilds the ladder and the club paths, which
         `load` runs for the same reason: both are derived from `games`,
         so a round taken out from under them leaves both a round ahead.

    What is deliberately not touched is what the load did not touch either
    -- `finals_played`, `best_disposals`, `best_goals` and the obscurity
    components are all left to the full rebuild, so removing a round puts
    them back exactly where loading it left them.
    """
    from afl import derive_matches

    con = sqlite3.connect(db_path)
    try:
        if not table_exists(con, "manual_round_games"):
            print("Nothing stored: this database has no manual_round_games.")
            return 1
        counts = _round_footprint(con, season, round_name)
        if not any(counts.values()):
            print(f"Nothing to remove for round {round_name}, {season}.")
            return 1

        blocked = _upstream_owns(con, season, round_name)
        if blocked and not force:
            print(f"Refusing to remove round {round_name}, {season}.")
            print(f"  {blocked}.")
            print("  Use --forget to drop the stored copy instead, or "
                  "--force to remove it anyway.")
            return 1
        if blocked:
            print(f"warning: --force given; {blocked}")

        players = [row[0] for row in con.execute(
            "SELECT DISTINCT player_id FROM games "
            "WHERE season=? AND round=? AND player_id IS NOT NULL",
            (season, str(round_name)))]

        print(f"Round {round_name}, {season} will lose:")
        for label in ("matches", "match_details", "games",
                      "manual_round_games", "club_match_sources"):
            print(f"  {counts[label]:>6,}  {label}")
        print(f"  {len(players):>6,}  players whose career totals are "
              f"recomputed")
        if dry_run:
            print("\n--dry-run: nothing written")
            return 0
    finally:
        con.close()

    backup = _backup(db_path, keep_backups)
    if backup:
        print(f"\nBacked up to {backup}")

    source_round = (f"R{round_name}" if str(round_name).isdigit()
                    else round_name)
    con = sqlite3.connect(db_path)
    try:
        con.execute("DELETE FROM games WHERE season=? AND round=?",
                    (season, str(round_name)))
        con.execute(
            "DELETE FROM manual_round_games WHERE season=? AND round=?",
            (season, str(round_name)))
        con.execute(
            "DELETE FROM club_match_sources WHERE season=? AND round=? "
            "AND source_game_url IS NULL", (season, source_round))
        if table_exists(con, load_club_all_games.MANUAL_SOURCE_TABLE):
            con.execute(
                f"DELETE FROM {load_club_all_games.MANUAL_SOURCE_TABLE} "
                f"WHERE season=? AND round=?", (season, source_round))
        con.commit()
    finally:
        con.close()

    # Rebuilds `matches` from `games`, dropping the round's matches and
    # blanking every match's crowd and quarter scores in the process.
    derive_matches.run(str(db_path))

    invented: list[tuple] = []
    con = sqlite3.connect(db_path)
    try:
        counts_link = load_club_all_games.link_sources(con)
        print("  link: " + ", ".join(
            f"{k}={v}" for k, v in counts_link.items() if v))
        stats = load_club_all_games.apply_details(con)
        print("  details: " + ", ".join(
            f"{k}={v}" for k, v in stats.items() if v))
        restored = load_club_all_games.mirror_onto_matches(con)
        print(f"  restored crowd and quarter scores on {restored:,} matches")

        orphans = con.execute(
            "DELETE FROM match_details WHERE match_id NOT IN "
            "(SELECT match_id FROM matches)").rowcount
        if orphans:
            print(f"  dropped {orphans:,} orphaned match_details row(s)")

        if players:
            marks = ",".join("?" for _ in players)
            con.execute(
                f"""UPDATE players SET
                      career_games = (SELECT COUNT(*) FROM games g
                                      WHERE g.player_id = players.player_id),
                      career_goals = (SELECT COALESCE(SUM(g.goals), 0)
                                      FROM games g
                                      WHERE g.player_id = players.player_id),
                      final_season = (SELECT MAX(g.season) FROM games g
                                      WHERE g.player_id = players.player_id)
                    WHERE player_id IN ({marks})""", players)
            # A player with no games left is one this load invented, since
            # every other player in the table has at least one.
            invented = con.execute(
                f"SELECT player_id, player FROM players WHERE player_id IN "
                f"({marks}) AND career_games = 0", players).fetchall()
            for player_id, name in invented:
                print(f"  dropping player {player_id}  {name} "
                      "(no games left)")
            if invented:
                con.executemany("DELETE FROM players WHERE player_id = ?",
                                [(player_id,) for player_id, _ in invented])
            print(f"  recomputed career totals for "
                  f"{len(players) - len(invented):,} players")
        con.commit()
    finally:
        con.close()

    # The ladder and the club paths are derived from `games`, exactly as
    # they are after a load, and both are rebuilt wholesale from what is
    # left rather than adjusted -- so they come back to the state they were
    # in before the round was ever entered.
    print()
    from utils.shared import repair_database
    repair_database.run(str(db_path))

    print(f"\nRemoved round {round_name}, {season}. "
          f"Reload it with --dir <folder> --season {season} "
          f"--round {round_name}.")
    if invented:
        print("Obscurity is a percentile over the players who remain; run "
              "`python -m utils.shared.recompute_obscurity --sport afl` to "
              "rescore after dropping any.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Load a hand-entered AFL Tables round into the database.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--dir", type=Path,
                        help="folder holding the round summary and game files; "
                             "defaults to the last one loaded from")
    parser.add_argument("--season", type=int)
    parser.add_argument("--round", dest="round_name",
                        help="round name as games records it, e.g. 23 or GF")
    parser.add_argument("--summary",
                        help="filename of the round summary, when the folder "
                             "holds more than one non-game CSV")
    parser.add_argument("--dry-run", action="store_true",
                        help="parse, check and resolve without writing")
    parser.add_argument("--apply-only", action="store_true",
                        help="re-apply stored rounds after a rebuild")
    parser.add_argument("--force", action="store_true",
                        help="with --apply-only, re-apply a stored round even "
                             "if the upstream build now carries it")
    parser.add_argument("--forget", nargs=2, metavar=("SEASON", "ROUND"),
                        help="drop a stored round once upstream carries it")
    parser.add_argument("--remove", nargs=2, metavar=("SEASON", "ROUND"),
                        help="undo a hand-entered round completely: its "
                             "games, matches, stored rows and fixture rows, "
                             "with career totals recomputed")
    parser.add_argument("--keep-backups", type=int, default=5,
                        help="backups to retain before a --remove "
                             "(default 5; 0 disables the backup)")
    args = parser.parse_args(argv)

    if not args.db.exists():
        parser.error(f"no database at {args.db}")

    try:
        if args.remove:
            return remove(args.db, int(args.remove[0]), args.remove[1],
                          dry_run=args.dry_run, force=args.force,
                          keep_backups=args.keep_backups)
        if args.forget:
            return forget(args.db, int(args.forget[0]), args.forget[1])
        if args.apply_only:
            return apply_only(args.db, force=args.force)
        folder = args.dir or remembered_dir()
        if not folder:
            parser.error("--dir is required the first time; after that it "
                         "defaults to the last folder loaded from")
        if not (args.season and args.round_name):
            parser.error("--season and --round are required unless "
                         "--apply-only or --forget is given")
        if not folder.is_dir():
            parser.error(f"not a directory: {folder}")
        if not args.dir:
            print(f"using remembered folder {folder}")
        return load(args.db, folder, args.season, args.round_name,
                    summary=args.summary, dry_run=args.dry_run)
    except LoadError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
