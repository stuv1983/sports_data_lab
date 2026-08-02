#!/usr/bin/env python3
"""Explain why the solver thinks a player answers a square.

When gridleygame.com rejects an answer this script shows, for that exact
player, what the database believes and which clause let them through, so a
false positive can be traced to a rule rather than guessed at.

    python diagnose_answer.py "Harry Britter" -c "no grand finals" -c "multi-club player"
    python diagnose_answer.py "Joe Fox" -c "geelong" -c "oliver henry teammate"
    python diagnose_answer.py --list-rejects        # replay the saved log

Add --reject to append the case to docs/rejected_answers.csv, so a pattern
across many rejections becomes visible instead of being lost each session.
"""

from __future__ import annotations

# Run standalone from anywhere: the project root is one level up.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import csv
import datetime as dt
import sqlite3
import sys
from pathlib import Path

import parse_criteria
from data_paths import sport_db

LOG = Path("docs") / "rejected_answers.csv"
LOG_FIELDS = ["logged", "player", "player_id", "criterion", "label",
              "qualified", "note"]


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def table_exists(con, name) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def find_players(con, name: str) -> list[sqlite3.Row]:
    """Exact match first; fall back to a contains search for typos."""
    rows = con.execute(
        "SELECT player_id, player, debut_season, final_season, career_games, "
        "career_goals, n_clubs, finals_played FROM players "
        "WHERE LOWER(player) = LOWER(?) ORDER BY career_games DESC",
        (name,)).fetchall()
    if rows:
        return rows
    return con.execute(
        "SELECT player_id, player, debut_season, final_season, career_games, "
        "career_goals, n_clubs, finals_played FROM players "
        "WHERE LOWER(player) LIKE LOWER(?) ORDER BY career_games DESC LIMIT 10",
        (f"%{name}%",)).fetchall()


# ------------------------------------------------------------------ evidence

def clubs(con, pid: int) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT club_now, club_hist, MIN(season) AS first, MAX(season) AS last, "
        "COUNT(*) AS games, SUM(goals) AS goals FROM games WHERE player_id = ? "
        "GROUP BY club_now, club_hist ORDER BY first", (pid,)).fetchall()


def finals(con, pid: int) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT season, UPPER(TRIM(round)) AS rnd, club_hist, result, goals, "
        "marks, venue FROM games WHERE player_id = ? AND is_final = 1 "
        "ORDER BY season, rnd", (pid,)).fetchall()


def unclassified_finals(con, pid: int) -> list[sqlite3.Row]:
    """Rounds that are neither a numbered home-and-away round nor a known
    final. A player mis-scored on finals criteria usually shows up here."""
    return con.execute(
        "SELECT season, UPPER(TRIM(round)) AS rnd, COUNT(*) AS games "
        "FROM games WHERE player_id = ? AND is_final = 0 "
        "AND UPPER(TRIM(round)) NOT GLOB '[0-9]*' "
        "GROUP BY season, rnd ORDER BY season", (pid,)).fetchall()


def describe_player(con, row: sqlite3.Row) -> None:
    print(f"\n{row['player']}  (player_id {row['player_id']})")
    print(f"  career      {row['debut_season']}-{row['final_season']}  "
          f"{row['career_games']} games  {row['career_goals']} goals")
    print(f"  n_clubs     {row['n_clubs']}   finals_played "
          f"{row['finals_played']}")

    print("  clubs:")
    for c in clubs(con, row["player_id"]):
        hist = c["club_hist"]
        now = c["club_now"]
        same = "" if hist == now else f"  (recorded as {hist})"
        print(f"    {now:<22} {c['first']}-{c['last']}  "
              f"{c['games']:>3} games  {c['goals'] or 0:>3} goals{same}")

    fin = finals(con, row["player_id"])
    if fin:
        print("  finals:")
        for f in fin:
            print(f"    {f['season']}  {f['rnd']:<3} {f['club_hist']:<20} "
                  f"{f['result']}  {f['goals'] or 0} goals  {f['venue']}")
        gfs = [f for f in fin if f["rnd"] == "GF"]
        print(f"    grand finals: {len(gfs)}  "
              f"wins: {sum(1 for f in gfs if f['result'] == 'W')}")
    else:
        print("  finals:      none recorded")

    odd = unclassified_finals(con, row["player_id"])
    if odd:
        print("  rounds not counted as finals (possible mis-classification):")
        for o in odd:
            print(f"    {o['season']}  {o['rnd']!r}  {o['games']} games")


def test_criterion(con, pid: int, text: str) -> tuple[str, bool, str]:
    """Parse one criterion and report whether this player is in its result."""
    constraint, label = parse_criteria.parse(text)
    if constraint is None:
        return text, False, f"not parsed — {label}"
    sql, params = constraint
    try:
        hit = con.execute(
            f"SELECT 1 FROM ({sql}) AS q WHERE q.player_id = ? LIMIT 1",
            list(params) + [pid]).fetchone() is not None
        total = con.execute(
            f"SELECT COUNT(*) FROM ({sql}) AS q", list(params)).fetchone()[0]
    except sqlite3.Error as exc:
        return label, False, f"SQL error — {exc}"
    return label, hit, f"{total:,} players satisfy this criterion"


def log_reject(rows: list[dict]) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    new = not LOG.exists()
    with LOG.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOG_FIELDS)
        if new:
            writer.writeheader()
        writer.writerows(rows)
    print(f"\nLogged {len(rows)} row(s) to {LOG}")


def list_rejects() -> int:
    if not LOG.exists():
        print(f"No log yet at {LOG}")
        return 0
    rows = list(csv.DictReader(LOG.open(encoding="utf-8")))
    if not rows:
        print("Log is empty.")
        return 0
    counts: dict[str, int] = {}
    for row in rows:
        if row["qualified"] == "yes":
            counts[row["label"]] = counts.get(row["label"], 0) + 1
    print(f"{len(rows)} logged row(s) across "
          f"{len({r['player'] for r in rows})} rejected answer(s).\n")
    print("Criteria that admitted a rejected answer, most frequent first:")
    for label, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>3}  {label}")
    print("\nThe top entries are where the rules are too loose.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("player", nargs="?", help="player name as the game spells it")
    ap.add_argument("-c", "--criteria", action="append", default=[],
                    help="a square's criterion text; repeat for both axes")
    ap.add_argument("--db", default=None)
    ap.add_argument("--reject", action="store_true",
                    help="append this case to docs/rejected_answers.csv")
    ap.add_argument("--note", default="", help="free text stored with --reject")
    ap.add_argument("--list-rejects", action="store_true")
    args = ap.parse_args(argv)

    if args.list_rejects:
        return list_rejects()
    if not args.player:
        ap.error("a player name is required")

    db = args.db or sport_db("afl")
    if not Path(db).exists():
        print(f"No database at {db}. Run build_db.py first.", file=sys.stderr)
        return 2
    con = connect(db)

    matches = find_players(con, args.player)
    if not matches:
        print(f"No player matching {args.player!r}. "
              f"The game's spelling may differ from AFL Tables.")
        return 1
    if len(matches) > 1:
        print(f"{len(matches)} players match {args.player!r}:")
        for row in matches:
            print(f"  {row['player_id']:>6}  {row['player']}  "
                  f"{row['debut_season']}-{row['final_season']}  "
                  f"{row['career_games']} games")

    logged = []
    for row in matches:
        describe_player(con, row)
        if not args.criteria:
            continue
        print("  criteria:")
        for text in args.criteria:
            label, hit, detail = test_criterion(con, row["player_id"], text)
            mark = "QUALIFIES" if hit else "excluded "
            print(f"    [{mark}] {text!r} -> {label}")
            print(f"                {detail}")
            logged.append({
                "logged": dt.date.today().isoformat(),
                "player": row["player"],
                "player_id": row["player_id"],
                "criterion": text,
                "label": label,
                "qualified": "yes" if hit else "no",
                "note": args.note,
            })

    if args.criteria and all(
            r["qualified"] == "yes" for r in logged) and len(matches) == 1:
        print("\nEvery criterion admits this player, so if the game rejected "
              "them the disagreement is in the data or the rule, not the "
              "solver. Compare the club and finals evidence above with the "
              "game's own record.")

    if args.reject and logged:
        log_reject(logged)
    return 0


if __name__ == "__main__":
    sys.exit(main())
