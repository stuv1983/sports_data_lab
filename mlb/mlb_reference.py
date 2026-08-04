"""
mlb_reference.py -- Franchise names, lineage and stat eras, for import time.

Same problem and same shape as nba/nba_reference.py: core.Schema is a frozen
dataclass built while sports.py is being imported, long before a database is
open, but the franchise list and the lineage behind it are properties of the
*built data*. build_mlb_db.py measures all three from Lahman's Teams.csv and
TeamsFranchises.csv and writes data/mlb/reference/mlb_reference.json; this
module reads that file back.

The fallbacks below are what makes a clean clone work -- without them
`import sports` would hand the MLB schema an empty franchise list that only
breaks later inside a widget. They are the 30 current franchises, nothing
measured.

As with the NBA: because the values are frozen into the schema at import,
**Streamlit must be restarted after a build that changes the franchise list
or the measured eras.** The db_revision cache key invalidates queries, not a
dataclass.
"""

import json

import data_paths

PATH = data_paths.reference_dir("mlb") / "mlb_reference.json"


#: The 30 current franchises, under the names Lahman's TeamsFranchises.csv
#: gives them. Superseded by the build's measured list the moment PATH
#: exists.
FALLBACK_TEAMS = (
    "Arizona Diamondbacks", "Atlanta Braves", "Baltimore Orioles",
    "Boston Red Sox", "Chicago Cubs", "Chicago White Sox",
    "Cincinnati Reds", "Cleveland Guardians", "Colorado Rockies",
    "Detroit Tigers", "Houston Astros", "Kansas City Royals",
    "Los Angeles Angels", "Los Angeles Dodgers", "Miami Marlins",
    "Milwaukee Brewers", "Minnesota Twins", "New York Mets",
    "New York Yankees", "Oakland Athletics", "Philadelphia Phillies",
    "Pittsburgh Pirates", "San Diego Padres", "San Francisco Giants",
    "Seattle Mariners", "St. Louis Cardinals", "Tampa Bay Rays",
    "Texas Rangers", "Toronto Blue Jays", "Washington Nationals",
)

#: Current franchise -> every identity that counts as it. Measured by the
#: build from franchID, so the fallback is empty rather than a guess: an
#: incomplete lineage silently drops players from a club square, and no
#: lineage at all at least fails the same way for every franchise.
FALLBACK_LINEAGE: dict = {}

#: Statistic -> first season Lahman records it. Measured by the build.
#:
#: Every one of them is 1871 in the current export, which is a real fact
#: about the file rather than a placeholder: the modern Lahman release
#: backfills RBI and stolen-base columns to the first season. It is not the
#: same as full coverage -- thousands of 19th-century batting lines leave
#: those columns NULL -- and sports.MLB.empty_hint says so, because an era
#: cutoff cannot express "recorded, patchily".
FALLBACK_STAT_ERAS = {
    "at_bats": 1871, "doubles": 1871, "era": 1871, "hits": 1871,
    "home_runs": 1871, "losses": 1871, "rbis": 1871, "runs": 1871,
    "saves": 1871, "stolen_bases": 1871, "strikeouts": 1871,
    "triples": 1871, "walks": 1871, "wins": 1871,
}


def load():
    """The reference file as a dict, or {} if it is absent or unreadable.

    Degrading to the fallbacks is deliberate: a corrupt reference file must
    never break `import sports`.
    """
    try:
        with open(PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def teams():
    """Every current franchise, for the club picker."""
    measured = load().get("teams")
    if isinstance(measured, list) and measured:
        return tuple(measured)
    return FALLBACK_TEAMS


def club_lineage():
    """Current franchise -> the historical names that count as it."""
    measured = load().get("club_lineage")
    if isinstance(measured, dict) and measured:
        return {k: list(v) for k, v in measured.items()}
    return dict(FALLBACK_LINEAGE)


def venue_aliases():
    """Ballpark alias -> the name the database stores.

    Lahman gives one park per team-season and spells it consistently, so
    there is nothing to alias yet. The hook exists because core.Schema
    takes the field and explore.py's venue page reads it.
    """
    measured = load().get("venue_aliases")
    return dict(measured) if isinstance(measured, dict) else {}


def stat_eras():
    """Statistic -> the first season it is actually recorded."""
    measured = load().get("stat_eras")
    if isinstance(measured, dict) and measured:
        return {k: int(v) for k, v in measured.items()}
    return dict(FALLBACK_STAT_ERAS)
