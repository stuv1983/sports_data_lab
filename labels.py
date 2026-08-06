"""
labels.py -- Column names as a reader would write them.

Statistics are stored under their source's column name -- `home_runs`,
`goal_assists`, `fg3m`, `def_sacks` -- and every page used to print that
name straight onto the screen, so a picker offered "Statistic: home_runs"
and a leaderboard header read "goal_assists". The underscore is a database
detail; it should never reach the page.

Two forms, because both are needed and neither reads correctly in the
other's place:

    words("home_runs")  -> "home runs"     mid-sentence
    title("home_runs")  -> "Home Runs"     a widget label or column header

Acronyms are the reason this is not a one-line `.replace("_", " ")`:
`.title()` turns rbis into "Rbis" and fg3m into "Fg3M". ACRONYMS names the
tokens that have a fixed spelling, and OVERRIDES the handful of whole
columns that no token rule gets right.
"""

from __future__ import annotations

#: Token -> how that token is always written. Applied to both forms, so an
#: acronym keeps its capitals even in the mid-sentence one: "12 RBIs in a
#: season" is right and "12 rbis" is not.
ACRONYMS = {
    "rbi": "RBI", "rbis": "RBIs", "era": "ERA", "war": "WAR",
    "td": "TD", "tds": "TDs", "pat": "PAT", "epa": "EPA", "cpoe": "CPOE",
    "fg": "FG", "fgm": "FGM", "fga": "FGA", "ftm": "FTM", "fta": "FTA",
    "fg3m": "3PM", "fg3a": "3PA", "oreb": "OREB", "dreb": "DREB",
    "def": "defensive", "i50": "inside 50", "mvp": "MVP",
    "ppr": "PPR", "qb": "QB", "gwfg": "game-winning FG",
}

#: Whole columns whose reading no token rule produces.
OVERRIDES = {
    "inside50s": "inside 50s",
    "marks_i50": "marks inside 50",
    "one_percenters": "one-percenters",
    "plus_minus": "plus/minus",
    "brownlow": "Brownlow votes",
    "frees_for": "frees for",
    "frees_against": "frees against",
    "fg_made": "field goals made",
    "def_tackles_solo": "solo tackles",
}


def words(column) -> str:
    """`'home_runs'` -> `'home runs'`, for the middle of a sentence."""
    text = str(column or "").strip()
    if not text:
        return ""
    override = OVERRIDES.get(text.lower())
    if override:
        return override
    return " ".join(ACRONYMS.get(part.lower(), part)
                    for part in text.split("_") if part)


def title(column) -> str:
    """`'home_runs'` -> `'Home Runs'`, for a header or a widget label.

    A word that already carries a capital is left exactly as it is, which
    is what keeps "RBIs" from becoming "Rbis" and "3PM" from becoming
    "3Pm".
    """
    spelled = words(column)
    if not spelled:
        return ""
    return " ".join(
        word if any(ch.isupper() for ch in word) else word.capitalize()
        for word in spelled.split(" "))
