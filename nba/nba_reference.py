"""
nba_reference.py -- Team names, lineage and stat eras, for import time.

core.Schema is a frozen dataclass constructed while sports.py is being
imported, long before any database is open. But three of the NBA schema's
fields are properties of the *built data*: which teams exist, which
historical identities belong to which franchise, and which season each
statistic actually starts in. build_nba_db.py discovers all three and
writes them to data/nba/reference/nba_reference.json; this module reads
that file back.

The fallbacks below are what makes a clean clone work. Without them
`import sports` would fail, or worse, produce an NBA schema with an empty
club list that only breaks later inside a widget. They are deliberately
plain facts (the 30 current franchises and the relocations that change an
answer), not a guess at the whole dataset -- everything measured comes from
the build.

`load()` returning {} on a corrupt file is the same principle: a bad
reference file must degrade to the fallbacks, never break the import.

One consequence to know about: because the values are frozen into the
schema at import, **Streamlit must be restarted after a build that changes
the team list or the measured eras**. The db_revision cache key invalidates
queries, not a dataclass.
"""

import json

import data_paths

PATH = data_paths.reference_dir("nba") / "nba_reference.json"


#: The 30 current franchises, which is what a team picker should offer.
#: Superseded by the build's measured list the moment PATH exists.
FALLBACK_TEAMS = (
    "Atlanta Hawks", "Boston Celtics", "Brooklyn Nets",
    "Charlotte Hornets", "Chicago Bulls", "Cleveland Cavaliers",
    "Dallas Mavericks", "Denver Nuggets", "Detroit Pistons",
    "Golden State Warriors", "Houston Rockets", "Indiana Pacers",
    "Los Angeles Clippers", "Los Angeles Lakers", "Memphis Grizzlies",
    "Miami Heat", "Milwaukee Bucks", "Minnesota Timberwolves",
    "New Orleans Pelicans", "New York Knicks", "Oklahoma City Thunder",
    "Orlando Magic", "Philadelphia 76ers", "Phoenix Suns",
    "Portland Trail Blazers", "Sacramento Kings", "San Antonio Spurs",
    "Toronto Raptors", "Utah Jazz", "Washington Wizards",
)

#: Current franchise -> every identity that counts as that franchise.
#:
#: Expansion is one-directional, exactly as core.Schema.club_identities
#: defines it and as the AFL's Brisbane entry demonstrates: asking for the
#: Thunder includes the SuperSonics, but asking for the SuperSonics returns
#: only Sonics players. A Seattle square is a question about Seattle, and
#: folding it into Oklahoma City would answer a different one.
#:
#: Only franchises whose earlier identity a solver would name separately
#: are listed. A pure city move with no name change (Buffalo Braves -> San
#: Diego Clippers) is still listed, because the name is what appears in
#: club_hist and a lookup by "Buffalo Braves" has to find something.
#:
#: The Charlotte Hornets line is the contested one. The 1988-2002 Hornets
#: became the New Orleans Pelicans, and the 2004 expansion Bobcats took the
#: Hornets name and the original team's records in 2014. This follows the
#: league's own records disposition: the current Charlotte Hornets own the
#: Bobcats history and the pre-2002 Hornets history, and New Orleans holds
#: its own from 2002. It is a judgement, and it is the one to revisit first
#: if a square looks wrong.
FALLBACK_LINEAGE = {
    "Oklahoma City Thunder": ["Oklahoma City Thunder", "Seattle SuperSonics"],
    "Brooklyn Nets": ["Brooklyn Nets", "New Jersey Nets", "New York Nets"],
    "Memphis Grizzlies": ["Memphis Grizzlies", "Vancouver Grizzlies"],
    "Los Angeles Lakers": ["Los Angeles Lakers", "Minneapolis Lakers"],
    "Sacramento Kings": ["Sacramento Kings", "Kansas City Kings",
                         "Kansas City-Omaha Kings", "Cincinnati Royals",
                         "Rochester Royals"],
    "Washington Wizards": ["Washington Wizards", "Washington Bullets",
                           "Capital Bullets", "Baltimore Bullets",
                           "Chicago Zephyrs", "Chicago Packers"],
    "Atlanta Hawks": ["Atlanta Hawks", "St. Louis Hawks",
                      "Milwaukee Hawks", "Tri-Cities Blackhawks"],
    "Golden State Warriors": ["Golden State Warriors",
                              "San Francisco Warriors",
                              "Philadelphia Warriors"],
    "Philadelphia 76ers": ["Philadelphia 76ers", "Syracuse Nationals"],
    "Detroit Pistons": ["Detroit Pistons", "Fort Wayne Pistons"],
    "Houston Rockets": ["Houston Rockets", "San Diego Rockets"],
    "Los Angeles Clippers": ["Los Angeles Clippers", "San Diego Clippers",
                             "Buffalo Braves"],
    "Utah Jazz": ["Utah Jazz", "New Orleans Jazz"],
    "New Orleans Pelicans": ["New Orleans Pelicans", "New Orleans Hornets",
                             "New Orleans/Oklahoma City Hornets"],
    "Charlotte Hornets": ["Charlotte Hornets", "Charlotte Bobcats"],
    "Phoenix Suns": ["Phoenix Suns"],
    "San Antonio Spurs": ["San Antonio Spurs", "Dallas Chaparrals"],
    "Indiana Pacers": ["Indiana Pacers"],
    "Denver Nuggets": ["Denver Nuggets", "Denver Rockets"],
}

#: Arena aliases, lowercased. Sponsor renamings are the whole problem here:
#: one building has carried four names in twenty years and a solver will
#: type whichever one they remember.
FALLBACK_VENUE_ALIASES = {
    "staples center": "Crypto.com Arena",
    "crypto.com arena": "Crypto.com Arena",
    "the forum": "The Forum",
    "great western forum": "The Forum",
    "madison square garden": "Madison Square Garden",
    "msg": "Madison Square Garden",
    "boston garden": "Boston Garden",
    "td garden": "TD Garden",
    "fleetcenter": "TD Garden",
    "td banknorth garden": "TD Garden",
    "united center": "United Center",
    "chicago stadium": "Chicago Stadium",
    "oracle arena": "Oracle Arena",
    "the arena in oakland": "Oracle Arena",
    "chase center": "Chase Center",
    "the spectrum": "The Spectrum",
    "the palace of auburn hills": "The Palace of Auburn Hills",
    "key arena": "KeyArena",
    "keyarena": "KeyArena",
    "seattle center coliseum": "KeyArena",
}

#: Provisional. Every one of these is a guess until build_nba_db.py has
#: measured the built database and load_stat_coverage.py has confirmed it;
#: sports.NBA.stat_eras reads the measured values as soon as PATH exists.
#: `minutes` and `plus_minus` in particular are the two most likely to be
#: wrong, which is why the measured file supersedes them.
FALLBACK_STAT_ERAS = {
    "steals": 1973, "blocks": 1973, "turnovers": 1977,
    "oreb": 1973, "dreb": 1973, "fg3m": 1979, "fg3a": 1979,
    "minutes": 1951, "plus_minus": 1996, "rebounds": 1950,
}


def load():
    """The measured reference file, or {} when it is absent or unreadable.

    Swallowing the error is deliberate. A truncated or hand-edited JSON file
    must fall back to the constants above, not stop `import sports` and take
    the whole app down with it.
    """
    try:
        with open(PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_measured():
    """True when the build's reference file was read successfully."""
    return bool(load())


def teams():
    """Current franchise names, for the team picker and Schema.clubs."""
    found = load().get("teams")
    return list(found) if found else list(FALLBACK_TEAMS)


def club_lineage():
    """Current franchise -> every identity that counts as it."""
    found = load().get("club_lineage")
    return dict(found) if found else dict(FALLBACK_LINEAGE)


def venue_aliases():
    found = load().get("venue_aliases")
    return dict(found) if found else dict(FALLBACK_VENUE_ALIASES)


def stat_eras():
    """Stat -> first season it carries a real value. Measured when built."""
    found = load().get("stat_eras")
    return dict(found) if found else dict(FALLBACK_STAT_ERAS)


def summary():
    """One line for the Database Status panel and the build log."""
    if is_measured():
        data = load()
        lo, hi = (data.get("seasons") or [None, None])[:2]
        span = f" {lo}-{hi}" if lo and hi else ""
        return (f"measured{span} ({len(teams())} teams, "
                f"{len(club_lineage())} franchise lineages)")
    return f"provisional fallback ({len(FALLBACK_TEAMS)} teams)"
