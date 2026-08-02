#!/usr/bin/env python3
"""Database health reporting for Sports Data Lab.

One place to see what is loaded, how good the optional link layers are, and
where the data has known gaps -- instead of running several scripts and
reading their separate outputs.

Every check is read-only and degrades gracefully: a missing optional table
reports as "not loaded" rather than raising, so this page works on a clean
core build as well as a fully enriched one.

Usable two ways::

    python health.py --db gridley.db          # text report
    python health.py --db gridley.db --json   # machine-readable

and as the "Database Health" page inside app.py via :func:`health_page`.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

#: Optional link layers: table -> (label, id column, script hint).
LINK_LAYERS = {
    "captaincies": ("Captaincy", "player_id", "load_captains.py"),
    "rising_star_nominees": ("Rising Star", "player_id", "load_rising_star.py"),
}
TRUSTED_STATUSES = ("unique", "resolved")


# ------------------------------------------------------------------ probes

def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def table_counts(con: sqlite3.Connection) -> list[tuple[str, int]]:
    """Every table and its row count, largest first."""
    names = [row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    out = []
    for name in names:
        try:
            out.append((name, con.execute(
                f"SELECT COUNT(*) FROM \"{name}\"").fetchone()[0]))
        except sqlite3.Error:
            out.append((name, -1))
    return sorted(out, key=lambda row: -row[1])


def core_summary(con: sqlite3.Connection) -> dict:
    out: dict = {}
    if not table_exists(con, "games") or not table_exists(con, "players"):
        return {"error": "core tables missing; run build_db.py"}
    lo, hi = con.execute("SELECT MIN(season), MAX(season) FROM games").fetchone()
    out["season_min"], out["season_max"] = lo, hi
    out["seasons"] = con.execute(
        "SELECT COUNT(DISTINCT season) FROM games").fetchone()[0]
    out["players"] = con.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    out["player_games"] = con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    if table_exists(con, "matches"):
        out["matches"] = con.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    if table_exists(con, "team_seasons"):
        out["team_seasons"] = con.execute(
            "SELECT COUNT(*) FROM team_seasons").fetchone()[0]
    return out


def link_quality(con: sqlite3.Connection) -> list[dict]:
    """Trusted / untrusted breakdown for each optional link layer."""
    report = []
    for table, (label, id_col, script) in LINK_LAYERS.items():
        entry = {"layer": label, "table": table, "script": script}
        if not table_exists(con, table):
            entry["state"] = "not loaded"
            report.append(entry)
            continue
        cols = columns(con, table)
        if "match_status" not in cols:
            entry["state"] = "unexpected schema"
            report.append(entry)
            continue
        statuses = dict(con.execute(
            f"SELECT match_status, COUNT(*) FROM {table} GROUP BY match_status"
        ).fetchall())
        total = sum(statuses.values())
        trusted = sum(statuses.get(s, 0) for s in TRUSTED_STATUSES)
        entry.update({
            "state": "loaded" if total else "empty",
            "total": total,
            "trusted": trusted,
            "untrusted": total - trusted,
            "statuses": statuses,
        })
        if "match_method" in cols:
            entry["methods"] = dict(con.execute(
                f"SELECT match_method, COUNT(*) FROM {table} "
                f"WHERE match_status IN {TRUSTED_STATUSES} GROUP BY match_method"
            ).fetchall())
        report.append(entry)
    return report


def untrusted_rows(con: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """The actual rows needing review, so problems are actionable."""
    out = []
    for table, (label, _id, _script) in LINK_LAYERS.items():
        if not table_exists(con, table):
            continue
        cols = columns(con, table)
        if "match_status" not in cols:
            continue
        name_col = "player" if "player" in cols else None
        season_col = "season" if "season" in cols else None
        club_col = "club" if "club" in cols else None
        select = ", ".join(c for c in (season_col, name_col, club_col) if c)
        if not select:
            continue
        extra = ", notes" if "notes" in cols else ""
        rows = con.execute(
            f"SELECT match_status, {select}{extra} FROM {table} "
            f"WHERE match_status NOT IN {TRUSTED_STATUSES} "
            f"ORDER BY {season_col or select} LIMIT ?", (limit,)
        ).fetchall()
        for row in rows:
            out.append({"layer": label, "status": row[0],
                        "detail": " | ".join(str(v) for v in row[1:] if v)})
    return out


def rising_star_coverage(con: sqlite3.Connection) -> dict:
    """Season coverage and the latest archived nomination season."""
    if not table_exists(con, "rising_star_nominees"):
        return {"state": "not loaded"}
    rows = con.execute(
        "SELECT season, COUNT(*) FROM rising_star_nominees "
        "GROUP BY season ORDER BY season"
    ).fetchall()
    if not rows:
        return {"state": "empty"}
    seasons = [season for season, _ in rows]
    missing = [s for s in range(min(seasons), max(seasons) + 1)
               if s not in set(seasons)]
    return {
        "state": "loaded",
        "season_min": min(seasons),
        "season_max": max(seasons),
        "seasons": len(seasons),
        "latest_count": rows[-1][1],
        "missing_seasons": missing,
        "by_season": rows,
    }


def stat_era_starts(con: sqlite3.Connection, stats: list[str] | None = None) -> list[tuple]:
    """First season each detailed statistic carries a non-null, non-zero value."""
    if not table_exists(con, "games"):
        return []
    cols = columns(con, "games")
    candidates = stats or [s for s in (
        "disposals", "kicks", "handballs", "marks", "tackles", "hitouts",
        "inside50s", "clearances", "rebounds", "contested", "contested_marks",
        "marks_i50", "one_percenters", "goal_assists", "brownlow", "goals",
    ) if s in cols]
    out = []
    for stat in candidates:
        row = con.execute(
            f"SELECT MIN(season) FROM games WHERE {stat} IS NOT NULL AND {stat} != 0"
        ).fetchone()
        out.append((stat, row[0] if row else None))
    return sorted(out, key=lambda r: (r[1] is None, r[1] or 0))


def integrity_warnings(con: sqlite3.Connection) -> list[str]:
    """Cheap checks that catch the failure modes seen in this project."""
    warnings = []
    if not table_exists(con, "games"):
        return ["core games table missing"]

    game_cols = columns(con, "games")
    key_cols = [c for c in ("player_id", "season", "round", "date", "club_hist")
                if c in game_cols]
    # Without a round or date column, several games in one season at one club
    # are indistinguishable and every multi-game season looks like a duplicate.
    if {"round", "date"} & set(key_cols) and len(key_cols) >= 3:
        keys = ", ".join(key_cols)
        dupes = con.execute(
            f"SELECT COUNT(*) FROM (SELECT {keys} FROM games "
            f"GROUP BY {keys} HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        if dupes:
            warnings.append(f"{dupes:,} duplicate player-game keys")

    orphans = con.execute(
        "SELECT COUNT(*) FROM games g LEFT JOIN players p "
        "ON p.player_id = g.player_id WHERE p.player_id IS NULL"
    ).fetchone()[0]
    if orphans:
        warnings.append(f"{orphans:,} game rows have no matching player")

    # Wooden-spoon integrity: the bug that needed repair_database.py.
    if table_exists(con, "team_seasons") and "ladder_pos" in columns(con, "team_seasons"):
        bad = con.execute(
            "SELECT COUNT(*) FROM (SELECT season FROM team_seasons "
            "WHERE ladder_pos IS NOT NULL GROUP BY season "
            "HAVING COUNT(*) FILTER (WHERE ladder_pos = ("
            "  SELECT MAX(ladder_pos) FROM team_seasons t2 "
            "  WHERE t2.season = team_seasons.season)) != 1)"
        ).fetchone()[0]
        if bad:
            warnings.append(f"{bad} season(s) lack exactly one last-placed club "
                            "(run repair_database.py)")

    # Unrendered source templates that silently break club linking. Rows
    # already corrected by a reviewed override are not a problem: the source
    # wording is deliberately retained, so only untrusted leaks are reported.
    if table_exists(con, "rising_star_nominees"):
        leaks = con.execute(
            "SELECT COUNT(*) FROM rising_star_nominees "
            "WHERE (club LIKE '%$%' OR club LIKE '%{%') "
            f"AND match_status NOT IN {TRUSTED_STATUSES}"
        ).fetchone()[0]
        if leaks:
            warnings.append(f"{leaks} Rising Star row(s) contain unrendered "
                            "source template text and are unresolved")

    for table, (label, id_col, script) in LINK_LAYERS.items():
        if table_exists(con, table) and "match_status" in columns(con, table):
            untrusted = con.execute(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE match_status NOT IN {TRUSTED_STATUSES}"
            ).fetchone()[0]
            if untrusted:
                warnings.append(f"{label}: {untrusted} row(s) not trusted "
                                "by search/solver")
    return warnings


def source_dates(con: sqlite3.Connection) -> list[tuple[str, str]]:
    if not table_exists(con, "meta"):
        return []
    try:
        return con.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
    except sqlite3.Error:
        return []


def collect(con: sqlite3.Connection) -> dict:
    """Everything, in one dict, for the UI and the CLI to share."""
    return {
        "core": core_summary(con),
        "tables": table_counts(con),
        "links": link_quality(con),
        "untrusted": untrusted_rows(con),
        "rising_star": rising_star_coverage(con),
        "stat_eras": stat_era_starts(con),
        "warnings": integrity_warnings(con),
        "meta": source_dates(con),
    }


# ------------------------------------------------------------------ report

def print_report(report: dict) -> None:
    core = report["core"]
    print("=" * 62)
    print("DATABASE HEALTH")
    print("=" * 62)
    if "error" in core:
        print(f"  {core['error']}")
        return
    print(f"  Seasons        {core['season_min']}-{core['season_max']} "
          f"({core['seasons']} seasons)")
    print(f"  Players        {core['players']:,}")
    print(f"  Player-games   {core['player_games']:,}")
    for key in ("matches", "team_seasons"):
        if key in core:
            print(f"  {key:<14} {core[key]:,}")

    print("\nOPTIONAL LAYERS")
    for entry in report["links"]:
        if entry["state"] != "loaded":
            print(f"  {entry['layer']:<14} {entry['state']} "
                  f"(run {entry['script']})")
            continue
        print(f"  {entry['layer']:<14} {entry['trusted']:,} trusted "
              f"/ {entry['total']:,} total"
              + (f"  [{entry['untrusted']} NOT trusted]"
                 if entry["untrusted"] else ""))
        for status, count in sorted(entry.get("statuses", {}).items()):
            print(f"      {status:<16} {count:>6,}")

    rs = report["rising_star"]
    if rs.get("state") == "loaded":
        print("\nRISING STAR COVERAGE")
        print(f"  Seasons        {rs['season_min']}-{rs['season_max']} "
              f"({rs['seasons']} archived)")
        print(f"  Latest season  {rs['season_max']} "
              f"({rs['latest_count']} nominations)")
        if rs["missing_seasons"]:
            print(f"  Missing        {rs['missing_seasons']}")

    if report["untrusted"]:
        print("\nROWS NEEDING REVIEW")
        for row in report["untrusted"]:
            print(f"  [{row['layer']}/{row['status']}] {row['detail']}")

    if report["stat_eras"]:
        print("\nDETAILED-STAT START SEASONS")
        for stat, season in report["stat_eras"]:
            print(f"  {stat:<18} {season if season else 'never recorded'}")

    if report["meta"]:
        print("\nSOURCE REFRESH")
        for key, value in report["meta"]:
            print(f"  {key:<24} {value}")

    print("\nWARNINGS")
    if report["warnings"]:
        for warning in report["warnings"]:
            print(f"  ! {warning}")
    else:
        print("  none -- all checks clean")
    print("=" * 62)


# ------------------------------------------------------------------ UI page

def health_page(SPORT, con) -> None:
    """Streamlit page. Imported lazily so the CLI needs no Streamlit."""
    import streamlit as st

    st.header("Database Health")
    st.caption("Read-only diagnostics: what is loaded, how good the optional "
               "link layers are, and where the data has known gaps.")
    report = collect(con)
    core = report["core"]
    if "error" in core:
        st.error(core["error"])
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Seasons", f"{core['season_min']}–{core['season_max']}")
    c2.metric("Players", f"{core['players']:,}")
    c3.metric(f"Player-{SPORT.vocab.games}", f"{core['player_games']:,}")
    c4.metric("Matches", f"{core.get('matches', 0):,}")

    warnings = report["warnings"]
    if warnings:
        for warning in warnings:
            st.warning(warning)
    else:
        st.success("All integrity checks clean.")

    st.subheader("Optional layers")
    rows = []
    for entry in report["links"]:
        rows.append({
            "Layer": entry["layer"],
            "State": entry["state"],
            "Trusted": f"{entry.get('trusted', 0):,}",
            "Not trusted": entry.get("untrusted", 0),
            "Total": f"{entry.get('total', 0):,}",
            "Loader": entry["script"],
        })
    st.dataframe(rows, width="stretch", hide_index=True)

    if report["untrusted"]:
        st.subheader("Rows needing review")
        st.caption("Retained for audit; excluded from search and solver results.")
        st.dataframe(report["untrusted"], width="stretch", hide_index=True)

    rs = report["rising_star"]
    if rs.get("state") == "loaded":
        st.subheader("Rising Star coverage")
        st.write(f"Archived {rs['season_min']}–{rs['season_max']} "
                 f"({rs['seasons']} seasons). Latest: {rs['season_max']} "
                 f"with {rs['latest_count']} nominations.")
        if rs["missing_seasons"]:
            st.warning(f"Missing seasons: {rs['missing_seasons']}")
        st.bar_chart({str(season): count for season, count in rs["by_season"]})

    with st.expander("Detailed-stat start seasons"):
        st.caption("Statistics recorded only from a later era. Cross-era "
                   "comparisons should filter on availability.")
        st.dataframe(
            [{"Statistic": stat, "First recorded": season or "never"}
             for stat, season in report["stat_eras"]],
            width="stretch", hide_index=True)

    with st.expander("Tables and row counts"):
        st.dataframe([{"Table": name, "Rows": f"{count:,}"}
                      for name, count in report["tables"]],
                     width="stretch", hide_index=True)

    if report["meta"]:
        with st.expander("Source refresh dates"):
            st.dataframe([{"Key": k, "Value": v} for k, v in report["meta"]],
                         width="stretch", hide_index=True)


# ------------------------------------------------------------------ CLI

def default_db() -> str:
    try:
        from data_paths import sport_db
    except ImportError:
        return "gridley.db"
    return sport_db("afl", "gridley.db")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=default_db())
    parser.add_argument("--json", action="store_true",
                        help="emit the report as JSON")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero when any warning is raised")
    args = parser.parse_args(argv)

    if not Path(args.db).exists():
        print(f"No database at {args.db}. Run build_db.py first.", file=sys.stderr)
        return 2
    con = sqlite3.connect(args.db)
    try:
        report = collect(con)
    finally:
        con.close()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report)
    return 1 if (args.strict and report["warnings"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
