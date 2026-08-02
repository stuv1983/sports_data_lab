#!/usr/bin/env python3
"""
derive_matches.py -- Build the canonical `matches` table from `games`.

Every player-game row already carries season, round, date, venue, the club
played for, the opponent, and both scores. Collapsing those rows gives the
full match history without a single new HTTP request: one row per match,
with a stable `match_id` written back onto `games`.

This is a pure derivation. It reads `games`, writes `matches`, and adds one
column to `games`. It does not touch players, awards, draft or link tables,
and it is safe to re-run.

Stable identity
---------------
`match_key` is the real identity: "season|round|date|home|away", built from
the data itself. `match_id` is a small integer assigned on first sight and
then *reused* -- an existing matches table is read first, known keys keep
their id, and only genuinely new matches get new ones. So a mid-season
refresh will not renumber history out from under saved games, bookmarks or
anything else holding a match_id.

Home and away
-------------
build_db.py persists `is_home`. If you are running against a database built
before that change the column will be missing; this script still works, but
it cannot orient a match. In that case the two clubs are written to
home_team/away_team in alphabetical order and `home_away_known` is set to 0,
so nothing downstream mistakes a guess for a fact. Rebuild with the current
build_db.py to get real orientation.

Not derivable
-------------
Quarter scores and attendance are not in the player-stats source. The
columns are created and left NULL so a later import can fill them in place
without a schema migration.

Usage:
    python derive_matches.py                     # ./gridley.db
    python derive_matches.py --db my.db
    python derive_matches.py --report            # show integrity problems
    python derive_matches.py --dry-run           # derive and check, write nothing
"""

import argparse
import sqlite3
import sys

from data_paths import default_db

QUARTER_COLS = [
    "home_q1", "home_q2", "home_q3", "home_q4",
    "away_q1", "away_q2", "away_q3", "away_q4",
]

MATCH_COLS = [
    "match_id", "match_key", "season", "round", "match_date", "venue",
    "home_team", "away_team", "home_team_now", "away_team_now",
    "home_score", "away_score", "winner", "margin", "is_final",
    "home_away_known", "home_players", "away_players", "attendance",
] + QUARTER_COLS

FINALS = {"EF", "QF", "SF", "PF", "GF"}


def _has_column(con, table, column):
    return column in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _table_exists(con, name):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def load_games(con):
    import pandas as pd

    if not _table_exists(con, "games"):
        sys.exit("No `games` table. Run build_db.py first.")

    oriented = _has_column(con, "games", "is_home")
    cols = ["rowid AS rid", "season", "round", "date", "venue",
            "club_hist", "club_now", "opponent", "result",
            "points_for", "points_against", "is_final"]
    if oriented:
        cols.append("is_home")

    df = pd.read_sql_query(f"SELECT {', '.join(cols)} FROM games", con)
    if not oriented:
        # Unknown orientation. Flagged, not faked -- see the docstring.
        df["is_home"] = None
    print(f"Read {len(df):,} player-game rows "
          f"({'oriented' if oriented else 'ORIENTATION UNKNOWN'})")
    return df, oriented


def build_matches(df, oriented):
    """One row per match, plus a rid -> match_key map for the backfill."""
    import numpy as np
    import pandas as pd

    df = df.copy()
    for c in ("club_hist", "opponent", "round", "date", "venue"):
        df[c] = df[c].astype(str).str.strip()

    if oriented:
        home_flag = df["is_home"].fillna(0).astype(int) == 1
        df["home_team"] = df["club_hist"].where(home_flag, df["opponent"])
        df["away_team"] = df["opponent"].where(home_flag, df["club_hist"])
    else:
        # Deterministic but arbitrary: alphabetical, so the key is stable
        # even though the orientation is not real.
        pair = np.sort(df[["club_hist", "opponent"]].to_numpy(), axis=1)
        df["home_team"], df["away_team"] = pair[:, 0], pair[:, 1]

    df["match_key"] = (df["season"].astype(str) + "|" + df["round"] + "|"
                       + df["date"] + "|" + df["home_team"] + "|"
                       + df["away_team"])

    # Score for each side, taken from the rows of the club that side is.
    df["is_home_row"] = (df["club_hist"] == df["home_team"])
    df["home_score_row"] = df["points_for"].where(df["is_home_row"],
                                                  df["points_against"])
    df["away_score_row"] = df["points_against"].where(df["is_home_row"],
                                                      df["points_for"])
    df["home_player"] = df["is_home_row"].astype(int)
    df["away_player"] = (~df["is_home_row"]).astype(int)

    g = df.groupby("match_key", sort=False)
    m = pd.DataFrame({
        "season": g["season"].first(),
        "round": g["round"].first(),
        "match_date": g["date"].first(),
        "venue": g["venue"].first(),
        "home_team": g["home_team"].first(),
        "away_team": g["away_team"].first(),
        # median, not first: one corrupt row cannot move the match score.
        "home_score": g["home_score_row"].median(),
        "away_score": g["away_score_row"].median(),
        "is_final": g["is_final"].max(),
        "home_players": g["home_player"].sum(),
        "away_players": g["away_player"].sum(),
    }).reset_index()

    # club_now for each side, via the lineage already stored on games.
    lineage = (pd.concat([df[["club_hist", "club_now"]]])
               .drop_duplicates("club_hist")
               .set_index("club_hist")["club_now"])
    m["home_team_now"] = m["home_team"].map(lineage).fillna(m["home_team"])
    m["away_team_now"] = m["away_team"].map(lineage).fillna(m["away_team"])

    m["margin"] = (m["home_score"] - m["away_score"]).abs()
    m["winner"] = np.where(m["home_score"] > m["away_score"], m["home_team"],
                  np.where(m["away_score"] > m["home_score"], m["away_team"],
                           None))
    m.loc[m["home_score"].isna() | m["away_score"].isna(),
          ["winner", "margin"]] = None

    # is_final: trust the round name over whatever the rows happened to say.
    m["is_final"] = m["round"].str.upper().str.strip().isin(FINALS).astype(int)

    m["home_away_known"] = int(bool(oriented))
    m["attendance"] = None
    for c in QUARTER_COLS:
        m[c] = None

    m = m.sort_values(["season", "match_date", "home_team", "away_team"],
                      kind="mergesort").reset_index(drop=True)
    return m, df


def assign_ids(con, m):
    """Reuse existing ids for known keys; append new ones above the max."""
    import pandas as pd

    existing = {}
    next_id = 1
    if _table_exists(con, "matches") and _has_column(con, "matches", "match_key"):
        rows = con.execute("SELECT match_key, match_id FROM matches").fetchall()
        existing = {k: i for k, i in rows}
        if existing:
            next_id = max(existing.values()) + 1
        print(f"Reusing {len(existing):,} existing match_ids")

    ids = []
    for key in m["match_key"]:
        if key in existing:
            ids.append(existing[key])
        else:
            ids.append(next_id)
            next_id += 1
    m["match_id"] = ids
    new = len(m) - sum(1 for k in m["match_key"] if k in existing)
    if existing:
        print(f"{new:,} new matches")
    return m


def check(m, df):
    """Integrity checks. Returns problem_label -> DataFrame."""
    import pandas as pd

    problems = {}

    # Exactly two clubs per match, and both sides fielded players.
    clubs = df.groupby("match_key")["club_hist"].nunique()
    bad = clubs[clubs != 2]
    if len(bad):
        problems["match with != 2 clubs"] = m[m["match_key"].isin(bad.index)]

    empty = m[(m["home_players"] == 0) | (m["away_players"] == 0)]
    if len(empty):
        problems["one side has no players"] = empty

    # Score must agree across every row of a match, not just on average.
    for side, col in (("home", "home_score_row"), ("away", "away_score_row")):
        spread = df.groupby("match_key")[col].nunique(dropna=True)
        bad = spread[spread > 1]
        if len(bad):
            problems[f"inconsistent {side} score"] = m[
                m["match_key"].isin(bad.index)]

    missing = m[m["home_score"].isna() | m["away_score"].isna()]
    if len(missing):
        problems["no score recorded"] = missing

    # Unusual squad sizes are worth eyeballing but are not errors: early
    # VFL sides fielded 18-20, modern ones 22-23, and injury-hit rounds vary.
    odd = m[(m["home_players"] < 12) | (m["away_players"] < 12)]
    if len(odd):
        problems["fewer than 12 players a side"] = odd

    unmapped = int(df["match_key"].isna().sum())
    if unmapped:
        problems["games row with no match_key"] = pd.DataFrame(
            {"rows": [unmapped]})

    return problems


def write(con, m, rid_map):
    m[MATCH_COLS].to_sql("matches", con, if_exists="replace", index=False)

    if not _has_column(con, "games", "match_id"):
        con.execute("ALTER TABLE games ADD COLUMN match_id INTEGER")

    key_to_id = dict(zip(m["match_key"], m["match_id"]))
    rid_map = rid_map[["rid", "match_key"]].copy()
    rid_map["match_id"] = rid_map["match_key"].map(key_to_id)

    con.execute("DROP TABLE IF EXISTS _match_map")
    con.execute("CREATE TEMP TABLE _match_map (rid INTEGER, match_id INTEGER)")
    con.executemany(
        "INSERT INTO _match_map VALUES (?, ?)",
        rid_map[["rid", "match_id"]].dropna().astype(int).itertuples(
            index=False, name=None))
    con.execute("CREATE INDEX ix_mm ON _match_map(rid)")
    con.execute("""UPDATE games SET match_id =
                   (SELECT m.match_id FROM _match_map m WHERE m.rid = games.rowid)""")
    con.execute("DROP TABLE _match_map")

    for stmt in [
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_matches_id ON matches(match_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_matches_key ON matches(match_key)",
        "CREATE INDEX IF NOT EXISTS ix_matches_season ON matches(season, round)",
        "CREATE INDEX IF NOT EXISTS ix_matches_date ON matches(match_date)",
        "CREATE INDEX IF NOT EXISTS ix_matches_venue ON matches(venue)",
        "CREATE INDEX IF NOT EXISTS ix_matches_home ON matches(home_team_now)",
        "CREATE INDEX IF NOT EXISTS ix_matches_away ON matches(away_team_now)",
        "CREATE INDEX IF NOT EXISTS ix_matches_margin ON matches(margin)",
        "CREATE INDEX IF NOT EXISTS ix_games_matchid ON games(match_id)",
    ]:
        con.execute(stmt)
    con.commit()

    linked = con.execute(
        "SELECT COUNT(*) FROM games WHERE match_id IS NOT NULL").fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    print(f"Linked {linked:,} of {total:,} games rows to a match_id")


def run(db_path, report=False, dry_run=False):
    """Derive and write. Importable so build_db.py can finish the job itself."""
    try:
        import pandas  # noqa: F401
    except ImportError:
        sys.exit("Missing dependency. Run:  pip install pandas")

    con = sqlite3.connect(db_path)
    raw, oriented = load_games(con)
    m, df = build_matches(raw, oriented)

    print(f"Derived {len(m):,} matches, seasons "
          f"{int(m.season.min())}-{int(m.season.max())}")

    problems = check(m, df)
    if problems:
        print("\nIntegrity notes:")
        for label, rows in problems.items():
            print(f"  {len(rows):>6,}  {label}")
            if report:
                cols = [c for c in ("season", "round", "match_date", "venue",
                                    "home_team", "away_team", "home_score",
                                    "away_score", "home_players",
                                    "away_players") if c in rows.columns]
                print(rows[cols].head(25).to_string(index=False) if cols
                      else rows.head(25).to_string(index=False))
                print()
    else:
        print("Integrity checks: clean")

    if dry_run:
        print("\n--dry-run: nothing written")
        con.close()
        return

    m = assign_ids(con, m)
    write(con, m, df)
    if _table_exists(con, "meta"):
        con.execute("DELETE FROM meta WHERE key = 'matches_derived'")
        con.execute(
            "INSERT INTO meta VALUES ('matches_derived', datetime('now'))")
    con.commit()
    con.close()
    print("Done.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=default_db("afl"))
    ap.add_argument("--report", action="store_true",
                    help="print the rows behind each integrity problem")
    ap.add_argument("--dry-run", action="store_true",
                    help="derive and check without writing")
    a = ap.parse_args()
    run(a.db, report=a.report, dry_run=a.dry_run)


if __name__ == "__main__":
    main()
