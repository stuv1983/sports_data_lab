#!/usr/bin/env python3
"""Advanced Search smoke test -- runs real queries against the live database.

This is a different check to test_family_search.py: that one proves the
compiler's *logic* is correct against a small synthetic database built in
memory. This one proves the feature actually works end to end against your
real gridley/afl.db -- real schema, real family data, real row counts.

    python test_advanced_search_live.py
    python test_advanced_search_live.py --db data\\afl\\afl.db
    python test_advanced_search_live.py --show 10   # print more sample rows

Read-only throughout: opens the database with query_only=ON, same as app.py.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Runnable standalone as well as under pytest, so it bootstraps its own path.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def note(ok: bool, label: str, detail: str = "") -> None:
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label}"
    if detail:
        line += f"  -- {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.execute("PRAGMA query_only = ON")
    return con


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def run_query(schema, con, query: str) -> tuple[list[str], list[str]]:
    """Return (player names, description strings) for one Advanced Search query."""
    sql, params, spec = Q.compile_query(schema, query, con=con)
    rows = con.execute(sql, params).fetchall()
    names = [row[0] for row in rows]
    return names, Q.describe(spec)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--db", default=None,
        help="database path (default: resolved the same way app.py does)",
    )
    ap.add_argument(
        "--show", type=int, default=3,
        help="sample player names to print per query (default 3)",
    )
    args = ap.parse_args(argv)

    import data_paths
    import sports
    global Q
    import query_filters_family as Q

    db_path = args.db or data_paths.sport_db("afl", "gridley.db")
    if not Path(db_path).is_file():
        print(f"error: database not found at {db_path}", file=sys.stderr)
        return 1

    print("==============================================================")
    print("Advanced Search live smoke test")
    print("==============================================================")
    print(f"  database: {db_path}\n")

    con = connect(db_path)
    schema = sports.AFL_SCHEMA

    # ---------------------------------------------------------------- 1
    print("1. Family tables present")
    have_members = table_exists(con, "family_members")
    have_rels = table_exists(con, "family_relationships")
    note(have_members, "family_members table exists")
    note(have_rels, "family_relationships table exists")
    if not (have_members and have_rels):
        print(
            "\nFamily layer not loaded -- run afl/scrape_wikipedia_families.py "
            "then afl/load_family_relationships.py first."
        )
        return 1

    trusted = con.execute(
        "SELECT COUNT(*) FROM family_members "
        "WHERE match_status IN ('unique','resolved') "
        "AND player_id IS NOT NULL"
    ).fetchone()[0]
    note(trusted > 0, "trusted family members linked", f"{trusted:,}")

    # ---------------------------------------------------------------- 2
    print("\n2. Base search still works unmodified")
    try:
        names, desc = run_query(schema, con, "games>=150 sort:obscurity")
        note(len(names) > 0, "a plain non-family query returns rows",
             f"{len(names):,} players")
    except Exception as exc:  # noqa: BLE001
        note(False, "a plain non-family query returns rows", str(exc))

    print("\n2b. Rejects the invalid old grid-criterion phrasing, "
          "as intended (not the same as the base search syntax)")
    try:
        Q.compile_query(schema, "150+ GAMES PLAYED", con=con)
        note(False, "'150+ GAMES PLAYED' is rejected as bad search syntax")
    except Q.QuerySyntaxError:
        note(True, "'150+ GAMES PLAYED' is rejected as bad search syntax")

    # ---------------------------------------------------------------- 3
    print("\n3. family:true")
    names, desc = run_query(con=con, schema=schema, query="family:true sort:obscurity")
    note(len(names) > 0, "family:true returns players", f"{len(names):,} players")
    if names:
        print(f"        e.g. {', '.join(names[:args.show])}")

    # ---------------------------------------------------------------- 4
    for relation in ("brother", "sibling", "parent_child", "father_son", "extended"):
        print(f"\n4. family_relation:{relation}")
        query = f"family_relation:{relation} sort:obscurity"
        try:
            names, desc = run_query(schema, con, query)
            note(True, f"{query!r} parses and executes", f"{len(names):,} players")
            if names:
                print(f"        e.g. {', '.join(names[:args.show])}")
        except Exception as exc:  # noqa: BLE001
            note(False, f"{query!r} parses and executes", str(exc))

    # ---------------------------------------------------------------- 5
    print("\n5. Brother narrower than sibling")
    brother_names, _ = run_query(schema, con, "family_relation:brother sort:obscurity")
    sibling_names, _ = run_query(schema, con, "family_relation:sibling sort:obscurity")
    note(
        len(sibling_names) >= len(brother_names),
        "sibling count >= brother count",
        f"sibling={len(sibling_names):,} brother={len(brother_names):,}",
    )

    # ---------------------------------------------------------------- 6
    print("\n6. Combined with a base-search filter")
    query = "family_relation:brother postseason:true sort:obscurity"
    try:
        names, desc = run_query(schema, con, query)
        note(True, f"{query!r} parses and executes", f"{len(names):,} players")
        if names:
            print(f"        e.g. {', '.join(names[:args.show])}")
    except Exception as exc:  # noqa: BLE001
        note(False, f"{query!r} parses and executes", str(exc))

    # ---------------------------------------------------------------- 7
    print("\n7. related_to: a real linked player")
    row = con.execute(
        "SELECT p.player FROM family_members fm "
        "JOIN players p ON p.player_id = fm.player_id "
        "WHERE fm.match_status IN ('unique','resolved') "
        "AND EXISTS (SELECT 1 FROM family_members fm2 "
        "WHERE fm2.family_key = fm.family_key "
        "AND fm2.source_member_id <> fm.source_member_id "
        "AND fm2.match_status IN ('unique','resolved') "
        "AND fm2.player_id IS NOT NULL) "
        "LIMIT 1"
    ).fetchone()
    if row is None:
        note(False, "found a linked player to test related_to with")
    else:
        subject = row[0]
        query = f'related_to:"{subject}" sort:obscurity'
        try:
            names, desc = run_query(schema, con, query)
            note(
                len(names) > 0 and subject not in names,
                f"related_to:{subject!r} returns relatives, not the player themself",
                f"{len(names):,} relatives" if names else "no relatives",
            )
            if names:
                print(f"        e.g. {', '.join(names[:args.show])}")
        except Exception as exc:  # noqa: BLE001
            note(False, f"related_to:{subject!r} parses and executes", str(exc))

    # ---------------------------------------------------------------- 8
    print("\n8. relative_club: a club with linked family members")
    club_row = con.execute(
        "SELECT g.club_now, COUNT(*) FROM family_members fm "
        "JOIN games g ON g.player_id = fm.player_id "
        "WHERE fm.match_status IN ('unique','resolved') "
        "GROUP BY g.club_now ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()
    if club_row is None:
        note(False, "found a club to test relative_club with")
    else:
        club = club_row[0]
        query = f"relative_club:{club} sort:obscurity"
        try:
            names, desc = run_query(schema, con, query)
            note(len(names) > 0, f"relative_club:{club!r} returns players",
                 f"{len(names):,} players")
            if names:
                print(f"        e.g. {', '.join(names[:args.show])}")
        except Exception as exc:  # noqa: BLE001
            note(False, f"relative_club:{club!r} parses and executes", str(exc))

    # ---------------------------------------------------------------- 9
    print("\n9. Draft-specific filters still work alongside the broad layer")
    # A missing family_draft table is a legitimate "not loaded" state and
    # should raise QuerySyntaxError, not crash. See KNOWN_ISSUE below if it
    # does crash -- that is a real bug in afl/family_draft.py, not this test.
    for query in ("father_son:true sort:obscurity", "family_draft:true sort:obscurity"):
        try:
            names, desc = run_query(schema, con, query)
            note(True, f"{query!r} parses and executes", f"{len(names):,} players")
        except Q.QuerySyntaxError as exc:
            if "family-draft data is not loaded" in str(exc):
                note(True, f"{query!r} declines cleanly (family-draft not loaded)")
            else:
                note(False, f"{query!r} parses and executes", str(exc))
        except sqlite3.OperationalError as exc:
            if "readonly database" in str(exc):
                note(
                    False,
                    f"{query!r} parses and executes",
                    "KNOWN ISSUE: afl/family_draft.py:ensure_family_draft_table() "
                    "still uses the pre-hotfix placeholder strategy and "
                    "crashes on the read-only Streamlit connection when the "
                    "family_draft table has not been imported. Same class of "
                    "bug as the one fixed in afl/family_relationships.py -- "
                    "needs the same _query_only(con) guard backported.",
                )
            else:
                raise

    # ---------------------------------------------------------------- 10
    print("\n10. Bad input is rejected, not silently ignored")
    try:
        Q.compile_query(schema, "family_relation:not_a_real_relation", con=con)
        note(False, "an invalid family_relation value is rejected")
    except Q.QuerySyntaxError:
        note(True, "an invalid family_relation value is rejected")
    except Exception as exc:  # noqa: BLE001
        note(False, "an invalid family_relation value is rejected",
             f"wrong exception type: {exc}")

    con.close()

    print("\n==============================================================")
    if FAILURES:
        print(f"{len(FAILURES)} failing check(s):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("Advanced Search live smoke test: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
