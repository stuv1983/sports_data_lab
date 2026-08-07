#!/usr/bin/env python3
"""
test_draftguru.py -- Fixture harness for the Draftguru import.

The real CSVs could not be inspected when this was written, so this builds
a synthetic tree with the documented headings -- including the awkward ones
(`# ↧`, non-breaking spaces in names, the blank `Column 2`, the 1981 page's
`Detail` instead of `Draft`) -- and asserts the pipeline end to end:

  * every documented heading is either mapped or reported as unmapped
  * the loader writes all four tables
  * a person seen on three different pages collapses to one dg_person_id
  * namesakes resolve to the right player_id
  * an award row whose person is ambiguous cannot reach a solver result

    python test_draftguru.py

Point it at the real tree instead with:

    python -m utils.afl.load_draftguru --root data/afl/raw/draftguru --inspect
"""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---


import os
import shutil
import sqlite3
import subprocess
import sys

ROOT = "tests/fixtures/draftguru"
DB = "test_draftguru.db"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  -- {detail}" if detail else ""))
    return cond


def write(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    import csv
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(["" if v is None else v for v in r] for r in rows)


def make_players(con):
    """A stats table with two real namesake shapes and one pre-draft player."""
    con.execute("""CREATE TABLE players (
        player_id INTEGER, player TEXT, name_key TEXT, birth_year INT,
        birth_year_min INT, birth_year_max INT, debut_season INT,
        final_season INT, career_games INT, career_goals INT,
        clubs_hist TEXT, clubs_now TEXT, n_clubs INT, obscurity REAL,
        finals_played INT)""")
    rows = [
        # two Josh Kennedys: different clubs, overlapping careers
        (1, "Josh Kennedy", "josh kennedy", 1987, 1987, 1987, 2006, 2021,
         293, 700, "Carlton|West Coast", "Carlton|West Coast", 2, 20.0, 20),
        (2, "Josh Kennedy", "josh kennedy", 1988, 1988, 1988, 2007, 2022,
         294, 120, "Hawthorn|Sydney", "Hawthorn|Sydney", 2, 22.0, 25),
        # pre-draft era player: award rows only, no draft row
        (3, "Percy Outram", "percy outram", 1900, 1900, 1900, 1921, 1930,
         120, 200, "St Kilda", "St Kilda", 1, 88.0, 2),
        # a namesake pair with nothing to tell them apart
        (4, "Peter Brown", "peter brown", 1950, 1950, 1950, 1970, 1980,
         100, 50, "Fitzroy", "Fitzroy", 1, 70.0, 1),
        (5, "Peter Brown", "peter brown", 1950, 1950, 1950, 1970, 1980,
         100, 50, "Fitzroy", "Fitzroy", 1, 70.0, 1),
    ]
    con.executemany(
        "INSERT INTO players VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()


META_DRAFT = ["Year", "Competition", "Record Category", "Draft Type",
              "Page Title", "Source URL", "Scraped At UTC", "Source Row"]


def make_tree():
    if os.path.exists(ROOT):
        shutil.rmtree(ROOT)

    # --- modern draft year page, with the arrow in the '#' heading -------
    hdr = META_DRAFT + ["Pick", "Draft", "# \u21a7", "Club", "Club URL",
                        "Signing", "Player", "Player URL", "Age", "Height",
                        "Original Club", "Original Club URL", "Grade",
                        "Games", "Goals", "Coaches", "Brownlow", "Awards"]
    write(f"{ROOT}/draft_years/2006.csv", hdr, [
        [2006, "AFL", "draft", "National Draft", "2006 Draft", "u", "t", 1,
         4, "National Draft", 4, "Carlton", "/c/carlton", "",
         "Josh\u00a0Kennedy", "/p/josh-kennedy-1", "19yr", "192cm",
         "Northern Knights", "/o/nk", "TAC", 293, 700, 40, 60, "AA(3)"],
        [2006, "AFL", "draft", "National Draft", "2006 Draft", "u", "t", 2,
         # namesake, different club and a year later -- must not collide
         12, "National Draft", 12, "Hawthorn", "/c/hawthorn", "",
         "Josh Kennedy", "/p/josh-kennedy-2", "18yr", "188cm",
         "Sydney Uni", "/o/su", "", 294, 120, 10, 30, ""],
    ])

    # A trade of a player who debuted six years earlier. The debut window
    # must not rule this out -- it did, for 1,426 real rows.
    write(f"{ROOT}/draft_years/2013.csv", hdr, [
        [2013, "AFL", "draft", "Trade", "2013 Draft", "u", "t", 1,
         "", "Trade", "", "Sydney", "/c/sydney", "", "Josh Kennedy",
         "/p/josh-kennedy-2", "25yr", "188cm", "Hawthorn", "/c/hawthorn",
         "", 294, 120, 10, 30, ""],
    ])

    # A delisted 25-year-old re-drafted in the Rookie Draft: neither a
    # first-time entry nor a trade. The rookie rule has to accept both.
    write(f"{ROOT}/draft_years/2014.csv", hdr, [
        [2014, "AFL", "draft", "Rookie", "2014 Draft", "u", "t", 1,
         "", "Rookie", 3, "Sydney", "/c/sydney", "", "Josh Kennedy",
         "/p/josh-kennedy-2", "26yr", "188cm", "Hawthorn", "/c/hawthorn",
         "", 294, 120, 10, 30, ""],
    ])

    # --- 1981-style page: Detail instead of Draft, no Signing ------------
    hdr81 = META_DRAFT + ["Pick", "# \u21a7", "Club", "Club URL", "Detail",
                          "Player", "Player URL", "Age", "Height",
                          "Original Club", "Original Club URL", "Grade",
                          "Games", "Goals", "Coaches", "Brownlow", "Awards"]
    write(f"{ROOT}/draft_years/1981.csv", hdr81, [
        [1981, "VFL", "draft", "National Draft", "1981 Draft", "u", "t", 1,
         1, 1, "Fitzroy", "/c/fitzroy", "Zone", "Peter Brown", "/p/peter-brown",
         "18yr", "180cm", "Preston", "/o/p", "", 100, 50, 0, 0, ""],
    ])

    # --- All-Australian team page ---------------------------------------
    aahdr = ["Year", "Award Category", "Award Name", "Award Slug",
             "Page Title", "Source URL", "Scraped At UTC", "Source Row",
             "Position", "Captain", "Player", "Player URL", "Age", "Club",
             "Club URL", "Drafted", "Games", "Games Pre", "Times AA"]
    write(f"{ROOT}/all_australian_by_year/2015.csv", aahdr, [
        # "VC" must not count as a captaincy.
        [2015, "All-Australian", "All-Australian Team", "all-australian",
         "2015 AA", "u", "t", 1, "Full Forward", "VC", "Josh\u00a0Kennedy",
         "/p/josh-kennedy-1", 28, "West Coast", "/c/wc", 2006, 200, 180, 3],
        [2015, "All-Australian", "All-Australian Team", "all-australian",
         "2015 AA", "u", "t", 2, "Centre", "C", "Josh Kennedy",
         "/p/josh-kennedy-2", 27, "Sydney", "/c/syd", 2006, 190, 170, 2],
    ])

    # --- standard award schema ------------------------------------------
    stdhdr = ["Award Category", "Award Name", "Award Slug", "Page Title",
              "Source URL", "Scraped At UTC", "Source Row", "Year",
              "Year URL", "Player", "Player URL", "Height", "Club",
              "Club URL", "Age", "Prior Games", "Season Games",
              "Season Goals", "Drafted", "From", "From URL", "Clubs",
              "Career Games"]
    write(f"{ROOT}/awards/coleman.csv", stdhdr, [
        ["Award", "Coleman Medal", "coleman", "Coleman", "u", "t", 1,
         2015, "/y/2015", "Josh\u00a0Kennedy", "/p/josh-kennedy-1", "192cm",
         "West Coast", "/c/wc", 28, 180, 20, 75, 2006, "Northern Knights",
         "/o/nk", "Carlton, West Coast", 293],
    ])

    # --- vote-based schema ----------------------------------------------
    votehdr = ["Award Category", "Award Name", "Award Slug", "Page Title",
               "Source URL", "Scraped At UTC", "Source Row", "Year",
               "Year URL", "Player", "Player URL", "Votes", "Age", "Club",
               "Club URL", "Drafted", "Games"]
    write(f"{ROOT}/awards/brownlow-medal.csv", votehdr, [
        ["Award", "Brownlow Medal", "brownlow-medal", "Brownlow", "u", "t", 1,
         2012, "/y/2012", "Josh Kennedy", "/p/josh-kennedy-2", 26, 24,
         "Sydney", "/c/syd", 2006, 190],
    ])

    # --- club best and fairest, incl. a pre-draft-era winner -------------
    write(f"{ROOT}/club_best_and_fairest/st_kilda.csv", stdhdr, [
        ["Club Award", "Trevor Barker Award", "st_kilda", "St Kilda B&F",
         "u", "t", 1, 1925, "/y/1925", "Percy Outram", "/p/percy-outram",
         "180cm", "St Kilda", "/c/stk", 25, 40, 18, 12, "", "", "",
         "St Kilda", 120],
        # ambiguous person: two identical Peter Browns, no distinguishing
        # evidence. Must NOT reach a solver result.
        ["Club Award", "Fitzroy B&F", "fitzroy", "Fitzroy B&F", "u", "t", 2,
         1975, "/y/1975", "Peter Brown", "/p/peter-brown-x", "180cm",
         "Fitzroy", "/c/fitz", 25, 40, 18, 12, "", "", "", "Fitzroy", 100],
    ])

    # --- pick #1, with the blank second heading -------------------------
    p1hdr = ["Award Category", "Award Name", "Award Slug", "Page Title",
             "Source URL", "Scraped At UTC", "Source Row", "Year", "Year URL",
             "Column 2", "Club", "Club URL", "Player", "Player URL",
             "Original Club", "Original Club URL", "Height", "Games", "Goals",
             "Coaches Votes", "Brownlow Votes", "Honours"]
    write(f"{ROOT}/national_draft_pick_1.csv", p1hdr, [
        ["Award", "National Draft Pick 1", "national_draft_pick_1", "P1",
         "u", "t", 1, 2006, "/y/2006", "Priority", "Carlton", "/c/carlton",
         "Bryce Gibbs", "/p/bryce-gibbs", "Glenelg", "/o/g", "187cm", 231,
         60, 20, 30, ""],
    ])


def run(*args):
    r = subprocess.run([sys.executable, *args], capture_output=True, text=True)
    if r.returncode != 0:
        print((r.stderr or r.stdout)[-1500:])
    return r


def main():
    print("=" * 62)
    print("Draftguru import fixture harness")
    print("=" * 62)

    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    make_players(con)
    con.close()
    make_tree()

    print("\n1. Loader")
    r = run("afl/load_draftguru.py", "--root", ROOT, "--db", DB)
    check("afl/load_draftguru.py completes", r.returncode == 0)
    unmapped = [l for l in r.stdout.splitlines() if "not mapped" in l]
    check("every documented heading is mapped", not unmapped,
          unmapped[0] if unmapped else "no unmapped headings")

    con = sqlite3.connect(DB)
    tables = {t[0] for t in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("all four tables written",
          {"dg_people", "draft", "awards", "all_australian"} <= tables,
          str(sorted(tables)))

    n = con.execute("SELECT COUNT(*) FROM draft").fetchone()[0]
    check("both draft page layouts loaded", n == 5, f"{n} rows")

    # The 1981 page has no Draft column; the metadata column must fill in.
    dt = con.execute("SELECT draft_type FROM draft WHERE draft_year=1981"
                     ).fetchone()[0]
    check("1981 page falls back to the metadata Draft Type",
          dt == "National Draft", repr(dt))

    nb = con.execute("SELECT name_key FROM draft WHERE player_url="
                     "'/p/josh-kennedy-1'").fetchone()[0]
    check("non-breaking space in a name normalises",
          nb == "josh kennedy", repr(nb))

    # One person, three pages (draft, AA, Coleman) -> one dg_person_id.
    ids = con.execute("""
        SELECT COUNT(DISTINCT dg_person_id) FROM (
          SELECT dg_person_id FROM draft WHERE player_url='/p/josh-kennedy-1'
          UNION SELECT dg_person_id FROM all_australian
                WHERE player_url='/p/josh-kennedy-1'
          UNION SELECT dg_person_id FROM awards
                WHERE player_url='/p/josh-kennedy-1')""").fetchone()[0]
    check("one person across three pages is one dg_person_id", ids == 1,
          f"{ids} ids")

    npeople = con.execute("SELECT COUNT(*) FROM dg_people").fetchone()[0]
    check("Player URL separates the two Josh Kennedys",
          con.execute("SELECT COUNT(*) FROM dg_people WHERE "
                      "name_key='josh kennedy'").fetchone()[0] == 2,
          f"{npeople} people total")

    cap = con.execute("SELECT COUNT(*) FROM all_australian WHERE is_captain=1"
                      ).fetchone()[0]
    vc = con.execute("SELECT COUNT(*) FROM all_australian WHERE "
                     "is_vice_captain=1").fetchone()[0]
    check("captain flag set, vice-captain not counted as captain",
          cap == 1 and vc == 1, f"C={cap} VC={vc}")

    note = con.execute("SELECT note FROM awards WHERE award_slug="
                       "'national_draft_pick_1'").fetchone()[0]
    check("the blank 'Column 2' heading is preserved", note == "Priority",
          repr(note))
    con.close()

    print("\n2. Draft linking")
    r = run("afl/link_draft.py", "--db", DB)
    check("afl/link_draft.py still runs against the new draft table",
          r.returncode == 0)
    con = sqlite3.connect(DB)
    st = con.execute("""SELECT l.match_status FROM draft_links l
                        JOIN draft d ON d.rowid = l.draft_rowid
                        WHERE d.draft_type = 'Trade'""").fetchone()
    check("a trade of an established player is not ruled implausible",
          st and st[0] in ("unique", "resolved"), st[0] if st else "no row")
    rk = con.execute("""SELECT l.match_status FROM draft_links l
                        JOIN draft d ON d.rowid = l.draft_rowid
                        WHERE d.draft_type = 'Rookie'""").fetchone()
    check("a delisted player re-drafted as a rookie is not implausible",
          rk and rk[0] in ("unique", "resolved"), rk[0] if rk else "no row")
    con.close()

    print("\n3. Person linking")
    r = run("afl/link_people.py", "--db", DB)
    check("afl/link_people.py completes", r.returncode == 0)

    con = sqlite3.connect(DB)
    got = dict(con.execute("""
        SELECT p.player_url, l.player_id FROM person_links l
        JOIN dg_people p ON p.dg_person_id = l.dg_person_id
        WHERE l.player_id IS NOT NULL"""))
    check("Josh Kennedy #1 links to player_id 1",
          got.get("/p/josh-kennedy-1") == 1, str(got.get("/p/josh-kennedy-1")))
    check("Josh Kennedy #2 links to player_id 2",
          got.get("/p/josh-kennedy-2") == 2, str(got.get("/p/josh-kennedy-2")))
    check("award-only pre-draft player links via club and season",
          got.get("/p/percy-outram") == 3, str(got.get("/p/percy-outram")))
    check("indistinguishable namesake is left unlinked",
          got.get("/p/peter-brown-x") is None,
          str(got.get("/p/peter-brown-x")))
    check("a draftee who never played is unmatched",
          con.execute("SELECT match_status FROM person_links l JOIN dg_people p"
                      " ON p.dg_person_id=l.dg_person_id WHERE "
                      "p.player_url='/p/bryce-gibbs'").fetchone()[0]
          == "unmatched")

    print("\n4. Constraints")
    from afl import awards as A
    check("awards_available gate is true", A.awards_available(con))
    for name, c in [("All-Australian", A.all_australian(1)),
                    ("AA captain", A.all_australian_captain()),
                    ("AA 2+ times", A.all_australian(2)),
                    ("Brownlow", A.brownlow_medallist()),
                    ("Coleman", A.coleman_medallist()),
                    ("club B&F", A.best_and_fairest(1)),
                    ("B&F at St Kilda", A.best_and_fairest_at("st kilda")),
                    ("state-league medal", A.state_league_medallist()),
                    ("pick #1", A.number_one_draft_pick())]:
        try:
            n = con.execute(f"SELECT COUNT(*) FROM players p WHERE "
                            f"p.player_id IN ({c[0]})", c[1]).fetchone()[0]
            check(f"{name} executes", True, f"{n} players")
        except sqlite3.Error as e:
            check(f"{name} executes", False, str(e))

    bf = A.best_and_fairest(1)
    leaked = con.execute(f"""
        SELECT COUNT(*) FROM person_links l
        WHERE l.match_status NOT IN ('from_draft','unique','resolved')
          AND l.player_id IN ({bf[0]})""", bf[1]).fetchone()[0]
    check("unresolved people cannot reach a constraint result", leaked == 0)

    n_bf = con.execute(f"SELECT COUNT(*) FROM players p WHERE p.player_id IN "
                       f"({bf[0]})", bf[1]).fetchone()[0]
    check("the ambiguous Peter Brown B&F is excluded", n_bf == 1,
          f"{n_bf} B&F winners (Outram only)")

    src = open("afl/awards.py").read()
    check("every award constraint filters on match_status",
          src.count("match_status IN ('from_draft','unique','resolved')")
          + src.count("_OK") - 1 >= src.count("FROM awards a")
          + src.count("FROM all_australian t"))
    con.close()

    print("\n" + "=" * 62)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("Fixture pipeline green. Now run it against the real tree.")


if __name__ == "__main__":
    main()
