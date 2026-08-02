#!/usr/bin/env python3
"""
link_draft.py -- Resolve Draftguru rows to player_ids.

Name alone is not a safe key: 1,007 players in the stats table share a name
with someone else (six Peter Browns, four Mark Williamses). Draftguru also
embeds non-breaking spaces, so naive exact matching returns almost nothing.

This resolves each draft row using, in order:
  1. normalised name key (Unicode + whitespace safe)
  2. birth year, derived from draft_year - draft_age vs the player's
     age-derived birth year
  3. the drafting club appearing among the player's clubs
  4. debut season falling in a plausible window after an entry draft

Every row is written to `draft_links` with an explicit status:
  unique      one name match, plausible
  resolved    several name matches, evidence picked one
  ambiguous   several matches, evidence insufficient -- NOT linked
  implausible evidence rules the match out, or a non-entry row has no
              positive evidence beyond the name -- NOT linked
  unmatched   no name match (usually a pick who never played a senior game)

Only `unique` and `resolved` rows get a player_id. Nothing is silently linked.

    python link_draft.py
    python link_draft.py --report        # show ambiguous rows for review
"""

# Run standalone from anywhere: the project root is one level up.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import sqlite3
import sys

from names import normalise_name

# A draftee normally debuts within a few years of being drafted.
DEBUT_WINDOW = (0, 10)
BIRTH_TOLERANCE = 1  # years

# The Draftguru year pages are not only entry drafts. They also list
# trades, free agency, pre-season and mid-season selections -- movements of
# players who often debuted years earlier. Applying the debut window to
# those rows rules out the correct player: a 2012 trade of someone who
# debuted in 2004 is a gap of -8, which looks impossible and isn't.
#
# So the test depends on what kind of row it is. For an entry draft the
# senior debut must follow it. A movement or mixed selection row cannot use
# the senior career as a hard boundary: real Draftguru rows include players
# traded before their AFL debut and players traded after their last senior
# game. For those rows the career window is only a weak ranking hint.
# Three behaviours, not two:
#   entry     the player's first list place, so the debut must follow
#   movement  trade/free agency; senior debut/final is not decisive
#   either    entry and relisting cases occur on the same page
#
# The Rookie Draft is the reason `either` exists. It takes first-time
# draftees and delisted players being re-rookied in the same round, so
# neither test alone is right for it; a rookie row only has to be
# plausible one way or the other. The Pre-Season and SSP lists behave the
# same way. Trades and free agency are the only movements that are
# certain -- you cannot trade someone who was never on a list.
ENTRY_TYPES = ("national", "mini")
MOVEMENT_TYPES = ("trade", "free agency")


def draft_rule(draft_type):
    t = (draft_type or "").lower()
    if any(k in t for k in ENTRY_TYPES):
        return "entry"
    if any(k in t for k in MOVEMENT_TYPES):
        return "movement"
    return "either"


def is_entry(draft_type):
    """Kept for callers that only need the strict-entry question."""
    return draft_rule(draft_type) == "entry"


def load_players(con):
    rows = con.execute(
        "SELECT player_id, player, name_key, birth_year, birth_year_min, "
        "birth_year_max, debut_season, final_season, clubs_hist, clubs_now "
        "FROM players").fetchall()
    by_key = {}
    for r in rows:
        by_key.setdefault(r[2], []).append({
            "player_id": r[0], "player": r[1], "birth_year": r[3],
            "by_min": r[4], "by_max": r[5],
            "debut": r[6], "final": r[7],
            "clubs": set((r[8] or "").split("|")) | set((r[9] or "").split("|")),
        })
    return by_key


def expected_birth_years(draft_year, draft_age):
    """
    Draftguru reports age as a whole number, so a player aged N at a draft
    held late in the calendar year was born in either (year - N) or
    (year - N - 1) depending on whether their birthday had passed. Return
    the inclusive range rather than a single year.
    """
    if not draft_year or not draft_age:
        return None
    return (draft_year - draft_age - 1, draft_year - draft_age)


def observed_birth_window(cand):
    """Reliable birth-year window for one AFL Tables candidate.

    Nearly all players have a one-year observed span. A handful have wildly
    noisy min/max values, so letting the full span match would turn corrupted
    age rows into positive identity evidence. In those cases use the median
    birth year instead.
    """
    centre = cand.get("birth_year")
    lo, hi = cand.get("by_min"), cand.get("by_max")
    if centre is not None and lo is not None and hi is not None and hi - lo > 2:
        centre = int(round(centre))
        return centre - BIRTH_TOLERANCE, centre + BIRTH_TOLERANCE, True
    vals = [v for v in (lo, hi, centre) if v is not None]
    if not vals:
        return None
    return (int(min(vals)) - BIRTH_TOLERANCE,
            int(max(vals)) + BIRTH_TOLERANCE, False)


def score(cand, draft_year, draft_age, club, draft_type=None):
    """
    Evidence for one candidate.
    Returns (points, audit, positive_signals). `positive_signals` counts
    independent evidence that agrees with the candidate; name alone does not
    count. None points means disqualified.
    """
    pts, signals = 0, 0
    audit = {"debut_window_match": None, "club_match": None,
             "birth_match": None, "notes": []}

    if cand["debut"] is not None and draft_year:
        gap = cand["debut"] - draft_year
        rule = draft_rule(draft_type)
        if rule == "entry":
            entry_ok = DEBUT_WINDOW[0] - 1 <= gap <= DEBUT_WINDOW[1]
            if not entry_ok:
                audit["debut_window_match"] = 0
                audit["notes"].append(
                    f"{draft_type or rule} in {draft_year}: debut {gap:+d}y, "
                    f"career {cand['debut']}-{cand['final']}")
                return None, audit, 0
            audit["debut_window_match"] = 1
            audit["notes"].append(f"debut {gap:+d}y (entry)")
            pts += 2
            signals += 1
        else:
            near = (cand["debut"] <= draft_year + 5
                    and (cand["final"] is None
                         or cand["final"] >= draft_year - 5))
            if near:
                audit["debut_window_match"] = 1
                audit["notes"].append(
                    f"senior career near {draft_year} ({rule}; weak)")
                pts += 1
            else:
                audit["notes"].append(
                    f"{rule} row: senior career window not decisive")

    exp = expected_birth_years(draft_year, draft_age)
    observed = observed_birth_window(cand)
    if exp and observed:
        # Overlap of the draft-derived range with the observed range,
        # widened by BIRTH_TOLERANCE for age-record noise.
        lo, hi, noisy = observed
        if exp[0] <= hi and exp[1] >= lo:
            audit["birth_match"] = 1
            suffix = " (median used; noisy source span)" if noisy else ""
            audit["notes"].append(
                f"birth {exp[0]}-{exp[1]} vs {lo}-{hi}{suffix}")
            pts += 3
            signals += 1
        else:
            audit["birth_match"] = 0
            suffix = " (median used; noisy source span)" if noisy else ""
            audit["notes"].append(
                f"birth {exp[0]}-{exp[1]} vs {lo}-{hi} MISS{suffix}")
            pts -= 2

    if club:
        if club in cand["clubs"]:
            audit["club_match"] = 1
            audit["notes"].append("club matches")
            pts += 2
            signals += 1
        else:
            audit["club_match"] = 0

    return pts, audit, signals


CLUB_FIX = {
    "Brisbane": "Brisbane Lions", "GWS": "GWS",
    "Greater Western Sydney": "GWS", "Kangaroos": "North Melbourne",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/afl/afl.db")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()

    con = sqlite3.connect(a.db)
    if not con.execute("SELECT name FROM sqlite_master WHERE name='draft'"
                       ).fetchall():
        sys.exit("No draft table. Run load_draftguru.py first.")

    by_key = load_players(con)
    draft = con.execute(
        "SELECT rowid, player, draft_year, pick, draft_type, club, draft_age "
        "FROM draft").fetchall()

    out, counts = [], {}
    for rowid, name, dyear, pick, dtype, club, dage in draft:
        key = normalise_name(name)
        club = CLUB_FIX.get((club or "").strip(), (club or "").strip())
        cands = by_key.get(key, [])
        exp = expected_birth_years(dyear, dage)
        by_lo, by_hi = (exp if exp else (None, None))

        method = None
        audit = {"debut_window_match": None, "club_match": None,
                 "birth_match": None, "notes": []}

        if not cands:
            status, pid = "unmatched", None
            method = "no_name_match"
            audit["notes"] = ["no player of that name in stats table"]
        elif len(cands) == 1:
            pts, audit, signals = score(cands[0], dyear, dage, club, dtype)
            if pts is None:
                status, pid, method = "implausible", None, "single_name_ruled_out"
            elif signals == 0 or pts <= 0:
                status, pid, method = "implausible", None, "single_name_no_evidence"
                audit["notes"].append(
                    "no net positive evidence beyond the name")
            else:
                status, pid, method = "unique", cands[0]["player_id"], "single_name_match"
        else:
            scored = []
            for c in cands:
                pts, cand_audit, signals = score(c, dyear, dage, club, dtype)
                if pts is not None and signals > 0 and pts > 0:
                    scored.append((pts, c, cand_audit))
            scored.sort(key=lambda x: -x[0])
            if not scored:
                status, pid, method = "implausible", None, "all_candidates_ruled_out"
                audit["notes"] = [
                    f"{len(cands)} namesakes, none with positive evidence"]
            elif len(scored) == 1 or scored[0][0] > scored[1][0]:
                status, pid = "resolved", scored[0][1]["player_id"]
                method = "namesake_disambiguated"
                audit = scored[0][2]
                audit["notes"] = [f"{len(cands)} namesakes"] + audit["notes"]
            else:
                tied = sum(1 for p_, _, _ in scored if p_ == scored[0][0])
                status, pid, method = "ambiguous", None, "namesakes_tied"
                audit["notes"] = [f"{len(cands)} namesakes, {tied} tied on evidence"]

        counts[status] = counts.get(status, 0) + 1
        out.append((rowid, key, pid, status, method, len(cands),
                    by_lo, by_hi,
                    audit.get("debut_window_match"), audit.get("club_match"),
                    audit.get("birth_match"), "; ".join(audit["notes"])))

    con.execute("DROP TABLE IF EXISTS draft_links")
    con.execute("""CREATE TABLE draft_links (
        draft_rowid         INTEGER PRIMARY KEY,
        name_key            TEXT,
        player_id           INTEGER,
        match_status        TEXT,
        match_method        TEXT,
        candidate_count     INTEGER,
        birth_year_min      INTEGER,
        birth_year_max      INTEGER,
        debut_window_match  INTEGER,
        club_match          INTEGER,
        birth_match         INTEGER,
        confidence_notes    TEXT)""")
    con.executemany(
        "INSERT INTO draft_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", out)
    con.execute("CREATE INDEX ix_dl_pid ON draft_links(player_id)")
    con.execute("CREATE INDEX ix_dl_status ON draft_links(match_status)")
    con.commit()

    total = len(out)
    print(f"{total:,} draft rows processed\n")
    for k in ("unique", "resolved", "ambiguous", "implausible", "unmatched"):
        n = counts.get(k, 0)
        print(f"  {k:<12}{n:>6}  {n/total*100:5.1f}%")
    linked = counts.get("unique", 0) + counts.get("resolved", 0)
    print(f"\n{linked:,} rows carry a player_id. "
          f"{counts.get('ambiguous',0):,} need review.")
    print("Unmatched rows are mostly picks who never played a senior game.")

    if a.report and counts.get("ambiguous"):
        print("\nAmbiguous rows:")
        for r in con.execute("""SELECT d.player, d.draft_year, d.club,
                                       l.confidence_notes
                                FROM draft_links l JOIN draft d
                                  ON d.rowid = l.draft_rowid
                                WHERE l.match_status='ambiguous' LIMIT 40"""):
            print(f"  {r[0]:<24}{r[1]}  {r[2]:<18}{r[3]}")
    con.close()


if __name__ == "__main__":
    main()
