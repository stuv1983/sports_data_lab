#!/usr/bin/env python3
"""
Synthetic NBA source CSVs, shaped to exercise the cases that actually break.

Small enough to read, but every row is here for a reason:

  * a Seattle SuperSonics career and an Oklahoma City Thunder career, so
    one-directional franchise lineage can be tested in both directions;
  * a player who appeared for both, so a lineage square does not
    double-count him;
  * a 1971 career with NULL steals and blocks, so "career steals >= 1"
    must exclude him rather than read the gap as zero;
  * a playoff run ending in a Finals game, so champion / played_in_the
    _finals / won_the_finals have something to be true of;
  * an exact duplicate box score, so the dedupe pass has work to do;
  * a one-game player, because that is the shape of most real obscure
    answers.

Used by tests/test_build_nba_db.py and tests/test_constraints_nba.py.
"""

import csv
from pathlib import Path

from nba import nba_source

# franchise_id -> (team_id, name, is_current)
TEAMS = [
    # Two identities on one franchise: the relocation case.
    ("okc", "okc:sea", "Seattle SuperSonics", 0, 1967, 2007),
    ("okc", "okc", "Oklahoma City Thunder", 1, 2008, None),
    ("bos", "bos", "Boston Celtics", 1, 1946, None),
    ("lal", "lal", "Los Angeles Lakers", 1, 1960, None),
]

# source_player_id, name, birth_year
PLAYERS = [
    ("p1", "Slick Watkins", 1948),      # 1971 career: NULL steals/blocks
    ("p2", "Dale Ferriter", 1980),      # Sonics only
    ("p3", "Marcus Oyelaran", 1985),    # Sonics then Thunder
    ("p4", "Ray Bellhouse", 1984),      # Celtics, champion
    ("p5", "Tomas Ilves", 1990),        # one game, Lakers
    ("p6", "Jonah Kirkbride", 1988),    # Thunder only, never Seattle
]

#: Enough of each to exercise the squares that read them. Tomas Ilves is
#: the born-outside case and Slick Watkins the unrecorded one, so the
#: criterion has to distinguish a foreign birthplace from a missing.
COUNTRIES = {"p2": "USA", "p3": "USA", "p4": "USA",
             "p5": "Estonia", "p6": "Canada"}

POSITIONS = {"p1": "G", "p2": "G", "p3": "GF", "p4": "F", "p5": "C",
             "p6": "FC"}

SEASONS = (1971, 2006, 2009, 2010)

#: (source_player_id, season, league, tier). Marcus Oyelaran is selected in
#: a Thunder season (2009) and a Sonics one (2006); Ray Bellhouse only in
#: a Celtics season (2009), never in his 2010 Lakers one -- which is what
#: "All-NBA with club" has to tell apart.
ALL_NBA = [("p3", 2009, "NBA", "1st"), ("p3", 2006, "NBA", "2nd"),
           ("p4", 2009, "NBA", "3rd")]


def _rows_for(season):
    """(matches, player_games) for one season."""
    label = f"{season}-{(season + 1) % 100:02d}"

    def match(mid, date, phase, home, away, hs, as_, rnd=None, venue=None):
        return {"match_id": mid, "season": season, "season_label": label,
                "date": date, "phase": phase, "round": rnd,
                "home_team_id": home, "away_team_id": away,
                "home_score": hs, "away_score": as_,
                "venue": venue, "attendance": None}

    def line(pid, mid, team, pts, reb=None, ast=None, stl=None, blk=None,
             minutes=None, fg3m=None):
        row = {"source_player_id": pid, "match_id": mid, "team_id": team}
        for column in nba_source.STAT_COLUMNS:
            row[column] = None
        row.update({"points": pts, "rebounds": reb, "assists": ast,
                    "steals": stl, "blocks": blk, "minutes": minutes,
                    "fg3m": fg3m})
        return row

    if season == 1971:
        # Before steals, blocks and three-pointers were recorded. Those
        # columns stay None -- the whole point of the fixture.
        matches = [match("g1971a", "1972-01-10", "regular", "okc:sea", "bos",
                         101, 99, venue="KeyArena"),
                   match("g1971b", "1972-01-20", "regular", "bos", "okc:sea",
                         110, 95, venue="Boston Garden")]
        games = [line("p1", "g1971a", "okc:sea", 22, reb=8, ast=3, minutes=34),
                 line("p1", "g1971b", "okc:sea", 14, reb=6, ast=2, minutes=30)]
        return matches, games

    if season == 2006:
        matches = [match("g2006a", "2006-11-02", "regular", "okc:sea", "lal",
                         98, 92, venue="KeyArena"),
                   match("g2006b", "2006-11-09", "regular", "lal", "okc:sea",
                         105, 88, venue="Staples Center")]
        games = [
            line("p2", "g2006a", "okc:sea", 31, 5, 7, 2, 1, 38, 4),
            line("p2", "g2006b", "okc:sea", 18, 4, 5, 1, 0, 35, 2),
            line("p3", "g2006a", "okc:sea", 9, 3, 1, 0, 0, 16, 1),
            # An exact duplicate box score: the real NBA duplicate mode.
            line("p3", "g2006a", "okc:sea", 9, 3, 1, 0, 0, 16, 1),
            line("p5", "g2006b", "lal", 4, 1, 0, 0, 0, 8, 0),
        ]
        return matches, games

    if season == 2009:
        matches = [match("g2009a", "2009-12-01", "regular", "okc", "bos",
                         96, 101, venue="Chesapeake Energy Arena"),
                   match("g2009b", "2010-01-15", "regular", "bos", "okc",
                         112, 104, venue="TD Garden"),
                   match("g2009p", "2010-05-20", "playoff", "bos", "okc",
                         99, 90, rnd="CF", venue="TD Garden"),
                   match("g2009f", "2010-06-10", "playoff", "bos", "lal",
                         103, 98, rnd="F", venue="TD Garden")]
        games = [
            line("p3", "g2009a", "okc", 24, 6, 4, 2, 1, 36, 3),
            line("p3", "g2009b", "okc", 19, 5, 6, 1, 0, 34, 2),
            line("p3", "g2009p", "okc", 27, 7, 3, 3, 1, 40, 4),
            # Thunder only. Never appears under the Seattle identity, which
            # is what makes the one-directional lineage test discriminating:
            # he must answer an Oklahoma City square and not a Seattle one.
            line("p6", "g2009a", "okc", 12, 4, 8, 1, 0, 28, 1),
            line("p6", "g2009b", "okc", 8, 2, 5, 0, 0, 24, 0),
            line("p4", "g2009a", "bos", 15, 9, 2, 1, 2, 30, 0),
            line("p4", "g2009b", "bos", 21, 11, 3, 0, 1, 33, 1),
            line("p4", "g2009p", "bos", 18, 10, 4, 2, 3, 38, 0),
            line("p4", "g2009f", "bos", 26, 12, 5, 1, 2, 42, 2),
        ]
        return matches, games

    # 2010: a 50-point game, so a single-game milestone square has an answer.
    matches = [match("g2010a", "2010-11-05", "regular", "okc", "lal",
                     121, 118, venue="Chesapeake Energy Arena")]
    games = [line("p3", "g2010a", "okc", 52, 8, 9, 4, 2, 44, 7),
             line("p4", "g2010a", "lal", 11, 3, 1, 0, 0, 22, 1)]
    return matches, games


def write(root):
    """Write the fixture CSVs under `root`. Returns the path."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)

    def dump(name, columns, rows):
        with open(root / name, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(columns))
            writer.writeheader()
            for row in rows:
                writer.writerow({c: row.get(c) for c in columns})

    dump("teams.csv", nba_source.TEAM_COLUMNS, [
        {"team_id": tid, "franchise_id": fid, "name": name,
         "city": name.rsplit(" ", 1)[0], "nickname": name.rsplit(" ", 1)[-1],
         "abbreviation": tid.split(":")[-1].upper()[:3],
         "first_season": first, "last_season": last, "is_current": current}
        for fid, tid, name, current, first, last in TEAMS])

    dump("players.csv", nba_source.PLAYER_COLUMNS, [
        {"source_player_id": pid, "player": name, "birth_year": born,
         "position": POSITIONS.get(pid), "height_cm": None,
         "weight_kg": None, "birth_country": COUNTRIES.get(pid)}
        for pid, name, born in PLAYERS])

    for season in SEASONS:
        matches, games = _rows_for(season)
        dump(f"matches_{season}.csv", nba_source.MATCH_COLUMNS, matches)
        by_phase = {}
        for row in games:
            phase = next(m["phase"] for m in matches
                         if m["match_id"] == row["match_id"])
            by_phase.setdefault(phase, []).append(row)
        for phase, rows in by_phase.items():
            dump(f"player_games_{season}_{phase}.csv",
                 nba_source.PLAYER_GAME_COLUMNS, rows)
    return root


def write_all_nba(db):
    """Load the fixture's All-NBA selections into a built database.

    Separate from write() because the selections are keyed on player_id,
    which only exists once nba/build_nba_db.py has assigned it.
    """
    import sqlite3

    from nba import scrape_all_nba

    con = sqlite3.connect(db)
    try:
        con.executescript(scrape_all_nba.DDL)
        con.execute("DELETE FROM nba_all_nba")
        ids = dict(con.execute(
            "SELECT source_player_id, player_id FROM players"))
        con.executemany(
            "INSERT INTO nba_all_nba (player_id, season, season_label, "
            "league, tier, player_name, player_ref, position, match_status) "
            "VALUES (?,?,?,?,?,?,?,?,'exact name, in season')",
            [(ids[pid], season, f"{season}-{(season + 1) % 100:02d}",
              league, tier, dict((p, n) for p, n, _ in PLAYERS)[pid],
              None, None)
             for pid, season, league, tier in ALL_NBA if pid in ids])
        for statement in scrape_all_nba.INDEXES:
            con.execute(statement)
        con.commit()
    finally:
        con.close()


if __name__ == "__main__":
    import sys
    print(write(sys.argv[1] if len(sys.argv) > 1 else "data/nba/raw/csv"))
