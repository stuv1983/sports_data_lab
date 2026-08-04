#!/usr/bin/env python3
"""A synthetic Lahman export, small enough to assert every row of.

Same purpose as tests/nba_fixture.py: give mlb/build_mlb_db.py something to
build from that is not the real 40MB export, so the build's behaviour can be
pinned exactly. The shape is chosen to exercise the cases that were wrong or
absent before:

  * a player traded mid-season (two Batting stints, one team-season row each)
  * a franchise that changed name (Brooklyn -> Los Angeles), so club_hist and
    club_now differ and the lineage is measurable
  * a pitcher whose games come from Appearances, not from his batting line
  * a World Series with a recorded winner and loser
  * a season with no postseason at all, so `postseason_played` is NULL rather
    than 0 for a career confined to it
"""

import csv
from pathlib import Path

PEOPLE = [
    # playerID, birthYear, nameFirst, nameLast
    ("brookj01", 1930, "Jack", "Brooks"),
    ("dodgem01", 1932, "Mel", "Dodger"),
    ("pitchr01", 1935, "Roy", "Pitcher"),
    ("earlyt01", 1848, "Tom", "Early"),
]

#: (yearID, lgID, teamID, franchID, name, park)
TEAMS = [
    (1871, "NA", "BR1", "LAD", "Brooklyn Atlantics", "Union Grounds"),
    (1955, "NL", "BRO", "LAD", "Brooklyn Dodgers", "Ebbets Field"),
    (1958, "NL", "LAN", "LAD", "Los Angeles Dodgers", "Memorial Coliseum"),
    (1955, "AL", "NYA", "NYY", "New York Yankees", "Yankee Stadium I"),
]

#: (franchID, franchName, active)
FRANCHISES = [("LAD", "Los Angeles Dodgers", "Y"),
              ("NYY", "New York Yankees", "Y")]

#: (playerID, yearID, teamID, G_all)
APPEARANCES = [
    ("brookj01", 1955, "BRO", 100),
    ("brookj01", 1955, "NYA", 40),      # traded mid-season
    ("brookj01", 1958, "LAN", 120),
    ("dodgem01", 1955, "BRO", 150),
    ("pitchr01", 1955, "BRO", 60),      # relief pitcher: 60 games, 8 at-bats
    ("earlyt01", 1871, "BR1", 30),
]

#: (playerID, yearID, stint, teamID, G, AB, R, H, 2B, 3B, HR, RBI, SB, BB, SO)
BATTING = [
    ("brookj01", 1955, 1, "BRO", 100, 380, 60, 110, 20, 3, 25, 80, 5, 40, 50),
    ("brookj01", 1955, 2, "NYA", 40, 150, 20, 40, 8, 1, 10, 30, 1, 15, 20),
    ("brookj01", 1958, 1, "LAN", 120, 450, 70, 130, 25, 4, 32, 95, 6, 50, 60),
    ("dodgem01", 1955, 1, "BRO", 150, 600, 90, 180, 30, 5, 40, 120, 10, 60, 70),
    ("pitchr01", 1955, 1, "BRO", 60, 8, 0, 1, 0, 0, 0, 0, 0, 0, 5),
    ("earlyt01", 1871, 1, "BR1", 30, 120, 25, 35, 4, 2, 0, None, None, 3, None),
]

#: (playerID, yearID, stint, teamID, W, L, SV, IPouts, ERA)
PITCHING = [
    ("pitchr01", 1955, 1, "BRO", 12, 6, 9, 300, 2.70),
]

#: (yearID, round, playerID, teamID, G, AB, R, H, 2B, 3B, HR, RBI, SB, BB, SO)
BATTING_POST = [
    (1955, "WS", "brookj01", "BRO", 7, 25, 4, 8, 1, 0, 2, 5, 0, 3, 4),
    (1955, "WS", "dodgem01", "BRO", 7, 28, 5, 9, 2, 0, 1, 6, 1, 4, 5),
]

#: (playerID, yearID, round, teamID, G, W, L, SV, IPouts, ERA)
PITCHING_POST = [
    ("pitchr01", 1955, "WS", "BRO", 3, 1, 0, 1, 21, 1.29),
]

#: (yearID, round, teamIDwinner, teamIDloser, wins, losses, ties)
SERIES_POST = [(1955, "WS", "BRO", "NYA", 4, 3, 0)]

#: (playerID, awardID, yearID, lgID)
AWARDS = [("dodgem01", "Most Valuable Player", 1955, "NL")]


def _write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def write(folder):
    """Write the whole synthetic export into `folder`."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    _write(folder / "People.csv",
           ["playerID", "birthYear", "nameFirst", "nameLast"], PEOPLE)
    _write(folder / "Teams.csv",
           ["yearID", "lgID", "teamID", "franchID", "name", "park"], TEAMS)
    _write(folder / "TeamsFranchises.csv",
           ["franchID", "franchName", "active"], FRANCHISES)
    _write(folder / "Appearances.csv",
           ["playerID", "yearID", "teamID", "G_all"], APPEARANCES)
    _write(folder / "Batting.csv",
           ["playerID", "yearID", "stint", "teamID", "G", "AB", "R", "H",
            "2B", "3B", "HR", "RBI", "SB", "BB", "SO"], BATTING)
    _write(folder / "Pitching.csv",
           ["playerID", "yearID", "stint", "teamID", "W", "L", "SV",
            "IPouts", "ERA"], PITCHING)
    _write(folder / "BattingPost.csv",
           ["yearID", "round", "playerID", "teamID", "G", "AB", "R", "H",
            "2B", "3B", "HR", "RBI", "SB", "BB", "SO"], BATTING_POST)
    _write(folder / "PitchingPost.csv",
           ["playerID", "yearID", "round", "teamID", "G", "W", "L", "SV",
            "IPouts", "ERA"], PITCHING_POST)
    _write(folder / "SeriesPost.csv",
           ["yearID", "round", "teamIDwinner", "teamIDloser", "wins",
            "losses", "ties"], SERIES_POST)
    _write(folder / "AwardsPlayers.csv",
           ["playerID", "awardID", "yearID", "lgID"], AWARDS)
    return folder


if __name__ == "__main__":
    import sys
    print(write(sys.argv[1] if len(sys.argv) > 1 else "mlb_fixture"))
