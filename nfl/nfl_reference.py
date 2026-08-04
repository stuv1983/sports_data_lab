"""
nfl/nfl_reference.py -- Teams, lineage and stat eras, for import time.

core.Schema is a frozen dataclass built while sports.py is imported, before
any database is open, but three of its NFL fields are properties of the
built data: which statistic columns nflreadpy actually returned, which
season each of them starts in, and which teams appear. nfl/patch_nfl_db.py
measures all three from the built database -- the stat eras come straight
out of its `stat_coverage` table -- and writes them to
data/nfl/reference/nfl_reference.json, which this module reads back.

The fallbacks are what makes a clean clone import. They are deliberately
plain facts, not a guess at the dataset: nflverse's weekly player statistics
begin in 1999, so every fallback era is 1999.

The team abbreviation is the stable key here, not the team name. nflverse
names have changed within a single franchise and a single code (WAS three
times since 1999), so ABBREVIATION_NOW maps the code a row carries to the
franchise it counts as today, and the name catalogue is only ever a display
lookup.

Because the values are frozen into the schema at import, Streamlit must be
restarted after a patch that changes the measured lists.
"""

import json

import data_paths

PATH = data_paths.reference_dir("nfl") / "nfl_reference.json"


#: The 32 current franchises, which is what a team picker should offer.
FALLBACK_TEAMS = (
    "Arizona Cardinals", "Atlanta Falcons", "Baltimore Ravens",
    "Buffalo Bills", "Carolina Panthers", "Chicago Bears",
    "Cincinnati Bengals", "Cleveland Browns", "Dallas Cowboys",
    "Denver Broncos", "Detroit Lions", "Green Bay Packers",
    "Houston Texans", "Indianapolis Colts", "Jacksonville Jaguars",
    "Kansas City Chiefs", "Las Vegas Raiders", "Los Angeles Chargers",
    "Los Angeles Rams", "Miami Dolphins", "Minnesota Vikings",
    "New England Patriots", "New Orleans Saints", "New York Giants",
    "New York Jets", "Philadelphia Eagles", "Pittsburgh Steelers",
    "San Francisco 49ers", "Seattle Seahawks", "Tampa Bay Buccaneers",
    "Tennessee Titans", "Washington Commanders",
)

#: Team code -> the franchise that code counts as today. Every code the
#: weekly data has carried since 1999 is here; the older ones matter for
#: `matches` and `rosters`, which reach back to 1920.
#:
#: Expansion is one-directional, as core.Schema.club_identities defines it:
#: a Las Vegas square includes the Oakland years, and an Oakland square
#: returns only Oakland. patch_nfl_db.py writes `club_now` from this map and
#: `club_hist` from the name the row was played under.
ABBREVIATION_NOW = {
    "ARI": "Arizona Cardinals", "ARZ": "Arizona Cardinals",
    "PHO": "Arizona Cardinals", "CRD": "Arizona Cardinals",
    "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens", "RAV": "Baltimore Ravens",
    "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",
    "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",
    "CLE": "Cleveland Browns", "CLV": "Cleveland Browns",
    "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos",
    "DET": "Detroit Lions",
    "GB": "Green Bay Packers", "GNB": "Green Bay Packers",
    "HOU": "Houston Texans", "HTX": "Houston Texans",
    "IND": "Indianapolis Colts", "CLT": "Indianapolis Colts",
    "JAX": "Jacksonville Jaguars", "JAC": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "KAN": "Kansas City Chiefs",
    "LV": "Las Vegas Raiders", "OAK": "Las Vegas Raiders",
    "RAI": "Las Vegas Raiders",
    "LAC": "Los Angeles Chargers", "SD": "Los Angeles Chargers",
    "SDG": "Los Angeles Chargers",
    "LAR": "Los Angeles Rams", "LA": "Los Angeles Rams",
    "STL": "Los Angeles Rams", "RAM": "Los Angeles Rams",
    "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NWE": "New England Patriots",
    "NO": "New Orleans Saints", "NOR": "New Orleans Saints",
    "NYG": "New York Giants",
    "NYJ": "New York Jets",
    "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers",
    "SF": "San Francisco 49ers", "SFO": "San Francisco 49ers",
    "SEA": "Seattle Seahawks",
    "TB": "Tampa Bay Buccaneers", "TAM": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "OTI": "Tennessee Titans",
    "WAS": "Washington Commanders", "WSH": "Washington Commanders",
}

#: Current franchise -> every identity a square for it should match. Only
#: relocations and renamings a solver would name separately are listed.
FALLBACK_LINEAGE = {
    "Las Vegas Raiders": ["Las Vegas Raiders", "Oakland Raiders",
                          "Los Angeles Raiders"],
    "Los Angeles Chargers": ["Los Angeles Chargers", "San Diego Chargers"],
    "Los Angeles Rams": ["Los Angeles Rams", "St. Louis Rams",
                         "Cleveland Rams"],
    "Tennessee Titans": ["Tennessee Titans", "Tennessee Oilers",
                         "Houston Oilers"],
    "Washington Commanders": ["Washington Commanders",
                              "Washington Football Team",
                              "Washington Redskins"],
    "Indianapolis Colts": ["Indianapolis Colts", "Baltimore Colts"],
    "Arizona Cardinals": ["Arizona Cardinals", "Phoenix Cardinals",
                          "St. Louis Cardinals", "Chicago Cardinals"],
    "New England Patriots": ["New England Patriots", "Boston Patriots"],
    "Kansas City Chiefs": ["Kansas City Chiefs", "Dallas Texans"],
    "New York Jets": ["New York Jets", "New York Titans"],
}

#: The statistics offered as grid axes, from the ~130 numeric columns the
#: weekly dataset carries. Curated rather than exhaustive: a stat picker
#: listing passing_cpoe, wopr and fg_missed_50_59 is not usable, and none of
#: those is a question anyone asks of a player.
#:
#: The names are nflverse's own and are the ones to check first after an
#: nflreadpy upgrade -- `interceptions` and `sacks` are already gone, split
#: into the thrown and defensive halves below. patch_nfl_db.py reports any
#: name here that the built `games` table does not have.
#:
#: `touchdowns` is not an nflverse column: patch_nfl_db.py derives it, and
#: it is the sport's headline counting stat.
FALLBACK_STATS = (
    "touchdowns", "passing_yards", "passing_tds", "passing_interceptions",
    "completions", "attempts", "carries", "rushing_yards", "rushing_tds",
    "receptions", "targets", "receiving_yards", "receiving_tds",
    "special_teams_tds", "def_sacks", "def_tackles_solo",
    "def_interceptions", "fg_made", "fantasy_points",
)

#: nflverse weekly player statistics begin in 1999. Anything measured from
#: `stat_coverage` supersedes this the moment the reference file exists.
FIRST_STAT_SEASON = 1999


def load():
    """The measured reference file, or {} when it is absent or unreadable."""
    try:
        with open(PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_measured():
    return bool(load())


def teams():
    found = load().get("teams")
    return list(found) if found else list(FALLBACK_TEAMS)


def club_lineage():
    found = load().get("club_lineage")
    return dict(found) if found else dict(FALLBACK_LINEAGE)


def codes_for(club):
    """Every team code that counts as this franchise.

    The draft and roster tables key on codes, not names, and they do not
    agree on which code: `draft_picks` carries the Pro-Football-Reference
    forms (NWE, GNB), `players.draft_team` the nflverse ones (NE, GB). Both
    are in ABBREVIATION_NOW, so both resolve here.
    """
    return sorted(code for code, name in ABBREVIATION_NOW.items()
                  if name == club)


def venue_aliases():
    """Stadium aliases. Empty until the built venues have been looked at."""
    return dict(load().get("venue_aliases") or {})


def stats():
    """Statistic columns the build actually produced, in display order."""
    found = load().get("stats")
    return list(found) if found else list(FALLBACK_STATS)


def stat_eras():
    """Stat -> first season carrying a real value, from stat_coverage."""
    found = load().get("stat_eras")
    if found:
        return {k: int(v) for k, v in found.items()}
    return {stat: FIRST_STAT_SEASON for stat in FALLBACK_STATS}


def summary():
    """One line for the Database Status panel."""
    if is_measured():
        data = load()
        lo, hi = (data.get("seasons") or [None, None])[:2]
        span = f" {lo}-{hi}" if lo and hi else ""
        return f"measured{span} ({len(stats())} statistics)"
    return f"provisional fallback ({len(FALLBACK_STATS)} statistics)"
