#!/usr/bin/env python3
"""
test_integration.py -- Clean-build release gate.

Builds the database from scratch and asserts the whole pipeline, so that
verification can never again pass against an out-of-band patched artefact.
Every check runs against the file afl/build_db.py just produced.

    python test_integration.py              # full run (rebuilds, ~90s)
    python test_integration.py --keep-db    # reuse an existing gridley.db

Exit code 0 means release-ready.
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
import os
import sqlite3
import subprocess
import sys

DB = "test_gridley.db"

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail else ""))
    return condition


# ------------------------------------------------------------------ 1 & 2
def build(keep):
    print("\n1-2. Clean build from the cached .rda")
    if not keep and os.path.exists(DB):
        os.remove(DB)
    if not os.path.exists(DB):
        r = subprocess.run([sys.executable, "afl/build_db.py", "--db", DB],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout[-2000:], r.stderr[-2000:])
            check("afl/build_db.py completes", False, "non-zero exit")
            return None
    check("afl/build_db.py completes", True)
    return sqlite3.connect(DB)


# ---------------------------------------------------------------------- 3
def test_source_assertion():
    """Required source columns are asserted before transformation."""
    print("\n3. Source-column assertion")
    raw = open("afl/build_db.py", encoding="utf-8").read()
    # Strip comments: a comment naming the wrong column is documentation,
    # not a defect. Only executable lines matter here.
    code = "\n".join(l.split("#")[0] for l in raw.splitlines())
    check("asserts required source columns",
          "missing = [c for c in required if c not in df.columns]" in code)
    check("uses Home.score/Away.score (not Home.Points) in code",
          "Home.score" in code and "Home.Points" not in code)


# ---------------------------------------------------------------------- 4
def test_result_column(con):
    print("\n4. games.result holds meaningful W/L/D")
    cols = {r[1] for r in con.execute("PRAGMA table_info(games)")}
    if not check("result column exists", "result" in cols):
        return
    counts = dict(con.execute(
        "SELECT result, COUNT(*) FROM games GROUP BY result"))
    for v in ("W", "L", "D"):
        check(f"result contains {v}", counts.get(v, 0) > 0,
              f"{counts.get(v, 0):,} rows")
    w, l = counts.get("W", 0), counts.get("L", 0)
    check("wins and losses balance (<1%)", abs(w - l) / max(w, 1) < 0.01,
          f"W={w:,} L={l:,}")
    check("no NULL results", counts.get(None, 0) == 0)


# ---------------------------------------------------------------------- 5
def test_harvey(con):
    print("\n5. Brent Harvey W-D-L reproduces AFL Tables")
    r = dict(con.execute(
        "SELECT result, COUNT(*) FROM games WHERE player='Brent Harvey' "
        "GROUP BY result"))
    got = (r.get("W"), r.get("D"), r.get("L"))
    check("Harvey is 235-3-194", got == (235, 3, 194), f"got {got}")
    total = con.execute("SELECT career_games FROM players WHERE "
                        "player='Brent Harvey'").fetchone()
    check("Harvey career games = 432", total and total[0] == 432)


# ---------------------------------------------------------------------- 6
def test_indexes(con):
    print("\n6. All expected indexes exist")
    expected = {
        "ix_games_player", "ix_games_club", "ix_games_season",
        "ix_games_disp", "ix_games_goals", "ix_players_obsc",
        "ix_final", "ix_pc", "ix_cs", "ix_pl",
        "ix_players_key", "ix_games_result",
    }
    have = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    missing = expected - have
    check(f"all {len(expected)} indexes present", not missing,
          f"missing {sorted(missing)}" if missing else f"{len(expected)} found")


# ---------------------------------------------------------------------- 7
def test_finals_constraints(con):
    print("\n7. Finals constraints execute and return sane counts")
    from afl import constraints as C
    for name, c in [("no finals wins", C.no_finals_wins()),
                    ("never won a final", C.never_won_a_final()),
                    ("never played finals", C.never_played_finals()),
                    ("premiership player", C.premiership_player())]:
        try:
            n = con.execute(
                f"SELECT COUNT(*) FROM players p WHERE p.player_id IN ({c[0]})",
                c[1]).fetchone()[0]
            check(f"{name} runs", n > 0, f"{n:,} players")
        except sqlite3.OperationalError as e:
            check(f"{name} runs", False, str(e))

    # A premiership player must not also be "never won a final".
    prem, never = C.premiership_player(), C.never_won_a_final()
    overlap = con.execute(
        f"SELECT COUNT(*) FROM players p WHERE p.player_id IN ({prem[0]}) "
        f"AND p.player_id IN ({never[0]})").fetchone()[0]
    check("premiership and never-won-a-final are disjoint", overlap == 0,
          f"overlap {overlap}")


# ---------------------------------------------------------------------- 8
def test_square_sql(con):
    print("\n8. All nine square SQL files generate and execute")
    r = subprocess.run([sys.executable, "afl/make_sql.py"],
                       capture_output=True, text=True)
    check("afl/make_sql.py completes", r.returncode == 0)
    files = [f"sql/cell_r{i}c{j}.sql" for i in (1, 2, 3) for j in (1, 2, 3)]
    check("nine cell files written", all(os.path.exists(f) for f in files))

    ok = 0
    for f in files:
        sql = "".join(l for l in open(f) if not l.startswith("."))
        try:
            con.execute(sql.strip().rstrip(";")).fetchall()
            ok += 1
        except sqlite3.Error as e:
            print(f"      {f}: {e}")
    check("all nine execute against the database", ok == 9, f"{ok}/9")

    runner = open("sql/run_all.sql", encoding="utf-8").read()
    check("run_all.sql uses absolute paths",
          all(l.split(".read ")[1].startswith("/") or ":" in l.split(".read ")[1]
              for l in runner.splitlines() if l.startswith(".read ")))


# ---------------------------------------------------------------------- 9
def test_draft_fixtures(con):
    """Unicode whitespace and namesake regression fixtures."""
    print("\n9. Draft linking fixtures (Unicode whitespace + namesakes)")
    from names import normalise_name

    check("NBSP normalises to plain space",
          normalise_name("Martin\xa0Leslie") == normalise_name("Martin Leslie"))
    check("zero-width space handled",
          normalise_name("Steven\u200bFebey") == "steven febey")
    check("curly apostrophe normalises",
          normalise_name("Balyn O\u2019Brien") == normalise_name("Balyn O'Brien"))
    check("case and padding handled",
          normalise_name("  ANDREW   PAYZE ") == "andrew payze")

    # Build a fixture draft table: real players, NBSP names, plus the
    # known namesake collisions that must resolve to the right player.
    fixtures = con.execute("""
        SELECT player, birth_year, debut_season, clubs_hist, player_id
        FROM players WHERE debut_season >= 1987 AND career_games >= 1
        AND name_key IN ('josh kennedy','mark williams','sam reid',
                         'jack ross','charlie cameron','peter brown',
                         'matthew kennedy','tom lynch','jordan lewis')
    """).fetchall()
    if not check("namesake fixtures found", len(fixtures) >= 8,
                 f"{len(fixtures)} players"):
        return

    rows = [(p.replace(" ", "\xa0"), d - 1, 5, "National",
             c.split("|")[0], int(d - 1 - by))
            for p, by, d, c, pid in fixtures]
    expect = {(p.replace(" ", "\xa0"), d - 1): pid
              for p, by, d, c, pid in fixtures}
    rows.append(("Fictional\xa0Neverplayed", 2001, 70, "Rookie",
                 "Carlton", 18))

    con.execute("DROP TABLE IF EXISTS draft")
    con.execute("CREATE TABLE draft (player TEXT, draft_year INT, pick INT,"
                " draft_type TEXT, club TEXT, draft_age INT)")
    con.executemany("INSERT INTO draft VALUES (?,?,?,?,?,?)", rows)
    con.commit()

    naive = con.execute(
        "SELECT COUNT(*) FROM draft d WHERE EXISTS (SELECT 1 FROM players p "
        "WHERE LOWER(p.player) = LOWER(d.player))").fetchone()[0]
    check("naive exact matching fails (regression guard)", naive == 0,
          f"{naive} of {len(rows)}")

    r = subprocess.run([sys.executable, "afl/link_draft.py", "--db", DB],
                       capture_output=True, text=True)
    check("afl/link_draft.py completes", r.returncode == 0, r.stderr[-200:])

    con2 = sqlite3.connect(DB)
    linked = dict(con2.execute(
        "SELECT match_status, COUNT(*) FROM draft_links GROUP BY match_status"))
    check("no false positives: nothing ambiguous linked",
          con2.execute("SELECT COUNT(*) FROM draft_links WHERE "
                       "match_status='ambiguous' AND player_id IS NOT NULL"
                       ).fetchone()[0] == 0)
    check("never-played pick is unmatched",
          linked.get("unmatched", 0) >= 1, str(linked))

    wrong = 0
    for r_ in con2.execute("""SELECT d.player, d.draft_year, l.player_id
                              FROM draft_links l JOIN draft d
                                ON d.rowid = l.draft_rowid
                              WHERE l.player_id IS NOT NULL"""):
        want = expect.get((r_[0], r_[1]))
        if want is not None and want != r_[2]:
            wrong += 1
            print(f"      MISLINK {r_[0]!r} {r_[1]} -> {r_[2]}, want {want}")
    check("every linked namesake resolves to the correct player_id",
          wrong == 0, f"{wrong} mislinks")

    audit_cols = {r[1] for r in con2.execute("PRAGMA table_info(draft_links)")}
    required = {"match_status", "match_method", "candidate_count",
                "birth_year_min", "birth_year_max", "debut_window_match",
                "club_match", "confidence_notes"}
    check("draft_links carries all audit fields", required <= audit_cols,
          f"missing {sorted(required - audit_cols)}")
    con2.close()


# --------------------------------------------------------------------- 10
def test_no_unresolved_in_results(con):
    print("\n10. Ambiguous/unmatched rows cannot reach solver results")
    from afl import constraints as C
    if not C.draft_available(con):
        check("draft constraints gated when tables absent", True, "skipped")
        return
    for name, c in [("draft pick 1-10", C.draft_pick_between(1, 10)),
                    ("drafted by Carlton", C.drafted_by("Carlton"))]:
        leaked = con.execute(f"""
            SELECT COUNT(*) FROM draft_links l
            WHERE l.match_status NOT IN ('unique','resolved')
              AND l.player_id IN ({c[0]})""", c[1]).fetchone()[0]
        check(f"{name} excludes unresolved rows", leaked == 0)
    src = open("afl/constraints.py", encoding="utf-8").read()
    check("every draft constraint filters on match_status",
          src.count("match_status IN ('unique','resolved')")
          >= src.count("JOIN draft d"))



# --------------------------------------------------------------------- 11
def test_grid_fixtures(con):
    """Every captured Gridley criterion parses or is declined."""
    from afl import parse_criteria as P
    from afl.grid_fixtures import GRIDS, LOOSE_CRITERIA

    span = f"#{min(_numbers())}-#{max(_numbers())}"
    print(f"\n11. Real grid fixtures ({span})")

    total = mapped = 0
    wrong_decline = []

    def audit(gid, crit, unsupported):
        """One criterion: parses when it should, declines when it should."""
        nonlocal total, mapped
        total += 1
        cn, label = P.parse(crit)
        should_decline = crit in unsupported
        if cn is None:
            if not should_decline:
                wrong_decline.append(f"{gid}: {crit!r} -- {label}")
            return
        if should_decline:
            wrong_decline.append(
                f"{gid}: {crit!r} should be declined, got {label!r}")
            return
        mapped += 1
        # VALIDATION_OPTIONAL_FIXTURES_V1 — the clean build contains
        # core AFL tables only. Draftguru and captain criteria are
        # executed by their dedicated enriched-database suites.
        import re as _re
        referenced = {name.casefold() for name in _re.findall(
            r'\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)', cn[0],
            flags=_re.I)}
        # VALIDATION_OPTIONAL_RISING_STAR_V1 — Rising Star nominations are
        # an optional layer like draft/award/captain data, so a clean core
        # build has no rising_star_nominees table.  The criterion must still
        # parse; executing its SQL belongs to the enriched-database suite.
        # VALIDATION_FAMILY_LAYER_V1 — the broad Wikipedia family layer is
        # optional like draft/award/captain/Rising Star data, so a clean
        # core build has no family tables.  The criterion must still parse;
        # executing its SQL belongs to test_family_relationships.py.
        optional = {'draft', 'draft_links', 'awards',
                    'all_australian', 'person_links', 'captaincies',
                    'rising_star_nominees',
                    'family_members', 'family_relationships'}
        if referenced & optional:
            return
        n = con.execute(
            f"SELECT COUNT(*) FROM players p WHERE p.player_id "
            f"IN ({cn[0]})", cn[1]).fetchone()[0]
        if n == 0:
            wrong_decline.append(f"{gid}: {crit!r} matched 0 players")

    for gid, g in GRIDS.items():
        for crit in g["cols"] + g["rows"]:
            audit(gid, crit, g["unsupported"])

    # Partial captures carry criteria with no known axis. They cannot be
    # intersected, but each one must still parse or decline correctly --
    # that is the whole reason #1113 was recorded before its board was.
    for gid, g in LOOSE_CRITERIA.items():
        for crit in g["criteria"]:
            audit(gid, crit, g["unsupported"])

    for w in wrong_decline:
        print(f"      {w}")
    check(f"all {total} criteria parse or decline correctly",
          not wrong_decline, f"{mapped} mapped, {total - mapped} declined")

    # Every supported criterion must return a non-empty player set.
    check("no supported criterion returns an empty set",
          not any("matched 0" in w for w in wrong_decline))

    # #1106 outcome record: the tool's picks scored 1.8 rarity.
    from afl import constraints as CC
    row = CC.goals_at_multiple_clubs(30, 2)
    col = CC.played_for("St Kilda")
    top = CC.solve(con, [row, col], limit=1)
    check("#1106 R1C1 still ranks Percy Outram first",
          top and top[0][0] == "Percy Outram",
          top[0][0] if top else "no result")


def _numbers():
    from afl import historic_grids as HG
    return [g.number for g in HG.GRIDS]


# ------------------------------------------------------------------- 11b
def test_criterion_semantics(con):
    """
    The boundary and grouping rules behind the #1113 builders.

    These are the checks that would have caught the bug they replaced:
    "LESS THAN 20 GOALS" used to parse as career_goals_min(20), which is
    the exact opposite question and still returns a plausible-looking
    list of players.
    """
    print("\n11b. Criterion semantics")
    from afl import parse_criteria as P
    from afl import constraints as C

    def parsed(text):
        cn, label = P.parse(text)
        assert cn is not None, f"{text!r} declined: {label}"
        return cn, label

    # -- strict vs inclusive caps -------------------------------------
    cn, label = parsed("LESS THAN 20 GOALS — CAREER")
    check("'less than 20 goals' is a cap, not a floor", "<=" in cn[0], label)
    check("'less than 20 goals' binds at 19, not 20", cn[1] == [19], cn[1])

    cn, _ = parsed("20 OR FEWER CAREER GOALS")
    check("'20 or fewer' keeps the boundary player", cn[1] == [20], cn[1])

    strict = C.count(con, [P.parse("LESS THAN 20 GOALS — CAREER")[0]])
    incl = C.count(con, [P.parse("20 OR FEWER CAREER GOALS")[0]])
    exact = con.execute(
        "SELECT COUNT(*) FROM players WHERE career_goals = 20").fetchone()[0]
    check("strict and inclusive caps differ by exactly the boundary",
          incl - strict == exact, f"{incl} - {strict} = {incl - strict}, "
                                  f"{exact} players on exactly 20")

    cn, _ = parsed("UNDER 50 GAMES")
    check("'under 50 games' binds at 49", cn[1] == [49], cn[1])

    # -- won a final, and the phrases that mean the opposite ----------
    cn, label = parsed("WON A FINALS GAME")
    check("'won a finals game' filters on a finals win",
          "is_final = 1" in cn[0] and "result = 'W'" in cn[0], label)
    for negated, expect in (("NO FINALS WINS", "no finals wins"),
                            ("NEVER WON A FINAL", "never won a final")):
        _, label = parsed(negated)
        check(f"{negated!r} is not read as a finals win",
              label == expect, label)
    _, label = parsed("MCG WON A FINAL")
    check("a venue still claims 'won a final at'",
          label == "won a final at mcg", label)
    _, label = parsed("GRAND FINAL WIN")
    check("'grand final win' is a premiership, not an appearance",
          label == "premiership player", label)

    # -- season averages ----------------------------------------------
    cn, label = parsed("AVG 5+ MARKS — SEASON")
    check("season average groups by player and season",
          "GROUP BY player_id, season" in " ".join(cn[0].split()), label)
    check("season average declares its appearance floor",
          f"min {C.SEASON_AVG_MIN_GAMES} games" in label, label)
    check("season average excludes unrecorded games rather than zeroing "
          "them", "marks IS NOT NULL" in cn[0])

    floor = C.count(con, [C.season_stat_average_min("marks", 5)])
    nofloor = C.count(con, [C.season_stat_average_min("marks", 5,
                                                      min_games=1)])
    check("the appearance floor actually excludes tiny seasons",
          floor <= nofloor, f"{floor} with a floor, {nofloor} without")


# ------------------------------------------------------------------- 11c
def test_historic_grid_library(con):
    """Historic grids distinguish parser support from loaded data layers."""
    print("\n11c. Historic grid library")
    from afl import historic_grids as HG
    from afl import parse_criteria as P
    import sports

    reports = HG.analyse_all(con, sports.get("afl"))
    check("every captured grid is analysed",
          len(reports) == len(HG.GRIDS), f"{len(reports)} grids")

    for report in reports:
        grid = report.grid
        if not grid.complete:
            continue
        if report.unsupported:
            actual = {item.text for item in report.unsupported}
            declared = set(grid.unsupported)
            reasons_present = all(bool(item.reason) for item in report.unsupported)
            check(f"{grid.key}: unavailable criteria are named, not swapped",
                  declared <= actual and reasons_present, report.line())
        else:
            check(f"{grid.key}: all nine intersections execute",
                  report.squares_ok is True, report.line())

    # VALIDATION_FAMILY_LAYER_V1 — BROTHER PLAYED moved from "unsupported" to
    # the broad Wikipedia family layer, exactly as CLUB CAPTAIN and Rising
    # Star nominations did before it. Only criteria with no data source at
    # all belong in this list, and none of the captured grids now carry one.
    genuinely_unsupported = ()
    declined = {item.text for report in reports for item in report.unsupported}
    for criterion in genuinely_unsupported:
        check(f"{criterion} is still declined", criterion in declined,
              "; ".join(sorted(declined)) or "none")

    brother, brother_label = P.parse("BROTHER PLAYED")
    check("BROTHER PLAYED parses as the optional family constraint",
          brother is not None and brother_label == "brother also played",
          brother_label or "declined")

    captain, captain_label = P.parse("CLUB CAPTAIN")
    check("CLUB CAPTAIN parses as the optional captaincy constraint",
          captain is not None, captain_label)

    nominee, nominee_label = P.parse("RISING STAR NOMINATION")
    check("RISING STAR NOMINATION parses as the optional nomination constraint",
          nominee is not None, nominee_label)

    ready = HG.supported_grids(reports)
    check("authentic mode offers only fully-supported grids",
          all(r.grid.complete and not r.unsupported and
              r.squares_ok is not False for r in ready),
          f"{len(ready)} of {len(reports)} playable")

    # Practice mode is tested only for genuinely unsupported source criteria.
    for report in reports:
        if not report.grid.complete or not report.grid.unsupported:
            continue
        cache = {}
        rows, cols, swaps = HG.practice_board(con, report, cache)
        axes = list(rows or []) + list(cols or [])
        key = report.grid.key
        check(f"{key}: practice board has six live axes",
              len(axes) == 6 and all(a.constraint for a in axes),
              f"{len(axes)}/6")
        check(f"{key}: every replacement names what it replaced",
              len(swaps) == len(report.grid.unsupported) and
              all(original and replacement for original, replacement in swaps),
              "; ".join(f"{a} -> {b}" for a, b in swaps) or "none")
        labels = [a.text.casefold() for a in axes]
        check(f"{key}: no axis is used twice",
              len(labels) == len(set(labels)))
        rows2, cols2, swaps2 = HG.practice_board(con, report, cache)
        state1 = [(a.text, a.replaced_from) for a in axes]
        state2 = [(a.text, a.replaced_from)
                  for a in list(rows2 or []) + list(cols2 or [])]
        check(f"{key}: a rerun does not reshuffle the board",
              state1 == state2 and swaps == swaps2)



# --------------------------------------------------------------------- 12
def test_rebuild_idempotent():
    """
    afl/build_db.py must be safe to re-run over an existing database.

    This check exists because a real failure slipped through: every other
    test deletes the file first, so the rebuild path was never exercised
    and `CREATE TABLE meta` blew up on the second run.
    """
    print("\n12. Rebuild over an existing database is idempotent")
    if not os.path.exists(DB):
        check("database present to rebuild over", False)
        return
    r = subprocess.run([sys.executable, "afl/build_db.py", "--db", DB],
                       capture_output=True, text=True)
    ok = r.returncode == 0
    if not ok:
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:]
        check("second afl/build_db.py run succeeds", False, " ".join(tail))
        return
    check("second afl/build_db.py run succeeds", True)

    con = sqlite3.connect(DB)
    n = con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    check("games table not duplicated on rebuild", n == 693194, f"{n:,} rows")
    meta_rows = con.execute(
        "SELECT key, COUNT(*) FROM meta GROUP BY key").fetchall()
    meta_counts = {key: count for key, count in meta_rows}
    expected_meta = {"source", "seasons", "built", "matches_derived"}
    check("meta table not duplicated",
          set(meta_counts) == expected_meta and
          all(count == 1 for count in meta_counts.values()),
          repr(meta_counts))
    idx = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name LIKE 'ix%'")}
    check("all indexes survive a rebuild", len(idx) >= 15, f"{len(idx)} found")
    con.close()

    ddl = open("afl/build_db.py", encoding="utf-8").read()
    code = "\n".join(l.split("#")[0] for l in ddl.splitlines())
    bare = [l.strip() for l in code.splitlines()
            if "CREATE TABLE" in l and "IF NOT EXISTS" not in l
            and "DROP TABLE IF EXISTS" not in code[:code.index(l)]]
    # `bare` was computed and then thrown away, leaving this check asserting
    # only that one DROP string appears somewhere in the file -- which stays
    # true however many unguarded CREATE TABLEs are added. It now asserts
    # what its name claims.
    check("no unguarded CREATE TABLE remains",
          not bare and "DROP TABLE IF EXISTS meta" in code,
          "; ".join(bare))



# --------------------------------------------------------------------- 13
def test_widget_parameters():
    """
    Every constraint parameter must have a real widget in the app.

    This exists because 'venue' fell through to a numeric spinner, so the
    Ground picker showed '28' instead of a list of grounds.
    """
    print("\n13. Every constraint parameter has a typed widget")
    from afl import constraints as C
    app = open("app.py", encoding="utf-8").read()
    # VALIDATION_OPTIONAL_RISING_STAR_V1 — "season" is a real typed widget:
    # app.py bounds it to the database's actual season span (year_kinds), so
    # it never renders as an unbounded spinner. Listing it as *handled* rather
    # than merely numeric keeps the assertion strict: app.py must still
    # reference it by name for this check to pass.
    handled = {"club", "player", "player_id", "stat", "stat_a", "stat_b",
               "venue", "kind", "source", "avg", "award", "times", "season",
               "place", "votes", "ground_status", "ground_metric"}
    numeric = {"games", "goals", "n", "n_a", "n_b", "clubs", "from", "to"}
    unknown = set()
    for kind, (fn, argnames) in C.BUILDERS.items():
        for a in argnames:
            if a in handled:
                if f'a == "{a}"' not in app and f'"{a}"' not in app:
                    unknown.add(f"{kind}:{a}")
            elif a not in numeric:
                unknown.add(f"{kind}:{a} (would fall through to a spinner)")
    check("no parameter falls through to a bare number input",
          not unknown, ", ".join(sorted(unknown)) or "all typed")

    check("teammate squares use player_id, not a free-text name",
          C.BUILDERS["Teammate of\u2026"][1] == ["player_id"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-db", action="store_true")
    a = ap.parse_args()

    print("=" * 62)
    print("Gridley clean-build integration test")
    print("=" * 62)

    con = build(a.keep_db)
    if con is None:
        sys.exit(1)

    test_source_assertion()
    test_result_column(con)
    test_harvey(con)
    test_indexes(con)
    test_finals_constraints(con)
    test_square_sql(con)
    test_draft_fixtures(con)
    con.commit()
    test_no_unresolved_in_results(sqlite3.connect(DB))
    test_grid_fixtures(sqlite3.connect(DB))
    test_criterion_semantics(sqlite3.connect(DB))
    test_historic_grid_library(sqlite3.connect(DB))
    test_rebuild_idempotent()
    test_widget_parameters()

    print("\n" + "=" * 62)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("Release criteria met.")


if __name__ == "__main__":
    main()
