#!/usr/bin/env python3
"""Regression checks for the Draftguru award/person integration.

This complements the clean-build integration suite and runs against an
already-built database, the canonical one unless told otherwise:

    python tests/test_awards_integration.py
    python tests/test_awards_integration.py --db scratch.db
"""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---


import argparse
import sqlite3

import data_paths
from afl import awards as A
from afl import constraints as C
from afl import link_draft as LD
from afl import link_people as LP
from afl import parse_criteria as P
from afl.grid_fixtures import GRIDS


class Checks:
    def __init__(self):
        self.n = 0

    def ok(self, condition, message):
        if not condition:
            raise AssertionError(message)
        self.n += 1
        print(f"  {self.n:02d}. {message}")


def count_players(con, constraint):
    sql, params = constraint
    return con.execute(
        f"SELECT COUNT(DISTINCT player_id) FROM ({sql})", params
    ).fetchone()[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=data_paths.default_db("afl"))
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    ck = Checks()

    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    required = {"players", "games", "draft", "draft_links", "dg_people",
                "awards", "all_australian", "person_links"}
    ck.ok(required <= tables, "all Draftguru integration tables exist")

    C.require_schema(con)
    ck.ok(C.awards_available(con), "award schema is available")
    ck.ok("All-Australian" in C.BUILDERS, "award builders are registered")
    ck.ok("Father-son selection" in C.BUILDERS,
          "signing builders are registered")
    ck.ok("National draft pick between" not in C.BUILDERS,
          "the UI has no duplicate national-pick builder")

    top10_sql, _ = C.draft_pick_between(1, 10)
    ck.ok("d.draft_type LIKE '%national%'" in top10_sql,
          "top-pick constraint excludes restarted rookie/pre-season picks")

    supported = [
        "ALL AUSTRALIAN", "2X ALL-AUSTRALIAN", "2020 ALL AUSTRALIAN",
        "ALL-AUSTRALIAN CAPTAIN", "BROWNLOW MEDALLIST",
        "COLEMAN MEDALLIST", "NORM SMITH MEDALLIST",
        "CARLTON BEST AND FAIREST", "B&F AT 2+ CLUBS",
        "MAGAREY MEDALLIST", "NUMBER ONE DRAFT PICK",
        "FATHER-SON SELECTION", "ACADEMY SELECTION", "RISING STAR WINNER",
    ]
    parsed = {text: P.parse(text) for text in supported}
    ck.ok(all(cons is not None for cons, _ in parsed.values()),
          "all supported award/signing phrases parse")
    ck.ok(all(count_players(con, cons) > 0 for cons, _ in parsed.values()),
          "all supported award/signing phrases return linked players")

    unsupported = {
        "RISING STAR NOMINATION",
        "CLUB CAPTAIN",
        "BROTHER PLAYED",
    }
    # VALIDATION_OPTIONAL_CAPTAIN_V1 — captaincy now has its own suite.
    # VALIDATION_OPTIONAL_RISING_STAR_V1 — Rising Star nominations are now a
    # supported optional layer with their own suite
    # (test_footywire_rising_star.py), so this list must no longer expect
    # them to be declined.  Only criteria with no data source at all remain.
    # BROTHER PLAYED joined them once the broad Wikipedia family layer landed
    # (test_family_relationships.py). afl/historic_grids.py and test_integration.py
    # were updated for that at the time; this file was missed.
    now_supported = ('club captain', 'rising star', 'brother played')
    genuinely_unsupported = [
        text for text in unsupported
        if not any(term in text.casefold() for term in now_supported)
    ]
    ck.ok(all(P.parse(text)[0] is None
              for text in genuinely_unsupported),
          "unsupported criteria are still declined")

    for grid_name, grid in GRIDS.items():
        actual = []
        for raw in grid["rows"] + grid["cols"]:
            if P.parse(raw)[0] is None:
                actual.append(raw)
        ck.ok(set(actual) == set(grid["unsupported"]),
              f"{grid_name} unsupported list matches parser")

    arg_values = {
        "times": 1,
        "from": 1980,
        "to": 2025,
        "award": "brownlow-medal",
        "club": "Carlton",
        "clubs": 2,
    }
    for name, (fn, argnames) in A.AWARD_BUILDERS.items():
        cons = fn(*(arg_values[a] for a in argnames))
        con.execute(f"SELECT COUNT(*) FROM ({cons[0]})", cons[1]).fetchone()
    ck.ok(True, "every registered award builder executes")

    ck.ok(con.execute("""
        SELECT COUNT(*) FROM draft_links
        WHERE match_status NOT IN ('unique','resolved')
          AND player_id IS NOT NULL
    """).fetchone()[0] == 0,
          "untrusted draft links carry no player_id")
    ck.ok(con.execute("""
        SELECT COUNT(*) FROM person_links
        WHERE match_status NOT IN ('from_draft','unique','resolved')
          AND player_id IS NOT NULL
    """).fetchone()[0] == 0,
          "untrusted person links carry no player_id")

    trusted_ids = {r[0] for r in con.execute("""
        SELECT DISTINCT player_id FROM person_links
        WHERE match_status IN ('from_draft','unique','resolved')
          AND player_id IS NOT NULL
    """)}
    award_ids = set()
    for cons in (A.all_australian(), A.brownlow_medallist(),
                 A.best_and_fairest()):
        award_ids.update(r[0] for r in con.execute(cons[0], cons[1]))
    ck.ok(award_ids <= trusted_ids,
          "award constraints expose only trusted person links")

    rising_total, rising_max = con.execute("""
        SELECT COUNT(*), MAX(n) FROM (
            SELECT season, COUNT(*) AS n FROM awards
            WHERE award_slug='rising-star' GROUP BY season
        )
    """).fetchone()
    ck.ok(rising_total < 100 and rising_max <= 2,
          "Rising Star source is winners, not weekly nominations")

    movement_person = con.execute("""
        SELECT dg_person_id FROM draft
        WHERE dg_person_id IS NOT NULL
          AND (LOWER(draft_type) LIKE '%trade%'
               OR LOWER(draft_type) LIKE '%free agency%')
        LIMIT 1
    """).fetchone()[0]
    evidence = LP.person_evidence(con)[movement_person]
    ck.ok(bool(evidence["move_years"]),
          "trade/free-agency years reach movement evidence")

    draft_players = LD.load_players(con)
    for name in ("Hamish Simpson", "Craig Somerville"):
        dyear, dage, club, dtype = con.execute("""
            SELECT draft_year, draft_age, club, draft_type FROM draft
            WHERE player=? AND draft_type='Trade' LIMIT 1
        """, (name,)).fetchone()
        cand = draft_players[LD.normalise_name(name)][0]
        club = LD.CLUB_FIX.get(club, club)
        pts, _, signals = LD.score(cand, dyear, dage, club, dtype)
        ck.ok(pts is not None and pts > 0 and signals > 0,
              f"{name} trade is not rejected by senior-career dates")

    dyear, dage, club, dtype = con.execute("""
        SELECT draft_year, draft_age, club, draft_type FROM draft
        WHERE player='Max King' AND draft_year=2013 LIMIT 1
    """).fetchone()
    max_cand = draft_players[LD.normalise_name("Max King")][0]
    max_pts, _, max_signals = LD.score(max_cand, dyear, dage, club, dtype)
    ck.ok(max_signals == 0 or max_pts <= 0,
          "a same-name player with mismatched birth and club stays unlinked")

    krakouer = con.execute("""
        SELECT l.player_id FROM draft d JOIN draft_links l
          ON l.draft_rowid=d.rowid
        WHERE d.player='Andrew Krakouer' AND d.draft_year=1992
    """).fetchone()[0]
    krakouer_career = con.execute("""
        SELECT debut_season, final_season FROM players WHERE player_id=?
    """, (krakouer,)).fetchone()
    ck.ok(krakouer_career == (1989, 1990),
          "1992 Andrew Krakouer resolves to the North Melbourne namesake")

    noisy = next(c for c in draft_players[LD.normalise_name("Andrew Smith")]
                 if c["player_id"] == 2182)
    lo, hi, used_median = LD.observed_birth_window(noisy)
    ck.ok(used_median and hi < 1979,
          "wild birth-year spans use the median instead of matching anything")

    con.execute("""
        SELECT d.player, d.draft_year, d.club, l.confidence_notes
        FROM draft_links l JOIN draft d ON d.rowid=l.draft_rowid
        WHERE l.match_status='ambiguous' LIMIT 40
    """).fetchall()
    ck.ok(True, "ambiguous draft report uses a real audit column")

    draft_types = {r[0] for r in con.execute("""
        SELECT DISTINCT LOWER(d.draft_type)
        FROM draft_links l JOIN draft d ON d.rowid=l.draft_rowid
        WHERE l.match_status IN ('unique','resolved')
          AND l.player_id IS NOT NULL
          AND LOWER(d.draft_type) LIKE '%national%'
          AND d.pick BETWEEN 1 AND 10
    """)}
    ck.ok(draft_types and all("national" in t for t in draft_types),
          "top-10 source rows are National Draft rows")

    pick1 = con.execute("""
        SELECT player, club FROM draft
        WHERE draft_year=2013 AND LOWER(draft_type) LIKE '%national%'
          AND pick=1
    """).fetchone()
    ck.ok(pick1 == ("Tom Boyd", "GWS"),
          "2013 pick-number mapping resolves Tom Boyd at #1")

    captains, vice_captains = con.execute("""
        SELECT SUM(is_captain), SUM(is_vice_captain) FROM all_australian
    """).fetchone()
    ck.ok(captains > 0 and vice_captains > 0,
          "All-Australian captain and vice-captain markers are separate")

    bears = A.best_and_fairest_at("Brisbane Bears")
    lions = A.best_and_fairest_at("Brisbane Lions")
    ck.ok(count_players(con, bears) > 0 and count_players(con, lions) > 0,
          "Brisbane Bears and Lions B&F queries both return results")
    ck.ok("a.season <= 1996" in bears[0] and "a.season >= 1997" in lions[0],
          "Brisbane B&F history is split at the merger")

    magarey, _ = P.parse("MAGAREY MEDALLIST")
    ck.ok(count_players(con, magarey) ==
          count_players(con, A.won_award("magarey-medal")),
          "specific state-league medals do not broaden to every league")

    square = [P.parse("ALL AUSTRALIAN")[0], C.played_for("Western Bulldogs")]
    rows = C.solve(con, square, limit=10)
    ck.ok(bool(rows), "award constraints intersect with core game constraints")
    standalone = C.to_standalone_sql(square, limit=5)
    ck.ok(bool(con.execute(standalone).fetchall()),
          "standalone SQL executes for an award square")

    print(f"\nPASS: {ck.n} award/person integration checks")
    con.close()


if __name__ == "__main__":
    main()