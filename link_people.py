#!/usr/bin/env python3
"""
link_people.py -- Resolve Draftguru people to AFL Tables player_ids.

load_draftguru.py gives every Draftguru person a stable id (dg_person_id,
from their Player URL). This links that person once, and every draft row,
award row and All-Australian selection they own inherits it. That is a
strictly better position than linking each award row by name: a 1979
All-Australian row carries a name and a club and nothing else, while the
same person's draft row carries an age and a year.

Two sources of evidence, in order:

  1. draft_links, if link_draft.py has run. A person whose draft rows all
     resolved to the same player_id is linked with status `from_draft`.
  2. name + club + activity window, for people who never appear in a draft
     (anyone active before 1981, and anyone who was never drafted). The
     award year must fall inside the player's debut-final span, and the
     award club must be one of theirs.

Statuses match link_draft.py's vocabulary, and only `unique` / `resolved`
/ `from_draft` carry a player_id:

  from_draft  inherited from an unambiguous draft link
  unique      one name match, evidence consistent
  resolved    several namesakes, evidence picked one
  ambiguous   namesakes tied -- NOT linked
  unevidenced name match only, with no independent signal -- NOT linked
  implausible one match but the years or clubs don't work -- NOT linked
  unmatched   no player of that name

    python link_people.py
    python link_people.py --report        # show ambiguous people
"""

# Run standalone from anywhere: this file lives at the project root.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import argparse
import sqlite3
import sys

from data_paths import default_db
from link_draft import draft_rule

LINKED = ("from_draft", "unique", "resolved")

CLUB_FIX = {
    "Brisbane": "Brisbane Lions",
    "Brisbane Bears": "Brisbane Bears",
    "Greater Western Sydney": "GWS", "GWS Giants": "GWS", "GWS": "GWS",
    "Kangaroos": "North Melbourne", "North Melbourne": "North Melbourne",
    "Footscray": "Western Bulldogs", "South Melbourne": "Sydney",
    "Sydney Swans": "Sydney", "West Coast Eagles": "West Coast",
    "Gold Coast Suns": "Gold Coast", "Adelaide Crows": "Adelaide",
    "Port Adelaide Power": "Port Adelaide", "St. Kilda": "St Kilda",
}


def fix_club(c):
    c = (c or "").strip()
    return CLUB_FIX.get(c, c)


def load_players(con):
    by_key = {}
    for r in con.execute(
            "SELECT player_id, player, name_key, debut_season, final_season, "
            "career_games, clubs_hist, clubs_now, birth_year, birth_year_min, "
            "birth_year_max FROM players"):
        by_key.setdefault(r[2], []).append({
            "player_id": r[0], "player": r[1],
            "debut": r[3], "final": r[4], "games": r[5],
            "clubs": {c for c in (r[6] or "").split("|") if c}
                     | {c for c in (r[7] or "").split("|") if c},
            "birth_year": r[8], "by_min": r[9], "by_max": r[10],
        })
    return by_key


def person_evidence(con):
    """
    Everything Draftguru knows about a person that is not their name: the
    seasons and clubs they are associated with, their career games, and the
    birth-year range implied by their age at a draft.

    This deliberately reads the `draft` table as well as the award tables.
    An earlier version read only the awards, which meant the ~65% of people
    who appear on a draft page and nowhere else were matched on name alone:
    they scored zero, and a name with no namesake was enough to link them.
    Name alone is exactly what this project refuses to trust, so draft rows
    now carry their own evidence here.
    """
    ev = {}

    def get(pid):
        return ev.setdefault(pid, {"seasons": set(), "clubs": set(),
                                   "entry_years": set(), "move_years": set(),
                                   "either_years": set(),
                                   "career_games": None,
                                   "birth_lo": None, "birth_hi": None})

    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    if "draft" in tables:
        for pid, dyear, dage, club, games, dtype in con.execute(
                "SELECT dg_person_id, draft_year, draft_age, club, games, "
                "draft_type FROM draft WHERE dg_person_id IS NOT NULL"):
            e = get(pid)
            if dyear:
                # Entry drafts and movements (trade, free agency,
                # pre-season, mid-season) imply different things about when
                # the player debuted, so they are kept apart.
                bucket = {
                    "entry": "entry_years",
                    "movement": "move_years",
                    "either": "either_years",
                }[draft_rule(dtype)]
                e[bucket].add(int(dyear))
            club = fix_club(club)
            if club:
                e["clubs"].add(club)
            if games and not e["career_games"]:
                e["career_games"] = int(games)
            if dyear and dage:
                # Draftguru ages are whole numbers, so a player aged N at a
                # draft held late in the year was born in (year - N) or
                # (year - N - 1). Keep the range, as link_draft.py does.
                lo, hi = int(dyear) - int(dage) - 1, int(dyear) - int(dage)
                e["birth_lo"] = (lo if e["birth_lo"] is None
                                 else min(e["birth_lo"], lo))
                e["birth_hi"] = (hi if e["birth_hi"] is None
                                 else max(e["birth_hi"], hi))

    award_q = []
    if "awards" in tables:
        award_q.append("SELECT dg_person_id, season, club, career_games, "
                       "clubs_text FROM awards WHERE dg_person_id IS NOT NULL")
    if "all_australian" in tables:
        award_q.append("SELECT dg_person_id, season, club, NULL, NULL "
                       "FROM all_australian WHERE dg_person_id IS NOT NULL")
    for q in award_q:
        for pid, season, club, cg, clubs_text in con.execute(q):
            e = get(pid)
            if season:
                e["seasons"].add(int(season))
            club = fix_club(club)
            if club:
                e["clubs"].add(club)
            # "Carlton, West Coast" -- a full club history, and the
            # strongest single club signal an award page carries.
            for c in (clubs_text or "").split(","):
                c = fix_club(c)
                if c:
                    e["clubs"].add(c)
            if cg and not e["career_games"]:
                e["career_games"] = int(cg)
    return ev


EMPTY_EV = {"seasons": set(), "clubs": set(), "entry_years": set(),
            "move_years": set(), "either_years": set(), "career_games": None,
            "birth_lo": None, "birth_hi": None}

# A draftee normally debuts within a few years of being drafted.
DEBUT_WINDOW = (-1, 10)
BIRTH_TOLERANCE = 1


def observed_birth_window(cand):
    """Return a robust AFL Tables birth window, suppressing noisy spans."""
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


def score(cand, ev):
    """
    Evidence for one candidate player.

    Returns (points, signals, notes). `signals` counts positive independent
    matches; negative evidence changes the score but does not make a name-only
    candidate linkable. Points of None means disqualified outright.
    """
    notes, pts, signals = [], 0, 0

    seasons = sorted(ev["seasons"])
    if seasons and cand["debut"] is not None:
        # An award season must sit inside the player's career, allowing a
        # year either side for awards recorded against the following year.
        outside = [s for s in seasons
                   if s < cand["debut"] - 1 or s > cand["final"] + 1]
        if outside:
            notes.append(f"award {outside[0]} outside "
                         f"{cand['debut']}-{cand['final']}")
            return None, 0, notes
        notes.append(f"{len(seasons)} award season(s) inside career")
        pts += 3
        signals += 1

    if cand["debut"] is not None:
        for dy in sorted(ev["entry_years"]):
            gap = cand["debut"] - dy
            if gap < DEBUT_WINDOW[0] or gap > DEBUT_WINDOW[1]:
                notes.append(f"debut {gap:+d}y from the {dy} entry draft")
                return None, 0, notes
        if ev["entry_years"]:
            notes.append(f"{len(ev['entry_years'])} entry-draft year(s) consistent")
            pts += 2
            signals += 1

        # Trade, free-agency, rookie, pre-season and SSP rows are not bounded
        # by the senior debut/final seasons. Players can move before debut or
        # after their last senior game. A nearby career is useful for ranking
        # namesakes, but a distant one is not a hard contradiction.
        non_entry = sorted(set(ev["move_years"]) | set(ev.get("either_years", ())))
        if non_entry:
            nearby = [dy for dy in non_entry
                      if cand["debut"] <= dy + 5
                      and (cand["final"] is None or cand["final"] >= dy - 5)]
            if nearby:
                notes.append(f"{len(nearby)} non-entry year(s) near senior career")
                pts += 1
            else:
                notes.append("non-entry years not used as a hard career boundary")

    observed = observed_birth_window(cand)
    if ev["birth_lo"] is not None and observed:
        lo, hi, noisy = observed
        if ev["birth_lo"] <= hi and ev["birth_hi"] >= lo:
            suffix = " (median used; noisy source span)" if noisy else ""
            notes.append(
                f"birth {ev['birth_lo']}-{ev['birth_hi']} vs {lo}-{hi}{suffix}")
            pts += 3
            signals += 1
        else:
            suffix = " (median used; noisy source span)" if noisy else ""
            notes.append(f"birth {ev['birth_lo']}-{ev['birth_hi']} "
                         f"vs {lo}-{hi} MISS{suffix}")
            pts -= 2

    if ev["clubs"]:
        hit = ev["clubs"] & cand["clubs"]
        if hit:
            notes.append(f"club {sorted(hit)[0]}")
            pts += 3
            signals += 1
        else:
            notes.append(f"club {sorted(ev['clubs'])[0]} not among "
                         f"{sorted(cand['clubs'])[:3]}")
            pts -= 2

    cg = ev.get("career_games")
    if cg and cand["games"]:
        if abs(cg - cand["games"]) <= 3:
            notes.append(f"career games {cg}~{cand['games']}")
            pts += 2
            signals += 1
        elif abs(cg - cand["games"]) > 25:
            notes.append(f"career games {cg} vs {cand['games']} MISS")
            pts -= 2
    return pts, signals, notes


def from_draft_links(con):
    """dg_person_id -> player_id where all successful draft links agree."""
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "draft_links" not in tables:
        return {}, 0
    rows = con.execute("""
        SELECT d.dg_person_id, l.player_id
        FROM draft_links l JOIN draft d ON d.rowid = l.draft_rowid
        WHERE l.match_status IN ('unique','resolved')
          AND l.player_id IS NOT NULL AND d.dg_person_id IS NOT NULL
    """).fetchall()
    seen = {}
    for pid, player_id in rows:
        seen.setdefault(pid, set()).add(player_id)
    agreed = {p: next(iter(s)) for p, s in seen.items() if len(s) == 1}
    return agreed, len(seen) - len(agreed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=default_db("afl"))
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()

    con = sqlite3.connect(a.db)
    have = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "dg_people" not in have:
        sys.exit("No dg_people table. Run load_draftguru.py first.")
    if "players" not in have:
        sys.exit("No players table. Run build_db.py first.")

    by_key = load_players(con)
    ev_all = person_evidence(con)
    inherited, conflicted = from_draft_links(con)

    out, counts = [], {}
    for dgid, person_key, url, has_url, name, key in con.execute(
            "SELECT dg_person_id, person_key, player_url, has_url, player, "
            "name_key FROM dg_people").fetchall():

        if dgid in inherited:
            status, pid, method = "from_draft", inherited[dgid], "draft_link"
            notes = "inherited from draft_links"
        else:
            cands = by_key.get(key, [])
            ev = ev_all.get(dgid) or EMPTY_EV
            if not cands:
                status, pid, method = "unmatched", None, "no_name_match"
                notes = "no player of that name in the stats table"
            else:
                scored, evaluated = [], []
                for c in cands:
                    pts, sig, n = score(c, ev)
                    if pts is not None:
                        row = (pts, c, "; ".join(n), sig)
                        evaluated.append(row)
                        if sig > 0 and pts > 0:
                            scored.append(row)
                scored.sort(key=lambda x: -x[0])
                if not scored:
                    if evaluated and all(x[3] == 0 for x in evaluated):
                        status, pid, method = "unevidenced", None, "name_only"
                        notes = ("no positive club, season, birth-year or "
                                 "career-games evidence for this person")
                    else:
                        status, pid, method = "implausible", None, "all_ruled_out"
                        notes = (f"{len(cands)} name match(es), but the "
                                 "available evidence is contradictory")
                elif len(cands) == 1:
                    status, pid = "unique", scored[0][1]["player_id"]
                    method, notes = "single_name_match", scored[0][2]
                elif len(scored) == 1 or scored[0][0] > scored[1][0]:
                    status, pid = "resolved", scored[0][1]["player_id"]
                    method = "namesake_disambiguated"
                    notes = f"{len(cands)} namesakes; {scored[0][2]}"
                else:
                    tied = sum(1 for x in scored if x[0] == scored[0][0])
                    status, pid, method = "ambiguous", None, "namesakes_tied"
                    notes = f"{len(cands)} namesakes, {tied} tied on evidence"

        counts[status] = counts.get(status, 0) + 1
        ev = ev_all.get(dgid) or {}
        seasons = ev.get("seasons") or ()
        out.append((dgid, key, pid, status, method, has_url,
                    len(by_key.get(key, [])),
                    min(seasons, default=None),
                    max(seasons, default=None),
                    "|".join(sorted(ev.get("clubs") or ())) or None, notes))

    con.execute("DROP TABLE IF EXISTS person_links")
    con.execute("""CREATE TABLE person_links (
        dg_person_id     INTEGER PRIMARY KEY,
        name_key         TEXT,
        player_id        INTEGER,
        match_status     TEXT,
        match_method     TEXT,
        has_url          INTEGER,
        candidate_count  INTEGER,
        season_min       INTEGER,
        season_max       INTEGER,
        clubs_seen       TEXT,
        confidence_notes TEXT)""")
    con.executemany("INSERT INTO person_links VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    out)
    con.execute("CREATE INDEX ix_pl_pid ON person_links(player_id)")
    con.execute("CREATE INDEX ix_pl_status ON person_links(match_status)")
    con.commit()

    total = len(out)
    print(f"{total:,} Draftguru people\n")
    for k in ("from_draft", "unique", "resolved", "ambiguous", "unevidenced",
              "implausible", "unmatched"):
        n = counts.get(k, 0)
        print(f"  {k:<12}{n:>6}  {n/max(total,1)*100:5.1f}%")
    linked = sum(counts.get(k, 0) for k in LINKED)
    print(f"\n{linked:,} people carry a player_id. "
          f"{counts.get('ambiguous', 0):,} need review.")
    if conflicted:
        print(f"{conflicted} people had draft rows pointing at different "
              f"player_ids and were re-scored from scratch.")
    print("Unmatched people are mostly draftees who never played a senior "
          "AFL/VFL game, and state-league award winners who never did.")

    if a.report and counts.get("ambiguous"):
        print("\nAmbiguous:")
        for r in con.execute("""
                SELECT p.player, l.season_min, l.season_max, l.clubs_seen,
                       l.confidence_notes
                FROM person_links l JOIN dg_people p
                  ON p.dg_person_id = l.dg_person_id
                WHERE l.match_status = 'ambiguous' LIMIT 40"""):
            print(f"  {r[0]:<24}{r[1]}-{r[2]}  {(r[3] or ''):<22}{r[4]}")
    con.close()


if __name__ == "__main__":
    main()
