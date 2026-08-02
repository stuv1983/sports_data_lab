#!/usr/bin/env python3
"""Shared 18-club source manifest and parsing helpers.

The visible club model intentionally contains only the current AFL clubs.
Historical names remain properties of the match database; Brisbane Bears and
Fitzroy are not aliases of Brisbane Lions and are never rolled into its totals.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAW_DIR = PROJECT_ROOT / "data" / "afl" / "raw" / "clubs"


@dataclass(frozen=True)
class ClubSource:
    club_id: str
    name: str
    wikipedia_title: str
    afltables_slug: str
    db_club_now: str
    abbreviation: str

    @property
    def wikipedia_url(self) -> str:
        title = self.wikipedia_title.replace(" ", "_")
        return f"https://en.wikipedia.org/wiki/{title}"

    @property
    def afltables_player_totals_url(self) -> str:
        return f"https://afltables.com/afl/stats/teams/{self.afltables_slug}.html"

    @property
    def afltables_records_url(self) -> str:
        return (
            "https://afltables.com/afl/stats/teams/"
            f"{self.afltables_slug}/playershi.html"
        )

    @property
    def afltables_all_games_url(self) -> str:
        return f"https://afltables.com/afl/teams/{self.afltables_slug}/allgames.html"

    @property
    def afltables_all_time_url(self) -> str:
        return (
            "https://afltables.com/afl/stats/alltime/"
            f"{self.afltables_slug}.html"
        )


CLUBS: tuple[ClubSource, ...] = (
    ClubSource("adelaide", "Adelaide", "Adelaide Football Club", "adelaide", "Adelaide", "ADE"),
    ClubSource("brisbane_lions", "Brisbane Lions", "Brisbane Lions", "brisbanel", "Brisbane Lions", "BL"),
    ClubSource("carlton", "Carlton", "Carlton Football Club", "carlton", "Carlton", "CARL"),
    ClubSource("collingwood", "Collingwood", "Collingwood Football Club", "collingwood", "Collingwood", "COLL"),
    ClubSource("essendon", "Essendon", "Essendon Football Club", "essendon", "Essendon", "ESS"),
    ClubSource("fremantle", "Fremantle", "Fremantle Football Club", "fremantle", "Fremantle", "FRE"),
    ClubSource("geelong", "Geelong", "Geelong Football Club", "geelong", "Geelong", "GEEL"),
    ClubSource("gold_coast", "Gold Coast", "Gold Coast Suns", "goldcoast", "Gold Coast", "GC"),
    ClubSource("gws", "Greater Western Sydney", "Greater Western Sydney Giants", "gws", "GWS", "GWS"),
    ClubSource("hawthorn", "Hawthorn", "Hawthorn Football Club", "hawthorn", "Hawthorn", "HAW"),
    ClubSource("melbourne", "Melbourne", "Melbourne Football Club", "melbourne", "Melbourne", "MELB"),
    ClubSource("north_melbourne", "North Melbourne", "North Melbourne Football Club", "kangaroos", "North Melbourne", "NM"),
    ClubSource("port_adelaide", "Port Adelaide", "Port Adelaide Football Club", "padelaide", "Port Adelaide", "PA"),
    ClubSource("richmond", "Richmond", "Richmond Football Club", "richmond", "Richmond", "RICH"),
    ClubSource("st_kilda", "St Kilda", "St Kilda Football Club", "stkilda", "St Kilda", "STK"),
    ClubSource("sydney", "Sydney", "Sydney Swans", "swans", "Sydney", "SYD"),
    ClubSource("west_coast", "West Coast", "West Coast Eagles", "westcoast", "West Coast", "WCE"),
    ClubSource("western_bulldogs", "Western Bulldogs", "Western Bulldogs", "bullldogs", "Western Bulldogs", "WB"),
)

CLUB_BY_ID = {club.club_id: club for club in CLUBS}

# Source-only identities. These are NOT current clubs and never appear in the
# Club Explorer or the 18-club catalogue: they exist so that All Games pages
# cover the whole match history. Brisbane Bears and Fitzroy stay separate from
# Brisbane Lions. South Melbourne and Footscray are already covered by the
# Sydney and Western Bulldogs pages, so they are not repeated here.
HISTORICAL_SOURCES: tuple[ClubSource, ...] = (
    ClubSource("brisbane_bears", "Brisbane Bears", "Brisbane Bears",
               "brisbaneb", "Brisbane Bears", "BB"),
    ClubSource("fitzroy", "Fitzroy", "Fitzroy Football Club",
               "fitzroy", "Fitzroy", "FITZ"),
    ClubSource("university", "University", "University Football Club",
               "university", "University", "UNI"),
)

ALL_GAMES_SOURCES: tuple[ClubSource, ...] = CLUBS + HISTORICAL_SOURCES
ALL_GAMES_BY_ID = {club.club_id: club for club in ALL_GAMES_SOURCES}


def all_games_clubs(club_ids: list[str] | None = None) -> list[ClubSource]:
    """Clubs whose All Games pages are required for complete match coverage."""
    if not club_ids:
        return list(ALL_GAMES_SOURCES)
    unknown = sorted(set(club_ids) - set(ALL_GAMES_BY_ID))
    if unknown:
        raise ValueError(f"unknown club id(s): {', '.join(unknown)}")
    return [ALL_GAMES_BY_ID[cid] for cid in club_ids]

SOURCE_FILES = {
    "wikipedia": "wikipedia.json",
    "afltables_player_totals": "afltables_player_totals.html",
    "afltables_records": "afltables_records.html",
    "afltables_all_time": "afltables_all_time_players.html",
    "afltables_all_games": "afltables_all_games.html",
}

STAT_HEADERS = {
    "GM": "games", "KI": "kicks", "MK": "marks", "HB": "handballs",
    "DI": "disposals", "GL": "goals", "BH": "behinds", "HO": "hitouts",
    "TK": "tackles", "RB": "rebounds", "IF": "inside50s",
    "CL": "clearances", "CG": "clangers", "FF": "frees_for",
    "FA": "frees_against", "BR": "brownlow", "CP": "contested",
    "UP": "uncontested", "CM": "contested_marks", "MI": "marks_i50",
    "1%": "one_percenters", "BO": "bounces", "GA": "goal_assists",
}


def selected_clubs(club_ids: list[str] | None = None) -> list[ClubSource]:
    if not club_ids:
        return list(CLUBS)
    unknown = sorted(set(club_ids) - set(CLUB_BY_ID))
    if unknown:
        raise ValueError(f"unknown club id(s): {', '.join(unknown)}")
    return [CLUB_BY_ID[cid] for cid in club_ids]


def source_path(raw_dir: Path, club: ClubSource, source_type: str) -> Path:
    return raw_dir / club.club_id / SOURCE_FILES[source_type]


def clean_text(value: object) -> str:
    text = str(value or "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def source_name_to_display(name: str) -> str:
    """Convert AFL Tables 'Surname, Given' to the database display form."""
    name = clean_text(name)
    if "," not in name:
        return name
    family, given = (part.strip() for part in name.split(",", 1))
    return f"{given} {family}".strip()


def fallback_name_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", source_name_to_display(value))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold().replace("’", "'")
    return re.sub(r"[^a-z0-9]+", "", value)
