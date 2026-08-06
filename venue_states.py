"""Shared US jurisdiction lookups for NFL, MLB and NBA venues.

The grid databases deliberately keep the source venue text unchanged.  This
module is the one place that translates those names to a US jurisdiction, so
the three sports do not each grow a slightly different list of states.

"52 jurisdictions" in the UI means the 50 states plus Washington, D.C. and
Puerto Rico.  Canadian and overseas venues are intentionally not assigned to
a US state.
"""

from __future__ import annotations

import csv
import sqlite3
from collections import Counter, defaultdict
from functools import lru_cache

import data_paths

US_JURISDICTIONS = (
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"),
    ("AR", "Arkansas"), ("CA", "California"), ("CO", "Colorado"),
    ("CT", "Connecticut"), ("DE", "Delaware"), ("FL", "Florida"),
    ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"),
    ("IL", "Illinois"), ("IN", "Indiana"), ("IA", "Iowa"),
    ("KS", "Kansas"), ("KY", "Kentucky"), ("LA", "Louisiana"),
    ("ME", "Maine"), ("MD", "Maryland"), ("MA", "Massachusetts"),
    ("MI", "Michigan"), ("MN", "Minnesota"), ("MS", "Mississippi"),
    ("MO", "Missouri"), ("MT", "Montana"), ("NE", "Nebraska"),
    ("NV", "Nevada"), ("NH", "New Hampshire"), ("NJ", "New Jersey"),
    ("NM", "New Mexico"), ("NY", "New York"),
    ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"),
    ("OK", "Oklahoma"), ("OR", "Oregon"), ("PA", "Pennsylvania"),
    ("RI", "Rhode Island"), ("SC", "South Carolina"),
    ("SD", "South Dakota"), ("TN", "Tennessee"), ("TX", "Texas"),
    ("UT", "Utah"), ("VT", "Vermont"), ("VA", "Virginia"),
    ("WA", "Washington"), ("WV", "West Virginia"),
    ("WI", "Wisconsin"), ("WY", "Wyoming"),
    ("DC", "Washington, D.C."), ("PR", "Puerto Rico"),
)

STATE_NAMES = tuple(name for _, name in US_JURISDICTIONS)
_STATE_CODES = {name.casefold(): code for code, name in US_JURISDICTIONS}
_STATE_CODES.update({code.casefold(): code for code, _ in US_JURISDICTIONS})


def state_code(value: str) -> str:
    """Return the postal code for a selector label or postal code."""
    try:
        return _STATE_CODES[str(value).strip().casefold()]
    except KeyError as exc:
        raise ValueError(f"Unknown US state or jurisdiction: {value}") from exc


# nflverse's schedule has a stable stadium_id even when naming rights change.
# The leading location code is therefore safer than maintaining every sponsor
# name (FedExField/Commanders Field, Heinz/Acrisure, and so on).
_NFL_STADIUM_PREFIX_STATES = {
    "ATL": "GA", "BAL": "MD", "BOS": "MA", "BRG": "LA",
    "BUF": "NY", "CAR": "NC", "CHI": "IL", "CIN": "OH",
    "CLE": "OH", "DAL": "TX", "DEN": "CO", "DET": "MI",
    "GNB": "WI", "HOU": "TX", "IND": "IN", "JAX": "FL",
    "KAN": "MO", "LAX": "CA", "MIA": "FL", "MIN": "MN",
    "NAS": "TN", "NOR": "LA", "NYC": "NJ", "OAK": "CA",
    "PHI": "PA", "PHO": "AZ", "PIT": "PA", "SAN": "TX",
    "SEA": "WA", "SFO": "CA", "STL": "MO", "TAM": "FL",
    "VEG": "NV", "WAS": "MD",
}


_NBA_ARENA_STATES = {
    "American Airlines Center": "TX", "Ball Arena": "CO",
    "Barclays Center": "NY", "Capital One Arena": "DC",
    "Chase Center": "CA", "Crypto.com Arena": "CA",
    "Delta Center": "UT", "FedExForum": "TN", "Fiserv Forum": "WI",
    "Frost Bank Center": "TX", "Gainbridge Fieldhouse": "IN",
    "Golden 1 Center": "CA", "Intuit Dome": "CA", "Kaseya Center": "FL",
    "Kia Center": "FL", "Little Caesars Arena": "MI",
    "Madison Square Garden": "NY", "Moda Center": "OR",
    "Moody Center": "TX", "Mortgage Matchup Center": "AZ",
    "Paycom Center": "OK", "Rocket Arena": "OH",
    "Smoothie King Center": "LA", "Spectrum Center": "NC",
    "State Farm Arena": "GA", "T-Mobile Arena": "NV",
    "Target Center": "MN", "TD Garden": "MA", "Toyota Center": "TX",
    "United Center": "IL", "Xfinity Mobile Arena": "PA",
}


@lru_cache(maxsize=1)
def _nfl_venue_states() -> dict[str, str]:
    """Read the names actually present and classify them by stadium ID."""
    db = data_paths.sport_db("nfl")
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT DISTINCT stadium, stadium_id FROM matches "
            "WHERE stadium IS NOT NULL AND stadium_id IS NOT NULL"
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return {}
    return {
        str(venue): _NFL_STADIUM_PREFIX_STATES[prefix]
        for venue, stadium_id in rows
        if (prefix := str(stadium_id)[:3]) in _NFL_STADIUM_PREFIX_STATES
    }


@lru_cache(maxsize=1)
def _mlb_venue_states() -> dict[str, str]:
    """Derive Lahman's season-level park labels from its park catalogue.

    ``games.venue`` comes from Teams.csv, while city/state lives in Parks.csv
    and the bridge between them is HomeGames.csv.  The most-used home park is
    the season label's state; one-off neutral-site games therefore cannot
    move a team's whole player-season to the wrong state.
    """
    raw = data_paths.raw_dir("mlb")
    required = (raw / "Parks.csv", raw / "HomeGames.csv", raw / "Teams.csv")
    if not all(path.exists() for path in required):
        return {}

    with required[0].open(encoding="utf-8-sig", newline="") as handle:
        park_rows = list(csv.DictReader(handle))
    parks = {row["parkkey"]: row for row in park_rows}

    homes: dict[tuple[int, str], list[dict]] = defaultdict(list)
    with required[1].open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            homes[(int(row["yearkey"]), row["teamkey"])].append(row)

    scores: dict[str, Counter] = defaultdict(Counter)
    with required[2].open(encoding="utf-8-sig", newline="") as handle:
        for team in csv.DictReader(handle):
            venue = (team.get("park") or "").strip()
            key = (int(team["yearID"]), team["teamID"])
            for home in homes.get(key, ()):
                park = parks.get(home["parkkey"])
                if park and park.get("country") == "US":
                    scores[venue][park["state"]] += int(home.get("games") or 0)

    # Parks.csv also names aliases that Teams.csv uses directly.  These fill
    # historical/Negro-league labels that have no HomeGames bridge.
    aliases: dict[str, set[str]] = defaultdict(set)
    for park in park_rows:
        if park.get("country") != "US":
            continue
        names = [park.get("parkname", ""),
                 *(park.get("parkalias", "").split(";"))]
        for name in names:
            if name.strip():
                aliases[name.strip()].add(park["state"])

    result = {venue: counts.most_common(1)[0][0]
              for venue, counts in scores.items() if counts}
    result.update({venue: next(iter(states)) for venue, states in aliases.items()
                   if venue not in result and len(states) == 1})

    # Unambiguous spelling variants in Teams.csv that the catalogue does not
    # repeat verbatim. Canadian parks remain deliberately unmapped.
    result.update({
        "Steinbrenner Field ": "FL", "Neil Park": "OH",
        "South Side Park": "IL", "Ponce de Leon Park": "GA",
    })
    return result


def venues_for_state(sport_key: str, state: str) -> tuple[str, ...]:
    """Venue names in one US jurisdiction for a supported sport."""
    code = state_code(state)
    key = sport_key.strip().lower()
    if key == "nfl":
        mapping = _nfl_venue_states()
    elif key == "mlb":
        mapping = _mlb_venue_states()
    elif key == "nba":
        mapping = _NBA_ARENA_STATES
    else:
        return ()
    return tuple(sorted(venue for venue, venue_state in mapping.items()
                        if venue_state == code))
